"""Command-line reviewer for diffs, CI, and whole repositories.

    reviewbot review --diff samples/vulnerable_pr.diff
    reviewbot review --git main...HEAD --fail-on high
    git diff | reviewbot review --diff -

    reviewbot repo owner/repo                       # download from GitHub and review every source file
    reviewbot repo https://github.com/owner/repo/tree/main/src --plan
    reviewbot repo owner/repo@v2.1 --include "*.py" --budget 5
    reviewbot repo ./path/to/local/checkout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .engine import ReviewEngine, build_client
from .models import SEVERITY_RANK, Severity
from .repo_review import RepoReviewer, plan_summary
from .repo_source import RepoError
from .report import summary_markdown, terminal_report


def _read_diff(args: argparse.Namespace) -> str:
    if args.git:
        return subprocess.run(["git", "diff", args.git], check=True, capture_output=True, text=True).stdout
    if args.diff == "-":
        return sys.stdin.read()
    with open(args.diff, encoding="utf-8") as fh:
        return fh.read()


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mock", action="store_true", help="use the offline mock reviewer")
    p.add_argument("--model", help="override REVIEW_MODEL")
    p.add_argument("--min-confidence", type=float, help="override REVIEW_MIN_CONFIDENCE")
    p.add_argument(
        "--fail-on", choices=[s.value for s in Severity],
        help="exit 1 if any finding is at or above this severity (for CI gates)",
    )


def _settings(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Settings:
    settings = Settings.from_env()
    if args.mock:
        settings.use_mock_llm = True
    if args.model:
        settings.model = args.model
    if args.min_confidence is not None:
        settings.min_confidence = args.min_confidence
    if not settings.use_mock_llm and not settings.anthropic_api_key and not getattr(args, "plan", False):
        parser.error("ANTHROPIC_API_KEY is not set. Export it or pass --mock.")
    return settings


class _PlanningClient:
    """Stands in for Claude when only estimating (--plan): never called, only its model name is used."""

    def __init__(self, model: str):
        self.model = model


def _gate(result, fail_on: str | None) -> int:
    if fail_on:
        worst = result.max_severity()
        if worst is not None and SEVERITY_RANK[worst] <= SEVERITY_RANK[Severity(fail_on)]:
            return 1
    return 0


def _cmd_review(args, parser) -> int:
    settings = _settings(args, parser)
    engine = ReviewEngine(build_client(settings), settings)
    result = asyncio.run(engine.review_diff(_read_diff(args)))
    if args.format == "json":
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    elif args.format == "markdown":
        print(summary_markdown(result))
    else:
        print(terminal_report(result))
    return _gate(result, args.fail_on)


def _cmd_repo(args, parser) -> int:
    settings = _settings(args, parser)
    if args.plan and not args.mock and not settings.anthropic_api_key:
        client = _PlanningClient(settings.model)  # estimate Claude's cost even without a key
    else:
        client = build_client(settings)
    engine = ReviewEngine(client, settings)
    reviewer = RepoReviewer(engine, settings)
    err = sys.stderr

    async def go():
        print(f"Loading {args.source} ...", file=err)
        review = await reviewer.prepare(args.source, ref=args.ref, path=args.path, include=args.include,
                                        exclude=args.exclude, max_files=args.max_files)
        print(plan_summary(review.snapshot, review.plan), file=err)
        if args.plan:
            for f in review.plan.reviewable:
                print(f"  {f.path}", file=err)
            return None
        if not args.yes:
            reviewer.check_budget(review, args.budget)
        print("Reviewing ...", file=err)
        return await reviewer.run(review, lambda d, n: print(f"  chunk {d}/{n} done", file=err))

    try:
        review = asyncio.run(go())
    except RepoError as exc:
        print(f"error: {exc}", file=err)
        return 2
    if review is None:
        return 0

    result = review.result
    out_path = Path(args.out) if args.out else Path(settings.repo_output_dir) / (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", review.snapshot.label).strip("_") + ".md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(review.markdown, encoding="utf-8")

    if args.format == "json":
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    elif args.format == "markdown":
        print(review.markdown)
    else:
        print(terminal_report(result))
    print(f"\nReport written to {out_path}", file=err)
    return _gate(result, args.fail_on)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewbot", description="AI code review for diffs and repositories")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="review a diff")
    source = review.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", help="path to a unified diff file, or - for stdin")
    source.add_argument("--git", metavar="RANGE", help="git revision range, e.g. main...HEAD")
    review.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    _common(review)

    repo = sub.add_parser("repo", help="download a GitHub repository (or read a local folder) and review it")
    repo.add_argument("source", help="owner/repo, owner/repo@ref, a github.com URL, or a local directory")
    repo.add_argument("--ref", help="branch, tag or commit (default: the repository's default branch)")
    repo.add_argument("--path", help="only review this subdirectory, e.g. src/api")
    repo.add_argument("--include", action="append", default=[], metavar="GLOB",
                      help="only review matching files, e.g. '*.py' (repeatable)")
    repo.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                      help="skip matching files, e.g. 'tests/*' (repeatable)")
    repo.add_argument("--max-files", type=int, help="refuse to review more files than this (default 150)")
    repo.add_argument("--budget", type=float, help="refuse if the estimated cost in USD is higher (default 2.00)")
    repo.add_argument("--yes", action="store_true", help="skip the budget check")
    repo.add_argument("--plan", action="store_true", help="show files and cost estimate, then stop")
    repo.add_argument("--out", help="where to write the Markdown report (default reviews/<repo>.md)")
    repo.add_argument("--format", choices=["text", "markdown", "json"], default="text",
                      help="what to print to stdout")
    _common(repo)

    args = parser.parse_args(argv)
    if args.command == "repo":
        return _cmd_repo(args, parser)
    return _cmd_review(args, parser)


if __name__ == "__main__":
    sys.exit(main())
