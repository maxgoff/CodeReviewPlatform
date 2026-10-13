"""Database helpers."""

import sqlite3

DB_PATH = "accounts.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
