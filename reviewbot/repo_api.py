"""HTTP API for whole-repository reviews (also what the web UI talks to).

    GET  /api/info                        model, budget, whether a token is required
    POST /repo-reviews                    {"repo": "owner/repo", "plan_only": true}  -> 202 {"id": ...}
    POST /repo-reviews/{id}/start         start a planned review
    GET  /repo-reviews                    recent jobs, newest first
    GET  /repo-reviews/{id}               status, plan, progress, findings
    GET  /repo-reviews/{id}/report        the Markdown report

Jobs live in memory, so they disappear when the server restarts. Use a database and a queue in production.
Only GitHub repositories are accepted here, never local paths, and the server-side REPO_BUDGET_USD is a
hard ceiling on what one request may spend.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import uuid
from collections import Counter, OrderedDict
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, PrivateAttr

from .config import Settings
from .models import Finding, Usage
from .repo_review import RepoReview, RepoReviewer, plan_summary
from .repo_source import RepoError, parse_repo_spec

log = logging.getLogger("reviewbot.repo")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class RepoReviewRequest(BaseModel):
    repo: str = Field(description="owner/repo, owner/repo@ref or a github.com URL")
    ref: str | None = None
    path: str | None = None
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_files: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, gt=0)
    plan_only: bool = False


class PlanInfo(BaseModel):
    label: str
    commit: str | None
    repo_full_name: str | None
    files: list[str]
    lines: int
    skipped: dict[str, int]
    chunks: int
    calls: int
    est_input_tokens: int
    est_output_tokens: int
    est_cost_usd: float | None
    budget_usd: float
    over_budget: bool
    summary: str


class Progress(BaseModel):
    done: int = 0
    total: int = 0


class RepoJob(BaseModel):
    id: str
    repo: str
    status: str = "queued"  # queued | planning | planned | reviewing | done | rejected | failed
    created_at: str
    finished_at: str | None = None
    error: str | None = None
    plan: PlanInfo | None = None
    progress: Progress | None = None
    counts: dict[str, int] | None = None
    findings: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    model: str | None = None
    report_markdown: str | None = Field(default=None, exclude=True)
    _review: RepoReview | None = PrivateAttr(default=None)
    _budget: float = PrivateAttr(default=0.0)

    def summary(self) -> dict:
        return {"id": self.id, "repo": self.repo, "status": self.status, "created_at": self.created_at,
                "counts": self.counts, "label": self.plan.label if self.plan else None}


def _plan_info(review: RepoReview, budget: float) -> PlanInfo:
    snap, plan = review.snapshot, review.plan
    skipped: Counter = Counter(snap.skipped)
    for why in plan.skipped.values():
        skipped["too large" if why.startswith("too large") else why] += 1
    cost = plan.est_cost_usd
    return PlanInfo(
        label=snap.label, commit=snap.commit, repo_full_name=snap.repo_full_name,
        files=[c.path for c in plan.reviewable], lines=plan.lines, skipped=dict(skipped),
        chunks=len(plan.chunks), calls=plan.calls,
        est_input_tokens=plan.est_usage.input_tokens, est_output_tokens=plan.est_usage.output_tokens,
        est_cost_usd=cost, budget_usd=budget, over_budget=cost is not None and cost > budget,
        summary=plan_summary(snap, plan),
    )


def register_repo_routes(app: FastAPI, reviewer: RepoReviewer, settings: Settings, max_jobs: int = 50) -> None:
    jobs: OrderedDict[str, RepoJob] = OrderedDict()
    one_at_a_time = asyncio.Semaphore(1)  # downloads and reviews are expensive; queue them

    def authorize(request: Request, authorization: str | None) -> None:
        if settings.api_token:
            if not hmac.compare_digest(authorization or "", f"Bearer {settings.api_token}"):
                raise HTTPException(status_code=401, detail="Missing or wrong access token.")
        elif (request.client.host if request.client else "") not in LOCAL_HOSTS:
            raise HTTPException(status_code=403, detail="Set REVIEWBOT_API_TOKEN to allow requests from other machines.")

    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get_job(job_id: str) -> RepoJob:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No review with that id. Reviews are forgotten when the server restarts.")
        return job

    async def plan_job(job: RepoJob, req: RepoReviewRequest) -> None:
        async with one_at_a_time:
            job.status = "planning"
            try:
                review = await reviewer.prepare(req.repo, ref=req.ref, path=req.path, include=req.include,
                                                exclude=req.exclude, max_files=req.max_files, allow_local=False)
            except RepoError as exc:
                job.status, job.error, job.finished_at = "rejected", str(exc), now()
                return
            except Exception as exc:
                log.exception("planning %s failed", job.id)
                job.status, job.error, job.finished_at = "failed", f"{type(exc).__name__}: {exc}", now()
                return
            job._review = review
            job.plan = _plan_info(review, job._budget)
            job.status = "planned"
        if not req.plan_only:
            if job.plan.over_budget:
                job.status, job.finished_at = "rejected", now()
                job.error = (f"Estimated cost ~${job.plan.est_cost_usd:.2f} is over the "
                             f"${job._budget:.2f} budget. Narrow the review or raise the budget.")
                job._review = None
                return
            await review_job(job)

    async def review_job(job: RepoJob) -> None:
        async with one_at_a_time:
            job.status = "reviewing"
            job.progress = Progress(done=0, total=job.plan.chunks)

            def tick(done: int, total: int) -> None:
                job.progress = Progress(done=done, total=total)

            try:
                review = await reviewer.run(job._review, tick)
                job.findings = review.result.findings + review.result.overflow
                job.counts = review.result.counts_by_severity()
                job.errors = review.result.errors
                job.usage = review.result.usage
                job.model = review.result.model
                job.report_markdown = review.markdown
                job.status = "done"
            except Exception as exc:
                log.exception("review %s failed", job.id)
                job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                job._review = None  # release the file contents
                job.finished_at = now()

    @app.get("/api/info")
    async def info() -> dict:
        return {
            "model": reviewer.engine.client.model,
            "mock": reviewer.engine.client.model == "mock",
            "specialists": settings.specialists,
            "budget_usd": settings.repo_budget_usd,
            "max_files": settings.repo_max_files,
            "token_required": bool(settings.api_token),
            "github_token": bool(settings.github_token),
        }

    @app.post("/repo-reviews", status_code=202)
    async def create_repo_review(req: RepoReviewRequest, request: Request, background: BackgroundTasks,
                                 authorization: str | None = Header(default=None)) -> dict:
        authorize(request, authorization)
        try:
            spec = parse_repo_spec(req.repo, ref=req.ref, path=req.path)  # fail fast on bad input
        except RepoError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        job = RepoJob(id=uuid.uuid4().hex[:12], repo=spec.full_name + (f"@{spec.ref}" if spec.ref else ""),
                      created_at=now())
        job._budget = min(req.budget_usd or settings.repo_budget_usd, settings.repo_budget_usd)
        jobs[job.id] = job
        while len(jobs) > max_jobs:
            jobs.popitem(last=False)
        background.add_task(plan_job, job, req)
        return {"id": job.id, "status": job.status, "status_url": f"/repo-reviews/{job.id}",
                "report_url": f"/repo-reviews/{job.id}/report"}

    @app.post("/repo-reviews/{job_id}/start", status_code=202)
    async def start_repo_review(job_id: str, request: Request, background: BackgroundTasks,
                                authorization: str | None = Header(default=None)) -> dict:
        authorize(request, authorization)
        job = get_job(job_id)
        if job.status != "planned" or job._review is None:
            raise HTTPException(status_code=409, detail=f"This review can't be started (status: {job.status}).")
        if job.plan.over_budget:
            raise HTTPException(status_code=400, detail=(
                f"Estimated cost ~${job.plan.est_cost_usd:.2f} is over the ${job._budget:.2f} budget. "
                "Narrow the review with a folder or file patterns."))
        job.status = "queued"
        background.add_task(review_job, job)
        return {"id": job.id, "status": job.status}

    @app.get("/repo-reviews")
    async def list_repo_reviews(request: Request, authorization: str | None = Header(default=None)) -> list[dict]:
        authorize(request, authorization)
        return [j.summary() for j in reversed(jobs.values())]

    @app.get("/repo-reviews/{job_id}")
    async def repo_review_status(job_id: str, request: Request,
                                 authorization: str | None = Header(default=None)) -> RepoJob:
        authorize(request, authorization)
        return get_job(job_id)

    @app.get("/repo-reviews/{job_id}/report", response_class=PlainTextResponse)
    async def repo_review_report(job_id: str, request: Request,
                                 authorization: str | None = Header(default=None)) -> PlainTextResponse:
        authorize(request, authorization)
        job = get_job(job_id)
        if job.report_markdown is None:
            raise HTTPException(status_code=409, detail=f"The report isn't ready yet (status: {job.status}).")
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", job.plan.label if job.plan else job.repo).strip("_")
        return PlainTextResponse(job.report_markdown, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
