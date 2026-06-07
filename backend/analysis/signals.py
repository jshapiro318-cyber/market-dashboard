"""Multi-factor signal aggregation.

Each factor produces a score in [-10, +10] with the reasons that contributed.
The overall signal is a weighted sum; confidence reflects agreement between factors.

This is *not* a forecasting model. It is a structured summary of independent
technical and sentiment readings, scored against textbook rules.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import pstdev


@dataclass
class Factor:
    name: str
    score: float
    weight: float
    reasons: list[str]

    def to_dict(self):
        d = asdict(self)
        d["score"] = round(d["score"], 2)
        return d


WEIGHTS = {
    "Trend": 0.20,
    "Momentum": 0.17,
    "Volume": 0.12,
    "Volatility": 0.08,
    "Patterns": 0.13,
    "Sentiment": 0.12,
    "Fundamentals": 0.18,  # higher than sentiment — actual business reality matters more than headlines
}


def clamp(x, lo=-10.0, hi=10.0):
    return max(lo, min(hi, x))


def trend_factor(quote: dict, ind: dict) -> Factor:
    score = 0.0
    reasons: list[str] = []
    price = quote["last"]
    ema9, ema21, ema50, ema200 = ind.get("ema_9"), ind.get("ema_21"), ind.get("ema_50"), ind.get("ema_200")

    if ema200 is not None:
        if price > ema200:
            score += 3
            reasons.append("Price above 200-day EMA (long-term uptrend)")
        else:
            score -= 3
            reasons.append("Price below 200-day EMA (long-term downtrend)")

    if ema50 is not None and ema200 is not None:
        if ema50 > ema200:
            score += 2
            reasons.append("50-EMA above 200-EMA")
        else:
            score -= 2
            reasons.append("50-EMA below 200-EMA")

    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            score += 2
            reasons.append("9-EMA above 21-EMA (short-term uptrend)")
        else:
            score -= 2
            reasons.append("9-EMA below 21-EMA (short-term downtrend)")

    slope = ind.get("price_slope_20", 0)
    if slope > 0.002:
        score += 2
        reasons.append(f"Positive 20-bar price slope ({slope*100:.2f}%/bar)")
    elif slope < -0.002:
        score -= 2
        reasons.append(f"Negative 20-bar price slope ({slope*100:.2f}%/bar)")

    return Factor("Trend", clamp(score), WEIGHTS["Trend"], reasons)


def momentum_factor(ind: dict) -> Factor:
    score = 0.0
    reasons: list[str] = []
    rsi = ind.get("rsi_14")
    macd = ind.get("macd", {})

    if rsi is not None:
        if rsi >= 70:
            score -= 3
            reasons.append(f"RSI overbought ({rsi:.1f}) — mean-reversion risk")
        elif rsi <= 30:
            score += 3
            reasons.append(f"RSI oversold ({rsi:.1f}) — potential bounce")
        elif 50 < rsi < 70:
            score += 2
            reasons.append(f"RSI bullish zone ({rsi:.1f})")
        elif 30 < rsi < 50:
            score -= 2
            reasons.append(f"RSI bearish zone ({rsi:.1f})")

    hist = macd.get("histogram")
    hist_prev = macd.get("hist_prev")
    if hist is not None and hist_prev is not None:
        if hist > 0 and hist_prev <= 0:
            score += 4
            reasons.append("MACD histogram crossed positive")
        elif hist < 0 and hist_prev >= 0:
            score -= 4
            reasons.append("MACD histogram crossed negative")
        elif hist > 0 and hist > hist_prev:
            score += 2
            reasons.append("MACD histogram positive and expanding")
        elif hist < 0 and hist < hist_prev:
            score -= 2
            reasons.append("MACD histogram negative and expanding")

    stoch = ind.get("stochastic", {})
    k = stoch.get("k")
    if k is not None:
        if k > 80:
            score -= 1
            reasons.append(f"Stochastic overbought (K={k:.0f})")
        elif k < 20:
            score += 1
            reasons.append(f"Stochastic oversold (K={k:.0f})")

    ret_5d = ind.get("ret_5d")
    if ret_5d is not None and ret_5d > 15.0:
        score -= 3.0  # Penalize heavy chasing
        reasons.append(f"Caution: Surged {ret_5d:.1f}% in 5 days (chasing risk)")

    return Factor("Momentum", clamp(score), WEIGHTS["Momentum"], reasons)


def volume_factor(quote: dict, ind: dict) -> Factor:
    score = 0.0
    reasons: list[str] = []
    rel_vol = quote.get("relative_volume", 1.0)
    is_green = quote["change"] > 0

    if rel_vol >= 2.0:
        if is_green:
            score += 5
            reasons.append(f"Heavy volume up day ({rel_vol:.1f}x avg)")
        else:
            score -= 5
            reasons.append(f"Heavy volume down day ({rel_vol:.1f}x avg)")
    elif rel_vol >= 1.5:
        if is_green:
            score += 3
            reasons.append(f"Above-average volume on green candle ({rel_vol:.1f}x)")
        else:
            score -= 3
            reasons.append(f"Above-average volume on red candle ({rel_vol:.1f}x)")
    elif rel_vol < 0.7:
        score -= 1
        reasons.append(f"Below-average volume ({rel_vol:.1f}x) — weak conviction")

    obv_slope = ind.get("obv_slope_20", 0)
    price_slope = ind.get("price_slope_20", 0)
    # OBV agreeing with price = healthy; diverging = warning
    if obv_slope > 0 and price_slope > 0:
        score += 2
        reasons.append("OBV confirms uptrend")
    elif obv_slope < 0 and price_slope < 0:
        score -= 2
        reasons.append("OBV confirms downtrend")
    elif obv_slope < 0 and price_slope > 0:
        score -= 3
        reasons.append("OBV divergence: price up, volume flow down")
    elif obv_slope > 0 and price_slope < 0:
        score += 3
        reasons.append("OBV divergence: price down, volume flow up (accumulation?)")

    return Factor("Volume", clamp(score), WEIGHTS["Volume"], reasons)


def volatility_factor(ind: dict) -> Factor:
    score = 0.0
    reasons: list[str] = []
    bb = ind.get("bbands", {})
    width_pctile = bb.get("width_percentile_60d")
    if width_pctile is not None:
        if width_pctile < 0.15:
            score += 3
            reasons.append("Bollinger squeeze — breakout pending (direction TBD)")
        elif width_pctile > 0.85:
            score -= 1
            reasons.append("Bollinger bands very wide — exhaustion risk")

    atr_pct = ind.get("atr_pct")
    if atr_pct is not None:
        if atr_pct > 0.04:
            score -= 2
            reasons.append(f"High volatility (ATR {atr_pct*100:.1f}% of price) — wider stops needed")
        elif atr_pct < 0.015:
            score += 4
            reasons.append(f"Tight consolidation (ATR {atr_pct*100:.2f}%) — prime value/breakout setup")

    return Factor("Volatility", clamp(score), WEIGHTS["Volatility"], reasons)


def patterns_factor(candles: list[dict], chart: list[dict]) -> Factor:
    score = 0.0
    reasons: list[str] = []
    for p in candles:
        sign = 1 if p["implication"] == "bullish" else -1 if p["implication"] == "bearish" else 0
        recency_weight = 1.0 if p["candle_index"] == -1 else 0.5 if p["candle_index"] == -2 else 0.25
        contribution = sign * p["confidence"] * 5 * recency_weight
        score += contribution
        if sign != 0:
            reasons.append(f"{p['name']} ({p['implication']}, conf {p['confidence']:.2f})")
    for p in chart:
        sign = 1 if p["implication"] == "bullish" else -1 if p["implication"] == "bearish" else 0
        score += sign * p["confidence"] * 4
        reasons.append(f"{p['name']} → {p['implication']}")
    return Factor("Patterns", clamp(score), WEIGHTS["Patterns"], reasons)


def fundamentals_factor(fund: dict | None, quote: dict) -> Factor:
    if not fund or fund.get("error"):
        return Factor("Fundamentals", 0.0, WEIGHTS["Fundamentals"], ["Fundamentals unavailable"])
    from . import fundamentals as fund_mod
    score, reasons = fund_mod.fundamentals_factor_score(fund, quote)
    if not reasons:
        reasons = ["No fundamental signals detected"]
    return Factor("Fundamentals", score, WEIGHTS["Fundamentals"], reasons)


def sentiment_factor(news_agg: dict) -> Factor:
    score = 0.0
    reasons: list[str] = []
    avg = news_agg.get("avg_compound", 0)
    count = news_agg.get("count", 0)

    if count == 0:
        reasons.append("No recent news available")
        return Factor("Sentiment", 0.0, WEIGHTS["Sentiment"], reasons)

    score = clamp(avg * 10)
    reasons.append(f"{count} recent articles, recency-weighted sentiment {avg:+.2f} ({news_agg.get('label')})")
    return Factor("Sentiment", score, WEIGHTS["Sentiment"], reasons)


def aggregate(factors: list[Factor], quote: dict, ind: dict) -> dict:
    weighted_sum = sum(f.score * f.weight for f in factors)
    scores = [f.score for f in factors]
    # Confidence: 1.0 means all factors agree strongly; near 0 means they cancel
    spread = pstdev(scores) if len(scores) > 1 else 0
    agreement = max(0.0, 1.0 - spread / 6.0)  # spread of 6 → 0 agreement
    magnitude = min(1.0, abs(weighted_sum) / 5.0)
    confidence = round(0.5 * agreement + 0.5 * magnitude, 3)

    if weighted_sum > 3:
        bias = "bullish"
    elif weighted_sum > 1:
        bias = "lean bullish"
    elif weighted_sum < -3:
        bias = "bearish"
    elif weighted_sum < -1:
        bias = "lean bearish"
    else:
        bias = "neutral"

    risks = _risk_flags(quote, ind, factors)

    return {
        "bias": bias,
        "score": round(weighted_sum, 2),
        "confidence": confidence,
        "horizon": "1-5 trading days",
        "factors": [f.to_dict() for f in factors],
        "risks": risks,
    }


def _risk_flags(quote: dict, ind: dict, factors: list[Factor]) -> list[str]:
    flags: list[str] = []
    rsi = ind.get("rsi_14")
    if rsi is not None and (rsi > 75 or rsi < 25):
        flags.append(f"RSI at extreme ({rsi:.1f}) — reversal risk elevated")
    atr_pct = ind.get("atr_pct")
    if atr_pct is not None and atr_pct > 0.05:
        flags.append("Volatility unusually high — use wider stops or smaller size")
    bb = ind.get("bbands", {})
    if bb.get("width_percentile_60d") is not None and bb["width_percentile_60d"] < 0.1:
        flags.append("Volatility extremely compressed — breakout direction is uncertain")
    scores = [f.score for f in factors]
    if scores and pstdev(scores) > 5:
        flags.append("Factors disagree strongly — signal quality low")
    flags.append("Past patterns do not guarantee future results. Educational tool — not financial advice.")
    return flags


def compute_signal(quote: dict, ind: dict, candle_patterns: list[dict], chart_patterns: list[dict], news_agg: dict, fundamentals: dict | None = None) -> dict:
    factors = [
        trend_factor(quote, ind),
        momentum_factor(ind),
        volume_factor(quote, ind),
        volatility_factor(ind),
        patterns_factor(candle_patterns, chart_patterns),
        sentiment_factor(news_agg),
        fundamentals_factor(fundamentals, quote),
    ]
    return aggregate(factors, quote, ind)
