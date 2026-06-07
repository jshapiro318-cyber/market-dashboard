"""Alpaca options API wrapper — chain queries + option orders.

Used by the wheel strategy. Requires the Alpaca paper account to have
**options trading enabled** (Level 2 for cash-secured puts / covered calls).
If not enabled, Alpaca returns 403/422 errors which we surface verbatim.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    OptionLatestTradeRequest,
    OptionSnapshotRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    OrderType,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest

from ..config import ALPACA_KEY, ALPACA_SECRET, IS_PAPER, assert_configured

log = logging.getLogger("options")
SHARES_PER_CONTRACT = 100

_trading: TradingClient | None = None
_data: OptionHistoricalDataClient | None = None


def _trading_client() -> TradingClient:
    global _trading
    if _trading is None:
        assert_configured()
        _trading = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=IS_PAPER)
    return _trading


def _data_client() -> OptionHistoricalDataClient:
    global _data
    if _data is None:
        assert_configured()
        _data = OptionHistoricalDataClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET)
    return _data


def find_contract(
    underlying: str,
    contract_type: str,
    target_strike: float,
    min_dte: int = 14,
    max_dte: int = 28,
) -> dict | None:
    """Find option contract closest to target_strike with DTE in [min_dte, max_dte].

    Uses yfinance for the chain (Alpaca's free OPRA tier returns empty data).
    The OCC symbol format yfinance returns matches what Alpaca expects, so we
    just pass the symbol through at execution time.
    """
    today = date.today()
    exp_min = today + timedelta(days=min_dte)
    exp_max = today + timedelta(days=max_dte)
    is_put = contract_type.lower() == "put"

    try:
        t = yf.Ticker(underlying.upper())
        expirations = t.options or ()
    except Exception as e:
        raise RuntimeError(f"yfinance option chain fetch failed for {underlying}: {e}")

    # Filter expirations to our DTE window
    valid_exps = []
    for exp_str in expirations:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if exp_min <= exp_date <= exp_max:
                valid_exps.append((exp_str, exp_date))
        except ValueError:
            continue
    if not valid_exps:
        return None

    # Pick the middle of the DTE window (or first valid)
    valid_exps.sort(key=lambda x: abs((x[1] - today).days - (min_dte + max_dte) // 2))
    chosen_exp, _ = valid_exps[0]

    try:
        chain = t.option_chain(chosen_exp)
    except Exception as e:
        raise RuntimeError(f"yfinance chain fetch failed for {underlying} {chosen_exp}: {e}")

    df = chain.puts if is_put else chain.calls
    if df is None or df.empty:
        return None

    # Pick strike closest to target with non-trivial OI/volume (otherwise quotes are stale)
    df = df.copy()

    # Directional filter: for calls, only consider strikes >= target (OTM/ATM);
    # for puts, only consider strikes <= target. This prevents selling ITM options
    # that are below cost basis.
    if is_put:
        directional = df[df["strike"] <= target_strike]
    else:
        directional = df[df["strike"] >= target_strike]
    # Fall back to full chain if no directional match
    pool = directional if not directional.empty else df

    pool = pool.copy()
    pool["dist"] = (pool["strike"] - target_strike).abs()
    # Prefer contracts with OI ≥ 50 (real liquidity)
    liquid = pool[pool["openInterest"].fillna(0) >= 50].sort_values("dist")
    pick = liquid.iloc[0] if not liquid.empty else pool.sort_values("dist").iloc[0]

    import math
    def clean_int(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return 0
        return int(v)

    def clean_float(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return 0.0
        return float(v)

    return {
        "symbol": pick["contractSymbol"],
        "strike": float(pick["strike"]),
        "expiration": chosen_exp,
        "type": contract_type.upper(),
        "underlying": underlying.upper(),
        "open_interest": clean_int(pick.get("openInterest")),
        "bid": clean_float(pick.get("bid")),
        "ask": clean_float(pick.get("ask")),
        "last_price": clean_float(pick.get("lastPrice")),
        "volume": clean_int(pick.get("volume")),
    }



def get_quote(option_symbol: str) -> dict | None:
    """Latest bid/ask/mid for an option symbol.

    Primary source: yfinance (Alpaca's free OPRA tier returns empty results
    for most contracts). Parses the OCC symbol to fetch the relevant chain.
    """
    parsed = _parse_occ_symbol(option_symbol)
    if not parsed:
        return None
    try:
        t = yf.Ticker(parsed["underlying"])
        chain = t.option_chain(parsed["expiration"])
        df = chain.puts if parsed["type"] == "P" else chain.calls
        row = df[df["contractSymbol"] == option_symbol]
        if row.empty:
            return None
        r = row.iloc[0]
        import math
        def clean_val(v):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return 0.0
            return float(v)

        bid = clean_val(r.get("bid"))
        ask = clean_val(r.get("ask"))
        last = clean_val(r.get("lastPrice"))
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        elif ask > 0:
            mid = ask
        elif bid > 0:
            mid = bid
        elif last > 0:
            mid = last
        else:
            return None
        return {"bid": bid, "ask": ask, "mid": round(mid, 2), "last": last, "source": "yfinance"}
    except Exception as e:
        log.debug("yfinance option quote failed for %s: %s", option_symbol, e)
        return None


def _parse_occ_symbol(symbol: str) -> dict | None:
    """Parse OCC symbol like AAPL260605P00280000 → underlying/expiration/type/strike.

    Format: <ticker><YY><MM><DD><C|P><strike*1000 padded to 8 digits>
    """
    import re
    m = re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", symbol)
    if not m:
        return None
    ticker, yy, mm, dd, ct, strike_raw = m.groups()
    expiration = f"20{yy}-{mm}-{dd}"
    strike = int(strike_raw) / 1000.0
    return {"underlying": ticker, "expiration": expiration, "type": ct, "strike": strike}


def sell_to_open(option_symbol: str, qty: int = 1, limit_price: float | None = None) -> dict:
    if limit_price is None:
        q = get_quote(option_symbol)
        if not q or not q["mid"]:
            raise ValueError(f"No quote for {option_symbol}")
        limit_price = round(q["mid"], 2)

    try:
        order = _trading_client().submit_order(
            LimitOrderRequest(
                symbol=option_symbol, qty=qty, side=OrderSide.SELL, type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                position_intent=PositionIntent.SELL_TO_OPEN,
            )
        )
    except APIError as e:
        raise RuntimeError(f"Alpaca rejected option SELL_TO_OPEN: {e}")
    return _order_summary(order, "SELL_TO_OPEN", option_symbol, qty, limit_price)


def buy_to_close(option_symbol: str, qty: int = 1, limit_price: float | None = None) -> dict:
    if limit_price is None:
        q = get_quote(option_symbol)
        if not q or not q["mid"]:
            raise ValueError(f"No quote for {option_symbol}")
        limit_price = round(q["mid"], 2)

    try:
        order = _trading_client().submit_order(
            LimitOrderRequest(
                symbol=option_symbol, qty=qty, side=OrderSide.BUY, type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                position_intent=PositionIntent.BUY_TO_CLOSE,
            )
        )
    except APIError as e:
        raise RuntimeError(f"Alpaca rejected option BUY_TO_CLOSE: {e}")
    return _order_summary(order, "BUY_TO_CLOSE", option_symbol, qty, limit_price)


def _order_summary(order, side: str, symbol: str, qty: int, limit_price: float) -> dict:
    status = order.status.value if hasattr(order.status, "value") else str(order.status)
    return {
        "order_id": str(order.id), "symbol": symbol, "qty": qty,
        "side": side, "limit_price": limit_price, "status": status,
    }


def get_open_option_position(option_symbol: str) -> dict | None:
    try:
        pos = _trading_client().get_open_position(option_symbol)
    except APIError:
        return None
    except Exception:
        return None
    return {
        "symbol": pos.symbol,
        "qty": float(pos.qty),
        "avg_entry_price": float(pos.avg_entry_price or 0),
        "current_price": float(pos.current_price or 0),
        "market_value": float(pos.market_value or 0),
        "unrealized_pl": float(pos.unrealized_pl or 0),
    }


def has_open_order(option_symbol: str) -> bool:
    """True if there's a live (non-terminal) order for this option symbol.
    Prevents the wheel from submitting duplicates while a put/call is working."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    try:
        orders = _trading_client().get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[option_symbol], limit=10)
        )
        return any(o.symbol == option_symbol for o in (orders or []))
    except Exception:
        return False


def shares_held(ticker: str) -> int:
    """Returns long shares of underlying (0 if none)."""
    try:
        pos = _trading_client().get_open_position(ticker.upper())
        return int(float(pos.qty))
    except Exception:
        return 0


def account_supports_options() -> tuple[bool, str]:
    """Probe the account for options eligibility. Returns (ok, message)."""
    try:
        acct = _trading_client().get_account()
        lvl = getattr(acct, "options_trading_level", None) or getattr(acct, "options_buying_power", None)
        if lvl is None:
            return True, "options trading eligibility could not be determined; will attempt and report errors"
        return True, f"options trading level: {lvl}"
    except Exception as e:
        return False, str(e)
