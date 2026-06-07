"""Top Movers scan — intraday hunt for biggest % gainers across the universe.

Built after observing that 14 of 16 of June 1's top movers weren't in the
mega-cap universe at all, and the 2 that were (MDB, ARM) spiked after the
9:45 research window so the daily strategy never saw them.

This module:
  1. Quickly scores every universe ticker's % change today
  2. Surfaces the top N gainers (and losers)
  3. For the top gainers, runs full analysis and identifies BUY candidates
     meeting strict intraday criteria (heavy volume + bullish setup)

Designed to be called every hour during market hours alongside the existing
intraday windows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import portfolio, scanner
from ..analysis import indicators as ind_mod
from ..analysis import patterns as pat_mod
from ..analysis import signals as sig_mod
from ..data import prices as price_mod
from ..data.universe import universe as _universe

log = logging.getLogger("movers")

# Criteria for "qualifies as a real mover to consider"
MIN_PCT_MOVE = 0.5          # captures quiet consolidations and non-movers
MIN_REL_VOLUME = 2.0        # 2x avg volume
MIN_SCORE = 3.0             # strict momentum/technical signal bar
MIN_CONFIDENCE = 0.40
MAX_BUYS_PER_SCAN = 5
MIN_PRICE_LIMIT = 5.00      # skip stocks priced under $5.00 (penny stocks)
MEME_BLACKLIST = {"GME", "AMC", "BBBY", "RIOT", "MARA", "PLUG"}

# Pure momentum override: ignore signal score if the price action is strong enough.
# But "smart" — skip if exhaustion signals say the stock is at the top.
MOMENTUM_OVERRIDE_PCT = 15.0       # +15% intraday
MOMENTUM_OVERRIDE_VOL = 3.0        # 3x avg volume
EXHAUSTION_RSI_MAX = 80            # don't chase if RSI already >80 (overbought)
EXHAUSTION_SMA50_MULT = 1.30       # don't chase if price >30% above 50-day avg
def get_min_rel_volume() -> float:
    """Return the relative volume threshold scaled by the time of day (ET)."""
    import pytz
    from datetime import datetime
    try:
        et = datetime.now(pytz.timezone("America/New_York"))
        market_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
        if et < market_open:
            return 0.5
        
        elapsed_minutes = (et - market_open).total_seconds() / 60.0
        if elapsed_minutes <= 30:
            return 0.5
        elif elapsed_minutes <= 90:  # Up to 11:00 AM
            return 1.0
        elif elapsed_minutes <= 150: # Up to 12:00 PM
            return 1.5
        else:
            return 2.0
    except Exception:
        return 2.0


def get_momentum_override_vol() -> float:
    """Return the momentum override volume threshold scaled by the time of day (ET)."""
    import pytz
    from datetime import datetime
    try:
        et = datetime.now(pytz.timezone("America/New_York"))
        market_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
        if et < market_open:
            return 1.0
        
        elapsed_minutes = (et - market_open).total_seconds() / 60.0
        if elapsed_minutes <= 30:
            return 1.0
        elif elapsed_minutes <= 90:
            return 1.5
        elif elapsed_minutes <= 150:
            return 2.0
        else:
            return 3.0
    except Exception:
        return 3.0


def _scrape_yahoo_gainers() -> list[str]:
    """Scrape Yahoo Finance for market-wide top gainers since our static universe misses them."""
    try:
        import requests
        from bs4 import BeautifulSoup
        url = "https://finance.yahoo.com/markets/stocks/gainers/"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        tickers = []
        for a in soup.find_all('a', href=True):
            if '/quote/' in a['href'] and '=' not in a['href']:
                ticker = a['href'].split('/quote/')[1].split('/')[0]
                if ticker and ticker.isalpha() and ticker.isupper() and ticker not in tickers:
                    tickers.append(ticker)
        return tickers[:50]
    except Exception as e:
        log.warning(f"Failed to scrape Yahoo gainers: {e}")
        return []


def get_today_movers(top_n: int = 25) -> dict:
    """Compute % change for every ticker in universe, rank.

    Uses the scanner's batch download (already chunked + resilient).
    Now augmented with dynamic scraping of market-wide top gainers.
    """
    syms = _universe()
    
    # Inject dynamic market-wide gainers so we don't miss massive runners
    market_gainers = _scrape_yahoo_gainers()
    for t in market_gainers:
        if t not in syms:
            syms.append(t)

    df_all = scanner._batch_download(syms)
    if df_all is None or df_all.empty:
        return {"gainers": [], "losers": [], "scanned": 0}

    rows = []
    for sym in syms:
        try:
            sub = scanner._slice_single(df_all, sym)
            if sub is None or len(sub) < 2:
                continue
            last = float(sub["Close"].iloc[-1])
            prev = float(sub["Close"].iloc[-2])
            vol = float(sub["Volume"].iloc[-1])
            avg_vol = float(sub["Volume"].tail(20).mean()) if len(sub) >= 20 else float(sub["Volume"].mean())
            pct = (last - prev) / prev * 100 if prev else 0
            rel_vol = vol / avg_vol if avg_vol else 1
            rows.append({
                "ticker": sym,
                "last": round(last, 2),
                "prev_close": round(prev, 2),
                "change_pct": round(pct, 2),
                "volume": int(vol),
                "rel_volume": round(rel_vol, 2),
            })
        except Exception:
            continue

    rows.sort(key=lambda r: -r["change_pct"])
    return {
        "scanned": len(rows),
        "gainers": rows[:top_n],
        "losers": list(reversed(rows[-top_n:])),
    }


def run_movers_scan(dry_run: bool = False, force_market_check: bool = True) -> dict:
    """Intraday movers scan: find biggest %-gainers, run signal on top picks,
    auto-buy those meeting strict criteria. Designed to catch what the daily
    strategy misses because of timing or research bias."""
    from . import strategy
    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "movers_scan", "status": "ok",
               "buys": [], "considered": [], "skipped": [], "errors": []}

    if force_market_check and not strategy.market_is_open():
        summary["status"] = "market closed"
        return strategy._save_run(summary)

    try:
        movers = get_today_movers(top_n=30)
    except Exception as e:
        summary["status"] = f"scan failed: {e}"
        return strategy._save_run(summary)

    summary["universe_scanned"] = movers["scanned"]
    summary["top_5_gainers"] = movers["gainers"][:5]

    today_buys = strategy._todays_auto_buy_count() if hasattr(strategy, "_todays_auto_buy_count") else 0
    if today_buys >= strategy.INTRADAY_MAX_BUYS_PER_DAY:
        summary["status"] = f"daily cap reached ({today_buys}/{strategy.INTRADAY_MAX_BUYS_PER_DAY})"
        return strategy._save_run(summary)

    # Determine dynamic relative volume and momentum override thresholds based on time of day
    min_rel_vol = get_min_rel_volume()
    mom_over_vol = get_momentum_override_vol()
    summary["min_relative_volume_threshold"] = min_rel_vol
    summary["momentum_override_vol_threshold"] = mom_over_vol

    # Consider top gainers that meet basic criteria
    # Consider top gainers that meet basic criteria
    candidates = [m for m in movers["gainers"]
                  if m["change_pct"] >= MIN_PCT_MOVE and m["rel_volume"] >= min_rel_vol]
    
    # Query all open buy orders once to check for duplicates and committed cash
    open_buys = portfolio.get_open_buy_orders()
    open_buy_tickers = {o.symbol.upper() for o in open_buys}
    
    # Calculate committed cash in open buys to deduct from available cash
    committed_open_buys = 0.0
    for o in open_buys:
        qty = float(o.qty or 0)
        filled = float(o.filled_qty or 0)
        price = float(o.limit_price or o.filled_avg_price or 0)
        committed_open_buys += (qty - filled) * price

    # Get current portfolio state once at the start of the loop
    state = portfolio.get_state()
    # Restrict buying to cash balance (de-leverages and disables stock margin borrowing)
    available_cash = state["cash"] - committed_open_buys
    equity = state["equity"]

    bought = 0
    for cand in candidates[:10]:  # full-analyze top 10 candidates
        if bought >= MAX_BUYS_PER_SCAN:
            break
        ticker = cand["ticker"]
        try:
            if portfolio.has_position(ticker) or ticker in open_buy_tickers:
                summary["skipped"].append({"ticker": ticker, "reason": "already held or pending buy"})
                continue
            if cand["last"] < MIN_PRICE_LIMIT:
                # Check for news support exception: at least 5 articles with positive sentiment
                has_news_exception = False
                news_count = 0
                news_sentiment = 0.0
                try:
                    raw_news = strategy.news_mod.fetch_news(ticker, limit=10)
                    news_agg = strategy.sent_mod.aggregate_news_sentiment(raw_news)
                    news_count = news_agg.get("count", 0)
                    news_sentiment = news_agg.get("avg_compound", 0.0)
                    if news_count >= 5 and news_sentiment > 0.15:
                        has_news_exception = True
                except Exception:
                    pass
                
                if not has_news_exception:
                    summary["skipped"].append({
                        "ticker": ticker,
                        "reason": f"price ${cand['last']:.2f} below minimum limit ${MIN_PRICE_LIMIT:.2f} (and no news support: count {news_count}, sentiment {news_sentiment:+.2f})"
                    })
                    continue
            if ticker in MEME_BLACKLIST:
                summary["skipped"].append({"ticker": ticker, "reason": "blacklisted speculative meme stock"})
                continue
            a = strategy.analyze_one(ticker)
            sig = a["signal"]
            considered = {
                "ticker": ticker,
                "change_pct": cand["change_pct"],
                "rel_volume": cand["rel_volume"],
                "score": sig["score"],
                "confidence": sig["confidence"],
                "bias": sig["bias"],
            }
            summary["considered"].append(considered)

            # SMART filters — skip exhaustion regardless of momentum
            ind = a.get("indicators", {})
            rsi = ind.get("rsi_14")
            sma50 = ind.get("sma_50")
            last = cand["last"]
            extended_pct = (last / sma50) if sma50 else 1.0
            if rsi is not None and rsi > EXHAUSTION_RSI_MAX:
                summary["skipped"].append({"ticker": ticker, "reason": f"RSI {rsi:.0f} > {EXHAUSTION_RSI_MAX} (exhaustion — likely top)"})
                continue
            if extended_pct > EXHAUSTION_SMA50_MULT:
                summary["skipped"].append({"ticker": ticker, "reason": f"price {(extended_pct-1)*100:.0f}% above 50-day MA (overextended — likely to fade)"})
                continue

            # Pure-momentum override: if price action is dominant, ignore signal score
            is_pure_momentum = (
                cand["change_pct"] >= MOMENTUM_OVERRIDE_PCT
                and cand["rel_volume"] >= mom_over_vol
                and sig["bias"] != "bearish"  # only block on fully bearish
                and sig["score"] >= 0.0       # ensure trend/momentum isn't structurally negative
            )
            if not is_pure_momentum:
                # Normal bar
                if sig["score"] < MIN_SCORE:
                    summary["skipped"].append({"ticker": ticker, "reason": f"score {sig['score']} below momentum bar {MIN_SCORE}"})
                    continue
                if sig["confidence"] < MIN_CONFIDENCE:
                    summary["skipped"].append({"ticker": ticker, "reason": f"conf {sig['confidence']:.2f} below {MIN_CONFIDENCE}"})
                    continue
                if sig["bias"] not in ("bullish", "lean bullish"):
                    summary["skipped"].append({"ticker": ticker, "reason": f"bias {sig['bias']}"})
                    continue

            # Place a buy at 4.9% of equity
            position_dollars = equity * strategy.POSITION_PCT * 0.98
            position_dollars = min(position_dollars, available_cash)
            shares = position_dollars / cand["last"]
            if shares < 0.0001 or available_cash < shares * cand["last"]:
                summary["skipped"].append({"ticker": ticker, "reason": f"insufficient cash (${available_cash:,.2f}) or size"})
                continue

            path = "MOMENTUM-OVERRIDE" if is_pure_momentum else "MOVERS"
            reason = (f"{path}: +{cand['change_pct']:.1f}% today @ {cand['rel_volume']:.1f}x vol | "
                      f"score {sig['score']}, {sig['bias']}, conf {int(sig['confidence']*100)}%")
            if dry_run:
                summary["buys"].append({"ticker": ticker, "shares": round(shares, 4), "price": cand["last"], "reason": reason, "dry": True})
            else:
                trade = portfolio.buy(ticker, shares=shares, reason=reason,
                                      signal={"bias": sig["bias"], "score": sig["score"], "confidence": sig["confidence"]},
                                      auto=True)
                summary["buys"].append({**trade})
                # Subtract estimated cost from available_cash for subsequent loop iterations
                available_cash -= shares * cand["last"]
            bought += 1
        except ValueError as ve:
            summary["skipped"].append({"ticker": ticker, "reason": str(ve)})
        except Exception as e:
            summary["errors"].append({"ticker": ticker, "phase": "movers_buy", "error": str(e)})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return strategy._save_run(summary)
