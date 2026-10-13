"""Data contracts shared by every stage of the pipeline."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .config import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICING_PER_MTOK


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    nit = "nit"


SEVERITY_RANK = {Severity.critical: 0, Severity.high: 1, Severity.medium: 2, Severity.low: 3, Severity.nit: 4}
SEVERITY_ICON = {Severity.critical: "🛑", Severity.high: "🔴", Severity.medium: "🟠", Severity.low: "🟡", Severity.nit: "⚪"}


class Category(str, Enum):
    security = "security"
    correctness = "correctness"
    performance = "performance"
    maintainability = "maintainability"
    testing = "testing"


class LLMFinding(BaseModel):
    """One defect found by a specialist reviewer, anchored to a line in the diff."""

    # This model is also the JSON schema Claude must follow (see llm.ClaudeClient), so field
    # descriptions are written as instructions to the model.

    path: str = Field(description="File path exactly as shown in the diff header.")
    line: int = Field(description="New-file line number, copied from the left gutter of the diff.")
    severity: Severity
    category: Category
    title: str = Field(description="One-line summary, under 80 characters.")
    explanation: str = Field(description="Why this is a problem and what could go wrong. 1-3 sentences.")
    suggestion: str = Field(
        default="",
        description="Replacement code for the cited line only, or an empty string if there is no simple fix.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0.0-1.0: how sure you are this is a real defect.")


class ReviewerOutput(BaseModel):
    findings: list[LLMFinding]


class Finding(LLMFinding):
    """A finding after the engine has attributed it to one or more reviewers."""

    reviewers: list[str] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.calls += other.calls

    def estimated_cost_usd(self, model: str) -> float | None:
        price = PRICING_PER_MTOK.get(model)
        if price is None:
            return None
        per_tok_in = price["input"] / 1_000_000
        per_tok_out = price["output"] / 1_000_000
        return round(
            self.input_tokens * per_tok_in
            + self.cache_creation_input_tokens * per_tok_in * CACHE_WRITE_MULTIPLIER
            + self.cache_read_input_tokens * per_tok_in * CACHE_READ_MULTIPLIER
            + self.output_tokens * per_tok_out,
            4,
        )


class ReviewResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    overflow: list[Finding] = Field(default_factory=list, description="Valid findings beyond max_comments.")
    dropped: dict[str, int] = Field(default_factory=dict)
    files_reviewed: list[str] = Field(default_factory=list)
    files_skipped: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.findings + self.overflow:
            counts[f.severity.value] += 1
        return counts

    def max_severity(self) -> Severity | None:
        all_findings = self.findings + self.overflow
        if not all_findings:
            return None
        return min((f.severity for f in all_findings), key=lambda s: SEVERITY_RANK[s])
