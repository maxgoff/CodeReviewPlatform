"""Whole-repository reviews: load -> plan -> guard (file count, budget) -> review -> report.

Shared by the CLI (`reviewbot repo ...`) and the API (`POST /repo-reviews`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .engine import ProgressFn, ReviewEngine, ReviewPlan
from .models import ReviewResult
from .report import repo_markdown
from .repo_source import (FileFilter, GitHubRepoFetcher, RepoError, RepoSnapshot, load_snapshot, parse_repo_spec,
                          to_changes)


@dataclass
class RepoReview:
    snapshot: RepoSnapshot
    plan: ReviewPlan
    result: ReviewResult | None = None
    markdown: str | None = None


def plan_summary(snapshot: RepoSnapshot, plan: ReviewPlan) -> str:
    lines = [
        f"Source:   {snapshot.label}",
        f"Files:    {len(plan.reviewable)} to review ({plan.lines:,} lines), "
        f"{sum(snapshot.skipped.values()) + len(plan.skipped)} skipped",
        f"Requests: {len(plan.chunks)} chunk(s) x {plan.calls // max(1, len(plan.chunks))} specialists = {plan.calls} model calls",
        f"Tokens:   ~{plan.est_usage.input_tokens:,} input, ~{plan.est_usage.output_tokens:,} output (estimate)",
    ]
    if plan.est_cost_usd is not None:
        lines.append(f"Cost:     up to ~${plan.est_cost_usd:.2f} before prompt-caching savings")
    return "\n".join(lines)


class RepoReviewer:
    def __init__(self, engine: ReviewEngine, settings: Settings, fetcher: GitHubRepoFetcher | None = None):
        self.engine = engine
        self.settings = settings
        self.fetcher = fetcher or GitHubRepoFetcher(
            settings.github_token, settings.github_api_url, max_download_mb=settings.repo_max_download_mb
        )

    async def prepare(self, source: str, *, ref: str | None = None, path: str | None = None,
                      include: list[str] | None = None, exclude: list[str] | None = None,
                      max_files: int | None = None, allow_local: bool = True) -> RepoReview:
        flt = FileFilter(include=include or [], exclude=exclude or [],
                         max_file_bytes=self.settings.repo_max_file_bytes)
        if allow_local:
            snapshot = await load_snapshot(source, flt, ref=ref, path=path, fetcher=self.fetcher)
        else:  # API callers may only name GitHub repositories, never paths on this server
            snapshot = await self.fetcher.fetch(parse_repo_spec(source, ref=ref, path=path), flt)
        if not snapshot.files:
            raise RepoError(f"No reviewable source files found in {snapshot.label}. "
                            f"Skipped: {dict(snapshot.skipped) or 'nothing'}.")
        limit = max_files or self.settings.repo_max_files
        if len(snapshot.files) > limit:
            dirs = ", ".join(f"{d} ({n})" for d, n in snapshot.top_directories())
            raise RepoError(
                f"{snapshot.label} has {len(snapshot.files)} source files, more than the limit of {limit}. "
                f"Narrow it with --path or --include, or raise --max-files. Largest directories: {dirs}."
            )
        plan = self.engine.plan(to_changes(snapshot), mode="repo")
        return RepoReview(snapshot, plan)

    def check_budget(self, review: RepoReview, budget_usd: float | None = None) -> None:
        budget = self.settings.repo_budget_usd if budget_usd is None else budget_usd
        cost = review.plan.est_cost_usd
        if cost is not None and cost > budget:
            raise RepoError(
                f"Estimated cost ~${cost:.2f} is over the ${budget:.2f} budget. "
                f"Narrow the review (--path, --include, --exclude) or raise the budget."
            )

    async def run(self, review: RepoReview, progress: ProgressFn | None = None) -> RepoReview:
        review.result = await self.engine.run(review.plan, progress)
        review.markdown = repo_markdown(
            review.result,
            label=review.snapshot.label,
            commit=review.snapshot.commit,
            repo_full_name=review.snapshot.repo_full_name,
            source_skipped=dict(review.snapshot.skipped),
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        return review
