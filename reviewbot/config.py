"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


# USD per million tokens. Check https://platform.claude.com/docs/en/about-claude/pricing
# before relying on these numbers; they are only used for the cost estimate in reports.
PRICING_PER_MTOK = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute cache writes
CACHE_READ_MULTIPLIER = 0.10


@dataclass
class Settings:
    anthropic_api_key: str | None = None
    model: str = "claude-sonnet-5-5"
    max_output_tokens: int = 4096

    use_mock_llm: bool = False
    specialists: list[str] = field(
        default_factory=lambda: ["security", "correctness", "maintainability"]
    )
    warm_cache: bool = True

    min_confidence: float = 0.7
    max_comments: int = 15
    max_file_lines: int = 1500
    chunk_char_budget: int = 60_000
    skip_globs: list[str] = field(
        default_factory=lambda: [
            "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
            "*.min.js", "*.min.css", "*.map", "*.svg", "*.png", "*.jpg", "*.gif", "*.pdf",
            "vendor/*", "node_modules/*", "dist/*", "build/*", "*_pb2.py", "*.generated.*",
        ]
    )

    max_concurrency: int = 4  # chunks reviewed at the same time

    # Whole-repository reviews
    repo_max_files: int = 150
    repo_max_file_bytes: int = 200_000
    repo_max_download_mb: int = 200
    repo_budget_usd: float = 2.00
    repo_max_findings: int = 100
    repo_output_dir: str = "reviews"
    api_token: str | None = None  # bearer token for POST /repo-reviews; unset = localhost only

    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str | None = None
    github_dry_run: bool = False
    dry_run_diff_path: str = "samples/vulnerable_pr.diff"
    dry_run_output_dir: str = "out"

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            model=os.getenv("REVIEW_MODEL", defaults.model),
            max_output_tokens=int(os.getenv("REVIEW_MAX_OUTPUT_TOKENS", defaults.max_output_tokens)),
            use_mock_llm=_bool("USE_MOCK_LLM", not os.getenv("ANTHROPIC_API_KEY")),
            specialists=_list("REVIEW_SPECIALISTS", defaults.specialists),
            warm_cache=_bool("REVIEW_WARM_CACHE", defaults.warm_cache),
            min_confidence=float(os.getenv("REVIEW_MIN_CONFIDENCE", defaults.min_confidence)),
            max_comments=int(os.getenv("REVIEW_MAX_COMMENTS", defaults.max_comments)),
            max_file_lines=int(os.getenv("REVIEW_MAX_FILE_LINES", defaults.max_file_lines)),
            chunk_char_budget=int(os.getenv("REVIEW_CHUNK_CHAR_BUDGET", defaults.chunk_char_budget)),
            skip_globs=_list("REVIEW_SKIP_GLOBS", defaults.skip_globs),
            max_concurrency=int(os.getenv("REVIEW_MAX_CONCURRENCY", defaults.max_concurrency)),
            repo_max_files=int(os.getenv("REPO_MAX_FILES", defaults.repo_max_files)),
            repo_max_file_bytes=int(os.getenv("REPO_MAX_FILE_BYTES", defaults.repo_max_file_bytes)),
            repo_max_download_mb=int(os.getenv("REPO_MAX_DOWNLOAD_MB", defaults.repo_max_download_mb)),
            repo_budget_usd=float(os.getenv("REPO_BUDGET_USD", defaults.repo_budget_usd)),
            repo_max_findings=int(os.getenv("REPO_MAX_FINDINGS", defaults.repo_max_findings)),
            repo_output_dir=os.getenv("REPO_OUTPUT_DIR", defaults.repo_output_dir),
            api_token=os.getenv("REVIEWBOT_API_TOKEN"),
            github_token=os.getenv("GITHUB_TOKEN"),
            github_api_url=os.getenv("GITHUB_API_URL", defaults.github_api_url),
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET"),
            github_dry_run=_bool("GITHUB_DRY_RUN", not os.getenv("GITHUB_TOKEN")),
            dry_run_diff_path=os.getenv("DRY_RUN_DIFF_PATH", defaults.dry_run_diff_path),
            dry_run_output_dir=os.getenv("DRY_RUN_OUTPUT_DIR", defaults.dry_run_output_dir),
        )
