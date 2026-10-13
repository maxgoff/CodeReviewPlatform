"""Model clients. The engine depends on the `LLMClient` protocol, not on a vendor SDK.

- ClaudeClient: calls the Claude API with structured outputs, so every response is schema-valid JSON.
- MockClient:   deterministic pattern-based reviewer for offline demos, tests and CI.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .diff_parser import FileChange, render_for_prompt
from .models import Category, LLMFinding, ReviewerOutput, Severity, Usage
from .prompts import SHARED_SYSTEM, Specialist, specialist_instructions

log = logging.getLogger(__name__)


@dataclass
class ReviewRequest:
    specialist: Specialist
    files: list[FileChange]
    mode: str = "diff"  # "diff" for pull requests, "repo" for whole-repository audits

    @property
    def diff_block(self) -> str:
        body = "\n\n".join(render_for_prompt(f) for f in self.files)
        return f"<diff>\n{body}\n</diff>"


class LLMClient(Protocol):
    model: str

    async def review(self, request: ReviewRequest) -> tuple[list[LLMFinding], Usage]: ...


class ClaudeClient:
    supports_prompt_cache = True

    def __init__(self, api_key: str | None, model: str, max_output_tokens: int = 4096, sdk_client=None):
        if sdk_client is None:
            from anthropic import AsyncAnthropic

            sdk_client = AsyncAnthropic(api_key=api_key, max_retries=3, timeout=120.0)
        self._client = sdk_client
        self.model = model
        self.max_output_tokens = max_output_tokens

    async def review(self, request: ReviewRequest) -> tuple[list[LLMFinding], Usage]:
        response = await self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=SHARED_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        # Shared, cacheable prefix: identical for every specialist on this chunk.
                        {"type": "text", "text": request.diff_block, "cache_control": {"type": "ephemeral"}},
                        # Specialist-specific suffix.
                        {"type": "text", "text": specialist_instructions(request.specialist, request.mode)},
                    ],
                }
            ],
            output_format=ReviewerOutput,
        )

        usage = Usage(
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            calls=1,
        )

        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"{request.specialist.name}: output truncated at max_tokens; split the chunk")
        if response.stop_reason == "refusal":
            log.warning("%s reviewer declined to review this chunk", request.specialist.name)
            return [], usage

        parsed: ReviewerOutput | None = response.parsed_output
        if parsed is None:
            raise RuntimeError(f"{request.specialist.name}: response did not match the schema")
        return parsed.findings, usage


# ---------------------------------------------------------------------------------------------
# Offline reviewer. Each rule: (regex, severity, category, title, explanation, confidence, fix)
# ---------------------------------------------------------------------------------------------

_MOCK_RULES: dict[str, list[tuple]] = {
    "security": [
        (r"execute\(\s*f[\"']|execute\([^)]*(%|\+|\.format\()", Severity.critical, Category.security,
         "SQL query built with string formatting",
         "User-controlled values are interpolated into SQL, allowing SQL injection. Use a parameterized query.",
         0.93, None),
        (r"shell\s*=\s*True|os\.system\(", Severity.high, Category.security,
         "Shell command built from variables",
         "Running through a shell with interpolated input allows command injection. Pass an argument list without shell=True.",
         0.88, None),
        (r"\beval\(|\bexec\(", Severity.high, Category.security,
         "eval/exec on dynamic input",
         "Evaluating strings as code lets an attacker run arbitrary Python.",
         0.85, None),
        (r"pickle\.loads?\(|yaml\.load\((?!.*SafeLoader)", Severity.high, Category.security,
         "Unsafe deserialization",
         "Deserializing untrusted data with pickle or yaml.load can execute arbitrary code.",
         0.86, None),
        (r"verify\s*=\s*False", Severity.medium, Category.security,
         "TLS certificate verification disabled",
         "Disabling verification allows man-in-the-middle attacks.",
         0.9, None),
        (r"(?i)ignore (all )?(previous|prior) instructions|ai reviewer|approve this (pr|pull request)",
         Severity.high, Category.security,
         "Text aimed at manipulating automated reviewers",
         "This comment addresses an AI reviewer directly and tries to change its behaviour. Treat as a prompt-injection attempt.",
         0.9, None),
    ],
    "correctness": [
        (r"except\s*:\s*$|except\s+Exception\s*:\s*$", Severity.medium, Category.correctness,
         "Exception swallowed",
         "Catching everything hides real failures and leaves callers with a silent wrong result.",
         0.8, None),
        (r"len\([^)]*\)\s*\+\s*1\b|<=\s*len\(", Severity.high, Category.correctness,
         "Off-by-one: index can reach len()",
         "The loop or comparison allows an index equal to the length, which raises IndexError or reads past the end.",
         0.82, None),
        (r"def \w+\([^)]*=\s*(\[\]|\{\})", Severity.medium, Category.correctness,
         "Mutable default argument",
         "The default list/dict is shared across calls, so state leaks between invocations.",
         0.9, None),
    ],
    "maintainability": [
        (r"execute\(\s*f[\"']", Severity.low, Category.maintainability,
         "SQL query built with an f-string",
         "Inline SQL assembled with f-strings is hard to audit and reuse.",
         0.75, None),
        (r"^\s*print\(", Severity.low, Category.maintainability,
         "Debug print left in code",
         "Use the logging module so output can be controlled per environment.",
         0.72, None),
        (r"#\s*TODO(?!\()", Severity.nit, Category.maintainability,
         "TODO without an owner",
         "Add an owner or ticket so this does not become permanent.",
         0.6, None),
    ],
}


class MockClient:
    """Deterministic stand-in for Claude. Good enough to demo the pipeline end to end."""

    model = "mock"

    async def review(self, request: ReviewRequest) -> tuple[list[LLMFinding], Usage]:
        findings: list[LLMFinding] = []
        for change in request.files:
            for hunk in change.hunks:
                for line in hunk.lines:
                    if line.kind != "+":
                        continue
                    for pattern, severity, category, title, explanation, confidence, fix in _MOCK_RULES.get(
                        request.specialist.name, []
                    ):
                        if re.search(pattern, line.text):
                            findings.append(
                                LLMFinding(
                                    path=change.path, line=line.new_no, severity=severity, category=category,
                                    title=title, explanation=explanation, suggestion=fix or "",
                                    confidence=confidence,
                                )
                            )
        chars = len(request.diff_block)
        return findings, Usage(input_tokens=chars // 4, output_tokens=60 * len(findings) + 20, calls=1)
