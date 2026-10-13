import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from reviewbot.app import create_app
from reviewbot.engine import ReviewEngine
from reviewbot.github import GitHubClient, verify_signature
from reviewbot.llm import MockClient
from reviewbot.report import comment_body
from reviewbot.models import Category, Finding, Severity
from tests.fakes import WEBHOOK_SECRET


class FakeGitHub:
    def __init__(self, diff):
        self.diff = diff
        self.reviews = []

    async def get_pr_diff(self, repo, number):
        return self.diff

    async def post_review(self, repo, number, payload):
        self.reviews.append((repo, number, payload))


def sign(body: bytes, secret=WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_event(action="opened", draft=False) -> bytes:
    return json.dumps({
        "action": action,
        "repository": {"full_name": "acme/accounts"},
        "pull_request": {"number": 7, "draft": draft, "head": {"sha": "abc123"}},
    }).encode()


def client_for(sample_diff, settings):
    gh = FakeGitHub(sample_diff)
    app = create_app(settings, ReviewEngine(MockClient(), settings), gh)
    return TestClient(app), gh


def post(client, body, event="pull_request", delivery="d-1", signature=None):
    return client.post("/webhook", content=body, headers={
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature or sign(body), "Content-Type": "application/json",
    })


def test_rejects_bad_signature(sample_diff, settings):
    client, gh = client_for(sample_diff, settings)
    assert post(client, pr_event(), signature="sha256=deadbeef").status_code == 401
    assert gh.reviews == []


def test_ping(sample_diff, settings):
    client, _ = client_for(sample_diff, settings)
    assert post(client, b"{}", event="ping").json() == {"status": "pong"}


def test_opened_pr_gets_inline_review(sample_diff, settings):
    client, gh = client_for(sample_diff, settings)
    resp = post(client, pr_event())
    assert resp.status_code == 202 and resp.json()["status"] == "queued"
    (repo, number, payload), = gh.reviews  # TestClient runs background tasks before returning
    assert (repo, number) == ("acme/accounts", 7)
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "abc123"
    lines = {c["line"] for c in payload["comments"]}
    assert {10, 21, 29, 37, 40} <= lines
    assert all(c["side"] == "RIGHT" and c["path"] == "app/users.py" for c in payload["comments"])
    assert "ReviewBot summary" in payload["body"]


def test_draft_and_closed_and_duplicates_ignored(sample_diff, settings):
    client, gh = client_for(sample_diff, settings)
    assert post(client, pr_event(draft=True), delivery="a").json()["reason"] == "draft PR"
    assert post(client, pr_event(action="closed"), delivery="b").json()["reason"] == "action closed"
    post(client, pr_event(), delivery="c")
    assert post(client, pr_event(), delivery="c").json()["reason"] == "duplicate delivery"
    assert len(gh.reviews) == 1


def test_verify_signature_requires_secret():
    assert not verify_signature("", b"x", sign(b"x", ""))
    assert verify_signature("s", b"x", sign(b"x", "s"))


def test_suggestion_block_only_for_single_line_fix():
    f = Finding(path="a.py", line=1, severity=Severity.high, category=Category.security, title="t",
                explanation="e", confidence=0.9, reviewers=["security"],
                suggestion='conn.execute("SELECT * FROM t WHERE n = ?", (n,))')
    assert "```suggestion" in comment_body(f)
    f.suggestion = "line1\nline2"
    assert "```suggestion" not in comment_body(f) and "```\nline1" in comment_body(f)


def test_github_client_falls_back_to_summary_on_422():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(422 if body["comments"] else 200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gh = GitHubClient("t", http=http)
    payload = {"commit_id": "x", "event": "COMMENT", "body": "summary",
               "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "bad"}]}
    asyncio.run(gh.post_review("o/r", 1, payload))
    assert len(calls) == 2 and calls[1]["comments"] == [] and "`a.py:3`" in calls[1]["body"]
