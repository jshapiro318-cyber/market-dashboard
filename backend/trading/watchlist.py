"""Custom watchlist — persisted tickers the user wants always tracked.

These get:
  - included in every scanner run (prepended to the default universe)
  - news re-fetched in every pulse, even if not currently held
  - first-class candidate status in the research window
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db


def add(tickers: list[str], note: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    added = []
    skipped = []
    with db._lock, db.connect() as c:
        for t in tickers:
            t = (t or "").strip().upper()
            if not t or not t.replace(".", "").replace("-", "").isalnum():
                continue
            existing = c.execute("SELECT 1 FROM watchlist WHERE ticker = ?", (t,)).fetchone()
            if existing:
                skipped.append(t)
                continue
            c.execute(
                "INSERT INTO watchlist (ticker, added_at, note) VALUES (?, ?, ?)",
                (t, now, note),
            )
            added.append(t)
    return {"added": added, "skipped_existing": skipped, "total": len(list_all())}


def remove(tickers: list[str]) -> dict:
    removed = []
    with db._lock, db.connect() as c:
        for t in tickers:
            t = (t or "").strip().upper()
            if not t:
                continue
            res = c.execute("DELETE FROM watchlist WHERE ticker = ?", (t,))
            if res.rowcount:
                removed.append(t)
    return {"removed": removed, "total": len(list_all())}


def clear() -> dict:
    with db._lock, db.connect() as c:
        c.execute("DELETE FROM watchlist")
    return {"cleared": True, "total": 0}


def list_all() -> list[dict]:
    with db._lock, db.connect() as c:
        rows = c.execute(
            "SELECT ticker, added_at, note FROM watchlist ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


def list_tickers() -> list[str]:
    return [r["ticker"] for r in list_all()]
