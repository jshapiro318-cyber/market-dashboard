"""News fetcher — Alpaca News API.

Replaces the prior yfinance implementation. Same return shape:
list[{title, summary, url, publisher, published (unix ts)}].
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from ..config import ALPACA_KEY, ALPACA_SECRET, NEWS_API_KEY, assert_configured

_client: NewsClient | None = None


def _get_client() -> NewsClient:
    global _client
    if _client is None:
        assert_configured()
        _client = NewsClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET)
    return _client


_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 300  # 5 min — news doesn't move that fast


def _fetch_alpaca(key: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        client = _get_client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=14)
        req = NewsRequest(
            symbols=key, start=start, end=end,
            limit=min(limit, 50), include_content=False, sort="desc",
        )
        resp = client.get_news(req)
        articles = getattr(resp, "news", None) or getattr(resp, "data", []) or []
        if isinstance(articles, dict):
            articles = articles.get(key, [])
        for a in articles[:limit]:
            published = getattr(a, "created_at", None)
            pub_ts = None
            if published:
                try:
                    if hasattr(published, "timestamp"):
                        pub_ts = published.timestamp()
                    else:
                        pub_ts = datetime.fromisoformat(str(published).replace("Z", "+00:00")).timestamp()
                except Exception:
                    pub_ts = None
            out.append({
                "title": getattr(a, "headline", "") or "",
                "summary": getattr(a, "summary", "") or "",
                "url": getattr(a, "url", "") or "",
                "publisher": getattr(a, "source", "") or getattr(a, "author", "") or "Alpaca",
                "published": pub_ts,
            })
    except Exception:
        pass
    return out


def _fetch_newsapi(key: str, limit: int) -> list[dict]:
    out: list[dict] = []
    if not NEWS_API_KEY:
        return out
    try:
        import requests
        url = f"https://newsapi.org/v2/everything?q={key}&apiKey={NEWS_API_KEY}&pageSize={min(limit, 50)}&sortBy=publishedAt"
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            articles = data.get("articles", [])
            for a in articles:
                pub_ts = None
                published = a.get("publishedAt")
                if published:
                    try:
                        pub_ts = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
                out.append({
                    "title": a.get("title") or "",
                    "summary": a.get("description") or "",
                    "url": a.get("url") or "",
                    "publisher": a.get("source", {}).get("name") or "NewsAPI",
                    "published": pub_ts,
                })
    except Exception:
        pass
    return out



def fetch_news(ticker: str, limit: int = 15, fast_mode: bool = False) -> list[dict]:
    """Aggregated per-ticker news across Alpaca + Yahoo Finance + Seeking Alpha
    + MarketWatch. De-duped by title prefix, sorted newest-first."""
    key = ticker.upper()
    now = time.time()
    cache_key = f"{key}:fast" if fast_mode else key
    if cache_key in _cache and now - _cache[cache_key][0] < _TTL:
        return _cache[cache_key][1][:limit]

    # Pull from all sources in parallel-ish (sequential since each is fast/cached)
    from . import news_sources as sources

    pool: list[dict] = []
    pool += _fetch_alpaca(key, 15)
    if NEWS_API_KEY:
        try:
            pool += _fetch_newsapi(key, 15)
        except Exception:
            pass
            
    if not fast_mode:
        try:
            pool += sources.yahoo_for_ticker(key, 10)
        except Exception:
            pass
        try:
            pool += sources.seekingalpha_for_ticker(key, 10)
        except Exception:
            pass
        try:
            pool += sources.marketwatch_for_ticker(key, 5)
        except Exception:
            pass

    # Dedupe by title prefix
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in pool:
        k = (item.get("title") or "")[:80].lower()
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(item)
    # Newest first; missing timestamps go last
    deduped.sort(key=lambda x: x.get("published") or 0.0, reverse=True)
    _cache[cache_key] = (now, deduped)
    return deduped[:limit]
