"""GitHub integration: verify webhooks, fetch PR diffs, post reviews with inline comments."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Protocol

import httpx

from .models import ReviewResult
from .report import comment_body, summary_markdown

log = logging.getLogger(__name__)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate X-Hub-Signature-256. Constant-time compare to avoid timing attacks."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def build_review_payload(result: ReviewResult, commit_sha: str) -> dict:
    return {
        "commit_id": commit_sha,
        "event": "COMMENT",  # never APPROVE or REQUEST_CHANGES: humans own the merge decision
        "body": summary_markdown(result),
        "comments": [
            {"path": f.path, "line": f.line, "side": "RIGHT", "body": comment_body(f)}
            for f in result.findings
        ],
    }


class GitHubAPI(Protocol):
    async def get_pr_diff(self, repo: str, number: int) -> str: ...
    async def post_review(self, repo: str, number: int, payload: dict) -> None: ...


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com", http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._api = api_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "reviewbot",
        }

    async def get_pr_diff(self, repo: str, number: int) -> str:
        resp = await self._http.get(
            f"{self._api}/repos/{repo}/pulls/{number}",
            headers={**self._headers, "Accept": "application/vnd.github.diff"},
        )
        resp.raise_for_status()
        return resp.text

    async def post_review(self, repo: str, number: int, payload: dict) -> None:
        url = f"{self._api}/repos/{repo}/pulls/{number}/reviews"
        headers = {**self._headers, "Accept": "application/vnd.github+json"}
        resp = await self._http.post(url, headers=headers, json=payload)
        if resp.status_code == 422 and payload.get("comments"):
            # Usually a line that moved under us (new push mid-review). Fall back to a summary-only review.
            log.warning("inline comments rejected (%s); posting summary only", resp.text[:200])
            listed = "\n".join(f"- `{c['path']}:{c['line']}`\n\n{c['body']}\n" for c in payload["comments"])
            fallback = {**payload, "comments": [], "body": payload["body"] + "\n\n### Findings\n\n" + listed}
            resp = await self._http.post(url, headers=headers, json=fallback)
        resp.raise_for_status()


class DryRunGitHub:
    """Stands in for GitHub during local demos: reads a diff from disk, writes the review to a JSON file."""

    def __init__(self, diff_path: str, output_dir: str):
        self.diff_path = Path(diff_path)
        self.output_dir = Path(output_dir)
        self.posted: list[dict] = []

    async def get_pr_diff(self, repo: str, number: int) -> str:
        return self.diff_path.read_text()

    async def post_review(self, repo: str, number: int, payload: dict) -> None:
        self.posted.append(payload)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out = self.output_dir / f"review_{repo.replace('/', '_')}_{number}.json"
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("dry run: review with %d inline comments written to %s", len(payload["comments"]), out)
