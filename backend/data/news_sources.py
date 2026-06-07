"""Additional news sources beyond Alpaca's feed: MarketWatch RSS, Yahoo
Finance RSS, and Seeking Alpha public per-ticker RSS.

All free, no auth required. Each source returns the same normalized shape:
    {title, summary, url, publisher, published (unix ts)}

Authenticated/paid endpoints (e.g. Seeking Alpha portfolio summary) are
NOT scraped — those require a user session and often violate ToS.
"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("news_sources")

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) MarketIntelDashboard/0.3",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 300  # 5 min


def _parse_pub(text: str | None) -> float | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None


def _fetch_rss(url: str, publisher: str, limit: int) -> list[dict]:
    """Generic RSS 2.0 fetcher. Returns normalised items."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log.debug("RSS fetch failed %s: %s", url, e)
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []

    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
        if not title:
            continue
        items.append({
            "title": title,
            "summary": _strip_html(desc),
            "url": link,
            "publisher": publisher,
            "published": _parse_pub(pub),
        })
        if len(items) >= limit:
            break
    return items


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _cached(key: str, fn) -> list[dict]:
    now = time.time()
    if key in _cache and now - _cache[key][0] < _TTL:
        return _cache[key][1]
    items = fn()
    _cache[key] = (now, items)
    return items


# ===================================================================
#  MarketWatch
# ===================================================================
# MW exposes topic feeds via Dow Jones' content gateway. Top stories
# and markets/economy work without auth.

MW_FEEDS = {
    "top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "markets": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "realtime": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
}


def marketwatch_headlines(category: str = "top", limit: int = 15) -> list[dict]:
    url = MW_FEEDS.get(category, MW_FEEDS["top"])
    return _cached(f"mw:{category}:{limit}", lambda: _fetch_rss(url, "MarketWatch", limit))


def marketwatch_for_ticker(ticker: str, limit: int = 10) -> list[dict]:
    """MarketWatch doesn't expose a per-ticker RSS — filter top/markets by
    ticker mention in title or summary. Crude but free and ToS-safe."""
    sym = ticker.upper()
    pool = marketwatch_headlines("top", 50) + marketwatch_headlines("markets", 50)
    matches = []
    for item in pool:
        text = (item["title"] + " " + (item.get("summary") or "")).upper()
        if f" {sym} " in f" {text} " or f"({sym})" in text or f"${sym}" in text:
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches


# ===================================================================
#  Yahoo Finance
# ===================================================================

def yahoo_for_ticker(ticker: str, limit: int = 10) -> list[dict]:
    sym = ticker.upper()
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
    return _cached(f"yh:{sym}:{limit}", lambda: _fetch_rss(url, "Yahoo Finance", limit))


# ===================================================================
#  Seeking Alpha (public per-ticker — no auth)
# ===================================================================
# SA has a few public RSS endpoints per ticker. We try the modern symbol
# feed and fall back to the older combined feed.

def seekingalpha_for_ticker(ticker: str, limit: int = 10) -> list[dict]:
    sym = ticker.upper()
    candidates = [
        f"https://seekingalpha.com/api/sa/combined/{sym}.xml",
        f"https://seekingalpha.com/symbol/{sym}/feed",
    ]
    for url in candidates:
        items = _fetch_rss(url, "Seeking Alpha", limit)
        if items:
            _cache[f"sa:{sym}:{limit}"] = (time.time(), items)
            return items
    return []


# ===================================================================
#  Combined per-ticker
# ===================================================================

def combined_for_ticker(ticker: str, limit: int = 20) -> list[dict]:
    """Pull from all free sources, de-dupe by title prefix, sort by recency."""
    pool = (
        yahoo_for_ticker(ticker, 10)
        + seekingalpha_for_ticker(ticker, 10)
        + marketwatch_for_ticker(ticker, 5)
    )
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in pool:
        key = (item["title"] or "")[:80].lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    # Sort newest first; items without a timestamp go last
    deduped.sort(key=lambda x: -(x.get("published") or 0))
    return deduped[:limit]
