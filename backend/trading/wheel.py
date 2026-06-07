"""The Wheel — automated cash-secured-put + covered-call cycle on Alpaca paper.

State machine per underlying:

    IDLE ──► SELL_PUT_OPEN ──assigned──► SELL_CALL_OPEN ──called away──► IDLE
              ▲   │                            ▲   │
              │   └─ 50% profit or expired ────┤   └─ 50% profit or expired ─┐
              │                                │                             │
              └────────────── (place new put) ─┴──── (place new call) ───────┘

Rules enforced:
  • Cash-secured: requires (strike × 100) cash on hand before selling a put.
  • Never sell a call below cost basis.
  • 50% profit early-close on both legs.
  • Premium tracked across cycles per ticker.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import db, notify, options as opts
from ..data import prices as price_mod

log = logging.getLogger("wheel")

# Strategy knobs (all per agent rules)
TARGET_PUT_STRIKE_PCT = 0.90       # 10% below current price
TARGET_CALL_STRIKE_PCT = 1.10      # 10% above cost basis (or current, whichever higher)
PROFIT_TAKE_PCT = 0.50             # close at 50% of max profit
MIN_DTE = 14                       # 2 weeks out
MAX_DTE = 28                       # 4 weeks out
SHARES_PER_CONTRACT = opts.SHARES_PER_CONTRACT  # 100

VALID_START_STATES = {"IDLE", "SELL_PUT_OPEN", "SELL_CALL_OPEN", "STOPPED"}


# ===========================================================================
#  Public API
# ===========================================================================

def start(ticker: str) -> dict:
    """Start a wheel on a ticker. Validates cash sufficiency for the CSP."""
    ticker = ticker.upper()
    quote = price_mod.fetch_quote(ticker)
    target_strike = quote["last"] * TARGET_PUT_STRIKE_PCT
    required_cash = target_strike * SHARES_PER_CONTRACT

    # Check cash
    from . import portfolio
    state = portfolio.get_state()
    if state["cash"] < required_cash:
        raise ValueError(
            f"Insufficient cash for cash-secured put on {ticker}: "
            f"need ~${required_cash:,.2f} (strike ${target_strike:.2f} × 100), "
            f"have ${state['cash']:,.2f}"
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    with db._lock, db.connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO wheel_runs
               (ticker, status, started_at, premium_collected, realized_pnl, cycles)
               VALUES (?, 'IDLE', ?, 0, 0, 0)""",
            (ticker, now_iso),
        )
    log.info("Wheel started on %s — cash required ~$%.2f", ticker, required_cash)
    return {
        "ticker": ticker, "status": "IDLE", "started_at": now_iso,
        "required_cash_for_csp": round(required_cash, 2),
        "next_action": "First tick will place a CSP near strike ~$%.2f, 2-4 weeks out" % target_strike,
    }


def stop(ticker: str) -> dict:
    """Stop the wheel (does NOT close existing option positions — manage them manually)."""
    with db._lock, db.connect() as c:
        c.execute("UPDATE wheel_runs SET status='STOPPED' WHERE ticker = ?", (ticker.upper(),))
    return {"ticker": ticker.upper(), "status": "STOPPED",
            "note": "Existing option positions left open — close them via the Alpaca dashboard or your sell button if you want them gone."}


def remove(ticker: str) -> dict:
    """Delete the wheel record entirely."""
    with db._lock, db.connect() as c:
        c.execute("DELETE FROM wheel_runs WHERE ticker = ?", (ticker.upper(),))
    return {"ticker": ticker.upper(), "removed": True}


def get_state(ticker: str) -> dict | None:
    with db._lock, db.connect() as c:
        r = c.execute("SELECT * FROM wheel_runs WHERE ticker = ?", (ticker.upper(),)).fetchone()
    return dict(r) if r else None


def list_active() -> list[dict]:
    with db._lock, db.connect() as c:
        rows = c.execute(
            "SELECT * FROM wheel_runs WHERE status != 'STOPPED' ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


def list_all() -> list[dict]:
    with db._lock, db.connect() as c:
        rows = c.execute("SELECT * FROM wheel_runs ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def list_legs(ticker: str | None = None, limit: int = 100) -> list[dict]:
    with db._lock, db.connect() as c:
        if ticker:
            rows = c.execute(
                "SELECT * FROM wheel_legs WHERE ticker = ? ORDER BY id DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM wheel_legs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def summary() -> dict:
    actives = list_active()
    all_runs = list_all()
    with db._lock, db.connect() as c:
        total_premium = float(c.execute(
            "SELECT COALESCE(SUM(premium_collected), 0) AS t FROM wheel_runs"
        ).fetchone()["t"])
    return {
        "active_wheels": len(actives),
        "total_wheels": len(all_runs),
        "total_premium_collected": round(total_premium, 2),
        "wheels": all_runs,
    }


def tick(ticker: str) -> dict:
    """Single tick of the wheel — called every 15 min during market hours."""
    state = get_state(ticker)
    if not state:
        return {"error": f"No wheel registered for {ticker.upper()}"}
    if state["status"] == "STOPPED":
        return {"ticker": ticker.upper(), "skipped": "stopped"}

    ticker = state["ticker"]
    try:
        if state["status"] == "IDLE":
            return _place_put(state)
        if state["status"] == "SELL_PUT_OPEN":
            return _handle_put_open(state)
        if state["status"] == "SELL_CALL_OPEN":
            return _handle_call_open(state)
    except Exception as e:
        log.exception("Wheel tick failed for %s", ticker)
        return {"ticker": ticker, "error": str(e)}

    return {"ticker": ticker, "skipped": f"unknown state {state['status']}"}


def sync_with_alpaca() -> dict:
    """Scan all open option positions in the Alpaca account and synchronize
    the database `wheel_runs` status to match the actual exchange positions.
    Specifically recovers IDLE wheels when an active short contract exists.
    """
    client = opts._trading_client()
    try:
        positions = client.get_all_positions()
    except Exception as e:
        log.error("sync_with_alpaca: failed to fetch positions: %s", e)
        return {"status": "error", "message": str(e)}

    synced = []
    for p in positions:
        symbol = p.symbol
        # OCC format option symbols always have at least 15 chars (e.g. AAPL260618P00280000)
        if len(symbol) < 15:
            continue
            
        parsed = opts._parse_occ_symbol(symbol)
        if not parsed:
            continue
            
        ticker = parsed["underlying"]
        state = get_state(ticker)
        # We only sync if the wheel is registered
        if not state:
            continue
            
        # Check if the database state is out of sync
        if state["current_option_symbol"] != symbol or state["status"] == "IDLE":
            qty = float(p.qty)
            side_status = "SELL_PUT_OPEN" if parsed["type"] == "P" else "SELL_CALL_OPEN"
            
            # A written put/call contract has a negative quantity
            if qty < 0:
                _update_state(
                    ticker,
                    status=side_status,
                    current_option_symbol=symbol,
                    current_option_strike=parsed["strike"],
                    current_option_expiration=parsed["expiration"],
                    current_option_entry_premium=float(p.avg_entry_price or 0),
                    last_check_at=datetime.now(timezone.utc).isoformat(),
                )
                synced.append(ticker)
                log.info("sync_with_alpaca: synced %s to %s with contract %s", ticker, side_status, symbol)
                
    return {"status": "ok", "synced": synced}


def tick_all() -> list[dict]:
    try:
        sync_with_alpaca()
    except Exception as e:
        log.exception("Wheel sync failed during tick_all: %s", e)

    out = []
    for active in list_active():
        out.append(tick(active["ticker"]))
    return out



# ===========================================================================
#  Internal helpers
# ===========================================================================

def _log_leg(ticker, leg_type, contract_symbol, side, strike, expiration, qty, price, premium_delta, reason):
    with db._lock, db.connect() as c:
        c.execute(
            """INSERT INTO wheel_legs
               (ts, ticker, leg_type, contract_symbol, side, strike, expiration, qty, price, premium_delta, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), ticker, leg_type, contract_symbol, side,
             strike, expiration, qty, price, premium_delta, reason),
        )


def _update_state(ticker, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [ticker]
    with db._lock, db.connect() as c:
        c.execute(f"UPDATE wheel_runs SET {cols} WHERE ticker = ?", vals)


def _place_put(state: dict) -> dict:
    ticker = state["ticker"]
    quote = price_mod.fetch_quote(ticker)
    target = quote["last"] * TARGET_PUT_STRIKE_PCT

    contract = opts.find_contract(ticker, "put", target, MIN_DTE, MAX_DTE)
    if not contract:
        return {"ticker": ticker, "error": f"No put contract found near ${target:.2f} ({MIN_DTE}-{MAX_DTE} DTE)"}

    q = opts.get_quote(contract["symbol"])
    if not q or not q["mid"]:
        return {"ticker": ticker, "error": f"No quote for {contract['symbol']}"}

    # Cash check just before placing
    from . import portfolio
    pst = portfolio.get_state()
    required = contract["strike"] * SHARES_PER_CONTRACT
    if pst["cash"] < required:
        return {"ticker": ticker, "error": f"insufficient cash: need ${required:.2f}, have ${pst['cash']:.2f}"}

    order = opts.sell_to_open(contract["symbol"], qty=1, limit_price=q["mid"])
    premium_received = q["mid"] * SHARES_PER_CONTRACT

    _log_leg(ticker, "PUT", contract["symbol"], "SELL_TO_OPEN",
             contract["strike"], contract["expiration"], 1, q["mid"], premium_received,
             f"Sell-to-open CSP, strike ${contract['strike']:.2f}, exp {contract['expiration']}")
    _update_state(ticker,
        status="SELL_PUT_OPEN",
        current_option_symbol=contract["symbol"],
        current_option_strike=contract["strike"],
        current_option_expiration=contract["expiration"],
        current_option_entry_premium=q["mid"],
        last_check_at=datetime.now(timezone.utc).isoformat(),
    )
    notify.send(f"WHEEL: SELL PUT {ticker}",
                f"Strike ${contract['strike']:.2f} exp {contract['expiration']} @ ${q['mid']:.2f} (${premium_received:.0f} premium)")
    return {
        "ticker": ticker, "action": "SELL_PUT",
        "contract": contract["symbol"], "strike": contract["strike"],
        "expiration": contract["expiration"], "premium_received": round(premium_received, 2),
        "order_status": order["status"],
    }


def _place_call(state: dict) -> dict:
    ticker = state["ticker"]
    cost_basis = state.get("underlying_cost_basis") or 0
    quote = price_mod.fetch_quote(ticker)
    # never sell call below cost basis
    target = max(cost_basis * 1.005 if cost_basis else 0, quote["last"] * TARGET_CALL_STRIKE_PCT)

    contract = opts.find_contract(ticker, "call", target, MIN_DTE, MAX_DTE)
    if not contract:
        return {"ticker": ticker, "error": f"No call contract found near ${target:.2f}"}
    if cost_basis and contract["strike"] < cost_basis:
        return {"ticker": ticker, "error":
                f"Closest call strike ${contract['strike']:.2f} below cost basis ${cost_basis:.2f} — skipping (will retry next tick)"}

    q = opts.get_quote(contract["symbol"])
    if not q or not q["mid"]:
        return {"ticker": ticker, "error": f"No quote for {contract['symbol']}"}

    order = opts.sell_to_open(contract["symbol"], qty=1, limit_price=q["mid"])
    premium_received = q["mid"] * SHARES_PER_CONTRACT

    _log_leg(ticker, "CALL", contract["symbol"], "SELL_TO_OPEN",
             contract["strike"], contract["expiration"], 1, q["mid"], premium_received,
             f"Sell-to-open covered call, strike ${contract['strike']:.2f}, exp {contract['expiration']}")
    _update_state(ticker,
        status="SELL_CALL_OPEN",
        current_option_symbol=contract["symbol"],
        current_option_strike=contract["strike"],
        current_option_expiration=contract["expiration"],
        current_option_entry_premium=q["mid"],
        last_check_at=datetime.now(timezone.utc).isoformat(),
    )
    notify.send(f"WHEEL: SELL CALL {ticker}",
                f"Strike ${contract['strike']:.2f} exp {contract['expiration']} @ ${q['mid']:.2f} (${premium_received:.0f} premium)")
    return {
        "ticker": ticker, "action": "SELL_CALL",
        "contract": contract["symbol"], "strike": contract["strike"],
        "expiration": contract["expiration"], "premium_received": round(premium_received, 2),
        "order_status": order["status"],
    }


def _close_at_profit(state: dict, reason: str) -> dict:
    ticker = state["ticker"]
    sym = state["current_option_symbol"]
    entry = state["current_option_entry_premium"] or 0

    q = opts.get_quote(sym)
    if not q or not q["mid"]:
        return {"ticker": ticker, "error": f"No quote for {sym} — can't close"}

    order = opts.buy_to_close(sym, qty=1, limit_price=q["mid"])
    net_per_share = entry - q["mid"]
    net_premium = net_per_share * SHARES_PER_CONTRACT

    leg_type = "PUT" if state["status"] == "SELL_PUT_OPEN" else "CALL"
    _log_leg(ticker, leg_type, sym, "BUY_TO_CLOSE",
             state.get("current_option_strike"), state.get("current_option_expiration"),
             1, q["mid"], -q["mid"] * SHARES_PER_CONTRACT, reason)
    with db._lock, db.connect() as c:
        c.execute(
            "UPDATE wheel_runs SET premium_collected = premium_collected + ?, cycles = cycles + 1, "
            "current_option_symbol = NULL, current_option_entry_premium = NULL "
            "WHERE ticker = ?",
            (net_premium, ticker),
        )
    notify.send(f"WHEEL: CLOSE {leg_type} {ticker}",
                f"Net premium kept: ${net_premium:+.0f} ({reason})")
    return {"ticker": ticker, "action": f"BUY_TO_CLOSE_{leg_type}",
            "net_premium": round(net_premium, 2), "reason": reason,
            "order_status": order["status"]}


def _handle_put_open(state: dict) -> dict:
    ticker = state["ticker"]
    held = opts.shares_held(ticker)

    # === Assignment detected (we got the shares) ===
    if held >= SHARES_PER_CONTRACT:
        from . import portfolio
        ps = portfolio.get_state()
        cost_basis = next((p["avg_cost"] for p in ps["positions"] if p["ticker"] == ticker), None)
        _log_leg(ticker, "PUT", state.get("current_option_symbol", ""), "ASSIGNED",
                 state.get("current_option_strike"), state.get("current_option_expiration"),
                 1, state.get("current_option_strike", 0) or 0, 0,
                 f"Put assigned — now hold {held} shares at ${cost_basis:.2f} cost basis"
                 if cost_basis else f"Put assigned — now hold {held} shares")
        _update_state(ticker, status="SELL_CALL_OPEN", underlying_cost_basis=cost_basis,
                      current_option_symbol=None, current_option_entry_premium=None)
        notify.send(f"WHEEL: ASSIGNED {ticker}",
                    f"Bought {held} shares at ~${cost_basis:.2f}. Placing covered call.")
        return _place_call(get_state(ticker))

    # === Option no longer open ===
    sym = state["current_option_symbol"]
    pos = opts.get_open_option_position(sym) if sym else None
    if pos is None:
        # BEFORE deciding the option expired, check if there's still a working order
        # for the same contract. If there is, the put just hasn't filled yet — hold.
        if sym and opts.has_open_order(sym):
            _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
            return {"ticker": ticker, "action": "WAIT_PUT_FILL", "contract": sym,
                    "note": "Sell-to-open put order still working — not duplicating."}
        _log_leg(ticker, "PUT", sym or "?", "EXPIRED",
                 state.get("current_option_strike"), state.get("current_option_expiration"),
                 1, 0, (state.get("current_option_entry_premium") or 0) * SHARES_PER_CONTRACT,
                 "Put expired worthless / closed — keep full entry premium")
        kept = (state.get("current_option_entry_premium") or 0) * SHARES_PER_CONTRACT
        with db._lock, db.connect() as c:
            c.execute(
                "UPDATE wheel_runs SET premium_collected = premium_collected + ?, cycles = cycles + 1, "
                "status = 'IDLE', current_option_symbol = NULL, current_option_entry_premium = NULL, "
                "current_option_strike = NULL, current_option_expiration = NULL "
                "WHERE ticker = ?", (kept, ticker)
            )
        # Now attempt to place a new put; if it fails, we stay IDLE (safe for retry)
        return _place_put(get_state(ticker))

    # === 50% profit early-close check ===
    entry = state["current_option_entry_premium"] or 0
    q = opts.get_quote(state["current_option_symbol"])
    if q and entry and q["mid"] <= entry * (1 - PROFIT_TAKE_PCT):
        if opts.has_open_order(state["current_option_symbol"]):
            _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
            return {"ticker": ticker, "action": "WAIT_CLOSE_FILL", "contract": state["current_option_symbol"],
                    "note": "Buy-to-close order already working — not duplicating."}
        close_res = _close_at_profit(state, f"50% profit (entry ${entry:.2f} → mid ${q['mid']:.2f})")
        if "error" in close_res:
            return close_res
        return _place_put(get_state(ticker))

    _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
    return {"ticker": ticker, "action": "HOLD_PUT",
            "option_mid": q["mid"] if q else None,
            "entry_premium": entry,
            "pct_to_profit_take": round((entry - (q["mid"] if q else entry)) / entry * 100, 1) if entry else None}


def _handle_call_open(state: dict) -> dict:
    ticker = state["ticker"]
    held = opts.shares_held(ticker)

    # === Shares called away (call exercised) ===
    if held < SHARES_PER_CONTRACT:
        _log_leg(ticker, "CALL", state.get("current_option_symbol") or "?", "CALLED_AWAY",
                 state.get("current_option_strike"), state.get("current_option_expiration"),
                 1, state.get("current_option_strike", 0) or 0, 0,
                 "Shares called away — wheel returns to put-selling stage")
        _update_state(ticker, status="IDLE", underlying_cost_basis=None,
                      current_option_symbol=None, current_option_entry_premium=None)
        notify.send(f"WHEEL: CALLED AWAY {ticker}",
                    f"Shares sold at ${state.get('current_option_strike', 0):.2f}. Starting new put cycle.")
        return _place_put(get_state(ticker))

    sym2 = state["current_option_symbol"]
    pos = opts.get_open_option_position(sym2) if sym2 else None
    if pos is None:
        # No current option symbol — previous placement failed or state was reset.
        # Just try to place a new call without logging another spurious "EXPIRED".
        if not sym2:
            result = _place_call(state)
            if "error" in result:
                log.warning("Wheel %s: place_call retry failed: %s", ticker, result["error"])
            return result
        # Same fix as put path — if a sell-to-open order is still working, just wait
        if opts.has_open_order(sym2):
            _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
            return {"ticker": ticker, "action": "WAIT_CALL_FILL", "contract": sym2,
                    "note": "Sell-to-open call order still working — not duplicating."}
        _log_leg(ticker, "CALL", sym2, "EXPIRED",
                 state.get("current_option_strike"), state.get("current_option_expiration"),
                 1, 0, (state.get("current_option_entry_premium") or 0) * SHARES_PER_CONTRACT,
                 "Call expired worthless — keep full entry premium")
        kept = (state.get("current_option_entry_premium") or 0) * SHARES_PER_CONTRACT
        with db._lock, db.connect() as c:
            c.execute(
                "UPDATE wheel_runs SET premium_collected = premium_collected + ?, cycles = cycles + 1, "
                "status = 'SELL_CALL_OPEN', current_option_symbol = NULL, current_option_entry_premium = NULL, "
                "current_option_strike = NULL, current_option_expiration = NULL "
                "WHERE ticker = ?", (kept, ticker)
            )
        # Attempt to place a new call; if it fails, state stays SELL_CALL_OPEN with no symbol (retry next tick)
        result = _place_call(get_state(ticker))
        if "error" in result:
            log.warning("Wheel %s: place_call failed after expiry: %s — will retry next tick", ticker, result["error"])
        return result

    entry = state["current_option_entry_premium"] or 0
    q = opts.get_quote(state["current_option_symbol"])
    if q and entry and q["mid"] <= entry * (1 - PROFIT_TAKE_PCT):
        if opts.has_open_order(state["current_option_symbol"]):
            _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
            return {"ticker": ticker, "action": "WAIT_CLOSE_FILL", "contract": state["current_option_symbol"],
                    "note": "Buy-to-close order already working — not duplicating."}
        close_res = _close_at_profit(state, f"50% profit (entry ${entry:.2f} → mid ${q['mid']:.2f})")
        if "error" in close_res:
            return close_res
        return _place_call(get_state(ticker))

    _update_state(ticker, last_check_at=datetime.now(timezone.utc).isoformat())
    return {"ticker": ticker, "action": "HOLD_CALL",
            "option_mid": q["mid"] if q else None, "entry_premium": entry}
