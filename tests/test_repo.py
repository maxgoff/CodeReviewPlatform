import asyncio
import io
import tarfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from reviewbot.app import create_app
from reviewbot.cli import main as cli_main
from reviewbot.config import Settings
from reviewbot.engine import ReviewEngine
from reviewbot.llm import MockClient
from reviewbot.repo_review import RepoReviewer
from tests.fakes import AWS_EXAMPLE_KEY
from reviewbot.repo_source import (FileFilter, GitHubRepoFetcher, RepoError, parse_repo_spec, read_directory,
                                   read_tarball, to_changes)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "sample_repo"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- spec parsing
@pytest.mark.parametrize("spec,expected", [
    ("octocat/Hello-World", ("octocat", "Hello-World", None, None)),
    ("octocat/Hello-World@v1.2", ("octocat", "Hello-World", "v1.2", None)),
    ("https://github.com/octocat/Hello-World", ("octocat", "Hello-World", None, None)),
    ("https://github.com/octocat/Hello-World.git", ("octocat", "Hello-World", None, None)),
    ("github.com/psf/requests/tree/main/src/requests", ("psf", "requests", "main", "src/requests")),
    ("git@github.com:psf/requests.git", ("psf", "requests", None, None)),
])
def test_parse_repo_spec(spec, expected):
    s = parse_repo_spec(spec)
    assert (s.owner, s.repo, s.ref, s.subpath) == expected


def test_parse_repo_spec_overrides_and_errors():
    s = parse_repo_spec("https://github.com/a/b/tree/main", ref="dev", path="/src/")
    assert (s.ref, s.subpath) == ("dev", "src")
    for bad in ("not a repo", "https://gitlab.com/a/b", "a/b/c"):
        with pytest.raises(RepoError):
            parse_repo_spec(bad)
    with pytest.raises(RepoError):
        parse_repo_spec("a/b", path="../etc")


# ---------------------------------------------------------------- filtering and tarballs
def test_file_filter():
    f = FileFilter(max_file_bytes=100)
    assert f.reason_to_skip("src/app.py", 10) is None
    assert f.reason_to_skip("Dockerfile", 10) is None
    assert f.reason_to_skip("web/node_modules/x/index.js", 10) == "dependency, build or tooling directory"
    assert f.reason_to_skip("logo.png", 10) == "not a source file type"
    assert "larger than" in f.reason_to_skip("src/big.py", 1000)
    assert FileFilter(include=["*.md"]).reason_to_skip("README.md", 10) is None
    assert FileFilter(exclude=["tests/*"]).reason_to_skip("tests/test_a.py", 10) == "excluded by --exclude"
    assert FileFilter().reason_to_skip(".github/workflows/ci.yml", 10) is None  # CI config is worth reviewing


def make_tarball(entries: dict[str, bytes], prefix="octo-demo-abc1234", extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


def test_read_tarball_is_safe_and_filtered():
    link = tarfile.TarInfo("octo-demo-abc1234/link.py")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    evil = tarfile.TarInfo("octo-demo-abc1234/../../evil.py")
    data = make_tarball({
        "app/main.py": b"print('hi')\n",
        "app/data.bin.py": b"\x00\x01\x02",
        "node_modules/lib/index.js": b"x=1\n",
        "README.md": b"# hi\n",
        "src/api/routes.py": b"def f():\n    return 1\n",
    }, extra=[link, evil])
    snap = read_tarball(data, "octo/demo@abc1234", "abc1234", FileFilter())
    assert [f.path for f in snap.files] == ["app/main.py", "src/api/routes.py"]
    assert snap.skipped["binary or not UTF-8"] == 1
    assert snap.skipped["dependency, build or tooling directory"] == 1
    sub = read_tarball(data, "x", None, FileFilter(), subpath="src")
    assert [f.path for f in sub.files] == ["src/api/routes.py"]


def test_read_directory_and_to_changes():
    snap = read_directory(SAMPLE, FileFilter())
    assert [f.path for f in snap.files] == ["app/db.py", "app/files.py", "app/slug.py", "app/users.py"]
    users = next(c for c in to_changes(snap) if c.path == "app/users.py")
    assert users.status == "added"
    assert users.line_text(10) == f'AWS_ACCESS_KEY_ID = "{AWS_EXAMPLE_KEY}"'
    assert users.commentable_lines == set(range(1, 42))


# ---------------------------------------------------------------- GitHub download
def github_transport(tarball: bytes, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if path == "/repos/octo/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/octo/demo/commits/main":
            return httpx.Response(200, text="abc1234def5678")
        if path == "/repos/octo/demo/tarball/abc1234def5678":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/octo/demo/legacy.tar.gz/abc"})
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=tarball)
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.MockTransport(handler)


def test_github_fetcher_resolves_default_branch_and_follows_redirect():
    seen = []
    tar = make_tarball({"app/main.py": b"import os\n"})
    fetcher = GitHubRepoFetcher("tok", http=httpx.AsyncClient(transport=github_transport(tar, seen)))
    snap = run(fetcher.fetch(parse_repo_spec("octo/demo"), FileFilter()))
    assert snap.label == "octo/demo@abc1234" and snap.commit == "abc1234def5678"
    assert snap.repo_full_name == "octo/demo"
    assert [f.path for f in snap.files] == ["app/main.py"]
    assert seen[0][2] == "Bearer tok"


def test_github_fetcher_errors():
    fetcher = GitHubRepoFetcher(None, http=httpx.AsyncClient(transport=github_transport(b"", [])))
    with pytest.raises(RepoError, match="set GITHUB_TOKEN"):
        run(fetcher.fetch(parse_repo_spec("octo/private"), FileFilter()))
    big = make_tarball({"a.py": b"x = 1\n" * 50_000})
    small = GitHubRepoFetcher(None, http=httpx.AsyncClient(transport=github_transport(big, [])), max_download_mb=0)
    with pytest.raises(RepoError, match="larger than"):
        run(small.fetch(parse_repo_spec("octo/demo"), FileFilter()))


# ---------------------------------------------------------------- reviewer, CLI, API
class SampleFetcher:
    """Pretends to be GitHub: serves samples/sample_repo for any repository."""

    async def fetch(self, spec, flt):
        snap = read_directory(SAMPLE, flt, spec.subpath)
        snap.label, snap.commit, snap.repo_full_name = f"{spec.full_name}@abc1234", "abc1234def", spec.full_name
        return snap


def reviewer(settings=None, client=None):
    settings = settings or Settings(use_mock_llm=True)
    return RepoReviewer(ReviewEngine(client or MockClient(), settings), settings, SampleFetcher())


def test_repo_review_end_to_end():
    r = reviewer()
    review = run(r.prepare("octo/demo"))
    assert review.plan.mode == "repo" and len(review.plan.reviewable) == 4
    run(r.run(review))
    lines = {(f.path, f.line) for f in review.result.findings}
    assert {("app/users.py", 10), ("app/users.py", 21), ("app/users.py", 37), ("app/files.py", 7)} <= lines
    assert "https://github.com/octo/demo/blob/abc1234def/app/users.py#L21" in review.markdown


def test_repo_review_guards():
    with pytest.raises(RepoError, match="more than the limit"):
        run(reviewer().prepare("octo/demo", max_files=2))
    with pytest.raises(RepoError, match="No reviewable"):
        run(reviewer().prepare("octo/demo", include=["*.rb"]))

    class Priced(MockClient):
        model = "claude-sonnet-5-5"

    r = reviewer(client=Priced())
    review = run(r.prepare("octo/demo"))
    assert review.plan.est_cost_usd > 0
    with pytest.raises(RepoError, match="over the"):
        r.check_budget(review, budget_usd=0.0001)
    r.check_budget(review, budget_usd=10)


def test_api_refuses_local_paths():
    with pytest.raises(RepoError):
        run(reviewer().prepare(str(SAMPLE), allow_local=False))


def test_cli_repo_on_local_folder(tmp_path, capsys):
    out = tmp_path / "report.md"
    assert cli_main(["repo", str(SAMPLE), "--mock", "--out", str(out)]) == 0
    assert "SQL query built with string formatting" in out.read_text()
    assert cli_main(["repo", str(SAMPLE), "--mock", "--out", str(out), "--fail-on", "high"]) == 1
    assert cli_main(["repo", str(SAMPLE), "--mock", "--plan"]) == 0
    assert "app/users.py" in capsys.readouterr().err
    assert cli_main(["repo", "nope", "--mock"]) == 2


def test_cli_plan_needs_no_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)  # no key and no flag: defaults to mock, plan still prices Claude
    assert cli_main(["repo", str(SAMPLE), "--plan"]) == 0
    assert "Cost:" in capsys.readouterr().err


def api_client(token="t0k"):
    settings = Settings(use_mock_llm=True, api_token=token, github_dry_run=True, github_webhook_secret="s")
    engine = ReviewEngine(MockClient(), settings)
    app = create_app(settings, engine, repo_reviewer=RepoReviewer(engine, settings, SampleFetcher()))
    return TestClient(app)


def test_api_repo_review_flow():
    c = api_client()
    auth = {"Authorization": "Bearer t0k"}
    assert c.post("/repo-reviews", json={"repo": "octo/demo"}).status_code == 401
    assert c.post("/repo-reviews", json={"repo": "../../etc"}, headers=auth).status_code == 400
    resp = c.post("/repo-reviews", json={"repo": "https://github.com/octo/demo"}, headers=auth)
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    job = c.get(f"/repo-reviews/{job_id}", headers=auth).json()  # background task already ran
    assert job["status"] == "done" and job["counts"]["critical"] == 2
    assert "report_markdown" not in job
    report = c.get(f"/repo-reviews/{job_id}/report", headers=auth)
    assert report.status_code == 200 and report.text.startswith("# ReviewBot repository review: octo/demo")

    planned = c.post("/repo-reviews", json={"repo": "octo/demo", "plan_only": True}, headers=auth).json()
    job = c.get(planned["status_url"], headers=auth).json()
    assert job["status"] == "planned" and "4 to review" in job["plan"]["summary"]
    assert c.get(planned["report_url"], headers=auth).status_code == 409
    assert c.get("/repo-reviews/missing", headers=auth).status_code == 404


def test_api_without_token_is_local_only():
    c = api_client(token=None)  # TestClient's client host is "testclient", i.e. not local
    assert c.post("/repo-reviews", json={"repo": "octo/demo"}).status_code == 403


def test_ui_is_served_with_strict_csp():
    c = api_client()
    page = c.get("/")
    assert page.status_code == 200 and "<title>ReviewBot</title>" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert c.get("/ui/app.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/ui/app.css").status_code == 200
    assert c.get("/ui/index.html").status_code == 404  # only the allow-listed assets
    assert c.get("/ui/..%2Fapp.py").status_code == 404


def test_api_info_and_plan_then_start():
    c = api_client()
    auth = {"Authorization": "Bearer t0k"}
    info = c.get("/api/info").json()
    assert info["mock"] is True and info["token_required"] is True and info["budget_usd"] == 2.0

    created = c.post("/repo-reviews", json={"repo": "octo/demo", "plan_only": True}, headers=auth).json()
    job = c.get(created["status_url"], headers=auth).json()
    assert job["status"] == "planned"
    assert job["plan"]["files"] == ["app/db.py", "app/files.py", "app/slug.py", "app/users.py"]
    assert job["plan"]["over_budget"] is False and job["plan"]["commit"] == "abc1234def"

    assert c.post(f"/repo-reviews/{created['id']}/start", headers=auth).status_code == 202
    job = c.get(created["status_url"], headers=auth).json()
    assert job["status"] == "done" and job["progress"] == {"done": 1, "total": 1}
    assert job["usage"]["calls"] == 3 and job["model"] == "mock"
    assert c.post(f"/repo-reviews/{created['id']}/start", headers=auth).status_code == 409

    listing = c.get("/repo-reviews", headers=auth).json()
    assert listing[0]["id"] == created["id"] and listing[0]["counts"]["critical"] == 2
    report = c.get(created["report_url"], headers=auth)
    assert 'filename="octo_demo_abc1234.md"' in report.headers["content-disposition"]


def test_api_budget_is_enforced():
    class Priced(MockClient):
        model = "claude-sonnet-5-5"

    settings = Settings(use_mock_llm=True, api_token="t", github_dry_run=True, repo_budget_usd=0.001)
    engine = ReviewEngine(Priced(), settings)
    c = TestClient(create_app(settings, engine, repo_reviewer=RepoReviewer(engine, settings, SampleFetcher())))
    auth = {"Authorization": "Bearer t"}

    planned = c.post("/repo-reviews", json={"repo": "octo/demo", "plan_only": True}, headers=auth).json()
    plan = c.get(planned["status_url"], headers=auth).json()["plan"]
    assert plan["over_budget"] is True and plan["budget_usd"] == 0.001
    assert c.post(f"/repo-reviews/{planned['id']}/start", headers=auth).status_code == 400

    # a caller can't raise the budget above the server's ceiling
    direct = c.post("/repo-reviews", json={"repo": "octo/demo", "budget_usd": 50}, headers=auth).json()
    job = c.get(direct["status_url"], headers=auth).json()
    assert job["status"] == "rejected" and "over the $0.00 budget" in job["error"]
