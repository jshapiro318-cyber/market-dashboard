"""Environment loader for Alpaca credentials and runtime config.

Reads `.env` (project root) at import time. Fails fast with a clear message
if required keys are missing — better than a cryptic Alpaca 401 mid-trade.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        # Don't override values set in the actual shell environment
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file(ENV_PATH)


ALPACA_KEY = os.environ.get("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.environ.get("APCA_API_SECRET_KEY", "").strip()
ALPACA_BASE_URL = os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets").strip()
ALPACA_DATA_FEED = os.environ.get("APCA_DATA_FEED", "iex").strip()
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "").strip()

IS_PAPER = "paper-api" in ALPACA_BASE_URL


def assert_configured() -> None:
    """Raise with an actionable message if Alpaca creds are missing."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError(
            "Alpaca API credentials not configured.\n"
            f"Edit {ENV_PATH} (copy from .env.example) and set APCA_API_KEY_ID "
            "and APCA_API_SECRET_KEY with keys from your Alpaca dashboard. "
            "After saving, restart the server."
        )


def configured() -> bool:
    return bool(ALPACA_KEY and ALPACA_SECRET)


def status() -> dict:
    return {
        "alpaca_configured": configured(),
        "alpaca_paper": IS_PAPER,
        "alpaca_base_url": ALPACA_BASE_URL,
        "alpaca_data_feed": ALPACA_DATA_FEED,
        "env_path": str(ENV_PATH),
        "env_file_exists": ENV_PATH.exists(),
    }
