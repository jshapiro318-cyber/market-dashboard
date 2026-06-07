"""SQLite-backed paper trading store."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "paper.sqlite"
_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                initial_cash REAL NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                ticker TEXT PRIMARY KEY,
                shares REAL NOT NULL,
                avg_cost REAL NOT NULL,
                opened_at TEXT NOT NULL,
                signal_at_entry TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                proceeds REAL NOT NULL,
                reason TEXT,
                signal_score REAL,
                signal_bias TEXT,
                confidence REAL,
                auto INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scanner_cache (
                key TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                summary TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_runs (
                date TEXT PRIMARY KEY,   -- YYYY-MM-DD in US/Eastern
                ts TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journals (
                date TEXT PRIMARY KEY,   -- YYYY-MM-DD in US/Eastern
                ts TEXT NOT NULL,
                path TEXT NOT NULL,
                preview TEXT
            );

            CREATE TABLE IF NOT EXISTS wheel_runs (
                ticker TEXT PRIMARY KEY,
                status TEXT NOT NULL,           -- IDLE | SELL_PUT_OPEN | SELL_CALL_OPEN | STOPPED
                started_at TEXT NOT NULL,
                last_check_at TEXT,
                premium_collected REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                cycles INTEGER NOT NULL DEFAULT 0,
                current_option_symbol TEXT,
                current_option_strike REAL,
                current_option_expiration TEXT,
                current_option_entry_premium REAL,
                underlying_cost_basis REAL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS wheel_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ticker TEXT NOT NULL,
                leg_type TEXT NOT NULL,         -- PUT | CALL
                contract_symbol TEXT NOT NULL,
                side TEXT NOT NULL,             -- SELL_TO_OPEN | BUY_TO_CLOSE | ASSIGNED | EXPIRED | CALLED_AWAY
                strike REAL,
                expiration TEXT,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                premium_delta REAL NOT NULL,    -- positive = received, negative = paid
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                ticker TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                note TEXT                       -- e.g. "from Seeking Alpha portfolio"
            );

            CREATE TABLE IF NOT EXISTS profit_taking_state (
                ticker TEXT PRIMARY KEY,
                stage INTEGER NOT NULL DEFAULT 0
            );
        """)
        # Seed portfolio if empty
        row = c.execute("SELECT 1 FROM portfolio WHERE id = 1").fetchone()
        if not row:
            from datetime import datetime, timezone
            c.execute(
                "INSERT INTO portfolio (id, cash, initial_cash, created_at) VALUES (1, ?, ?, ?)",
                (100_000.0, 100_000.0, datetime.now(timezone.utc).isoformat()),
            )
