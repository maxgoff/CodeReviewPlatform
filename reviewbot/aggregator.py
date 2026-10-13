"""Merge specialist output into one review a human will actually read.

Pipeline: validate location -> confidence gate -> de-duplicate -> rank -> cap.
Every dropped finding is counted by reason so you can tune thresholds with data.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .diff_parser import FileChange
from .models import SEVERITY_RANK, Finding

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "of", "in", "on", "to", "with", "is", "for", "and", "or", "be", "can"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def title_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_duplicate(a: Finding, b: Finding, line_window: int = 3, min_similarity: float = 0.3) -> bool:
    if a.path != b.path or abs(a.line - b.line) > line_window:
        return False
    return a.category == b.category or title_similarity(a.title, b.title) >= min_similarity


@dataclass
class AggregationResult:
    kept: list[Finding]
    overflow: list[Finding]
    dropped: dict[str, int] = field(default_factory=dict)


def aggregate(
    findings: list[Finding],
    files: list[FileChange],
    min_confidence: float,
    max_comments: int,
) -> AggregationResult:
    dropped: Counter[str] = Counter()
    by_path = {f.path: f for f in files}

    # 1. Location check: the model must point at a line that exists in the diff.
    located: list[Finding] = []
    for f in findings:
        change = by_path.get(f.path)
        if change is None:
            dropped["unknown_file"] += 1
        elif f.line not in change.commentable_lines:
            dropped["line_not_in_diff"] += 1
        else:
            located.append(f)

    # 2. Confidence gate.
    confident = [f for f in located if f.confidence >= min_confidence]
    dropped["below_confidence"] += len(located) - len(confident)

    # 3. De-duplicate. Strongest first, so the survivor is the most severe, most confident version.
    confident.sort(key=lambda f: (SEVERITY_RANK[f.severity], -f.confidence))
    unique: list[Finding] = []
    for f in confident:
        twin = next((u for u in unique if is_duplicate(u, f)), None)
        if twin is None:
            unique.append(f.model_copy(deep=True))
        else:
            dropped["duplicate"] += 1
            twin.reviewers = sorted(set(twin.reviewers) | set(f.reviewers))
            twin.confidence = max(twin.confidence, f.confidence)

    # 4. Rank and cap. Overflow is summarised in the review body rather than posted inline.
    unique.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.path, f.line))
    return AggregationResult(
        kept=unique[:max_comments],
        overflow=unique[max_comments:],
        dropped={k: v for k, v in dropped.items() if v},
    )
