"""Daily journal — structured Markdown summary written to journal/YYYY-MM-DD.md.

Required by the agent rules: 'always write a journal entry, even on days you
make no trades'. Captures portfolio status, research considered, trades
executed (with 5-question framework answers), and stop-loss exits.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytz

from . import db, portfolio, research

ET = pytz.timezone("America/New_York")
JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "journal"


def _today_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _today_runs() -> list[dict]:
    """All auto_runs whose started_at falls on today's ET date."""
    date_str = _today_str()
    out = []
    for r in portfolio.auto_runs(limit=1000):
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts).astimezone(ET)
            if dt.strftime("%Y-%m-%d") == date_str:
                out.append(r)
        except Exception:
            continue
    return out


def _today_trades() -> list[dict]:
    date_str = _today_str()
    out = []
    for t in portfolio.trades(limit=200):
        try:
            dt = datetime.fromisoformat(t["ts"]).astimezone(ET)
            if dt.strftime("%Y-%m-%d") == date_str:
                out.append(t)
        except Exception:
            continue
    return out


def write_today(force_market_check: bool = False) -> dict:
    """Compose and write today's journal. Returns {path, preview}."""
    date_str = _today_str()
    state = portfolio.get_state()
    trades_today = _today_trades()
    runs_today = _today_runs()
    research_today = research.get_today()

    md = _compose_markdown(date_str, state, trades_today, runs_today, research_today)

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{date_str}.md"
    path.write_text(md)

    preview = md[:400]
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO journals (date, ts, path, preview) VALUES (?, ?, ?, ?)",
            (date_str, datetime.now(timezone.utc).isoformat(), str(path), preview),
        )
    return {"date": date_str, "path": str(path), "preview": preview, "bytes": len(md)}


def _compose_markdown(date_str, state, trades_today, runs_today, research_today) -> str:
    L: list[str] = []
    L.append(f"# Trading Journal — {date_str}")
    L.append("")
    L.append(f"_Written at {datetime.now(ET).isoformat()}_")
    L.append("")

    # ========== Portfolio status ==========
    L.append("## Portfolio Status")
    L.append("")
    L.append(f"- **Cash**: ${state['cash']:,.2f}")
    L.append(f"- **Market Value**: ${state['market_value']:,.2f}")
    L.append(f"- **Equity**: ${state['equity']:,.2f}")
    L.append(f"- **Total Return**: {state['total_pnl_pct']:+.2f}% (${state['total_pnl']:+,.2f}) vs ${state['initial_cash']:,.0f} starting capital")
    L.append(f"- **Today's P&L**: {state['todays_pnl_pct']:+.2f}% (${state['todays_pnl']:+,.2f})")
    L.append(f"- **Capital Deployed**: ${state['cost_basis']:,.2f} ({state['cost_basis']/state['equity']*100:.1f}% of equity)")
    L.append(f"- **Open Positions**: {len(state['positions'])}")
    L.append("")

    # ========== Open positions ==========
    if state["positions"]:
        L.append("## Open Positions")
        L.append("")
        L.append("| Ticker | Shares | Avg Cost | Last | Market Value | P&L $ | P&L % | Opened |")
        L.append("|--------|--------|----------|------|-------------:|------:|------:|--------|")
        for p in state["positions"]:
            L.append(
                f"| **{p['ticker']}** | {p['shares']:.4f} | ${p['avg_cost']:.2f} | ${p['last']:.2f} | "
                f"${p['market_value']:,.2f} | ${p['unrealized_pnl']:+,.2f} | "
                f"{p['unrealized_pnl_pct']:+.2f}% | {p['opened_at'][:10]} |"
            )
        L.append("")

    # ========== Research window (9:45 AM ET) ==========
    L.append("## Research Window — 9:45 AM ET")
    L.append("")
    if not research_today:
        L.append("_No research recorded for today._")
    else:
        cands = research_today.get("candidates", [])
        L.append(f"Scanned the universe and produced {len(cands)} candidate(s) meeting the buy bar.")
        L.append("")
        if research_today.get("status") and research_today["status"] != "ok":
            L.append(f"_Status_: `{research_today['status']}`")
            L.append("")
        for c in cands:
            L.append(f"### {c['ticker']} @ ${c['decision_price']:.2f}")
            L.append(f"- Score: **{c['score']:+.2f}** · Bias: **{c['bias']}** · Confidence: **{int(c['confidence']*100)}%**")
            d = c.get("decisions", {})
            L.append("")
            L.append("**Decision Framework:**")
            L.append(f"1. **Cash on hand:** {d.get('q1_cash','—')}")
            L.append(f"2. **Open positions:** {d.get('q2_open_positions','—')}")
            L.append(f"3. **Recent news:** {d.get('q3_news','—')}")
            L.append(f"4. **Moving averages:** {d.get('q4_moving_averages','—')}")
            L.append(f"5. **Downside risk:** {d.get('q5_risk','—')}")
            if c.get("reasons"):
                L.append("")
                L.append("**Top factor reasons:**")
                for r in c["reasons"][:5]:
                    L.append(f"  - {r}")
            L.append("")
    L.append("")

    # ========== Trade window (10:00 AM ET) ==========
    L.append("## Trade Window — 10:00 AM ET")
    L.append("")
    trade_runs = [r for r in runs_today if r["summary"].get("kind") == "trade"]
    if not trade_runs:
        L.append("_No trade window run recorded._")
        L.append("")
    else:
        for run in trade_runs:
            s = run["summary"]
            sells = s.get("sells", [])
            buys = s.get("buys", [])
            skipped = s.get("skipped", [])

            if sells:
                L.append("### Sells")
                for x in sells:
                    L.append(f"- **SELL {x.get('ticker')}** {x.get('shares',0):.4f} sh @ ${x.get('price',0):.2f} — realized P&L ${x.get('realized_pnl',0):+,.2f}")
                    L.append(f"  - Reason: {x.get('reason','')}")
                L.append("")
            if buys:
                L.append("### Buys")
                for x in buys:
                    L.append(f"- **BUY {x.get('ticker')}** {x.get('shares',0):.4f} sh @ ${x.get('price',0):.2f} — cost ${x.get('cost',0):,.2f}")
                    L.append(f"  - Reason: {x.get('reason','')}")
                L.append("")
            if skipped:
                L.append("### Skipped")
                for x in skipped:
                    L.append(f"- {x.get('ticker','?')}: {x.get('reason','')}")
                L.append("")
            errs = s.get("errors", [])
            if errs:
                L.append("### Errors")
                for e in errs:
                    L.append(f"- {e}")
                L.append("")

    # ========== Today's trade log ==========
    if trades_today:
        L.append("## Today's Trade Log")
        L.append("")
        L.append("| Time (UTC) | Side | Ticker | Shares | Price | Net $ | Auto | Reason |")
        L.append("|------------|------|--------|--------|------:|------:|------|--------|")
        for t in trades_today:
            L.append(
                f"| {t['ts'][:19]} | {t['side']} | {t['ticker']} | {t['shares']:.4f} | "
                f"${t['price']:.2f} | ${t['proceeds']:+,.2f} | {'Y' if t.get('auto') else 'N'} | {(t.get('reason') or '')[:80]} |"
            )
        L.append("")

    # ========== Notes / system ==========
    L.append("## Notes")
    L.append("")
    L.append(f"- Journal generated at {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    L.append(f"- {len(trades_today)} trade(s) executed today; {len(runs_today)} agent run(s) completed.")
    L.append("- This system is paper-only and rule-based. Not financial advice.")
    L.append("")
    return "\n".join(L)


def read(date_str: str) -> str | None:
    path = JOURNAL_DIR / f"{date_str}.md"
    if not path.exists():
        return None
    return path.read_text()


def list_recent(limit: int = 30) -> list[dict]:
    with db._lock, db.connect() as c:
        rows = c.execute(
            "SELECT date, ts, path, preview FROM journals ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
