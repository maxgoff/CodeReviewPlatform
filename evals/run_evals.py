"""Score the reviewer against labelled diffs.

    python -m evals.run_evals            # offline mock reviewer
    python -m evals.run_evals --live     # real Claude calls (needs ANTHROPIC_API_KEY)

Each case lists `must_find` (recall) and `acceptable` (findings that don't count against precision).
A finding matches an expectation when the path is equal, the line is within +/-2 and, if the
expectation names one, the category is equal. Re-run after every prompt or threshold change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from reviewbot.config import Settings
from reviewbot.engine import ReviewEngine, build_client
from reviewbot.models import Finding

CASES = Path(__file__).parent / "cases"
LINE_WINDOW = 2


def matches(f: Finding, exp: dict) -> bool:
    if f.path != exp["path"] or abs(f.line - exp["line"]) > LINE_WINDOW:
        return False
    return "category" not in exp or f.category.value == exp["category"]


async def run(live: bool, model: str | None) -> int:
    settings = Settings.from_env()
    settings.use_mock_llm = not live
    if model:
        settings.model = model
    engine = ReviewEngine(build_client(settings), settings)

    total_tp = total_must = total_found = total_fp = 0
    print(f"{'case':20} {'recall':>8} {'precision':>10}  misses / false positives")
    print("-" * 78)
    for case_file in sorted(CASES.glob("*.json")):
        case = json.loads(case_file.read_text())
        result = await engine.review_diff((CASES / case["diff"]).read_text())
        found = result.findings + result.overflow

        hit = [e for e in case["must_find"] if any(matches(f, e) for f in found)]
        missed = [e for e in case["must_find"] if e not in hit]
        expected_any = case["must_find"] + case["acceptable"]
        false_pos = [f for f in found if not any(matches(f, e) for e in expected_any)]

        recall = len(hit) / len(case["must_find"]) if case["must_find"] else 1.0
        precision = (len(found) - len(false_pos)) / len(found) if found else 1.0
        notes = [f"MISS {e['label']}" for e in missed] + [f"FP {f.path}:{f.line} {f.title}" for f in false_pos]
        print(f"{case_file.stem:20} {recall:8.0%} {precision:10.0%}  {'; '.join(notes) or '-'}")

        total_tp += len(hit)
        total_must += len(case["must_find"])
        total_found += len(found)
        total_fp += len(false_pos)

    recall = total_tp / total_must if total_must else 1.0
    precision = (total_found - total_fp) / total_found if total_found else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    print("-" * 78)
    print(f"{'OVERALL':20} {recall:8.0%} {precision:10.0%}  F1 {f1:.2f}  ({engine.client.model})")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="call the Claude API instead of the mock")
    p.add_argument("--model", help="override REVIEW_MODEL")
    a = p.parse_args()
    raise SystemExit(asyncio.run(run(a.live, a.model)))
