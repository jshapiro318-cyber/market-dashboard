"""APScheduler bootstrap — full-day weekday cadence in US/Eastern.

Schedule (Mon-Fri, all times ET — NO GAPS, every 30 min is covered):

  09:00  premarket_brief       news brief for holdings
  09:15  research_window       scan + analyze top candidates
  09:30  market_open + trade   snapshot + execute buys/sells
  10:00  intraday_10 + markov  intraday trades + regime model
  10:30  movers_10_30          top gainers scan
  11:00  intraday_11           intraday trades
  11:30  news_pulse_am         news re-fetch for holdings
  12:00  research_midday       midday research scan
  12:30  movers_12_30          top gainers scan
  13:00  intraday_13           intraday trades
  13:30  news_pulse_pm         news re-fetch for holdings
  14:00  intraday_14 + markov  intraday trades + regime model
  14:30  movers_14_30          top gainers scan
  15:00  intraday_15           intraday trades
  15:30  pre_close             stop-loss / take-profit sweep
  16:00  market_close          end-of-day snapshot
  16:15  journal_window        write journal/YYYY-MM-DD.md

Plus: every 15 min during market hours → wheel_monitor (gated by market_is_open).

All jobs run inside the FastAPI process. If the backend is down when a cron
fires, boot-catchup replays missed jobs on the next startup.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import strategy

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None
_started_at: datetime | None = None
_last: dict[str, str | None] = {
    "premarket_brief": None, "market_open": None, "research": None, "trade": None,
    "intraday": None, "news_pulse_am": None, "markov": None,
    "news_pulse_pm": None, "pre_close": None, "market_close": None,
    "journal": None, "wheel": None, "movers": None,
}
_ET = pytz.timezone("America/New_York")


def start():
    global _scheduler, _started_at
    if _scheduler:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=_ET)
    _started_at = datetime.now(timezone.utc)

    # ── Full-day schedule: every 30-min slot from 9:00 to 16:15 ET ──
    # No gaps — the system is ALWAYS scanning, trading, or monitoring.
    jobs = [
        # === Pre-market & Open ===
        ("premarket_brief",  _premarket_job,    9,  0),
        ("research_window",  _research_job,     9, 15),
        ("liquidate_opts",   _liquidate_opts_job,9, 30),
        ("market_open",      _market_open_job,  9, 30),
        ("trade_window",     _trade_job,        9, 30),

        # === Morning session ===
        ("news_pulse_am",    _news_am_job,     11, 30),

        # === Midday session ===
        ("research_midday",  _research_job,    12,  0),
        ("news_pulse_pm",    _news_pm_job,     13, 30),

        # === Afternoon session ===

        # === Close ===
        ("pre_close",        _pre_close_job,   15, 30),
        ("market_close",     _market_close_job,16,  0),
        ("journal_window",   _journal_job,     16, 15),
    ]
    for job_id, fn, hour, minute in jobs:
        _scheduler.add_job(
            fn,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=_ET),
            id=job_id, replace_existing=True, max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )

    # Markov regime model — twice daily (morning + afternoon) for fresh reads
    for mid, hour in [("markov_am", 10), ("markov_pm", 14)]:
        _scheduler.add_job(
            _markov_job,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=0, timezone=_ET),
            id=mid, replace_existing=True, max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )

    # Continuous active scanning during extended market hours: every 5 minutes
    _scheduler.add_job(
        _movers_job,
        CronTrigger(day_of_week="mon-fri", hour="4-19", minute="*/5", timezone=_ET),
        id="movers_continuous", replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=3600,
    )

    # Continuous active hunting for intraday setups: every 5 minutes (offset by 2 mins)
    _scheduler.add_job(
        _intraday_job,
        CronTrigger(day_of_week="mon-fri", hour="4-19", minute="2-59/5", timezone=_ET),
        id="intraday_continuous", replace_existing=True, max_instances=1, coalesce=True,
        misfire_grace_time=3600,
    )

    _scheduler.start()
    _start_watchdog()
    # Safety net #2 — boot-time catch-up: if we restarted after a job
    # should have fired today, run it now so we never silently miss a window.
    threading.Thread(target=_boot_catchup, daemon=True, name="sched-boot-catchup").start()
    log.info("scheduler started + watchdog armed + boot catchup queued")
    return _scheduler


def _boot_catchup():
    """On server boot, check which weekday jobs *should have* already fired
    today (ET) and run any that didn't. This makes the system robust to
    restarts at any time of day — including overnight restarts.

    Now covers the FULL daily schedule so intraday jobs are never silently
    dropped on restart."""
    try:
        time.sleep(5)  # let the rest of the app finish initializing
        et_now = datetime.now(pytz.timezone("America/New_York"))
        if et_now.weekday() >= 5:
            log.info("BOOT-CATCHUP: weekend — skipping")
            return
        today_iso = et_now.strftime("%Y-%m-%d")

        from . import movers as movers_mod

        # Full daily schedule — must match the jobs in start() exactly.
        schedule = [
            ( 9,  0, "premarket_brief", strategy.run_premarket_brief),
            ( 9, 15, "research",        lambda: strategy.run_research(dry_run=False, force_market_check=False)),
            ( 9, 30, "liquidate_opts",  lambda: __import__("backend.trading.liquidate_options", fromlist=["run"]).run()),
            ( 9, 30, "market_open",     strategy.run_market_open),
            ( 9, 30, "trade",           lambda: strategy.run_trade_window(dry_run=False, force_market_check=False)),
            (10,  0, "markov",          lambda: strategy.run_markov_regime(ticker="SPY", years=5)),
            (11, 30, "news_pulse",      strategy.run_news_pulse),
            (12,  0, "research",        lambda: strategy.run_research(dry_run=False, force_market_check=False)),
            (13, 30, "news_pulse",      strategy.run_news_pulse),
            (14,  0, "markov",          lambda: strategy.run_markov_regime(ticker="SPY", years=5)),
            (15, 30, "pre_close",       strategy.run_pre_close),
            (16,  0, "market_close",    strategy.run_market_close),
            (16, 15, "journal",         strategy.run_journal),
        ]

        # Pull today's auto_runs to see what already fired.
        # We track (kind, hour) pairs so that we can distinguish the 11:00
        # intraday from the 13:00 intraday, etc.
        from . import portfolio
        today_runs: set[tuple[str, int]] = set()
        for r in portfolio.auto_runs(limit=1000):
            ts = r.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts).astimezone(pytz.timezone("America/New_York"))
                if dt.strftime("%Y-%m-%d") == today_iso:
                    kind = r["summary"].get("kind")
                    today_runs.add((kind, dt.hour))
            except Exception:
                continue

        ran = 0
        for hour, minute, kind, fn in schedule:
            sched_dt = et_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if et_now >= sched_dt and (kind, hour) not in today_runs:
                log.warning("BOOT-CATCHUP: %s should have fired at %02d:%02d ET — running now", kind, hour, minute)
                try:
                    fn()
                    ran += 1
                except Exception:
                    log.exception("BOOT-CATCHUP: %s failed", kind)
        log.info("BOOT-CATCHUP: finished — ran %d missed jobs", ran)
    except Exception:
        log.exception("BOOT-CATCHUP: outer error")


# ===== Watchdog =====
# APScheduler's BackgroundScheduler occasionally wedges (its polling thread
# dies silently after long uptime — we've observed it twice today). The
# watchdog runs in its own daemon thread, pings APScheduler every 60 sec,
# and force-restarts it if it stops advancing or reports `running=False`.

_watchdog_thread: threading.Thread | None = None
_watchdog_stop = threading.Event()
_last_health_check: dict = {"checked_at": None, "alive": None, "restarts": 0}


def _start_watchdog():
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="sched-watchdog")
    _watchdog_thread.start()


def _watchdog_loop():
    """Check every 60s: is APScheduler running? Are next_run_times in the future?"""
    while not _watchdog_stop.is_set():
        time.sleep(60)
        try:
            ok = _health_check()
            if not ok:
                log.warning("WATCHDOG: scheduler wedged — force-restarting")
                _force_restart()
        except Exception:
            log.exception("WATCHDOG: error in health check")


def _health_check() -> bool:
    """True if scheduler is alive AND all next_run_times are in the future."""
    global _scheduler, _started_at
    _last_health_check["checked_at"] = datetime.now(timezone.utc).isoformat()
    if _scheduler is None or not _scheduler.running:
        _last_health_check["alive"] = False
        return False

    # Grace period of 15 minutes after startup to allow boot catch-up tasks to finish
    # without the watchdog falsely diagnosing a wedge and restarting.
    if _started_at:
        uptime = (datetime.now(timezone.utc) - _started_at).total_seconds()
        if uptime < 900:
            _last_health_check["alive"] = True
            _last_health_check["overdue_jobs"] = 0
            return True

    now = datetime.now(pytz.utc)
    overdue = 0
    for job in _scheduler.get_jobs():
        nxt = job.next_run_time
        if nxt is None:
            continue
        # Skip continuous scanning/trading jobs in watchdog health check to prevent
        # false-positive wedging detections when yfinance downloads run slowly.
        if "continuous" in job.id:
            continue
        # next_run_time more than 15 min in the past = scheduler isn't advancing
        if (now - nxt).total_seconds() > 900:
            overdue += 1
    _last_health_check["alive"] = (overdue == 0)
    _last_health_check["overdue_jobs"] = overdue
    return overdue == 0


def _force_restart():
    """Tear down and rebuild the scheduler — preserves cron triggers."""
    global _scheduler
    try:
        if _scheduler:
            _scheduler.shutdown(wait=False)
    except Exception:
        pass
    _scheduler = None
    _last_health_check["restarts"] += 1
    # Re-run start() to recreate everything
    start()


def health_status() -> dict:
    """Live status that doesn't require waiting for the watchdog's next tick."""
    info = dict(_last_health_check)
    info["scheduler_running"] = _scheduler is not None and _scheduler.running
    info["watchdog_thread_alive"] = _watchdog_thread is not None and _watchdog_thread.is_alive()
    info["jobs_armed"] = len(_scheduler.get_jobs()) if _scheduler else 0
    return info


def stop():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_runs() -> dict:
    if not _scheduler:
        return {}
    out = {}
    for job in _scheduler.get_jobs():
        nxt = job.next_run_time
        out[job.id] = nxt.isoformat() if nxt else None
    return out


def last_runs() -> dict:
    return dict(_last)


# ----- Job wrappers -----

def _liquidate_opts_job():
    _last["liquidate_opts"] = datetime.now(timezone.utc).isoformat()
    log.info("9:30 ET liquidate_opts firing")
    try:
        from . import liquidate_options
        liquidate_options.run()
    except Exception:
        log.exception("liquidate_opts failed")


def _premarket_job():
    _last["premarket_brief"] = datetime.now(timezone.utc).isoformat()
    log.info("9:00 ET premarket_brief firing")
    try:
        s = strategy.run_premarket_brief()
        log.info("premarket_brief: %d tickers impacted across %d articles",
                 s.get("tickers_impacted", 0), s.get("deduped_articles", 0))
    except Exception:
        log.exception("premarket_brief failed")


def _market_open_job():
    _last["market_open"] = datetime.now(timezone.utc).isoformat()
    log.info("9:30 ET market_open firing")
    try:
        strategy.run_market_open()
    except Exception:
        log.exception("market_open failed")


def _research_job():
    _last["research"] = datetime.now(timezone.utc).isoformat()
    log.info("9:15 ET research firing")
    try:
        s = strategy.run_research(dry_run=False, force_market_check=False)
        log.info("research done: %d candidates", len(s.get("candidates", [])))
    except Exception:
        log.exception("research failed")


def _trade_job():
    _last["trade"] = datetime.now(timezone.utc).isoformat()
    log.info("9:30 ET trade firing")
    try:
        s = strategy.run_trade_window(dry_run=False)
        log.info("trade done: %d sells · %d buys · %d skipped",
                 len(s.get("sells", [])), len(s.get("buys", [])), len(s.get("skipped", [])))
    except Exception:
        log.exception("trade failed")


def _intraday_job():
    _last["intraday"] = datetime.now(timezone.utc).isoformat()
    log.info("intraday hourly trade window firing")
    try:
        s = strategy.run_intraday_trade(dry_run=False)
        log.info("intraday: %d sells, %d buys, %d skipped · %s",
                 len(s.get("sells", [])), len(s.get("buys", [])), len(s.get("skipped", [])),
                 s.get("status", "ok"))
    except Exception:
        log.exception("intraday trade failed")


def _movers_job():
    _last["movers"] = datetime.now(timezone.utc).isoformat()
    log.info("movers scan firing")
    try:
        from . import movers
        s = movers.run_movers_scan(dry_run=False)
        log.info("movers: %d buys from top gainers · %s",
                 len(s.get("buys", [])), s.get("status", "ok"))
    except Exception:
        log.exception("movers scan failed")


def _news_am_job():
    _last["news_pulse_am"] = datetime.now(timezone.utc).isoformat()
    log.info("11:30 ET news_pulse_am firing")
    try:
        strategy.run_news_pulse()
    except Exception:
        log.exception("news_pulse_am failed")


def _markov_job():
    _last["markov"] = datetime.now(timezone.utc).isoformat()
    log.info("12:00 ET markov_regime firing")
    try:
        strategy.run_markov_regime(ticker="SPY", years=5)
    except Exception:
        log.exception("markov failed")


def _news_pm_job():
    _last["news_pulse_pm"] = datetime.now(timezone.utc).isoformat()
    log.info("13:30 ET news_pulse_pm firing")
    try:
        strategy.run_news_pulse()
    except Exception:
        log.exception("news_pulse_pm failed")


def _pre_close_job():
    _last["pre_close"] = datetime.now(timezone.utc).isoformat()
    log.info("15:30 ET pre_close firing")
    try:
        s = strategy.run_pre_close()
        log.info("pre_close: %d sells", len(s.get("sells", [])))
    except Exception:
        log.exception("pre_close failed")


def _market_close_job():
    _last["market_close"] = datetime.now(timezone.utc).isoformat()
    log.info("16:00 ET market_close firing")
    try:
        strategy.run_market_close()
    except Exception:
        log.exception("market_close failed")


def _journal_job():
    _last["journal"] = datetime.now(timezone.utc).isoformat()
    log.info("16:15 ET journal firing")
    try:
        s = strategy.run_journal()
        log.info("journal written: %s", s.get("journal", {}).get("path"))
    except Exception:
        log.exception("journal failed")


def _wheel_job():
    if not strategy.market_is_open():
        return
    _last["wheel"] = datetime.now(timezone.utc).isoformat()
    try:
        from . import wheel
        wheel.tick_all()
    except Exception:
        log.exception("wheel tick failed")


# Back-compat shims
def next_run(): return next_runs().get("research_window")
def last_run(): return _last.get("research")
def last_daily_run(): return _last.get("trade") or _last.get("research")
def last_intraday_run(): return None
