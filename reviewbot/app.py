"""FastAPI service: GitHub webhook, repository review API and the web UI.

GitHub -> POST /webhook -> verify signature -> 202 Accepted -> background: fetch diff, review, post.
GitHub expects a response within 10 seconds, so the review always runs after the response is sent.

The web UI is served at / on port 27183 by default (REVIEWBOT_PORT to change it).
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from .config import Settings
from .engine import ReviewEngine, build_client
from .github import DryRunGitHub, GitHubAPI, GitHubClient, build_review_payload, verify_signature
from .repo_api import register_repo_routes
from .repo_review import RepoReviewer

log = logging.getLogger("reviewbot")

REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
DEFAULT_PORT = 27183  # unregistered with IANA, below the macOS and Linux ephemeral ranges
UI_DIR = Path(__file__).parent / "ui"
UI_FILES = {"app.css": "text/css", "app.js": "text/javascript", "favicon.svg": "image/svg+xml"}
UI_HEADERS = {
    # Findings echo text from reviewed code and model output; the page renders them as text only,
    # and this policy blocks inline and third-party scripts as a second line of defence.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DeliveryCache:
    """GitHub retries deliveries. Remember recent IDs so a retry does not produce a second review."""

    def __init__(self, size: int = 1000):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def seen_before(self, delivery_id: str | None) -> bool:
        if not delivery_id:
            return False
        if delivery_id in self._seen:
            return True
        self._seen[delivery_id] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return False


def create_app(
    settings: Settings | None = None,
    engine: ReviewEngine | None = None,
    github: GitHubAPI | None = None,
    repo_reviewer: RepoReviewer | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or ReviewEngine(build_client(settings), settings)
    if github is None:
        if settings.github_dry_run:
            github = DryRunGitHub(settings.dry_run_diff_path, settings.dry_run_output_dir)
        else:
            github = GitHubClient(settings.github_token or "", settings.github_api_url)
    deliveries = DeliveryCache()

    app = FastAPI(title="ReviewBot", version="0.1.0")
    app.state.settings, app.state.engine, app.state.github = settings, engine, github
    register_repo_routes(app, repo_reviewer or RepoReviewer(engine, settings), settings)

    async def process_pull_request(repo: str, number: int, head_sha: str) -> None:
        try:
            diff = await github.get_pr_diff(repo, number)
            result = await engine.review_diff(diff)
            await github.post_review(repo, number, build_review_payload(result, head_sha))
            log.info("%s#%d reviewed: %d comments, %s", repo, number, len(result.findings), result.counts_by_severity())
        except Exception:
            log.exception("review of %s#%d failed", repo, number)

    @app.get("/", include_in_schema=False)
    async def ui_index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html", media_type="text/html", headers=UI_HEADERS)

    @app.get("/ui/{name}", include_in_schema=False)
    async def ui_asset(name: str) -> FileResponse:
        if name not in UI_FILES:
            raise HTTPException(status_code=404)
        return FileResponse(UI_DIR / name, media_type=UI_FILES[name], headers=UI_HEADERS)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "model": engine.client.model, "dry_run": settings.github_dry_run}

    @app.post("/webhook", status_code=202)
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> dict:
        body = await request.body()
        if not verify_signature(settings.github_webhook_secret or "", body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid signature")

        if x_github_event == "ping":
            return {"status": "pong"}
        if x_github_event != "pull_request":
            return {"status": "ignored", "reason": f"event {x_github_event}"}
        if deliveries.seen_before(x_github_delivery):
            return {"status": "ignored", "reason": "duplicate delivery"}

        event = await request.json()
        action = event.get("action")
        pr = event.get("pull_request") or {}
        if action not in REVIEW_ACTIONS:
            return {"status": "ignored", "reason": f"action {action}"}
        if pr.get("draft"):
            return {"status": "ignored", "reason": "draft PR"}

        repo = event["repository"]["full_name"]
        number = pr["number"]
        background.add_task(process_pull_request, repo, number, pr["head"]["sha"])
        return {"status": "queued", "repo": repo, "pr": number}

    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.getenv("REVIEWBOT_HOST", "127.0.0.1")  # set 0.0.0.0 to accept other machines (and set a token)
    port = int(os.getenv("REVIEWBOT_PORT", DEFAULT_PORT))
    print(f"ReviewBot UI: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}", flush=True)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
