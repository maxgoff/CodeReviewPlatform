"""Send a signed pull_request webhook to a locally running ReviewBot.

    python scripts/simulate_webhook.py                     # -> http://localhost:27183/webhook
    python scripts/simulate_webhook.py --url https://xyz.ngrok.app/webhook --repo you/repo --pr 12 --sha <head sha>

Uses GITHUB_WEBHOOK_SECRET from the environment, the same secret the server checks.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import uuid

import httpx


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:27183/webhook")
    p.add_argument("--repo", default="acme/accounts")
    p.add_argument("--pr", type=int, default=7)
    p.add_argument("--sha", default="0000000000000000000000000000000000000000")
    p.add_argument("--action", default="opened")
    a = p.parse_args()

    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "dev-secret")
    body = json.dumps({
        "action": a.action,
        "repository": {"full_name": a.repo},
        "pull_request": {"number": a.pr, "draft": False, "head": {"sha": a.sha}},
    }).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = httpx.post(a.url, content=body, headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
    })
    print(resp.status_code, resp.json())


if __name__ == "__main__":
    main()
