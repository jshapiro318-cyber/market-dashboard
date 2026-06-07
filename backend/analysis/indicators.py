"""Technical indicators computed from OHLCV data.

All formulas follow standard textbook definitions (Wilder's RSI/ATR use
exponential smoothing with alpha=1/period; MACD uses EMAs).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0):
    middle = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle
    return upper, middle, lower, width


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3
):
    lowest = low.rolling(window=k_period, min_periods=k_period).min()
    highest = high.rolling(window=k_period, min_periods=k_period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    d = k.rolling(window=d_period, min_periods=d_period).mean()
    return k, d


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def vwap_session(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Rolling cumulative VWAP across the provided window.

    For true session VWAP you'd reset at market open; this is the simple
    cumulative form, sufficient for daily-bar analysis context.
    """
    tp = (high + low + close) / 3
    return (tp * volume).cumsum() / volume.cumsum().replace(0, np.nan)


def linear_slope(series: pd.Series, lookback: int) -> float:
    """Slope of best-fit line over the last `lookback` points, normalized by mean."""
    tail = series.dropna().tail(lookback)
    if len(tail) < lookback:
        return 0.0
    y = tail.values
    x = np.arange(len(y))
    slope = np.polyfit(x, y, 1)[0]
    mean = float(np.mean(y))
    return float(slope / mean) if mean else 0.0


def compute_all(df: pd.DataFrame) -> dict:
    """Compute indicators on a DataFrame with columns Open/High/Low/Close/Volume.

    Returns a dict of the most recent values (and a few derived diagnostics)
    suitable for JSON serialization.
    """
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    ema9 = ema(close, 9)
    ema21 = ema(close, 21)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    sma200 = sma(close, 200)

    rsi14 = rsi(close, 14)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_u, bb_m, bb_l, bb_width = bollinger_bands(close)
    atr14 = atr(high, low, close, 14)
    stoch_k, stoch_d = stochastic(high, low, close)
    obv_series = obv(close, volume)
    vwap_series = vwap_session(high, low, close, volume)

    # 20-day BB width history for squeeze detection
    bb_width_pctile = float(bb_width.rolling(60).rank(pct=True).iloc[-1]) if len(bb_width.dropna()) >= 60 else None

    return {
        "rsi_14": _last(rsi14),
        "macd": {
            "line": _last(macd_line),
            "signal": _last(macd_sig),
            "histogram": _last(macd_hist),
            "hist_prev": _last(macd_hist, offset=1),
        },
        "bbands": {
            "upper": _last(bb_u),
            "middle": _last(bb_m),
            "lower": _last(bb_l),
            "width": _last(bb_width),
            "width_percentile_60d": bb_width_pctile,
        },
        "atr_14": _last(atr14),
        "atr_pct": _safe_div(_last(atr14), _last(close)),
        "vwap": _last(vwap_series),
        "ema_9": _last(ema9),
        "ema_21": _last(ema21),
        "ema_50": _last(ema50),
        "ema_200": _last(ema200),
        "sma_20": _last(sma20),
        "sma_50": _last(sma50),
        "sma_200": _last(sma200),
        "stochastic": {"k": _last(stoch_k), "d": _last(stoch_d)},
        "obv_slope_20": linear_slope(obv_series, 20),
        "price_slope_20": linear_slope(close, 20),
        # Cross-state flags
        "ema9_above_21": _last(ema9) is not None and _last(ema21) is not None and _last(ema9) > _last(ema21),
        "ema50_above_200": _last(ema50) is not None and _last(ema200) is not None and _last(ema50) > _last(ema200),
        "price_above_ema200": _last(close) is not None and _last(ema200) is not None and _last(close) > _last(ema200),
    }


def _last(series: pd.Series, offset: int = 0):
    s = series.dropna()
    if len(s) <= offset:
        return None
    val = s.iloc[-1 - offset]
    if pd.isna(val):
        return None
    return float(val)


def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b
