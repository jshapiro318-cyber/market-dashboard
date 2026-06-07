"""Candlestick and chart pattern detection.

Implementations follow Bulkowski / Steve Nison reference definitions.
Returned `implication` is the standard textbook bias for the pattern in
isolation — the signal aggregator weighs it against trend context.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd


@dataclass
class Pattern:
    name: str
    candle_index: int  # negative offset from end (-1 = most recent)
    implication: str  # "bullish" | "bearish" | "indecision" | "continuation"
    confidence: float  # 0-1

    def to_dict(self):
        return asdict(self)


def _body(o, c):
    return abs(c - o)


def _upper(o, h, c):
    return h - max(o, c)


def _lower(o, l, c):
    return min(o, c) - l


def _is_green(o, c):
    return c > o


def _is_red(o, c):
    return c < o


def _rng(h, l):
    return h - l


def detect_candlestick_patterns(df: pd.DataFrame, lookback: int = 5) -> list[dict]:
    """Scan the last `lookback` candles for classical patterns.

    Requires at least 3 prior candles for multi-candle pattern context.
    """
    patterns: list[Pattern] = []
    n = len(df)
    if n < 5:
        return []

    o = df["Open"].values
    h = df["High"].values
    l = df["Low"].values
    c = df["Close"].values

    start = max(2, n - lookback)
    for i in range(start, n):
        idx = i - n  # negative offset
        body = _body(o[i], c[i])
        upper = _upper(o[i], h[i], c[i])
        lower = _lower(o[i], l[i], c[i])
        rng = _rng(h[i], l[i])
        if rng <= 0:
            continue

        prev_body = _body(o[i - 1], c[i - 1])
        prev_green = _is_green(o[i - 1], c[i - 1])
        prev_red = _is_red(o[i - 1], c[i - 1])

        # Doji — very small body relative to range
        if body / rng < 0.1:
            patterns.append(Pattern("Doji", idx, "indecision", 0.6))

        # Hammer — small body at top, long lower shadow (>= 2x body), small upper shadow
        if body > 0 and lower >= 2 * body and upper < body and body / rng < 0.4:
            implication = "bullish" if _is_lower_in_downtrend(c, i) else "indecision"
            patterns.append(Pattern("Hammer", idx, implication, 0.7 if implication == "bullish" else 0.5))

        # Shooting Star — small body at bottom, long upper shadow, small lower shadow
        if body > 0 and upper >= 2 * body and lower < body and body / rng < 0.4:
            implication = "bearish" if _is_higher_in_uptrend(c, i) else "indecision"
            patterns.append(Pattern("Shooting Star", idx, implication, 0.7 if implication == "bearish" else 0.5))

        # Marubozu — full body, almost no shadows
        if body / rng > 0.95:
            impl = "bullish" if _is_green(o[i], c[i]) else "bearish"
            patterns.append(Pattern(f"{impl.capitalize()} Marubozu", idx, impl, 0.6))

        # Bullish Engulfing
        if (
            prev_red
            and _is_green(o[i], c[i])
            and o[i] <= c[i - 1]
            and c[i] >= o[i - 1]
            and body > prev_body
        ):
            patterns.append(Pattern("Bullish Engulfing", idx, "bullish", 0.8))

        # Bearish Engulfing
        if (
            prev_green
            and _is_red(o[i], c[i])
            and o[i] >= c[i - 1]
            and c[i] <= o[i - 1]
            and body > prev_body
        ):
            patterns.append(Pattern("Bearish Engulfing", idx, "bearish", 0.8))

        # Morning Star (3-candle bullish reversal): red, small body, green closing >50% into 1st body
        if i >= 2:
            o0, c0 = o[i - 2], c[i - 2]
            o1, c1 = o[i - 1], c[i - 1]
            b0 = _body(o0, c0)
            b1 = _body(o1, c1)
            if (
                _is_red(o0, c0)
                and b1 < b0 * 0.5
                and _is_green(o[i], c[i])
                and c[i] > (o0 + c0) / 2
                and b0 > 0
            ):
                patterns.append(Pattern("Morning Star", idx, "bullish", 0.85))
            # Evening Star (3-candle bearish reversal)
            if (
                _is_green(o0, c0)
                and b1 < b0 * 0.5
                and _is_red(o[i], c[i])
                and c[i] < (o0 + c0) / 2
                and b0 > 0
            ):
                patterns.append(Pattern("Evening Star", idx, "bearish", 0.85))

        # Tweezer Top / Bottom
        if i >= 1 and abs(h[i] - h[i - 1]) / max(h[i], 1e-9) < 0.002 and prev_green and _is_red(o[i], c[i]):
            patterns.append(Pattern("Tweezer Top", idx, "bearish", 0.6))
        if i >= 1 and abs(l[i] - l[i - 1]) / max(l[i], 1e-9) < 0.002 and prev_red and _is_green(o[i], c[i]):
            patterns.append(Pattern("Tweezer Bottom", idx, "bullish", 0.6))

        # Three White Soldiers / Three Black Crows
        if i >= 2:
            if all(_is_green(o[j], c[j]) for j in (i - 2, i - 1, i)) and c[i] > c[i - 1] > c[i - 2]:
                patterns.append(Pattern("Three White Soldiers", idx, "bullish", 0.75))
            if all(_is_red(o[j], c[j]) for j in (i - 2, i - 1, i)) and c[i] < c[i - 1] < c[i - 2]:
                patterns.append(Pattern("Three Black Crows", idx, "bearish", 0.75))

    return [p.to_dict() for p in patterns]


def _is_lower_in_downtrend(close: np.ndarray, i: int, window: int = 5) -> bool:
    """Hammer is meaningful only after a downtrend."""
    if i < window:
        return False
    return close[i] < close[i - window]


def _is_higher_in_uptrend(close: np.ndarray, i: int, window: int = 5) -> bool:
    if i < window:
        return False
    return close[i] > close[i - window]


def detect_chart_patterns(df: pd.DataFrame, indicators: dict) -> list[dict]:
    """Higher-level chart-context observations: squeezes, breakouts, divergences."""
    out: list[dict] = []
    if len(df) < 30:
        return out

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # Bollinger squeeze — width in bottom decile of last 60 days
    bw_pctile = indicators.get("bbands", {}).get("width_percentile_60d")
    if bw_pctile is not None and bw_pctile < 0.15:
        out.append({"name": "Bollinger Band Squeeze", "implication": "volatility expansion pending", "confidence": 0.7})

    # 20-day breakout
    last_close = close.iloc[-1]
    high_20 = high.iloc[-21:-1].max()
    low_20 = low.iloc[-21:-1].min()
    if last_close > high_20:
        rel_vol = volume.iloc[-1] / volume.iloc[-21:-1].mean()
        out.append({
            "name": "20-Day High Breakout",
            "implication": "bullish",
            "confidence": min(0.9, 0.5 + 0.1 * (rel_vol - 1)),
        })
    elif last_close < low_20:
        rel_vol = volume.iloc[-1] / volume.iloc[-21:-1].mean()
        out.append({
            "name": "20-Day Low Breakdown",
            "implication": "bearish",
            "confidence": min(0.9, 0.5 + 0.1 * (rel_vol - 1)),
        })

    # Bullish RSI divergence — price LL, RSI HL over last 14 bars
    if "rsi_14" in indicators and indicators["rsi_14"] is not None:
        from .indicators import rsi
        rsi_series = rsi(close)
        if _has_bullish_divergence(close, rsi_series):
            out.append({"name": "Bullish RSI Divergence", "implication": "bullish", "confidence": 0.65})
        if _has_bearish_divergence(close, rsi_series):
            out.append({"name": "Bearish RSI Divergence", "implication": "bearish", "confidence": 0.65})

    # Golden / Death cross (50/200 SMA) — flag if it happened in last 5 days
    if len(df) >= 205:
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()
        for offset in range(1, 6):
            if pd.notna(sma50.iloc[-offset]) and pd.notna(sma200.iloc[-offset]) and pd.notna(sma50.iloc[-offset - 1]):
                if sma50.iloc[-offset - 1] <= sma200.iloc[-offset - 1] and sma50.iloc[-offset] > sma200.iloc[-offset]:
                    out.append({"name": f"Golden Cross ({offset}d ago)", "implication": "bullish", "confidence": 0.7})
                    break
                if sma50.iloc[-offset - 1] >= sma200.iloc[-offset - 1] and sma50.iloc[-offset] < sma200.iloc[-offset]:
                    out.append({"name": f"Death Cross ({offset}d ago)", "implication": "bearish", "confidence": 0.7})
                    break

    return out


def _has_bullish_divergence(close: pd.Series, rsi_series: pd.Series, window: int = 14) -> bool:
    c = close.tail(window).reset_index(drop=True)
    r = rsi_series.tail(window).reset_index(drop=True)
    if c.isna().any() or r.isna().any() or len(c) < window:
        return False
    half = window // 2
    p1 = int(np.argmin(c.iloc[:half].values))
    p2 = int(np.argmin(c.iloc[half:].values)) + half
    return c.iloc[p2] < c.iloc[p1] and r.iloc[p2] > r.iloc[p1]


def _has_bearish_divergence(close: pd.Series, rsi_series: pd.Series, window: int = 14) -> bool:
    c = close.tail(window).reset_index(drop=True)
    r = rsi_series.tail(window).reset_index(drop=True)
    if c.isna().any() or r.isna().any() or len(c) < window:
        return False
    half = window // 2
    p1 = int(np.argmax(c.iloc[:half].values))
    p2 = int(np.argmax(c.iloc[half:].values)) + half
    return c.iloc[p2] > c.iloc[p1] and r.iloc[p2] < r.iloc[p1]


def support_resistance(df: pd.DataFrame, n_levels: int = 3) -> dict:
    """Pivot-based S/R levels over last ~3 months."""
    if len(df) < 30:
        return {"support": [], "resistance": []}

    recent = df.tail(90) if len(df) >= 90 else df
    pivots = []
    h = recent["High"].values
    l = recent["Low"].values
    for i in range(2, len(recent) - 2):
        if h[i] > h[i - 1] and h[i] > h[i - 2] and h[i] > h[i + 1] and h[i] > h[i + 2]:
            pivots.append(("R", float(h[i])))
        if l[i] < l[i - 1] and l[i] < l[i - 2] and l[i] < l[i + 1] and l[i] < l[i + 2]:
            pivots.append(("S", float(l[i])))

    last = float(df["Close"].iloc[-1])
    resistances = sorted({round(p[1], 2) for p in pivots if p[0] == "R" and p[1] > last})[:n_levels]
    supports = sorted({round(p[1], 2) for p in pivots if p[0] == "S" and p[1] < last}, reverse=True)[:n_levels]
    return {"support": supports, "resistance": resistances}
