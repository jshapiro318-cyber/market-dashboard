"""Trade notifications — macOS native via osascript.

Best-effort: any failure is silently swallowed (no notify infra is fine).
For email/SMS, extend send() with additional channels.
"""
from __future__ import annotations

import logging
import platform
import shlex
import subprocess

log = logging.getLogger("notify")


def send(title: str, message: str, subtitle: str | None = None) -> None:
    if platform.system() != "Darwin":
        log.info("[notify] %s: %s", title, message)
        return
    try:
        parts = [f'display notification "{_esc(message)}"', f'with title "{_esc(title)}"']
        if subtitle:
            parts.append(f'subtitle "{_esc(subtitle)}"')
        script = " ".join(parts)
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning("notify failed: %s", e)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def trade(side: str, ticker: str, shares: float, price: float, reason: str, auto: bool = False):
    prefix = "AUTO " if auto else ""
    title = f"{prefix}{side} {ticker}"
    subtitle = f"{shares:.2f} sh @ ${price:.2f}"
    send(title, reason or "", subtitle=subtitle)
