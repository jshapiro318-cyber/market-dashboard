import os
import pandas as pd
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import config as cfg_mod
from backend.analysis import fundamentals as fund_mod
from backend.analysis import dcf as dcf_mod
from backend.analysis import equity_research as eq_mod
from backend.analysis import data_assistant as da_mod
from backend.analysis import indicators as ind_mod
from backend.analysis import patterns as pat_mod
from backend.analysis import sentiment as sent_mod
from backend.analysis import signals as sig_mod
from backend.data import news as news_mod
from backend.data import prices as price_mod
from backend.trading import db as trading_db
from backend.trading import journal as journal_mod
from backend.trading import portfolio as port_mod
from backend.trading import research as research_mod
from backend.trading import scanner as scanner_mod
from backend.trading import scheduler as sched_mod
from backend.trading import options as opts_mod
from backend.trading import strategy as strat_mod
from backend.trading import wheel as wheel_mod
from backend.trading import watchlist as wl_mod
from backend.trading import quant as quant_mod

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

DISCLAIMER = (
    "EDUCATIONAL TOOL — NOT FINANCIAL ADVICE. Signals are derived from public "
    "indicator math and rules-based pattern recognition, not from a predictive "
    "model. No system reliably forecasts markets. Paper trading does not model "
    "slippage, spread, or after-hours moves."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    trading_db.init()
    if cfg_mod.configured():
        sched_mod.start()
    else:
        import logging
        logging.getLogger("uvicorn").warning(
            "Alpaca not configured (.env missing or empty). Scheduler not started. "
            "Trading endpoints will return 503. See /api/config."
        )
    yield
    sched_mod.stop()


app = FastAPI(title="Market Analysis Dashboard", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Friendly 503 when Alpaca isn't configured
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    msg = str(exc)
    if "Alpaca API credentials not configured" in msg:
        return JSONResponse(
            status_code=503,
            content={
                "error": "alpaca_not_configured",
                "message": msg,
                "config": cfg_mod.status(),
            },
        )
    raise exc


# ---------- Analysis ----------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "scheduler_next_run": sched_mod.next_run(),
        "scheduler_last_run": sched_mod.last_run(),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/config")
def config_status():
    return cfg_mod.status()


@app.get("/api/candles/{ticker}")
def candles(ticker: str, period: str = "6mo", interval: str = "1d"):
    try:
        df = price_mod.fetch_history(ticker, period=period, interval=interval)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {
        "ticker": ticker.upper(),
        "interval": interval,
        "period": period,
        "candles": price_mod.candles_for_chart(df),
    }


@app.get("/api/analyze/{ticker}")
def analyze(ticker: str, period: str = "1y", interval: str = "1d"):
    try:
        df = price_mod.fetch_history(ticker, period=period, interval=interval)
    except ValueError as e:
        raise HTTPException(404, str(e))

    if len(df) < 30:
        raise HTTPException(400, f"Not enough history for {ticker} ({len(df)} bars).")

    quote = price_mod.fetch_quote(ticker)
    indicators = ind_mod.compute_all(df)
    candle_patterns = pat_mod.detect_candlestick_patterns(df, lookback=5)
    chart_patterns = pat_mod.detect_chart_patterns(df, indicators)
    levels = pat_mod.support_resistance(df)

    raw_news = news_mod.fetch_news(ticker, limit=15)
    news_agg = sent_mod.aggregate_news_sentiment(raw_news)
    fundamentals = fund_mod.fetch_fundamentals(ticker)

    signal = sig_mod.compute_signal(quote, indicators, candle_patterns, chart_patterns, news_agg, fundamentals=fundamentals)

    return {
        "ticker": ticker.upper(),
        "quote": quote,
        "indicators": indicators,
        "patterns": {"candlestick": candle_patterns, "chart": chart_patterns},
        "levels": levels,
        "news": raw_news,
        "news_summary": news_agg,
        "fundamentals": fundamentals,
        "signal": signal,
        "chart": price_mod.candles_for_chart(df, max_bars=180),
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/watchlist")
def watchlist(tickers: str = Query("SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMZN,GOOGL,META,AMD")):
    """Quick scan of a small custom list (with news per ticker)."""
    out = []
    for t in [s.strip().upper() for s in tickers.split(",") if s.strip()]:
        try:
            df = price_mod.fetch_history(t, period="3mo", interval="1d")
            quote = price_mod.fetch_quote(t)
            indicators = ind_mod.compute_all(df)
            candle_pats = pat_mod.detect_candlestick_patterns(df, lookback=3)
            chart_pats = pat_mod.detect_chart_patterns(df, indicators)
            news_agg = sent_mod.aggregate_news_sentiment(news_mod.fetch_news(t, limit=8))
            signal = sig_mod.compute_signal(quote, indicators, candle_pats, chart_pats, news_agg)
            out.append({
                "ticker": t,
                "last": quote["last"],
                "change_pct": quote["change_pct"],
                "rel_volume": quote["relative_volume"],
                "rsi": indicators.get("rsi_14"),
                "bias": signal["bias"],
                "score": signal["score"],
                "confidence": signal["confidence"],
                "top_reason": signal["factors"][0]["reasons"][0] if signal["factors"][0]["reasons"] else "",
            })
        except Exception as e:
            out.append({"ticker": t, "error": str(e)})
    out.sort(key=lambda r: r.get("score", 0) if "score" in r else -999, reverse=True)
    return {"results": out, "disclaimer": DISCLAIMER}


# ---------- Scanner ----------

@app.get("/api/scanner")
def scanner(force: bool = False):
    """Top bullish + top bearish across the S&P-style universe."""
    return scanner_mod.scan_universe(force=force)


# ---------- Portfolio ----------

class TradeRequest(BaseModel):
    ticker: str
    shares: float
    reason: Optional[str] = None


@app.get("/api/portfolio")
def portfolio_state():
    return port_mod.get_state()


@app.post("/api/portfolio/buy")
def portfolio_buy(req: TradeRequest):
    try:
        return port_mod.buy(req.ticker, req.shares, reason=req.reason or "manual")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/portfolio/sell")
def portfolio_sell(req: TradeRequest):
    try:
        return port_mod.sell(req.ticker, req.shares, reason=req.reason or "manual")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/portfolio/trades")
def portfolio_trades(limit: int = 100):
    return {"trades": port_mod.trades(limit=limit)}


@app.get("/api/portfolio/auto-runs")
def portfolio_auto_runs(limit: int = 30):
    return {"runs": port_mod.auto_runs(limit=limit)}


@app.post("/api/portfolio/reset")
def portfolio_reset():
    port_mod.reset()
    return port_mod.get_state()


# ---------- Agent windows: research / trade / journal ----------

@app.post("/api/research/run-now")
def research_run_now(dry_run: bool = True, force: bool = False):
    """9:45 ET research window. force=true bypasses market-hours gate."""
    return strat_mod.run_research(dry_run=dry_run, force_market_check=not force)


@app.post("/api/trade/run-now")
def trade_run_now(dry_run: bool = True, force: bool = False):
    """10:00 ET trade window. Requires today's research already cached."""
    return strat_mod.run_trade_window(dry_run=dry_run, force_market_check=not force)


@app.post("/api/journal/run-now")
def journal_run_now():
    """16:15 ET journal window — write today's markdown."""
    return strat_mod.run_journal()


@app.post("/api/market-open/run-now")
def market_open_run_now():
    """9:30 ET market open snapshot — warms scanner, fetches news for holdings."""
    return strat_mod.run_market_open()


@app.post("/api/news-pulse/run-now")
def news_pulse_run_now():
    """News pulse — fetches news for all held positions and surfaces big shifts."""
    return strat_mod.run_news_pulse()


@app.post("/api/markov/run-now")
def markov_run_now(ticker: str = "SPY", years: int = 5):
    """Run the Markov regime skill via subprocess. Default SPY 5y."""
    return strat_mod.run_markov_regime(ticker=ticker, years=years)


@app.post("/api/pre-close/run-now")
def pre_close_run_now():
    """15:30 ET stop-loss/take-profit sweep."""
    return strat_mod.run_pre_close()


@app.post("/api/market-close/run-now")
def market_close_run_now():
    """16:00 ET end-of-day snapshot."""
    return strat_mod.run_market_close()


@app.post("/api/intraday-trade/run-now")
def intraday_trade_run_now(dry_run: bool = True, force: bool = False):
    """Hourly intraday trade window — high-conviction bar, 8/day cap."""
    return strat_mod.run_intraday_trade(dry_run=dry_run, force_market_check=not force)


@app.get("/api/movers")
def movers_list(top_n: int = 25):
    """Top % gainers and losers across the universe right now."""
    from backend.trading import movers as movers_mod
    return movers_mod.get_today_movers(top_n=top_n)


@app.post("/api/movers/run-now")
def movers_run_now(dry_run: bool = True, force: bool = False):
    """Top-Movers scan: find biggest gainers, full-analyze top picks, buy those meeting strict criteria."""
    from backend.trading import movers as movers_mod
    return movers_mod.run_movers_scan(dry_run=dry_run, force_market_check=not force)


@app.post("/api/premarket-brief/run-now")
def premarket_brief_run_now():
    """9:15 ET pre-market news brief: 30-50 events scored for per-ticker BUY/SELL/WATCH."""
    return strat_mod.run_premarket_brief()


@app.get("/api/premarket-brief/latest")
def premarket_brief_latest():
    b = strat_mod.latest_premarket_brief()
    if not b:
        raise HTTPException(404, "No premarket brief yet. Run /api/premarket-brief/run-now or wait for the 9:15 ET fire.")
    return b


# ---------- Watchlist (custom tickers always tracked) ----------

class WatchlistBody(BaseModel):
    tickers: str  # comma-or-space separated
    note: Optional[str] = ""


@app.get("/api/watchlist/list")
def watchlist_list():
    return {"tickers": wl_mod.list_all()}


@app.post("/api/watchlist/add")
def watchlist_add(body: WatchlistBody):
    parts = [t.strip().upper() for t in body.tickers.replace(",", " ").split() if t.strip()]
    return wl_mod.add(parts, note=body.note or "")


@app.post("/api/watchlist/remove")
def watchlist_remove(body: WatchlistBody):
    parts = [t.strip().upper() for t in body.tickers.replace(",", " ").split() if t.strip()]
    return wl_mod.remove(parts)


@app.post("/api/watchlist/clear")
def watchlist_clear():
    return wl_mod.clear()

# ---------- Quant Lab ----------

class QuantBacktestRequest(BaseModel):
    ticker1: str
    ticker2: str
    period: Optional[str] = "1y"
    window: Optional[int] = 20


@app.get("/api/quant/context")
def quant_context():
    return quant_mod.get_quant_context()


@app.post("/api/quant/backtest")
def quant_backtest(req: QuantBacktestRequest):
    try:
        return quant_mod.run_pairs_backtest(req.ticker1, req.ticker2, req.period or "1y", req.window or 20)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Backtest execution failed: {e}")


@app.get("/api/quant/progress")
def quant_progress():
    return quant_mod.get_progress()


@app.post("/api/quant/deploy")
def quant_deploy():
    return quant_mod.validate_and_deploy()


@app.get("/api/quant/picks")
def quant_picks():
    return quant_mod.get_ultimate_picks()


class DCFRequest(BaseModel):
    ticker: str
    growth_rate: Optional[float] = None
    terminal_growth: Optional[float] = None
    wacc: Optional[float] = None


@app.post("/api/quant/dcf")
def quant_dcf(req: DCFRequest):
    try:
        growth = req.growth_rate
        if growth is not None and growth > 1.0:
            growth = growth / 100.0
        terminal = req.terminal_growth
        if terminal is not None and terminal > 1.0:
            terminal = terminal / 100.0
        wacc = req.wacc
        if wacc is not None and wacc > 1.0:
            wacc = wacc / 100.0
            
        return dcf_mod.run_dcf_valuation(req.ticker, growth, terminal, wacc)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"DCF execution failed: {e}")


@app.get("/api/quant/research/{ticker}")
def quant_research_report(ticker: str):
    try:
        return eq_mod.generate_research_report(ticker)
    except Exception as e:
        raise HTTPException(500, f"Report generation failed: {e}")


# ---------- Data Analysis Assistant Endpoints ----------

class AnalysisRunRequest(BaseModel):
    dataset: str
    type: str
    domain: Optional[str] = "general"

class CodeGenRequest(BaseModel):
    language: str
    type: str

class ReportRequest(BaseModel):
    dataset: str
    format: str = "markdown"

class DeleteDatasetRequest(BaseModel):
    filename: str

@app.get("/api/analysis/sample")
def get_analysis_sample():
    try:
        os.makedirs("data_storage", exist_ok=True)
        sample_path = "data_storage/user_behavior_sample.csv"
        if not os.path.exists(sample_path):
            raise HTTPException(404, "Sample file not found.")
        df = pd.read_csv(sample_path)
        stats = da_mod.get_summary_stats(df)
        return {"status": "ok", "filename": "user_behavior_sample.csv", "stats": stats}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(500, f"Failed to load sample dataset: {e}")

@app.get("/api/analysis/datasets")
def list_datasets():
    try:
        os.makedirs("data_storage", exist_ok=True)
        files = [
            {
                "filename": f,
                "size_bytes": os.path.getsize(os.path.join("data_storage", f)),
                "modified": os.path.getmtime(os.path.join("data_storage", f)),
            }
            for f in os.listdir("data_storage")
            if f.endswith(".csv")
        ]
        files.sort(key=lambda x: x["modified"], reverse=True)
        return {"status": "ok", "datasets": files}
    except Exception as e:
        raise HTTPException(500, f"Failed to list datasets: {e}")

@app.post("/api/analysis/delete")
def delete_dataset(req: DeleteDatasetRequest):
    try:
        path = os.path.join("data_storage", req.filename)
        if not os.path.exists(path):
            raise HTTPException(404, f"Dataset {req.filename} not found.")
        os.remove(path)
        return {"status": "ok", "deleted": req.filename}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {e}")

@app.post("/api/analysis/upload")
def upload_dataset(file: UploadFile = File(...)):
    try:
        os.makedirs("data_storage", exist_ok=True)
        filename = file.filename
        if not filename.endswith(".csv"):
            raise HTTPException(400, "Only CSV files are supported.")
        save_path = os.path.join("data_storage", filename)
        with open(save_path, "wb") as buffer:
            buffer.write(file.file.read())
        df = pd.read_csv(save_path)
        stats = da_mod.get_summary_stats(df)
        return {"status": "ok", "filename": filename, "stats": stats}
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {e}")

@app.post("/api/analysis/run")
def run_analysis(req: AnalysisRunRequest):
    try:
        path = os.path.join("data_storage", req.dataset)
        if not os.path.exists(path):
            raise HTTPException(404, f"Dataset {req.dataset} not found.")
        df = pd.read_csv(path)
        analysis_type = (req.type or "exploratory").lower()
        if analysis_type == "statistical":
            result = da_mod.run_statistical_analysis(df)
            return {"status": "ok", "dataset": req.dataset, "type": analysis_type, "result": result,
                    "stats": da_mod.get_summary_stats(df)}
        elif analysis_type == "predictive":
            result = da_mod.run_predictive_analysis(df)
            return {"status": "ok", "dataset": req.dataset, "type": analysis_type, "result": result,
                    "stats": da_mod.get_summary_stats(df)}
        elif analysis_type == "complete":
            result = da_mod.run_complete_analysis(df)
            return {"status": "ok", "dataset": req.dataset, "type": analysis_type, "result": result,
                    "stats": result["summary"]}
        else:
            stats = da_mod.get_summary_stats(df)
            return {"status": "ok", "dataset": req.dataset, "type": analysis_type, "stats": stats}
    except Exception as e:
        raise HTTPException(500, f"Analysis failed: {e}")

@app.post("/api/analysis/quality")
def run_quality_check(req: AnalysisRunRequest):
    try:
        path = os.path.join("data_storage", req.dataset)
        if not os.path.exists(path):
            raise HTTPException(404, f"Dataset {req.dataset} not found.")
        df = pd.read_csv(path)
        result = da_mod.run_quality_check(df)
        return {"status": "ok", "dataset": req.dataset, "result": result}
    except Exception as e:
        raise HTTPException(500, f"Quality check failed: {e}")

@app.post("/api/analysis/hypothesis")
def generate_hypotheses(req: AnalysisRunRequest):
    try:
        path = os.path.join("data_storage", req.dataset)
        if not os.path.exists(path):
            raise HTTPException(404, f"Dataset {req.dataset} not found.")
        df = pd.read_csv(path)
        result = da_mod.generate_hypotheses(df, domain=req.domain or "general")
        return {"status": "ok", "dataset": req.dataset, "result": result}
    except Exception as e:
        raise HTTPException(500, f"Hypothesis generation failed: {e}")

@app.post("/api/analysis/code")
def get_analysis_code(req: CodeGenRequest):
    try:
        code = da_mod.generate_code(req.language, req.type)
        return {"status": "ok", "language": req.language, "type": req.type, "code": code}
    except Exception as e:
        raise HTTPException(500, f"Code generation failed: {e}")

@app.post("/api/analysis/report")
def get_analysis_report(req: ReportRequest):
    try:
        path = os.path.join("data_storage", req.dataset)
        if not os.path.exists(path):
            raise HTTPException(404, f"Dataset {req.dataset} not found.")
        df = pd.read_csv(path)
        report_md = da_mod.generate_report(df, req.dataset)
        return {"status": "ok", "dataset": req.dataset, "report": report_md}
    except Exception as e:
        raise HTTPException(500, f"Report compilation failed: {e}")


@app.get("/api/research/today")
def research_today():
    r = research_mod.get_today()
    if r is None:
        raise HTTPException(404, "No research recorded for today (ET).")
    return r


@app.get("/api/journal/today")
def journal_today():
    import pytz
    from datetime import datetime
    today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    content = journal_mod.read(today)
    if content is None:
        raise HTTPException(404, f"No journal for {today}. Run /api/journal/run-now to create.")
    return {"date": today, "content": content}


@app.get("/api/journal/list")
def journal_list(limit: int = 30):
    return {"entries": journal_mod.list_recent(limit=limit)}


@app.get("/api/journal/{date_str}")
def journal_by_date(date_str: str):
    content = journal_mod.read(date_str)
    if content is None:
        raise HTTPException(404, f"No journal for {date_str}.")
    return {"date": date_str, "content": content}


# ---------- Wheel strategy ----------

class WheelStartRequest(BaseModel):
    ticker: str


@app.post("/api/wheel/start")
def wheel_start(req: WheelStartRequest):
    try:
        return wheel_mod.start(req.ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        # Options-not-enabled or other Alpaca error
        raise HTTPException(403, str(e))


@app.post("/api/wheel/stop")
def wheel_stop(req: WheelStartRequest):
    return wheel_mod.stop(req.ticker)


@app.post("/api/wheel/remove")
def wheel_remove(req: WheelStartRequest):
    return wheel_mod.remove(req.ticker)


@app.post("/api/wheel/tick")
def wheel_tick(req: WheelStartRequest):
    """Manually trigger one tick of the wheel for a ticker."""
    return wheel_mod.tick(req.ticker)


@app.post("/api/wheel/tick-all")
def wheel_tick_all():
    return {"results": wheel_mod.tick_all()}


@app.get("/api/wheel/summary")
def wheel_summary():
    return wheel_mod.summary()


@app.get("/api/wheel/{ticker}")
def wheel_state(ticker: str):
    s = wheel_mod.get_state(ticker)
    if not s:
        raise HTTPException(404, f"No wheel registered for {ticker.upper()}")
    return s


@app.get("/api/wheel/{ticker}/legs")
def wheel_legs(ticker: str, limit: int = 50):
    return {"ticker": ticker.upper(), "legs": wheel_mod.list_legs(ticker, limit=limit)}


@app.get("/api/options/eligibility")
def options_eligibility():
    ok, msg = opts_mod.account_supports_options()
    return {"ok": ok, "message": msg}


# Back-compat: legacy /api/auto/* endpoints
@app.post("/api/auto/run-now")
def auto_run_now(dry_run: bool = True):
    return strat_mod.run_daily(dry_run=dry_run)


@app.post("/api/auto/intraday-now")
def auto_intraday_now(dry_run: bool = True, force: bool = False):
    return strat_mod.run_intraday(dry_run=dry_run, force_market_check=not force)


@app.get("/api/scheduler/health")
def scheduler_health():
    """Watchdog status + a verdict on whether tomorrow's schedule will fire."""
    from datetime import datetime, timezone
    import pytz
    ET = pytz.timezone("America/New_York")
    et_now = datetime.now(ET)
    health = sched_mod.health_status()
    nexts = sched_mod.next_runs()
    overdue = []
    for job_id, ts in nexts.items():
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if (datetime.now(dt.tzinfo) - dt).total_seconds() > 300:
                overdue.append(job_id)
        except Exception:
            pass
    return {
        "now_et": et_now.isoformat(),
        "scheduler_running": health.get("scheduler_running"),
        "watchdog_thread_alive": health.get("watchdog_thread_alive"),
        "jobs_armed": health.get("jobs_armed"),
        "last_watchdog_check": health.get("checked_at"),
        "watchdog_restarts_so_far": health.get("restarts", 0),
        "overdue_jobs": overdue,
        "next_runs": nexts,
    }


@app.get("/api/auto/status")
def auto_status():
    return {
        "scheduler_next_runs": sched_mod.next_runs(),
        "last_runs": sched_mod.last_runs(),
        "market_is_open": strat_mod.market_is_open(),
        "rules": {
            "stop_loss_pct": strat_mod.STOP_LOSS_PCT,
            "take_profit_pct": strat_mod.TAKE_PROFIT_PCT,
            "max_new_positions": strat_mod.MAX_NEW_POSITIONS,
            "position_pct": strat_mod.POSITION_PCT,
            "max_position_pct_hard_cap": port_mod.MAX_POSITION_PCT,
            "limit_drift_pct": strat_mod.LIMIT_DRIFT_PCT,
            "buy_score_threshold": strat_mod.BUY_SCORE_THRESHOLD,
            "buy_confidence_threshold": strat_mod.BUY_CONFIDENCE_THRESHOLD,
        },
    }


# ---------- Static frontend ----------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(
            str(FRONTEND_DIR / "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"}
        )
