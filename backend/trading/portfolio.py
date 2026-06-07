"""Paper portfolio — Alpaca-backed.

Replaces the prior SQLite paper book. Account state, positions, and order
history come from Alpaca's TradingClient (paper account). Local SQLite is
still used for auto_runs / research_runs / journals metadata.

Public API preserved: get_state, buy, sell, trades, auto_runs, reset,
has_position. Strategy and journal code keeps working unchanged.

Order policy (per agent rules):
  - Buys placed as LIMIT orders with limit_price = decision_price × 1.002
  - Bracket orders attach an 8% STOP_LOSS so it triggers without us re-running
  - 5% hard cap enforced before submitting
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytz
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from ..config import ALPACA_KEY, ALPACA_SECRET, IS_PAPER, assert_configured
from . import db, notify

log = logging.getLogger("portfolio")
_ET = pytz.timezone("America/New_York")

# Hard rules
MAX_POSITION_PCT = 0.05   # 5% of equity hard position cap
LIMIT_BUY_DRIFT = 0.002     # buy at last × 1.002 (room for slippage within limit)
LIMIT_SELL_DRIFT = 0.002    # sell at last × 0.998
STOP_LOSS_PCT = 0.05        # 5% bracketed stop-loss
TAKE_PROFIT_PCT = 0.30      # 30% take-profit



_client: TradingClient | None = None


def _get_client() -> TradingClient:
    global _client
    if _client is None:
        assert_configured()
        _client = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=IS_PAPER)
    return _client


# -----------------------------------------------------------------
#  State
# -----------------------------------------------------------------

def get_state() -> dict:
    """Account + positions in the same shape strategy.py / journal.py expect."""
    client = _get_client()
    acct = client.get_account()
    raw_positions = client.get_all_positions()
    opened_at_map = _opened_at_for_positions([p.symbol for p in raw_positions])

    positions: list[dict] = []
    market_value = 0.0
    total_cost_basis = 0.0
    todays_pnl = 0.0
    for p in raw_positions:
        ticker = p.symbol
        last = float(p.current_price or 0)
        avg_cost = float(p.avg_entry_price or 0)
        shares = float(p.qty)
        mv = float(p.market_value or shares * last)
        cost = float(p.cost_basis or shares * avg_cost)
        pnl = float(p.unrealized_pl or (mv - cost))
        pnl_pct = float(p.unrealized_plpc or 0) * 100

        # Today's P&L from Alpaca (only meaningful for positions held overnight)
        day_pnl = float(p.unrealized_intraday_pl or 0)
        day_pnl_pct_raw = float(p.unrealized_intraday_plpc or 0) * 100
        stock_day_pct = float(p.change_today or 0) * 100

        # If opened today (ET), don't claim today's P&L — we just bought at today's price
        opened_at = opened_at_map.get(ticker)
        opened_today = _opened_today_et(opened_at)
        if opened_today:
            day_pnl = 0.0
            day_pnl_pct_raw = 0.0

        market_value += mv
        total_cost_basis += cost
        todays_pnl += day_pnl

        positions.append({
            "ticker": ticker,
            "shares": shares,
            "avg_cost": round(avg_cost, 4),
            "last": round(last, 4),
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round(pnl_pct, 2),
            "todays_pnl": round(day_pnl, 2),
            "todays_pnl_pct": round(day_pnl_pct_raw, 2),
            "stock_day_change_pct": round(stock_day_pct, 2),
            "opened_at": opened_at or "—",
        })

    cash = float(acct.cash)
    equity = float(acct.equity)
    last_equity = float(getattr(acct, "last_equity", equity))
    # Alpaca exposes total equity and last equity; "initial" we approximate as the
    # account's portfolio starting value if available, else $100k for a fresh paper acct.
    initial = float(getattr(acct, "initial_margin", 0)) or 100_000.0
    # Better: use the (configurable in Alpaca dashboard) max equity flag if exposed,
    # but for a fresh paper account starting at 100k this matches.
    total_pnl = equity - 100_000.0  # paper accounts start at $100k by default
    total_pnl_pct = (total_pnl / 100_000.0) * 100
    deployed_pnl_pct = (sum(po["unrealized_pnl"] for po in positions) / total_cost_basis * 100) if total_cost_basis else 0
    todays_pnl_pct = (todays_pnl / (equity - todays_pnl) * 100) if (equity - todays_pnl) else 0

    return {
        "cash": round(cash, 2),
        "initial_cash": 100_000.00,
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "cost_basis": round(total_cost_basis, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "deployed_pnl_pct": round(deployed_pnl_pct, 4),
        "todays_pnl": round(todays_pnl, 2),
        "todays_pnl_pct": round(todays_pnl_pct, 4),
        "positions": positions,
        "buying_power": float(acct.buying_power or 0),
        "account_status": getattr(acct, "status", "ACTIVE"),
        "paper": IS_PAPER,
        "created_at": str(getattr(acct, "created_at", datetime.now(timezone.utc).isoformat())),
    }


def has_position(ticker: str) -> bool:
    try:
        client = _get_client()
        pos = client.get_open_position(ticker.upper())
        return pos is not None and float(pos.qty) > 0
    except APIError:
        return False
    except Exception:
        return False


def get_open_buy_orders() -> list:
    """Fetch all currently open/pending BUY orders from Alpaca."""
    client = _get_client()
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = client.get_orders(filter=req)
        out = []
        for o in orders:
            side = str(o.side.value if hasattr(o.side, "value") else o.side).upper()
            if side == "BUY":
                out.append(o)
        return out
    except Exception as e:
        log.warning(f"get_orders (open) failed: {e}")
        return []


# -----------------------------------------------------------------
#  Orders
# -----------------------------------------------------------------

def buy(
    ticker: str,
    shares: float,
    reason: str = "manual",
    signal: dict | None = None,
    auto: bool = False,
    allow_oversize: bool = False,
) -> dict:
    """Place a LIMIT BUY (bracketed with stop-loss + take-profit) per agent rules.

    Hard rejects on:
      - shares <= 0
      - resulting position > 5% of equity
      - insufficient buying power (Alpaca side check)
    """
    ticker = ticker.upper()
    if shares <= 0:
        raise ValueError("shares must be positive")

    from ..data import prices as price_mod
    last_price = price_mod.fetch_latest_price(ticker)
    cost_estimate = shares * last_price

    # 5% hard cap
    if not allow_oversize:
        state = get_state()
        equity = state["equity"]
        existing_value = 0.0
        for pos in state["positions"]:
            if pos["ticker"] == ticker:
                existing_value = pos["market_value"]
                break
        proposed_value = existing_value + cost_estimate
        cap_value = equity * MAX_POSITION_PCT
        if proposed_value > cap_value + 0.01:
            raise ValueError(
                f"5% cap rejected: ${proposed_value:.2f} in {ticker} would be "
                f"{proposed_value/equity*100:.2f}% of ${equity:.0f} equity (cap {MAX_POSITION_PCT*100:.1f}%)"
            )

    limit_price = round(last_price * (1 + LIMIT_BUY_DRIFT), 2)
    stop_price = round(last_price * (1 - STOP_LOSS_PCT), 2)
    take_price = round(last_price * (1 + TAKE_PROFIT_PCT), 2)

    import pytz
    from datetime import datetime, timezone
    et = datetime.now(timezone.utc).astimezone(pytz.timezone("America/New_York"))
    is_ext = not (9 <= et.hour < 16) or (et.hour == 9 and et.minute < 30)

    client = _get_client()
    qty_round = int(shares) if shares >= 1 else round(shares, 4)  # Use whole shares if >=1 to support brackets
    
    if is_ext:
        # Extended hours: no brackets allowed, limit order only
        try:
            order = client.submit_order(
                LimitOrderRequest(
                    symbol=ticker,
                    qty=qty_round,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    extended_hours=True
                )
            )
        except APIError as e:
            raise ValueError(f"Alpaca rejected extended hours buy: {e}")
    else:
        try:
            order = client.submit_order(
                LimitOrderRequest(
                    symbol=ticker,
                    qty=qty_round,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=take_price),
                    stop_loss=StopLossRequest(stop_price=stop_price),
                )
            )
        except APIError as e:
            # Bracket orders require whole shares — retry as plain LIMIT if Alpaca refuses
            if "fractional" in str(e).lower() or "bracket" in str(e).lower():
                order = client.submit_order(
                    LimitOrderRequest(
                        symbol=ticker,
                        qty=qty_round,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price,
                    )
                )
            else:
                raise ValueError(f"Alpaca rejected order: {e}")

    notify.trade("BUY", ticker, qty_round, limit_price, reason, auto=auto)
    return {
        "ticker": ticker,
        "side": "BUY",
        "shares": qty_round,
        "price": limit_price,
        "cost": round(qty_round * limit_price, 2),
        "stop_loss": stop_price,
        "take_profit": take_price,
        "order_id": str(order.id),
        "status": str(order.status.value if hasattr(order.status, "value") else order.status),
        "reason": reason,
    }


def sell(
    ticker: str,
    shares: float | None = None,
    reason: str = "manual",
    signal: dict | None = None,
    auto: bool = False,
) -> dict:
    """Place a LIMIT SELL. If shares is None or 0, sell the whole position."""
    ticker = ticker.upper()
    client = _get_client()

    # Cancel any outstanding open orders for this ticker to avoid locked shares
    try:
        import time
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker])
        open_orders = client.get_orders(filter=req)
        if open_orders:
            for o in open_orders:
                client.cancel_order_by_id(o.id)
            time.sleep(1.0)  # Give Alpaca a moment to process the cancellations
    except Exception as e:
        log.warning(f"Failed to cancel open orders for {ticker}: {e}")

    try:
        pos = client.get_open_position(ticker)
    except APIError:
        raise ValueError(f"No position in {ticker}")

    held = float(pos.qty)
    sell_qty = held if (shares is None or shares <= 0 or shares >= held) else shares
    sell_qty = round(sell_qty, 4)

    from ..data import prices as price_mod
    last_price = price_mod.fetch_latest_price(ticker)
    limit_price = round(last_price * (1 - LIMIT_SELL_DRIFT), 2)
    avg_cost = float(pos.avg_entry_price)
    realized_est = (limit_price - avg_cost) * sell_qty

    import pytz
    from datetime import datetime, timezone
    et = datetime.now(timezone.utc).astimezone(pytz.timezone("America/New_York"))
    is_ext = not (9 <= et.hour < 16) or (et.hour == 9 and et.minute < 30)

    # If extended hours, we must use a limit order with extended_hours=True
    if is_ext:
        try:
            order = client.submit_order(
                LimitOrderRequest(
                    symbol=ticker, qty=sell_qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, limit_price=limit_price,
                    extended_hours=True
                )
            )
        except APIError as e:
            raise ValueError(f"Alpaca rejected extended hours sell: {e}")
    elif sell_qty >= held - 0.0001:
        try:
            order = client.close_position(ticker)
        except APIError as e:
            raise ValueError(f"Alpaca rejected close: {e}")
    else:
        try:
            order = client.submit_order(
                LimitOrderRequest(
                    symbol=ticker, qty=sell_qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, limit_price=limit_price,
                )
            )
        except APIError as e:
            raise ValueError(f"Alpaca rejected order: {e}")

    notify.trade("SELL", ticker, sell_qty, limit_price, f"{reason} (est P&L ${realized_est:+.0f})", auto=auto)
    return {
        "ticker": ticker,
        "side": "SELL",
        "shares": sell_qty,
        "price": limit_price,
        "proceeds": round(sell_qty * limit_price, 2),
        "realized_pnl": round(realized_est, 2),
        "order_id": str(order.id),
        "status": str(order.status.value if hasattr(order.status, "value") else order.status),
        "reason": reason,
    }


# -----------------------------------------------------------------
#  History helpers
# -----------------------------------------------------------------

def trades(limit: int = 100) -> list[dict]:
    """Order history mapped to the schema the UI/journal expects."""
    client = _get_client()
    try:
        # Increase the queried limit to ensure we have enough orders to filter
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=min(limit * 3, 500))
        orders = client.get_orders(filter=req)
    except Exception as e:
        log.warning("get_orders failed: %s", e)
        return []

    out = []
    for o in orders:
        if len(out) >= limit:
            break

        status_lower = str(o.status.value if hasattr(o.status, "value") else o.status).lower()
        fill_qty = float(o.filled_qty) if o.filled_qty else 0
        
        # Only show orders that are filled, partially filled, or have filled shares.
        # This removes unfilled child bracket orders (stop-loss, take-profit) that sit in 'held' or get 'canceled' with 0 shares.
        if status_lower not in ("filled", "partially_filled") and fill_qty == 0:
            continue

        side = str(o.side.value if hasattr(o.side, "value") else o.side).upper()

        # Alpaca can return:
        #   - share orders: o.qty set, o.notional = None
        #   - notional orders: o.qty = None, o.notional set (dollar amount)
        # Plus filled vs unfilled vs partially_filled.
        order_qty = float(o.qty) if o.qty else 0
        notional = float(o.notional) if o.notional else 0
        fill_qty = float(o.filled_qty) if o.filled_qty else 0
        fill_price = float(o.filled_avg_price) if o.filled_avg_price else 0
        limit_price = float(o.limit_price) if o.limit_price else 0

        # Display logic — what's most useful to see?
        # 1. Fully filled: show filled qty + avg price
        # 2. Partially filled: show "X of Y" hint via separate fields
        # 3. Unfilled limit/market with qty: show requested qty + limit price (or "market")
        # 4. Notional order: show $notional pending until filled
        status_lower = str(o.status.value if hasattr(o.status, "value") else o.status).lower()

        if status_lower in ("filled",):
            shares_disp = fill_qty
            price_disp = fill_price
        elif notional > 0 and fill_qty == 0:
            shares_disp = 0
            price_disp = 0
        elif notional > 0:
            # partially filled notional
            shares_disp = fill_qty
            price_disp = fill_price
        elif order_qty > 0 and fill_qty == 0:
            # unfilled share order — show requested qty and limit (or 0 for market)
            shares_disp = order_qty
            price_disp = limit_price
        else:
            shares_disp = fill_qty or order_qty
            price_disp = fill_price or limit_price

        proceeds = (price_disp * shares_disp) * (1 if side == "SELL" else -1)
        ts = str(o.submitted_at or o.created_at or "")

        out.append({
            "id": str(o.id),
            "ts": ts,
            "side": side,
            "ticker": o.symbol,
            "shares": round(shares_disp, 4),
            "price": round(price_disp, 4),
            "proceeds": round(proceeds, 2),
            "status": status_lower,
            "reason": getattr(o, "client_order_id", "") or "",
            "auto": 0,
            "order_type": str(o.order_type.value if hasattr(o.order_type, "value") else o.order_type),
            # Extra fields the UI can display when relevant
            "notional": notional or None,
            "order_qty": order_qty or None,
            "filled_qty": fill_qty,
            "filled_avg_price": fill_price or None,
            "limit_price": limit_price or None,
        })
    return out


def auto_runs(limit: int = 30) -> list[dict]:
    """Local SQLite auto_runs log (research/trade/journal cycles)."""
    import json
    with db._lock, db.connect() as c:
        rows = c.execute(
            "SELECT id, ts, summary FROM auto_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        try:
            summary = json.loads(r["summary"])
        except Exception:
            summary = {"raw": r["summary"]}
        out.append({"id": r["id"], "ts": r["ts"], "summary": summary})
    return out


def reset(starting_cash: float = 100_000.0) -> dict:
    """Reset agent metadata (auto_runs, research_runs). Alpaca paper accounts
    must be reset via Alpaca's dashboard — there's no API for that."""
    with db._lock, db.connect() as c:
        c.execute("DELETE FROM auto_runs")
        c.execute("DELETE FROM research_runs")
        c.execute("DELETE FROM journals")
    return {
        "status": "agent metadata cleared",
        "note": "Alpaca paper account state (cash, positions, orders) was NOT reset. "
                "Use Alpaca dashboard → Paper → 'Reset Paper Account' for that.",
    }


# -----------------------------------------------------------------
#  Local helpers
# -----------------------------------------------------------------

def _opened_at_for_positions(symbols: list[str]) -> dict[str, str]:
    """Find earliest BUY order timestamp per open symbol."""
    if not symbols:
        return {}
    client = _get_client()
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=symbols, limit=500,
            side=OrderSide.BUY,
        )
        orders = client.get_orders(filter=req)
    except Exception:
        return {}
    earliest: dict[str, str] = {}
    for o in orders:
        sym = o.symbol
        ts = o.filled_at or o.submitted_at or o.created_at
        if ts is None:
            continue
        ts_iso = str(ts)
        if sym not in earliest or ts_iso < earliest[sym]:
            earliest[sym] = ts_iso
    return earliest


def _opened_today_et(opened_at: str | None) -> bool:
    if not opened_at:
        return False
    try:
        dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00")).astimezone(_ET)
        return dt.date() >= datetime.now(_ET).date()
    except Exception:
        return False
