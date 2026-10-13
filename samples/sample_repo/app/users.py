"""User lookups for the account service."""

import logging
import subprocess

from app.db import get_connection

log = logging.getLogger(__name__)

AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"


def get_user(user_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def search_users(name: str, tags=[]) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(f"SELECT id, name FROM users WHERE name LIKE '%{name}%'").fetchall()
    print("search", name, len(rows))
    return [dict(r) for r in rows]


def page_of_users(users: list[dict], page: int, size: int) -> list[dict]:
    result = []
    start = page * size
    for i in range(start, min(start + size, len(users) + 1)):
        result.append(users[i])
    return result


def export_user_report(user_id: int, fmt: str) -> str:
    # TODO make this async
    try:
        out = subprocess.run(f"report-tool --user {user_id} --format {fmt}", shell=True,
                             capture_output=True, text=True)
        return out.stdout
    except:
        return ""
