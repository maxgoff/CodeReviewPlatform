"""The review engine: diff (or whole repository) in, ranked findings out.

    parse -> filter -> scan secrets & redact -> chunk -> specialists (parallel) -> aggregate
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

from .aggregator import aggregate
from .config import Settings
from .diff_parser import FileChange, chunk_files, parse_diff, render_for_prompt, should_skip
from .llm import ClaudeClient, LLMClient, MockClient, ReviewRequest
from .models import Finding, ReviewResult, Usage
from .prompts import SHARED_SYSTEM, SPECIALISTS, Specialist, specialist_instructions
from .secret_scan import scan_and_redact

log = logging.getLogger(__name__)

# Rough planning constants. Real counts come back in result.usage after the run.
CHARS_PER_TOKEN = 3.5
EST_OUTPUT_TOKENS_PER_CALL = 700


def build_client(settings: Settings) -> LLMClient:
    if settings.use_mock_llm:
        return MockClient()
    return ClaudeClient(settings.anthropic_api_key, settings.model, settings.max_output_tokens)


@dataclass
class ReviewPlan:
    reviewable: list[FileChange]
    skipped: dict[str, str]
    chunks: list[list[FileChange]]
    calls: int
    est_usage: Usage
    est_cost_usd: float | None  # upper bound: assumes no cache hits
    mode: str = "diff"
    lines: int = field(default=0)


ProgressFn = Callable[[int, int], None]


class ReviewEngine:
    def __init__(self, client: LLMClient, settings: Settings):
        self.client = client
        self.settings = settings
        unknown = [s for s in settings.specialists if s not in SPECIALISTS]
        if unknown:
            raise ValueError(f"Unknown specialists: {unknown}. Known: {sorted(SPECIALISTS)}")
        self.specialists: list[Specialist] = [SPECIALISTS[s] for s in settings.specialists]

    # ---------------------------------------------------------------- planning
    def plan(self, changes: list[FileChange], mode: str = "diff") -> ReviewPlan:
        reviewable, skipped = [], {}
        for change in changes:
            reason = should_skip(change, self.settings.skip_globs, self.settings.max_file_lines)
            if reason:
                skipped[change.path] = reason
            else:
                reviewable.append(change)
        chunks = chunk_files(reviewable, self.settings.chunk_char_budget)

        est = Usage()
        fixed = (len(SHARED_SYSTEM) + max(len(specialist_instructions(s, mode)) for s in self.specialists)) / CHARS_PER_TOKEN
        for chunk in chunks:
            diff_tokens = sum(len(render_for_prompt(c)) for c in chunk) / CHARS_PER_TOKEN
            for _ in self.specialists:
                est.input_tokens += int(fixed + diff_tokens)
                est.output_tokens += EST_OUTPUT_TOKENS_PER_CALL
                est.calls += 1
        lines = sum(len(h.lines) for c in reviewable for h in c.hunks)
        return ReviewPlan(reviewable, skipped, chunks, est.calls, est,
                          est.estimated_cost_usd(self.client.model), mode, lines)

    # ---------------------------------------------------------------- running
    async def review_diff(self, diff_text: str) -> ReviewResult:
        return await self.run(self.plan(parse_diff(diff_text), mode="diff"))

    async def review_changes(self, changes: list[FileChange], mode: str = "diff",
                             progress: ProgressFn | None = None) -> ReviewResult:
        return await self.run(self.plan(changes, mode), progress)

    async def run(self, plan: ReviewPlan, progress: ProgressFn | None = None) -> ReviewResult:
        result = ReviewResult(model=self.client.model)
        result.files_reviewed = [c.path for c in plan.reviewable]
        result.files_skipped = dict(plan.skipped)
        if not plan.reviewable:
            return result

        # Deterministic checks first. Redaction happens before anything is sent to a model.
        raw_findings: list[Finding] = scan_and_redact(plan.reviewable)

        # Chunks run concurrently, bounded so a big repository doesn't trip rate limits.
        sem = asyncio.Semaphore(max(1, self.settings.max_concurrency))
        done = 0

        async def one_chunk(chunk: list[FileChange]):
            nonlocal done
            async with sem:
                out = await self._run_specialists(chunk, plan.mode)
            done += 1
            if progress:
                progress(done, len(plan.chunks))
            return out

        for findings, usage, errors in await asyncio.gather(*(one_chunk(c) for c in plan.chunks)):
            raw_findings.extend(findings)
            result.usage.add(usage)
            result.errors.extend(errors)

        max_comments = self.settings.repo_max_findings if plan.mode == "repo" else self.settings.max_comments
        agg = aggregate(raw_findings, plan.reviewable, self.settings.min_confidence, max_comments)
        result.findings, result.overflow, result.dropped = agg.kept, agg.overflow, agg.dropped
        return result

    async def _run_specialists(self, chunk: list[FileChange], mode: str = "diff") -> tuple[list[Finding], Usage, list[str]]:
        requests = [ReviewRequest(spec, chunk, mode) for spec in self.specialists]

        async def run(req: ReviewRequest):
            try:
                llm_findings, usage = await self.client.review(req)
            except Exception as exc:  # one failing specialist must not sink the whole review
                log.exception("specialist %s failed", req.specialist.name)
                return [], Usage(), [f"{req.specialist.name}: {exc}"]
            findings = []
            for lf in llm_findings:
                if lf.category not in req.specialist.categories:
                    continue  # stay in your lane: off-topic findings are another specialist's job
                findings.append(Finding(**lf.model_dump(), reviewers=[req.specialist.name]))
            return findings, usage, []

        # Run the first request alone so it writes the prompt cache; the rest then read from it.
        cacheable = getattr(self.client, "supports_prompt_cache", False)
        if self.settings.warm_cache and cacheable and len(requests) > 1:
            outcomes = [await run(requests[0])]
            outcomes += await asyncio.gather(*(run(r) for r in requests[1:]))
        else:
            outcomes = await asyncio.gather(*(run(r) for r in requests))

        findings: list[Finding] = []
        usage = Usage()
        errors: list[str] = []
        for f, u, e in outcomes:
            findings.extend(f)
            usage.add(u)
            errors.extend(e)
        return findings, usage, errors
