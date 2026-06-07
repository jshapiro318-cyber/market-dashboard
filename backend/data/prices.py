"""Price data fetcher — Alpaca-backed.

Replaces the prior yfinance implementation. Maintains the same public API
(fetch_history → pandas DataFrame with OHLCV columns, fetch_quote → dict,
candles_for_chart → list[dict]) so the rest of the system is unchanged.

Notes:
 - Free tier uses the IEX feed (delayed/limited). Set APCA_DATA_FEED=sip in
   .env for full-market data if you have a paid plan.
 - Single in-process LRU-style cache (60 s) so repeated UI requests don't
   hammer the API.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from ..config import ALPACA_DATA_FEED, ALPACA_KEY, ALPACA_SECRET, assert_configured


_client: StockHistoricalDataClient | None = None


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        assert_configured()
        _client = StockHistoricalDataClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET)
    return _client


_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
_TTL_SECONDS = 60


def _period_to_days(period: str) -> int:
    period = (period or "1y").lower().strip()
    if period.endswith("mo"):
        return int(period[:-2]) * 31
    if period.endswith("y"):
        return int(period[:-1]) * 366
    if period.endswith("d"):
        return int(period[:-1])
    if period == "ytd":
        now = datetime.utcnow()
        return (now - datetime(now.year, 1, 1)).days
    return 365


def _interval_to_timeframe(interval: str) -> TimeFrame:
    interval = (interval or "1d").lower().strip()
    mapping = {
        "1m":  TimeFrame(1, TimeFrameUnit.Minute),
        "5m":  TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "30m": TimeFrame(30, TimeFrameUnit.Minute),
        "1h":  TimeFrame(1, TimeFrameUnit.Hour),
        "1d":  TimeFrame(1, TimeFrameUnit.Day),
        "1wk": TimeFrame(1, TimeFrameUnit.Week),
        "1mo": TimeFrame(1, TimeFrameUnit.Month),
    }
    return mapping.get(interval, TimeFrame(1, TimeFrameUnit.Day))


def fetch_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """OHLCV history for a ticker. Returns DataFrame with Open/High/Low/Close/Volume."""
    sym = ticker.upper()
    key = (sym, period, interval)
    now = time.time()
    if key in _cache and now - _cache[key][0] < _TTL_SECONDS:
        return _cache[key][1].copy()

    # IEX free tier requires end time to be at least 15 minutes ago
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    days = _period_to_days(period)
    start = end - timedelta(days=days)
    tf = _interval_to_timeframe(interval)

    client = _get_client()
    req = StockBarsRequest(
        symbol_or_symbols=sym,
        timeframe=tf,
        start=start,
        end=end,
        feed=ALPACA_DATA_FEED,
        limit=10000,
    )
    barset = client.get_stock_bars(req)
    df = barset.df
    if df is None or df.empty:
        raise ValueError(f"No data returned for {sym} (period={period}, interval={interval})")

    # Multi-index (symbol, timestamp) — drop symbol level
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)

    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

    _cache[key] = (now, df.copy())
    return df


def fetch_quote(ticker: str) -> dict:
    """Most recent close + daily change. Mirrors the old yfinance-based shape."""
    sym = ticker.upper()
    df = fetch_history(sym, period="1mo", interval="1d")
    if len(df) < 1:
        raise ValueError(f"No quote available for {sym}")

    last_row = df.iloc[-1]
    prev_row = df.iloc[-2] if len(df) >= 2 else last_row
    avg_vol = float(df["Volume"].tail(20).mean()) if len(df) >= 20 else float(df["Volume"].mean())
    change = float(last_row["Close"] - prev_row["Close"])
    change_pct = float(change / prev_row["Close"] * 100) if prev_row["Close"] else 0.0
    rel_vol = float(last_row["Volume"] / avg_vol) if avg_vol else 1.0

    ts = df.index[-1]
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return {
        "ticker": sym,
        "last": float(last_row["Close"]),
        "open": float(last_row["Open"]),
        "high": float(last_row["High"]),
        "low": float(last_row["Low"]),
        "volume": int(last_row["Volume"]),
        "avg_volume_20d": int(avg_vol),
        "relative_volume": round(rel_vol, 2),
        "change": round(change, 4),
        "change_pct": round(change_pct, 4),
        "as_of": ts.astimezone(timezone.utc).isoformat(),
    }


def fetch_latest_price(ticker: str) -> float:
    """Real-time-ish last price via latest bar (used for limit-drift check at trade time)."""
    sym = ticker.upper()
    client = _get_client()
    try:
        req = StockLatestBarRequest(symbol_or_symbols=sym, feed=ALPACA_DATA_FEED)
        latest = client.get_stock_latest_bar(req)
        if sym in latest:
            return float(latest[sym].close)
    except Exception:
        pass
    # Fallback to last close from history
    return fetch_quote(sym)["last"]


def candles_for_chart(df: pd.DataFrame, max_bars: int = 250) -> list[dict]:
    """Convert DataFrame → Lightweight Charts format."""
    tail = df.tail(max_bars)
    out = []
    for idx, row in tail.iterrows():
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "time": int(ts.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        })
    return out
