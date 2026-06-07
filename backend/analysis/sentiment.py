"""News sentiment analysis using VADER.

VADER is a rules-based sentiment analyzer that handles negation, intensifiers,
and slang reasonably well, with no model download. For more domain-tuned
analysis, swap in FinBERT (commented import below) — but it's a 400MB download.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()

# Boost financial terms that VADER under-weights
_FIN_LEXICON = {
    "beat": 2.5, "beats": 2.5, "outperform": 2.5, "upgrade": 2.5, "upgraded": 2.5,
    "buyback": 1.8, "dividend": 1.2, "raises": 2.0, "raised": 2.0,
    "miss": -2.5, "missed": -2.5, "downgrade": -2.5, "downgraded": -2.5,
    "lawsuit": -2.0, "probe": -1.8, "fraud": -3.5, "bankruptcy": -4.0,
    "guidance": 0.5, "guidance cut": -3.0, "guidance raised": 3.0,
    "layoffs": -2.0, "restructuring": -1.5, "sec investigation": -3.0,
    "record high": 2.0, "all-time high": 2.0, "52-week high": 1.5,
    "52-week low": -1.5, "plunge": -2.5, "soared": 2.5, "surged": 2.0, "tumbled": -2.5,
    # Defense contract boosts
    "selected": 2.5, "selection": 2.5, "anti-drone": 3.0, "directed-energy": 2.5,
    "defense evaluation": 3.0, "vulcan": 2.5, "procurement": 2.0, "award": 2.5,
    "awarded": 2.5, "evaluating": 1.5, "evaluation": 2.0, "technical review": 2.0,
    "contract": 2.0, "surging": 2.0
}
_analyzer.lexicon.update(_FIN_LEXICON)


def score_text(text: str) -> dict:
    if not text:
        return {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0, "label": "neutral"}
    # Preprocess proper nouns that skew VADER (e.g. Department of War contains "war" which VADER penalizes heavily)
    processed = text.replace("Department of War", "Department of Defense").replace("department of war", "department of defense")
    s = _analyzer.polarity_scores(processed)
    s["label"] = _label(s["compound"])
    return s


def _label(compound: float) -> str:
    if compound >= 0.5:
        return "very positive"
    if compound >= 0.15:
        return "positive"
    if compound <= -0.5:
        return "very negative"
    if compound <= -0.15:
        return "negative"
    return "neutral"


def aggregate_news_sentiment(news_items: list[dict], half_life_hours: float = 24.0) -> dict:
    """Weighted average news sentiment with exponential recency decay.

    Each item should have 'title', optionally 'summary', and 'published' (unix timestamp).
    Returns dict with avg compound score (-1..1), label, and count.
    """
    if not news_items:
        return {"avg_compound": 0.0, "label": "neutral", "count": 0, "weighted_count": 0.0}

    now = datetime.now(timezone.utc).timestamp()
    total_w = 0.0
    weighted_sum = 0.0
    enriched = []

    for item in news_items:
        text = (item.get("title", "") + ". " + (item.get("summary", "") or "")).strip()
        s = score_text(text)
        item["sentiment"] = {"compound": s["compound"], "label": s["label"]}

        pub_ts = item.get("published")
        if pub_ts:
            age_hours = max(0, (now - pub_ts) / 3600)
            weight = 0.5 ** (age_hours / half_life_hours)
        else:
            weight = 0.5
        weighted_sum += s["compound"] * weight
        total_w += weight
        enriched.append(item)

    avg = weighted_sum / total_w if total_w else 0.0
    return {
        "avg_compound": round(avg, 3),
        "label": _label(avg),
        "count": len(news_items),
        "weighted_count": round(total_w, 2),
    }
