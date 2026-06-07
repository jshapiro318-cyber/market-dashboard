"""Research persistence — today's analyzed candidates between the 9:45 AM
research window and the 10:00 AM trade window.

Keyed by US/Eastern date so research stays valid through the trade window
even if the backend restarts between the two events.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytz

from . import db

ET = pytz.timezone("America/New_York")


def _today_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def store_today(payload: dict) -> str:
    date_str = _today_str()
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO research_runs (date, ts, payload) VALUES (?, ?, ?)",
            (date_str, datetime.now(ET).isoformat(), json.dumps(payload)),
        )
    return date_str


def get(date_str: str) -> dict | None:
    with db._lock, db.connect() as c:
        row = c.execute("SELECT payload FROM research_runs WHERE date = ?", (date_str,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def get_today() -> dict | None:
    return get(_today_str())


def list_recent(limit: int = 14) -> list[dict]:
    with db._lock, db.connect() as c:
        rows = c.execute(
            "SELECT date, ts FROM research_runs ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
