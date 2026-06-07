"""Market scanner — runs the signal logic over a stock universe and ranks results.

Uses yfinance's batch download to fetch all tickers in one HTTP burst, then
computes indicators / patterns / signal locally. News is skipped in the bulk
scan (slow + rate-limited); the sentiment factor contributes 0 when absent.

Results are cached in SQLite for 30 min — the universe is large and re-running
on every page load would hammer Yahoo.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from ..config import ALPACA_DATA_FEED, ALPACA_KEY, ALPACA_SECRET, assert_configured
from . import db
from ..analysis import indicators as ind_mod
from ..analysis import patterns as pat_mod
from ..analysis import sentiment as sent_mod
from ..analysis import signals as sig_mod
from ..data.universe import universe

CACHE_TTL_SECONDS = 30 * 60


def scan_universe(force: bool = False, tickers: list[str] | None = None) -> dict:
    # Persistent watchlist always gets first-class treatment
    from . import watchlist as wl_mod
    watch = wl_mod.list_tickers()

    cache_key = "default" if tickers is None else "custom:" + ",".join(sorted(tickers))[:200]
    if not force:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    syms = tickers or universe()
    # Prepend watchlist symbols (de-duped)
    syms = list(dict.fromkeys(watch + syms))
    df_all = _batch_download(syms)

    results: list[dict] = []
    for sym in syms:
        try:
            df = _slice_single(df_all, sym)
            if df is None or len(df) < 60:
                continue
            quote = _quote_from_df(sym, df)
            indicators = ind_mod.compute_all(df)
            candle_pats = pat_mod.detect_candlestick_patterns(df, lookback=3)
            chart_pats = pat_mod.detect_chart_patterns(df, indicators)
            news_agg = {"avg_compound": 0.0, "label": "neutral", "count": 0, "weighted_count": 0.0}
            signal = sig_mod.compute_signal(quote, indicators, candle_pats, chart_pats, news_agg)
            results.append({
                "ticker": sym,
                "last": quote["last"],
                "change_pct": quote["change_pct"],
                "rel_volume": quote["relative_volume"],
                "rsi": indicators.get("rsi_14"),
                "bias": signal["bias"],
                "score": signal["score"],
                "confidence": signal["confidence"],
                "top_reasons": _top_reasons(signal),
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["score"], reverse=True)
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "universe_size": len(syms),
        "top_bullish": results[:25],
        "top_bearish": list(reversed(results[-25:])),
        "all": results,
    }
    _save_cache(cache_key, out)
    return out


_data_client: StockHistoricalDataClient | None = None


def _get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        assert_configured()
        _data_client = StockHistoricalDataClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET)
    return _data_client


CHUNK_SIZE = 25  # small enough that one bad symbol only loses a chunk


def _fetch_chunk(syms: list[str], start, end) -> pd.DataFrame | None:
    client = _get_data_client()
    req = StockBarsRequest(
        symbol_or_symbols=syms,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start, end=end,
        feed=ALPACA_DATA_FEED,
        limit=10000,
    )
    barset = client.get_stock_bars(req)
    df = barset.df
    if df is None or df.empty:
        return None
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _batch_download(syms: list[str]) -> pd.DataFrame:
    """Chunked batch fetch — Alpaca rejects whole request if any symbol is invalid,
    so we split into chunks and on failure, fall back to one-symbol-at-a-time
    within just that chunk (skipping the bad symbol)."""
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=200)
    frames: list[pd.DataFrame] = []

    for i in range(0, len(syms), CHUNK_SIZE):
        chunk = syms[i : i + CHUNK_SIZE]
        try:
            df = _fetch_chunk(chunk, start, end)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception:
            # Fallback: one symbol at a time so we keep the good ones
            for sym in chunk:
                try:
                    sub = _fetch_chunk([sym], start, end)
                    if sub is not None and not sub.empty:
                        frames.append(sub)
                except Exception:
                    continue  # bad/delisted symbol — skip silently

    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames)
    return combined


def _slice_single(df_all: pd.DataFrame, sym: str) -> pd.DataFrame | None:
    """Alpaca returns multi-index (symbol, timestamp). Slice for one symbol."""
    if df_all is None or df_all.empty:
        return None
    if isinstance(df_all.index, pd.MultiIndex):
        if sym not in df_all.index.get_level_values(0):
            return None
        sub = df_all.xs(sym, level=0)
    else:
        sub = df_all
    sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return sub if len(sub) > 0 else None


def _quote_from_df(sym: str, df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    avg_vol = float(df["Volume"].tail(20).mean()) if len(df) >= 20 else float(df["Volume"].mean())
    change = float(last["Close"] - prev["Close"])
    change_pct = float(change / prev["Close"] * 100) if prev["Close"] else 0.0
    rel_vol = float(last["Volume"] / avg_vol) if avg_vol else 1.0
    return {
        "ticker": sym,
        "last": float(last["Close"]),
        "open": float(last["Open"]),
        "high": float(last["High"]),
        "low": float(last["Low"]),
        "volume": int(last["Volume"]),
        "avg_volume_20d": int(avg_vol),
        "relative_volume": round(rel_vol, 2),
        "change": round(change, 4),
        "change_pct": round(change_pct, 4),
    }


def _top_reasons(signal: dict, n: int = 3) -> list[str]:
    """Sort factors by absolute contribution (|score × weight|) so the displayed
    reasons reflect what's actually driving the score — not the first-listed
    factor. Without this, every uptrending ticker shows the same boilerplate
    'Price above 200-day EMA' string because Trend is iterated first.

    Sign of contribution matches the signal direction so we only surface
    confirming reasons (don't show a +Trend reason for a bearish signal).
    """
    overall = signal.get("score", 0)
    direction = 1 if overall >= 0 else -1
    factors = signal.get("factors", []) or []

    def contribution(f):
        return f.get("score", 0) * f.get("weight", 0)

    # Confirming factors first (sign matches), each sorted by contribution magnitude.
    confirming = [f for f in factors if contribution(f) * direction > 0]
    confirming.sort(key=lambda f: -abs(contribution(f)))
    # Then any remaining factors (still pull a reason if confirming list is short).
    others = [f for f in factors if f not in confirming]
    others.sort(key=lambda f: -abs(contribution(f)))

    # Pick AT MOST ONE reason per factor so the top-N spans different lenses
    # (otherwise Trend's 3-4 sub-reasons monopolise the list and every uptrending
    # ticker looks identical).
    out: list[str] = []
    seen_factors: set[str] = set()
    for f in confirming + others:
        name = f.get("name", "?")
        if name in seen_factors:
            continue
        reasons = f.get("reasons", [])
        if not reasons:
            continue
        tagged = f"[{name}] {reasons[0]}"
        out.append(tagged)
        seen_factors.add(name)
        if len(out) >= n:
            return out
    return out


def _load_cache(key: str) -> dict | None:
    with db._lock, db.connect() as c:
        row = c.execute("SELECT ts, payload FROM scanner_cache WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        ts = datetime.fromisoformat(row["ts"])
    except Exception:
        return None
    if (datetime.now(timezone.utc) - ts).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return json.loads(row["payload"])


def _save_cache(key: str, payload: dict):
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO scanner_cache (key, ts, payload) VALUES (?, ?, ?)",
            (key, datetime.now(timezone.utc).isoformat(), json.dumps(payload)),
        )
