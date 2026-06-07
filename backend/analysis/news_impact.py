"""News → impact mapping for the pre-market brief.

For each article:
  1. Pull explicit ticker mentions ($AAPL, (AAPL), NASDAQ: AAPL).
  2. Map topic/sector keywords to ticker baskets (e.g. "iran" → energy + defense).
  3. Score sentiment via VADER (finance-tuned lexicon).
  4. Produce per-ticker direction + confidence.

Filters mentions against a known universe so random uppercase words like
'CEO', 'USD', 'ETF' don't get treated as tickers.
"""
from __future__ import annotations

import re
from collections import defaultdict

from . import sentiment as sent_mod
from ..data.universe import universe as _universe


# Cache the universe set for fast O(1) membership checks
_UNIVERSE: set[str] | None = None

def _get_universe() -> set[str]:
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = set(_universe())
    return _UNIVERSE


# ---- Ticker extraction ----

TICKER_PATTERNS = [
    re.compile(r'\$([A-Z]{1,5}(?:\.[A-Z])?)\b'),                     # $AAPL, $BRK.B
    re.compile(r'\(([A-Z]{1,5}(?:\.[A-Z])?)\)'),                     # (AAPL)
    re.compile(r'\b(?:NYSE|NASDAQ|AMEX|NYSEARCA):\s*([A-Z]{1,5}(?:\.[A-Z])?)\b'),
]

# Common English words / abbreviations that pattern-match as tickers but aren't
TICKER_BLOCKLIST = {
    "CEO","CFO","COO","CTO","USA","USD","EUR","GDP","CPI","PPI","SEC","IPO",
    "ETF","RSI","ATR","VIX","API","SDK","AI","ML","UK","US","EU","UN","OPEC",
    "FDA","FTC","DOJ","FBI","TBD","TLDR","ASAP","HQ","HR","PR","IT","OS",
}


def extract_explicit_tickers(text: str) -> set[str]:
    out: set[str] = set()
    for p in TICKER_PATTERNS:
        for m in p.finditer(text):
            sym = (m.group(1) or "").upper()
            if not sym or sym in TICKER_BLOCKLIST:
                continue
            # Normalise BRK.B variants if they ever appear with dash
            sym = sym.replace("-", ".")
            if sym in _get_universe():
                out.add(sym)
    return out


# ---- Sector / topic keyword map ----
# (keyword, tickers, modifier).  modifier=+1 means impact follows article
# sentiment; -1 means flips it (e.g. "tariff" → bullish-sounding mention
# is actually bearish for tariff-exposed names).

SECTOR_RULES: list[tuple[str, list[str], int]] = [
    # Energy / oil
    ("crude oil",        ["XOM","CVX","OXY","COP","SLB","XLE"], +1),
    ("oil prices",       ["XOM","CVX","OXY","COP","XLE"], +1),
    ("opec",             ["XOM","CVX","XLE"], +1),
    ("natural gas",      ["UNG","XOM"], +1),
    ("iran sanctions",   ["XOM","CVX","XLE","LMT","RTX","NOC"], +1),
    ("iran deal",        ["XLE","LMT","RTX"], +1),
    ("middle east",      ["XLE","LMT","RTX","NOC","GD","XOM","CVX"], +1),

    # Defense / geopolitics
    ("defense spending", ["LMT","RTX","NOC","GD","BA"], +1),
    ("pentagon",         ["LMT","RTX","NOC","GD"], +1),
    ("ukraine",          ["LMT","RTX","NOC","XLE"], +1),
    ("nato",             ["LMT","RTX","NOC","GD"], +1),

    # Rates / Fed
    ("rate cut",         ["XLF","XLRE","IWM","TLT","HYG"], +1),
    ("rate hike",        ["XLF","XLRE","IWM","TLT"], -1),
    ("hawkish fed",      ["TLT","XLRE","IWM","HYG"], -1),
    ("dovish fed",       ["TLT","XLRE","IWM","HYG","SPY","QQQ"], +1),
    ("treasury yield",   ["TLT","XLRE","XLU"], -1),
    ("inflation cools",  ["SPY","QQQ","TLT","XLF"], +1),
    ("inflation rises",  ["TLT","XLRE","XLY"], -1),
    ("cpi report",       ["TLT","SPY","XLF"], 0),
    ("ppi report",       ["TLT","SPY","XLF"], 0),
    ("jobs report",      ["TLT","SPY","XLF"], 0),
    ("non-farm payroll", ["TLT","SPY","XLF"], 0),
    ("unemployment",     ["TLT","SPY","XLP"], 0),

    # Recession / safety
    ("recession",        ["XLP","XLU","GLD","TLT","HYG"], +1),
    ("soft landing",     ["SPY","QQQ","XLF","IWM"], +1),
    ("hard landing",     ["XLP","GLD","TLT"], +1),

    # Semis / AI
    ("ai chip",          ["NVDA","AMD","AVGO","TSM","MU","ARM"], +1),
    ("semiconductor",    ["NVDA","AMD","AVGO","TSM","MU","INTC","KLAC","AMAT","LRCX","SMH","SOXX"], +1),
    ("data center",      ["NVDA","AMD","AVGO","DLR","EQIX"], +1),
    ("cloud computing",  ["MSFT","GOOGL","AMZN","CRM","NOW","SNOW"], +1),

    # Trade / tariff
    ("china tariff",     ["CAT","F","GM","DE","BA","AAPL"], -1),
    ("tariff",           ["CAT","F","GM","DE","BA"], -1),
    ("trade war",        ["CAT","BA","AAPL","XLI"], -1),
    ("china",            ["TSM","AAPL","TSLA","BABA"], 0),

    # Crypto
    ("bitcoin",          ["COIN","MARA","RIOT","MSTR"], +1),
    ("ethereum",         ["COIN","MARA","RIOT"], +1),
    ("crypto",           ["COIN","MARA","RIOT","MSTR"], +1),
    ("sec crypto",       ["COIN","MARA","RIOT"], -1),

    # EV / autos
    ("electric vehicle", ["TSLA","RIVN","LCID","F","GM"], +1),
    ("ev sales",         ["TSLA","RIVN","LCID","F","GM"], +1),
    ("ev demand",        ["TSLA","RIVN","LCID"], +1),

    # Other macro
    ("housing market",   ["XHB","HD","LOW","XLRE"], +1),
    ("retail sales",     ["XLY","WMT","TGT","COST","AMZN"], 0),
    ("consumer spending",["XLY","WMT","TGT","COST"], 0),
    ("layoffs",          ["XLY","XLC"], -1),

    # Earnings / company-specific patterns (no fixed ticker — follow explicit mention)
]


def extract_sector_impacts(text: str) -> list[tuple[str, int]]:
    """Returns list of (ticker, modifier) for any keyword matches.
    Modifier 0 means topic is relevant but direction is ambiguous — skipped."""
    low = text.lower()
    out: list[tuple[str, int]] = []
    for kw, tickers, mod in SECTOR_RULES:
        if mod == 0:
            continue
        if kw in low:
            for t in tickers:
                if t in _get_universe():
                    out.append((t, mod))
    return out


# ---- Per-article scoring ----

def score_article(article: dict) -> dict:
    """Score one article. Returns {ticker: {direction, confidence, reasons}}.

    direction: signed score (negative = sell-leaning, positive = buy-leaning)
    confidence: 0..1 magnitude
    reasons: list of why this ticker was tagged
    """
    title = (article.get("title") or "").strip()
    summary = (article.get("summary") or "").strip()
    text = (title + ". " + summary).strip()
    if not text:
        return {}

    s = sent_mod.score_text(text)
    compound = s["compound"]
    base_dir = 1 if compound > 0.1 else -1 if compound < -0.1 else 0
    magnitude = abs(compound)

    impacts: dict[str, dict] = defaultdict(lambda: {"direction": 0.0, "confidence": 0.0, "reasons": []})

    # Explicit mentions count fully
    for t in extract_explicit_tickers(text):
        impacts[t]["direction"] += base_dir * magnitude
        impacts[t]["confidence"] = max(impacts[t]["confidence"], magnitude)
        impacts[t]["reasons"].append(f"explicit mention ({s['label']}, {compound:+.2f})")

    # Sector matches weighted half
    for ticker, mod in extract_sector_impacts(text):
        impacts[ticker]["direction"] += base_dir * mod * magnitude * 0.5
        impacts[ticker]["confidence"] = max(impacts[ticker]["confidence"], magnitude * 0.5)
        if mod < 0:
            impacts[ticker]["reasons"].append(f"sector keyword (sentiment flipped, {compound:+.2f})")
        else:
            impacts[ticker]["reasons"].append(f"sector keyword ({s['label']}, {compound:+.2f})")

    return {t: dict(v) for t, v in impacts.items()}


# ---- Aggregate across many articles ----

def build_brief(articles: list[dict], min_conf: float = 0.2, min_articles: int = 1) -> dict:
    """Aggregate impacts across many articles → per-ticker action recommendation."""
    per_ticker: dict[str, list[dict]] = defaultdict(list)
    article_count = 0
    scored = []

    for a in articles:
        impacts = score_article(a)
        if not impacts:
            continue
        article_count += 1
        scored.append({"title": a.get("title"), "publisher": a.get("publisher"),
                       "url": a.get("url"), "published": a.get("published"), "impacts": impacts})
        for t, info in impacts.items():
            per_ticker[t].append({
                "title": a.get("title"),
                "publisher": a.get("publisher"),
                "url": a.get("url"),
                "direction": info["direction"],
                "confidence": info["confidence"],
                "reasons": info["reasons"],
            })

    rows = []
    for t, items in per_ticker.items():
        if len(items) < min_articles:
            continue
        net_direction = sum(i["direction"] for i in items)
        avg_conf = sum(i["confidence"] for i in items) / len(items)
        if avg_conf < min_conf:
            action = "WATCH"
        elif net_direction > 0.3:
            action = "BUY"
        elif net_direction < -0.3:
            action = "SELL"
        else:
            action = "WATCH"
        rows.append({
            "ticker": t,
            "action": action,
            "net_direction": round(net_direction, 3),
            "avg_confidence": round(avg_conf, 3),
            "article_count": len(items),
            "top_headlines": [i["title"] for i in items[:3] if i.get("title")],
            "top_reasons": list({r for i in items for r in i.get("reasons", [])})[:3],
        })

    rows.sort(key=lambda r: -abs(r["net_direction"]) * r["avg_confidence"])
    return {
        "article_count": article_count,
        "tickers_impacted": len(rows),
        "brief": rows,
        "scored_sample": scored[:5],
    }
