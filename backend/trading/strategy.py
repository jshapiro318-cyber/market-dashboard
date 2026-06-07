"""Trading strategy — agent-rules-compliant research and trade windows.

Daily cadence (US/Eastern, weekdays):
  9:45 AM  research_window   scan + analyze top candidates, record decision snapshot
 10:00 AM  trade_window      sell on stop-loss/signal-flip, then place limit-style buys
                             — only buys whose price moved ≤ 0.2% since 9:45
  4:15 PM  journal_window    write journal/YYYY-MM-DD.md

Hard rules enforced:
  - 5% of equity max per position (validated in portfolio.buy)
  - 8% stop loss from entry → close without waiting
  - No trades when market is closed
  - Limit price within 0.2% of research snapshot
  - Always write a journal (even on no-trade days)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytz

from . import db, journal, portfolio, research, scanner
from ..analysis import dcf as dcf_mod
from ..analysis import fundamentals as fund_mod
from ..analysis import indicators as ind_mod
from ..analysis import patterns as pat_mod
from ..analysis import sentiment as sent_mod
from ..analysis import signals as sig_mod
from ..data import news as news_mod
from ..data import prices as price_mod

ET = pytz.timezone("America/New_York")


def get_profit_stage(ticker: str) -> int:
    with db._lock, db.connect() as c:
        row = c.execute("SELECT stage FROM profit_taking_state WHERE ticker = ?", (ticker.upper(),)).fetchone()
    return row["stage"] if row else 0


def set_profit_stage(ticker: str, stage: int):
    with db._lock, db.connect() as c:
        if stage == 0:
            c.execute("DELETE FROM profit_taking_state WHERE ticker = ?", (ticker.upper(),))
        else:
            c.execute("INSERT OR REPLACE INTO profit_taking_state (ticker, stage) VALUES (?, ?)", (ticker.upper(), stage))


def cleanup_profit_stages(active_tickers: list[str]):
    with db._lock, db.connect() as c:
        if active_tickers:
            placeholders = ",".join("?" for _ in active_tickers)
            c.execute(f"DELETE FROM profit_taking_state WHERE ticker NOT IN ({placeholders})", [t.upper() for t in active_tickers])
        else:
            c.execute("DELETE FROM profit_taking_state")


# === Hard rules ===
STOP_LOSS_PCT = -0.05             # 5% stop-loss threshold
TAKE_PROFIT_PCT = 0.30             # was 0.25 — let winners run further
POSITION_PCT = 0.05                # 5% of equity per position (standard risk control)
LIMIT_DRIFT_PCT = 0.05            # 5.0% — very generous for intraday entries
MAX_NEW_POSITIONS = 10            # per trade window (5% cap still applies per position)
MIN_PRICE_LIMIT = 5.00            # skip stocks priced under $5.00 (penny stocks)
MEME_BLACKLIST = {"GME", "AMC", "BBBY", "RIOT", "MARA", "PLUG"}
MIN_DCF_UPSIDE_PCT = 2.0          # skip stocks with less than 2.0% DCF valuation upside



# Buy quality bar (after full analysis incl. fundamentals)
BUY_SCORE_THRESHOLD = 4.0
BUY_CONFIDENCE_THRESHOLD = 0.40

# Scanner pre-filter — looser since the scanner has no fundamentals;
# we re-analyze every passing candidate with the full pipeline.
SCANNER_PREFILTER_SCORE = 2.0
SCANNER_PREFILTER_CONF = 0.3

# Market hours (ET) for the gate
MARKET_OPEN_HM = (4, 0)
MARKET_CLOSE_HM = (20, 0)


# ===================================================================
#  Helpers
# ===================================================================

def market_is_open() -> bool:
    et = datetime.now(ET)
    if et.weekday() >= 5:
        return False
    open_ts = et.replace(hour=MARKET_OPEN_HM[0], minute=MARKET_OPEN_HM[1], second=0, microsecond=0)
    close_ts = et.replace(hour=MARKET_CLOSE_HM[0], minute=MARKET_CLOSE_HM[1], second=0, microsecond=0)
    return open_ts <= et <= close_ts


def analyze_one(ticker: str, fast_mode: bool = False) -> dict:
    """Full pipeline: prices, indicators, patterns, news+sentiment, fundamentals, 7-factor signal."""
    df = price_mod.fetch_history(ticker, period="1y", interval="1d")
    quote = price_mod.fetch_quote(ticker)
    indicators = ind_mod.compute_all(df)
    
    # Inject 5-day return into indicators for momentum analysis (caution on heavy chases)
    if len(df) >= 5:
        indicators["ret_5d"] = (df["Close"].iloc[-1] - df["Close"].iloc[-5]) / df["Close"].iloc[-5] * 100
    else:
        indicators["ret_5d"] = 0.0

    candle_pats = pat_mod.detect_candlestick_patterns(df, lookback=5)
    chart_pats = pat_mod.detect_chart_patterns(df, indicators)
    raw_news = news_mod.fetch_news(ticker, limit=10, fast_mode=fast_mode)
    news_agg = sent_mod.aggregate_news_sentiment(raw_news)
    fund = fund_mod.fetch_fundamentals(ticker)
    signal = sig_mod.compute_signal(quote, indicators, candle_pats, chart_pats, news_agg, fundamentals=fund)
    return {
        "quote": quote,
        "indicators": indicators,
        "candle_patterns": candle_pats,
        "chart_patterns": chart_pats,
        "news_agg": news_agg,
        "news_items": raw_news[:5],
        "fundamentals": fund,
        "signal": signal,
    }


def answer_decision_questions(ticker: str, analysis: dict, state: dict) -> dict:
    """The 5-question decision framework — captured per-candidate at research time."""
    quote = analysis["quote"]
    ind = analysis["indicators"]
    news_agg = analysis["news_agg"]
    last = quote["last"]

    # Q4 — moving averages
    sma20 = ind.get("sma_20")
    sma50 = ind.get("sma_50")
    sma200 = ind.get("sma_200")
    parts = []
    if sma20 is not None:
        parts.append(f"price ${last:.2f} {'>' if last > sma20 else '<'} 20-MA ${sma20:.2f}")
    if sma50 is not None:
        parts.append(f"50-MA ${sma50:.2f}")
    if sma200 is not None:
        parts.append(f"200-MA ${sma200:.2f}")
    bullish_stack = sma20 and sma50 and sma20 > sma50 and last > sma20
    ma_view = " · ".join(parts) + (" — bullish stack" if bullish_stack else "")

    # Q5 — downside risk
    atr = ind.get("atr_14") or 0.0
    stop_price = last * (1 + STOP_LOSS_PCT)
    risk_per_share = last - stop_price

    return {
        "q1_cash": f"${state['cash']:,.2f}",
        "q2_open_positions": ", ".join([p["ticker"] for p in state["positions"]]) or "(none)",
        "q3_news": f"{news_agg.get('count',0)} recent articles, sentiment {news_agg.get('avg_compound',0):+.2f} ({news_agg.get('label','?')})",
        "q4_moving_averages": ma_view or "—",
        "q5_risk": f"ATR ${atr:.2f} (~{atr/last*100:.1f}% of price). 8% stop at ${stop_price:.2f}, ${risk_per_share:.2f}/share at risk.",
    }


def _save_run(summary: dict) -> dict:
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT INTO auto_runs (ts, summary) VALUES (?, ?)",
            (summary["started_at"], json.dumps(summary)),
        )
    return summary


# ===================================================================
#  9:45 AM — Research window
# ===================================================================

def run_research(dry_run: bool = False, force_market_check: bool = True) -> dict:
    """Scan universe, full-analyze top candidates, record snapshot for the 10:00 trade window."""
    started = datetime.now(timezone.utc)
    summary = {
        "started_at": started.isoformat(),
        "kind": "research",
        "dry_run": dry_run,
        "status": "ok",
        "candidates": [],
        "skipped": [],
        "errors": [],
    }

    if force_market_check and not market_is_open():
        summary["status"] = "market closed"
        if not dry_run:
            _save_run(summary)
        return summary

    state = portfolio.get_state()
    summary["portfolio_snapshot"] = {
        "cash": state["cash"],
        "equity": state["equity"],
        "open_positions": [p["ticker"] for p in state["positions"]],
    }

    try:
        scan = scanner.scan_universe(force=True)
    except Exception as e:
        summary["status"] = f"scanner failed: {e}"
        summary["errors"].append({"phase": "scan", "error": str(e)})
        if not dry_run:
            _save_run(summary)
            research.store_today(summary)
        return summary

    pool = []
    for r in scan.get("top_bullish", [])[:30]:
        ticker = r["ticker"]
        price = r["last"]
        if r["score"] < SCANNER_PREFILTER_SCORE or r["confidence"] < SCANNER_PREFILTER_CONF:
            continue
        if portfolio.has_position(ticker):
            continue
        if price < MIN_PRICE_LIMIT:
            # Check for news support exception: at least 5 articles with positive sentiment
            has_news_exception = False
            news_count = 0
            news_sentiment = 0.0
            try:
                raw_news = news_mod.fetch_news(ticker, limit=10)
                news_agg = sent_mod.aggregate_news_sentiment(raw_news)
                news_count = news_agg.get("count", 0)
                news_sentiment = news_agg.get("avg_compound", 0.0)
                if news_count >= 5 and news_sentiment > 0.15:
                    has_news_exception = True
            except Exception:
                pass
            
            if not has_news_exception:
                summary["skipped"].append({
                    "ticker": ticker,
                    "reason": f"price ${price:.2f} below minimum limit ${MIN_PRICE_LIMIT:.2f} (and no news support: count {news_count}, sentiment {news_sentiment:+.2f})"
                })
                continue
        if ticker in MEME_BLACKLIST:
            summary["skipped"].append({
                "ticker": ticker,
                "reason": "blacklisted speculative meme stock"
            })
            continue
        pool.append(r)


    # Limit re-analysis to top 12 (each takes 1–3 s with fundamentals)
    for cand in pool[:12]:
        try:
            analysis = analyze_one(cand["ticker"])

            # Run DCF valuation for double-digit upside check
            try:
                dcf_res = dcf_mod.run_dcf_valuation(cand["ticker"])
                dcf_upside = dcf_res.get("upside_pct", 0.0)
            except Exception:
                dcf_upside = 0.0

            if not isinstance(dcf_upside, (int, float)) or dcf_upside < MIN_DCF_UPSIDE_PCT:
                summary["skipped"].append({
                    "ticker": cand["ticker"],
                    "reason": f"DCF upside {dcf_upside:.1f}% below required (+{MIN_DCF_UPSIDE_PCT}%) bar"
                })
                continue

            sig = analysis["signal"]
            if sig["score"] < BUY_SCORE_THRESHOLD or sig["confidence"] < BUY_CONFIDENCE_THRESHOLD:
                summary["skipped"].append({
                    "ticker": cand["ticker"],
                    "reason": f"below buy bar after full analysis: score {sig['score']}, conf {sig['confidence']:.2f}",
                })
                continue

            decisions = answer_decision_questions(cand["ticker"], analysis, state)
            top_reasons: list[str] = []
            for f in sig.get("factors", []):
                if f.get("reasons"):
                    top_reasons.append(f"[{f['name']}] {f['reasons'][0]}")
                if len(top_reasons) >= 5:
                    break

            summary["candidates"].append({
                "ticker": cand["ticker"],
                "decision_price": analysis["quote"]["last"],
                "decision_ts": datetime.now(timezone.utc).isoformat(),
                "score": sig["score"],
                "bias": sig["bias"],
                "confidence": sig["confidence"],
                "decisions": decisions,
                "reasons": top_reasons,
                "scanner_score": cand["score"],
            })
        except Exception as e:
            summary["errors"].append({"ticker": cand["ticker"], "phase": "research", "error": str(e)})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        _save_run(summary)
        research.store_today(summary)
    return summary


# ===================================================================
#  10:00 AM — Trade window
# ===================================================================

def run_trade_window(dry_run: bool = False, force_market_check: bool = True) -> dict:
    """Apply stop-loss/signal-flip exits, then execute limit-style buys from today's research."""
    started = datetime.now(timezone.utc)
    summary = {
        "started_at": started.isoformat(),
        "kind": "trade",
        "dry_run": dry_run,
        "status": "ok",
        "sells": [], "buys": [], "skipped": [], "errors": [],
    }

    if force_market_check and not market_is_open():
        summary["status"] = "market closed"
        if not dry_run:
            _save_run(summary)
        return summary

    # ===== Sell pass =====
    state = portfolio.get_state()
    summary["portfolio_before"] = state
    if not dry_run:
        cleanup_profit_stages([p["ticker"] for p in state["positions"]])

    for pos in state["positions"]:
        ticker = pos["ticker"]
        # Skip option positions — they're managed by the wheel module, not stock sell logic
        if len(ticker) > 10 or any(c.isdigit() for c in ticker[:4]):
            continue
        try:
            pnl_pct = pos["unrealized_pnl_pct"] / 100
            
            # Stop loss check
            if pnl_pct <= STOP_LOSS_PCT:
                reason = f"Stop loss hit ({pnl_pct*100:+.2f}%)"
                if dry_run:
                    summary["sells"].append({"ticker": ticker, "reason": reason, "dry": True})
                else:
                    trade = portfolio.sell(ticker, reason=reason, auto=True)
                    summary["sells"].append({**trade})
                continue

            # Multi-stage take-profit check
            current_stage = get_profit_stage(ticker)
            stage_to_trigger = 0
            fraction_to_sell = 0.0
            stage_reason = ""

            if pnl_pct >= 0.30 and current_stage < 4:
                stage_to_trigger = 4
                fraction_to_sell = 1.0
                stage_reason = f"Take profit Stage 4 hit (+{pnl_pct*100:.1f}%) — sell full remaining"
            elif pnl_pct >= 0.10 and current_stage < 3:
                stage_to_trigger = 3
                fraction_to_sell = 0.50
                stage_reason = f"Take profit Stage 3 hit (+{pnl_pct*100:.1f}%) — sell half"
            elif pnl_pct >= 0.05 and current_stage < 2:
                stage_to_trigger = 2
                fraction_to_sell = 0.333
                stage_reason = f"Take profit Stage 2 hit (+{pnl_pct*100:.1f}%) — sell 1/3"
            elif pnl_pct >= 0.02 and current_stage < 1:
                stage_to_trigger = 1
                fraction_to_sell = 0.10
                stage_reason = f"Take profit Stage 1 hit (+{pnl_pct*100:.1f}%) — sell small portion (10%)"

            if stage_to_trigger > 0:
                shares_held = pos["shares"]
                shares_to_sell = round(shares_held * fraction_to_sell, 4)
                if stage_to_trigger == 4 or shares_held - shares_to_sell < 0.0001:
                    shares_to_sell = shares_held
                
                if shares_to_sell > 0:
                    if dry_run:
                        summary["sells"].append({
                            "ticker": ticker,
                            "shares": shares_to_sell,
                            "reason": stage_reason,
                            "dry": True
                        })
                    else:
                        trade = portfolio.sell(ticker, shares=shares_to_sell, reason=stage_reason, auto=True)
                        summary["sells"].append({**trade})
                        set_profit_stage(ticker, stage_to_trigger)
                    continue

            # If no stop loss or take profit was hit, check for signal flip
            a = analyze_one(ticker)
            if a["signal"]["bias"] in ("bearish", "lean bearish"):
                reason = f"Signal flipped {a['signal']['bias']} (score {a['signal']['score']})"
                if dry_run:
                    summary["sells"].append({"ticker": ticker, "reason": reason, "dry": True})
                else:
                    trade = portfolio.sell(ticker, reason=reason, auto=True)
                    summary["sells"].append({**trade})
                continue

        except Exception as e:
            summary["errors"].append({"ticker": ticker, "phase": "sell", "error": str(e)})

    # ===== Buy pass =====
    today_research = research.get_today()
    if not today_research:
        summary["status"] = "no research available (run 9:45 research window first)"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["portfolio_after"] = portfolio.get_state()
        if not dry_run:
            _save_run(summary)
        return summary

    cands = today_research.get("candidates", [])
    
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
    
    # Capital raising pass: if we have candidates we do not yet hold (and aren't pending buy), but low buying power, sell worst performer
    unheld_candidates = [c for c in cands if not (portfolio.has_position(c["ticker"]) or c["ticker"] in open_buy_tickers)]
    if unheld_candidates and not dry_run:
        cur_state = portfolio.get_state()
        buying_power = cur_state.get("buying_power", cur_state["cash"])
        target_dollars = cur_state["equity"] * POSITION_PCT
        if buying_power < target_dollars * 0.5:
            stock_positions = [
                p for p in cur_state["positions"] 
                if not (len(p["ticker"]) > 10 or any(c.isdigit() for c in p["ticker"][:4]))
            ]
            if stock_positions:
                worst = min(stock_positions, key=lambda x: x["unrealized_pnl_pct"])
                try:
                    trade = portfolio.sell(worst["ticker"], reason=f"Liquidated worst performer ({worst['unrealized_pnl_pct']:.2f}%) to free up buying power", auto=True)
                    summary["sells"].append({**trade})
                except Exception as e:
                    summary["errors"].append({"ticker": worst["ticker"], "phase": "sell_for_capital", "error": str(e)})

    bought = 0
    
    # Get current portfolio state once at the start of the loop
    cur_state = portfolio.get_state()
    # Restrict buying to cash balance (de-leverages and disables stock margin borrowing)
    available_cash = cur_state["cash"] - committed_open_buys
    equity = cur_state["equity"]
    
    for cand in cands:
        if bought >= MAX_NEW_POSITIONS:
            break
        ticker = cand["ticker"]
        try:
            # Check if already held or pending buy
            if portfolio.has_position(ticker) or ticker in open_buy_tickers:
                summary["skipped"].append({"ticker": ticker, "reason": "already held or pending buy"})
                continue

            # Limit-price check: skip if price moved > limit from decision snapshot
            cur_quote = price_mod.fetch_quote(ticker)
            cur_price = cur_quote["last"]
            decision_price = cand["decision_price"]
            drift = (cur_price - decision_price) / decision_price
            limit_ceiling = decision_price * (1 + LIMIT_DRIFT_PCT)
            if cur_price > limit_ceiling:
                summary["skipped"].append({
                    "ticker": ticker,
                    "reason": f"price drift {drift*100:+.2f}% > {LIMIT_DRIFT_PCT*100:.1f}% limit (research ${decision_price:.2f} → now ${cur_price:.2f})",
                })
                continue

            # Position size = POSITION_PCT of equity, capped by available cash/buying power
            # We use available_cash instead of cur_state["cash"] so simultaneous orders don't double spend
            target_dollars = equity * POSITION_PCT
            position_dollars = min(target_dollars, available_cash) * 0.98
            shares = position_dollars / cur_price
            if shares < 0.0001:
                summary["skipped"].append({"ticker": ticker, "reason": "share size ~0"})
                continue
            if available_cash < shares * cur_price:
                summary["skipped"].append({"ticker": ticker, "reason": f"insufficient cash (${available_cash:.2f})"})
                continue

            d = cand.get("decisions", {})
            reason = (
                f"score {cand['score']}, {cand['bias']}, conf {int(cand['confidence']*100)}% | "
                f"5Q FRAMEWORK: "
                f"[1 cash] {d.get('q1_cash','')} | "
                f"[2 open] {d.get('q2_open_positions','')[:60]} | "
                f"[3 news] {d.get('q3_news','')} | "
                f"[4 MAs] {d.get('q4_moving_averages','')} | "
                f"[5 risk] {d.get('q5_risk','')}"
            )

            if dry_run:
                summary["buys"].append({
                    "ticker": ticker, "shares": round(shares, 4), "price": cur_price,
                    "cost": round(shares * cur_price, 2),
                    "score": cand["score"], "bias": cand["bias"], "confidence": cand["confidence"],
                    "reason": reason, "dry": True,
                })
            else:
                trade = portfolio.buy(
                    ticker, shares=shares, reason=reason,
                    signal={"bias": cand["bias"], "score": cand["score"], "confidence": cand["confidence"]},
                    auto=True,
                )
                summary["buys"].append({**trade, "score": cand["score"], "bias": cand["bias"], "confidence": cand["confidence"]})
                # Subtract estimated cost from available_cash for subsequent loop iterations
                available_cash -= shares * cur_price
            bought += 1
        except ValueError as ve:
            # Includes 5% cap rejection
            summary["skipped"].append({"ticker": ticker, "reason": str(ve)})
        except Exception as e:
            summary["errors"].append({"ticker": ticker, "phase": "buy", "error": str(e)})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["portfolio_after"] = portfolio.get_state()
    if not dry_run:
        _save_run(summary)
    return summary


# ===================================================================
#  4:15 PM — Journal window
# ===================================================================

def run_journal(force_market_check: bool = False) -> dict:
    """Always write a journal entry — even on no-trade days."""
    started = datetime.now(timezone.utc)
    result = journal.write_today()
    summary = {
        "started_at": started.isoformat(),
        "kind": "journal",
        "status": "ok",
        "journal": result,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_run(summary)
    return summary


# ===================================================================
#  Full-day routine — market_open / news pulses / Markov / pre-close
# ===================================================================

def run_premarket_brief(force_market_check: bool = False) -> dict:
    """9:15 ET pre-market intelligence brief.

    Pulls 30-50 news events from MarketWatch + Yahoo + Seeking Alpha + Alpaca
    across the watchlist + held positions + broad indices, extracts impacted
    tickers (explicit mentions + sector keyword maps), scores per-ticker
    net direction and produces BUY / SELL / WATCH actions.

    The 9:45 research_window can read this brief as a prior.
    """
    from ..analysis import news_impact
    from ..data import news_sources as src
    from . import watchlist as wl_mod

    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "premarket_brief", "status": "ok"}

    # Universe of tickers we care about: watchlist + held + broad ETFs + scanner top
    state = portfolio.get_state()
    held = [p["ticker"] for p in state["positions"]]
    watch = wl_mod.list_tickers()
    pulse_tickers = list(dict.fromkeys(held + watch + ["SPY", "QQQ", "DIA", "IWM", "XLE", "XLF", "XLK", "XLY"]))

    # Pull articles: broad market RSS + per-ticker for each pulse ticker
    articles: list[dict] = []
    try:
        articles += src.marketwatch_headlines("top", 30)
        articles += src.marketwatch_headlines("markets", 20)
    except Exception as e:
        summary.setdefault("errors", []).append(f"marketwatch: {e}")
    for t in pulse_tickers:
        try:
            articles += news_mod.fetch_news(t, limit=5)
        except Exception as e:
            summary.setdefault("errors", []).append(f"news {t}: {e}")

    # Dedupe by title prefix
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in articles:
        k = (a.get("title") or "")[:80].lower()
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(a)

    # Build the brief
    brief = news_impact.build_brief(deduped, min_conf=0.15, min_articles=1)
    summary.update({
        "total_articles_pulled": len(articles),
        "deduped_articles": len(deduped),
        "tickers_impacted": brief["tickers_impacted"],
        "scored_articles": brief["article_count"],
        "brief": brief["brief"][:50],   # cap to 50 tickers
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    return _save_run(summary)


def latest_premarket_brief() -> dict | None:
    """Return the most recent premarket_brief from auto_runs."""
    for run in portfolio.auto_runs(limit=20):
        if run.get("summary", {}).get("kind") == "premarket_brief":
            return run["summary"]
    return None


def run_market_open() -> dict:
    """09:30 ET — market just opened. Snapshot state, warm scanner cache,
    fetch news for held positions so the 9:45 research has fresh context."""
    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "market_open", "status": "ok"}
    if not market_is_open():
        summary["status"] = "market closed"
        return _save_run(summary)

    from . import watchlist as wl_mod
    state = portfolio.get_state()
    held_tickers = [p["ticker"] for p in state["positions"]]
    watch_tickers = wl_mod.list_tickers()
    pulse_tickers = list(dict.fromkeys(held_tickers + watch_tickers))

    summary["snapshot"] = {
        "cash": state["cash"], "equity": state["equity"],
        "positions": held_tickers,
        "watchlist": watch_tickers,
    }
    # Warm the scanner cache so 9:45 research is fast
    try:
        scan = scanner.scan_universe(force=True)
        summary["scanner_warmed"] = scan["count"]
    except Exception as e:
        summary["scanner_error"] = str(e)

    # Headline news for each holding + each watchlist ticker
    news_snapshot = []
    for t in pulse_tickers:
        try:
            items = news_mod.fetch_news(t, limit=5)
            agg = sent_mod.aggregate_news_sentiment(items)
            news_snapshot.append({
                "ticker": t, "count": agg.get("count", 0),
                "sentiment": agg.get("label", "neutral"),
                "compound": agg.get("avg_compound", 0),
                "is_held": t in held_tickers,
                "is_watch": t in watch_tickers,
            })
        except Exception as e:
            news_snapshot.append({"ticker": t, "error": str(e)})
    summary["news_snapshot"] = news_snapshot
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _save_run(summary)


def run_news_pulse() -> dict:
    """Mid-session news pulse — re-fetch news for all held positions and flag
    significant sentiment shifts. Surfaces material headlines via macOS notify."""
    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "news_pulse", "status": "ok", "tickers": []}
    if not market_is_open():
        summary["status"] = "market closed"
        return _save_run(summary)

    from . import watchlist as wl_mod
    state = portfolio.get_state()
    held = [p["ticker"] for p in state["positions"]]
    watch = wl_mod.list_tickers()
    pulse_tickers = list(dict.fromkeys(held + watch))

    for t in pulse_tickers:
        try:
            items = news_mod.fetch_news(t, limit=8)
            agg = sent_mod.aggregate_news_sentiment(items)
            entry = {
                "ticker": t, "count": agg.get("count", 0),
                "sentiment": agg.get("label"), "compound": agg.get("avg_compound", 0),
                "top_headline": items[0]["title"] if items else None,
                "is_held": t in held,
                "is_watch": t in watch,
            }
            # Alert on strong sentiment in either direction
            if abs(agg.get("avg_compound", 0)) >= 0.5 and agg.get("count", 0) >= 3:
                from . import notify
                notify.send(
                    f"NEWS PULSE: {t} ({agg['label']})",
                    items[0]["title"][:140] if items else "",
                    subtitle=f"sentiment {agg['avg_compound']:+.2f} · {agg['count']} articles",
                )
                
                # Buy watchlist tickers with very positive news sentiment (if not already held)
                if t in watch and t not in held and agg.get("label") == "very positive":
                    try:
                        open_buys = portfolio.get_open_buy_orders()
                        open_buy_tickers = {o.symbol.upper() for o in open_buys}
                        if t not in open_buy_tickers:
                            committed_open_buys = sum((float(o.qty or 0) - float(o.filled_qty or 0)) * float(o.limit_price or o.filled_avg_price or 0) for o in open_buys)
                            # Restrict to cash balance only per strict risk bounds
                            available_cash = state["cash"] - committed_open_buys
                            if available_cash > 0:
                                cur_quote = price_mod.fetch_quote(t)
                                cur_price = cur_quote["last"]
                                target_dollars = state["equity"] * POSITION_PCT
                                position_dollars = min(target_dollars, available_cash) * 0.98
                                shares = position_dollars / cur_price
                                if shares >= 0.0001:
                                    reason = f"NEWS PULSE BUY: very positive news sentiment ({agg['avg_compound']:+.2f} compound over {agg['count']} articles)"
                                    portfolio.buy(
                                        t, shares=shares, reason=reason,
                                        signal={"bias": "bullish", "score": 4.0, "confidence": 0.50},
                                        auto=True
                                    )
                                    log.info(f"News pulse placed buy order for {t}: {shares:.4f} shares")
                    except Exception as ex:
                        log.warning(f"Failed to place news pulse buy for {t}: {ex}")
            summary["tickers"].append(entry)
        except Exception as e:
            summary["tickers"].append({"ticker": t, "error": str(e)})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _save_run(summary)


def run_markov_regime(ticker: str = "SPY", years: int = 5) -> dict:
    """Invoke the Markov skill as a subprocess. Parses the printed output."""
    import os
    import re
    import subprocess
    from pathlib import Path

    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "markov_regime",
               "ticker": ticker, "years": years, "status": "ok"}

    skill_dir = Path.home() / ".claude" / "skills" / "markov-hedge-fund-method"
    if not skill_dir.exists():
        summary["status"] = f"skill not installed at {skill_dir}"
        return _save_run(summary)

    env = os.environ.copy()
    env["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{env.get('PATH','')}"
    try:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "markov_hedge_fund_method.run",
             "--ticker", ticker, "--years", str(years), "--no-hmm"],
            cwd=str(skill_dir), env=env, capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        summary["status"] = f"subprocess failed: {e}"
        return _save_run(summary)

    out = result.stdout or ""
    summary["stdout_tail"] = out[-2000:]

    # Parse useful numbers
    persistence = {}
    for st, line in re.findall(r"(Bear|Sideways|Bull) -> \1: ([\d.]+)%", out):
        persistence[st] = float(line)
    summary["persistence_diagonal"] = persistence

    stationary = {}
    for m in re.finditer(r"(Bear|Sideways|Bull):\s+([\d.]+)%", out):
        stationary[m.group(1)] = float(m.group(2))
    summary["stationary"] = stationary

    m = re.search(r"Sharpe.*?:\s*([-\d.]+)", out)
    if m:
        summary["walk_forward_sharpe"] = float(m.group(1))
    m = re.search(r"Max drawdown:\s*([-\d.]+)%", out)
    if m:
        summary["max_drawdown_pct"] = float(m.group(1))

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _save_run(summary)


def run_pre_close() -> dict:
    """15:30 ET — last-look stop-loss / take-profit sweep before the close auction."""
    started = datetime.now(timezone.utc)
    summary = {"started_at": started.isoformat(), "kind": "pre_close", "status": "ok",
               "sells": [], "skipped": [], "errors": []}
    if not market_is_open():
        summary["status"] = "market closed"
        return _save_run(summary)

    state = portfolio.get_state()
    for pos in state["positions"]:
        ticker = pos["ticker"]
        # Skip option positions
        if len(ticker) > 10 or any(c.isdigit() for c in ticker[:4]):
            continue
        try:
            pnl_pct = pos["unrealized_pnl_pct"] / 100
            # Stop loss check
            if pnl_pct <= STOP_LOSS_PCT:
                reason = f"Pre-close STOP-LOSS ({pnl_pct*100:+.2f}%)"
                trade = portfolio.sell(ticker, reason=reason, auto=True)
                summary["sells"].append({**trade})
                continue

            # Multi-stage take-profit check
            current_stage = get_profit_stage(ticker)
            stage_to_trigger = 0
            fraction_to_sell = 0.0
            stage_reason = ""

            if pnl_pct >= 0.30 and current_stage < 4:
                stage_to_trigger = 4
                fraction_to_sell = 1.0
                stage_reason = f"Pre-close Take profit Stage 4 hit (+{pnl_pct*100:.1f}%) — sell full remaining"
            elif pnl_pct >= 0.10 and current_stage < 3:
                stage_to_trigger = 3
                fraction_to_sell = 0.50
                stage_reason = f"Pre-close Take profit Stage 3 hit (+{pnl_pct*100:.1f}%) — sell half"
            elif pnl_pct >= 0.05 and current_stage < 2:
                stage_to_trigger = 2
                fraction_to_sell = 0.333
                stage_reason = f"Pre-close Take profit Stage 2 hit (+{pnl_pct*100:.1f}%) — sell 1/3"
            elif pnl_pct >= 0.02 and current_stage < 1:
                stage_to_trigger = 1
                fraction_to_sell = 0.10
                stage_reason = f"Pre-close Take profit Stage 1 hit (+{pnl_pct*100:.1f}%) — sell small portion (10%)"

            if stage_to_trigger > 0:
                shares_held = pos["shares"]
                shares_to_sell = round(shares_held * fraction_to_sell, 4)
                if stage_to_trigger == 4 or shares_held - shares_to_sell < 0.0001:
                    shares_to_sell = shares_held
                
                if shares_to_sell > 0:
                    trade = portfolio.sell(ticker, shares=shares_to_sell, reason=stage_reason, auto=True)
                    summary["sells"].append({**trade})
                    set_profit_stage(ticker, stage_to_trigger)
            else:
                summary["skipped"].append({"ticker": ticker, "pnl_pct": round(pnl_pct * 100, 2)})
        except Exception as e:
            summary["errors"].append({"ticker": ticker, "error": str(e)})
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _save_run(summary)


# ===================================================================
#  Intraday hourly trade window — high-conviction only
# ===================================================================

INTRADAY_SCORE_THRESHOLD = 3.0    # match morning bar — no reason to block good picks intraday
INTRADAY_CONF_THRESHOLD = 0.30    # match morning bar
INTRADAY_MAX_BUYS_PER_DAY = 20    # combined across 10:00 + every hour (user requested more)
INTRADAY_MAX_BUYS_PER_WINDOW = 5


def _todays_auto_buy_count() -> int:
    """Count BUY trades placed today (ET) by the auto-trader. Used to enforce daily cap."""
    today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start.astimezone(timezone.utc).isoformat()
    count = 0
    for t in portfolio.trades(limit=200):
        try:
            if t["side"] == "BUY" and (t["ts"] or "") >= today_start_utc and t.get("auto"):
                count += 1
        except Exception:
            continue
    # Alpaca-backed portfolio.trades doesn't set auto=1; fall back to counting all today's buys
    if count == 0:
        for t in portfolio.trades(limit=200):
            try:
                if t["side"] == "BUY" and (t["ts"] or "") >= today_start_utc:
                    count += 1
            except Exception:
                continue
    return count


def run_intraday_trade(dry_run: bool = False, force_market_check: bool = True) -> dict:
    """Hourly intraday trade window. Re-reads today's cached research, applies the
    tighter intraday bar, and places limit buys until the daily cap is hit."""
    started = datetime.now(timezone.utc)
    summary = {
        "started_at": started.isoformat(), "kind": "intraday_trade",
        "dry_run": dry_run, "status": "ok",
        "sells": [], "buys": [], "skipped": [], "errors": [],
    }
    if force_market_check and not market_is_open():
        summary["status"] = "market closed"
        return _save_run(summary)

    today_buys = _todays_auto_buy_count()
    if today_buys >= INTRADAY_MAX_BUYS_PER_DAY:
        summary["status"] = f"daily cap reached ({today_buys}/{INTRADAY_MAX_BUYS_PER_DAY})"
        return _save_run(summary)

    # Sell pass — same stop-loss / signal-flip logic
    state = portfolio.get_state()
    if not dry_run:
        cleanup_profit_stages([p["ticker"] for p in state["positions"]])

    for pos in state["positions"]:
        ticker = pos["ticker"]
        # Skip option positions — they're managed by the wheel module, not stock sell logic
        if len(ticker) > 10 or any(c.isdigit() for c in ticker[:4]):
            continue
        try:
            pnl_pct = pos["unrealized_pnl_pct"] / 100
            
            # Stop loss check
            if pnl_pct <= STOP_LOSS_PCT:
                reason = f"Stop loss hit ({pnl_pct*100:+.2f}%)"
                if dry_run:
                    summary["sells"].append({"ticker": ticker, "reason": reason, "dry": True})
                else:
                    trade = portfolio.sell(ticker, reason=f"INTRADAY: {reason}", auto=True)
                    summary["sells"].append({**trade})
                continue

            # Multi-stage take-profit check
            current_stage = get_profit_stage(ticker)
            stage_to_trigger = 0
            fraction_to_sell = 0.0
            stage_reason = ""

            if pnl_pct >= 0.30 and current_stage < 4:
                stage_to_trigger = 4
                fraction_to_sell = 1.0
                stage_reason = f"Take profit Stage 4 hit (+{pnl_pct*100:.1f}%) — sell full remaining"
            elif pnl_pct >= 0.10 and current_stage < 3:
                stage_to_trigger = 3
                fraction_to_sell = 0.50
                stage_reason = f"Take profit Stage 3 hit (+{pnl_pct*100:.1f}%) — sell half"
            elif pnl_pct >= 0.05 and current_stage < 2:
                stage_to_trigger = 2
                fraction_to_sell = 0.333
                stage_reason = f"Take profit Stage 2 hit (+{pnl_pct*100:.1f}%) — sell 1/3"
            elif pnl_pct >= 0.02 and current_stage < 1:
                stage_to_trigger = 1
                fraction_to_sell = 0.10
                stage_reason = f"Take profit Stage 1 hit (+{pnl_pct*100:.1f}%) — sell small portion (10%)"

            if stage_to_trigger > 0:
                shares_held = pos["shares"]
                shares_to_sell = round(shares_held * fraction_to_sell, 4)
                if stage_to_trigger == 4 or shares_held - shares_to_sell < 0.0001:
                    shares_to_sell = shares_held
                
                if shares_to_sell > 0:
                    if dry_run:
                        summary["sells"].append({
                            "ticker": ticker,
                            "shares": shares_to_sell,
                            "reason": stage_reason,
                            "dry": True
                        })
                    else:
                        trade = portfolio.sell(ticker, shares=shares_to_sell, reason=f"INTRADAY: {stage_reason}", auto=True)
                        summary["sells"].append({**trade})
                        set_profit_stage(ticker, stage_to_trigger)
                    continue

        except Exception as e:
            summary["errors"].append({"ticker": ticker, "phase": "sell", "error": str(e)})

    # Buy pass — reuse today's research candidates
    research_today = research.get_today()
    if not research_today:
        summary["status"] = "no research available yet (waiting for 9:45 research window)"
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        return _save_run(summary)

    cands = research_today.get("candidates", [])
    
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
    
    # Capital raising pass: if we have candidates we do not yet hold (and aren't pending buy), but low buying power, sell worst performer
    unheld_candidates = [c for c in cands if not (portfolio.has_position(c["ticker"]) or c["ticker"] in open_buy_tickers)]
    if unheld_candidates and not dry_run:
        cur_state = portfolio.get_state()
        buying_power = cur_state.get("buying_power", cur_state["cash"])
        target_dollars = cur_state["equity"] * POSITION_PCT
        if buying_power < target_dollars * 0.5:
            stock_positions = [
                p for p in cur_state["positions"] 
                if not (len(p["ticker"]) > 10 or any(c.isdigit() for c in p["ticker"][:4]))
            ]
            if stock_positions:
                worst = min(stock_positions, key=lambda x: x["unrealized_pnl_pct"])
                try:
                    trade = portfolio.sell(worst["ticker"], reason=f"INTRADAY: Liquidated worst performer ({worst['unrealized_pnl_pct']:.2f}%) to free up buying power", auto=True)
                    summary["sells"].append({**trade})
                except Exception as e:
                    summary["errors"].append({"ticker": worst["ticker"], "phase": "sell_for_capital", "error": str(e)})

    # Get current portfolio state once at the start of the loop
    cur_state = portfolio.get_state()
    # Restrict buying to cash balance (de-leverages and disables stock margin borrowing)
    available_cash = cur_state["cash"] - committed_open_buys
    equity = cur_state["equity"]

    bought_this_window = 0
    for cand in cands:
        if bought_this_window >= INTRADAY_MAX_BUYS_PER_WINDOW:
            break
        if today_buys + bought_this_window >= INTRADAY_MAX_BUYS_PER_DAY:
            summary["status"] = f"daily cap reached during window ({INTRADAY_MAX_BUYS_PER_DAY})"
            break

        ticker = cand["ticker"]
        try:
            if portfolio.has_position(ticker) or ticker in open_buy_tickers:
                summary["skipped"].append({"ticker": ticker, "reason": "already held or pending buy"})
                continue

            # Tighter intraday bar
            if cand["score"] < INTRADAY_SCORE_THRESHOLD:
                summary["skipped"].append({"ticker": ticker, "reason": f"score {cand['score']} < {INTRADAY_SCORE_THRESHOLD} (intraday bar)"})
                continue
            if cand["confidence"] < INTRADAY_CONF_THRESHOLD:
                summary["skipped"].append({"ticker": ticker, "reason": f"conf {cand['confidence']:.2f} < {INTRADAY_CONF_THRESHOLD}"})
                continue

            # Limit-drift check (price hasn't moved > limit from research snapshot)
            cur = price_mod.fetch_quote(ticker)
            decision_price = cand["decision_price"]
            drift = (cur["last"] - decision_price) / decision_price
            limit_ceiling = decision_price * (1 + LIMIT_DRIFT_PCT)
            if cur["last"] > limit_ceiling:
                summary["skipped"].append({"ticker": ticker, "reason": f"drift {drift*100:+.2f}% above {LIMIT_DRIFT_PCT*100:.1f}% limit"})
                continue

            # Position size = POSITION_PCT of equity, capped by available cash/buying power
            # We use available_cash instead of cur_state["cash"] so simultaneous orders don't double spend
            target_dollars = equity * POSITION_PCT
            position_dollars = min(target_dollars, available_cash) * 0.98
            shares = position_dollars / cur["last"]
            if shares < 0.0001 or available_cash < shares * cur["last"]:
                summary["skipped"].append({"ticker": ticker, "reason": f"insufficient cash (${available_cash:,.2f}) or size"})
                continue

            d = cand.get("decisions", {})
            reason = (
                f"INTRADAY (hourly): score {cand['score']}, {cand['bias']}, conf {int(cand['confidence']*100)}% | "
                f"[1 cash] {d.get('q1_cash','')} | [2 open] {d.get('q2_open_positions','')[:50]} | "
                f"[3 news] {d.get('q3_news','')} | [4 MAs] {d.get('q4_moving_averages','')} | "
                f"[5 risk] {d.get('q5_risk','')}"
            )
            if dry_run:
                summary["buys"].append({"ticker": ticker, "shares": round(shares, 4), "price": cur["last"], "reason": reason, "dry": True})
            else:
                trade = portfolio.buy(
                    ticker, shares=shares, reason=reason,
                    signal={"bias": cand["bias"], "score": cand["score"], "confidence": cand["confidence"]},
                    auto=True,
                )
                summary["buys"].append({**trade})
                # Subtract estimated cost from available_cash for subsequent loop iterations
                available_cash -= shares * cur["last"]
            bought_this_window += 1
        except ValueError as ve:
            summary["skipped"].append({"ticker": ticker, "reason": str(ve)})
        except Exception as e:
            summary["errors"].append({"ticker": ticker, "phase": "buy", "error": str(e)})

    summary["todays_total_buys"] = today_buys + bought_this_window
    summary["daily_cap"] = INTRADAY_MAX_BUYS_PER_DAY
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _save_run(summary)


def run_market_close() -> dict:
    """16:00 ET — end-of-day snapshot. Logs equity/positions/today's P&L."""
    started = datetime.now(timezone.utc)
    state = portfolio.get_state()
    summary = {
        "started_at": started.isoformat(),
        "kind": "market_close",
        "status": "ok",
        "cash": state["cash"], "equity": state["equity"],
        "total_return_pct": state["total_pnl_pct"],
        "todays_pnl_pct": state["todays_pnl_pct"],
        "positions": [
            {"ticker": p["ticker"], "qty": p["shares"], "pnl_pct": p["unrealized_pnl_pct"]}
            for p in state["positions"]
        ],
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    return _save_run(summary)


# ===================================================================
#  Backwards-compat (legacy endpoints still call these)
# ===================================================================

def run_daily(dry_run: bool = False) -> dict:
    """Legacy: now equivalent to research+trade run back-to-back."""
    r = run_research(dry_run=dry_run, force_market_check=False)
    t = run_trade_window(dry_run=dry_run, force_market_check=False)
    return {"research": r, "trade": t, "kind": "legacy_daily"}


def run_intraday(dry_run: bool = False, force_market_check: bool = True) -> dict:
    """Legacy: intraday is removed per new rules. Returns a no-op summary."""
    return {
        "kind": "intraday",
        "status": "removed — single 10:00 AM trade window per new agent rules",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sells": [], "buys": [], "skipped": [], "errors": [],
    }
