import logging
import time
from datetime import datetime, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from backend.config import ALPACA_KEY, ALPACA_SECRET

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("manual_liquidate")

def main():
    client = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=True)
    log.info("Starting manual liquidation of options and target stock positions...")

    # 1. Cancel any existing open orders for target symbols to avoid conflicts
    target_symbols = ["ANET", "FCX", "UPS", "AAPL260618P00280000", "AMZN260618P00235000", "NVDA260618P00200000"]
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = client.get_orders(filter=req)
        for o in open_orders:
            if o.symbol in target_symbols:
                log.info(f"Cancelling open order {o.id} for {o.symbol}")
                client.cancel_order_by_id(o.id)
        time.sleep(2)
    except Exception as e:
        log.error(f"Error cancelling open orders: {e}")

    # 2. Close option positions (short puts) using GTC Limit orders
    # Options do not trade in extended hours, so we place GTC Limit orders for tomorrow's open.
    options_to_close = [
        {"symbol": "AAPL260618P00280000", "qty": 1, "limit": 1.50},
        {"symbol": "AMZN260618P00235000", "qty": 1, "limit": 4.00},
        {"symbol": "NVDA260618P00200000", "qty": 1, "limit": 5.00},
    ]

    for opt in options_to_close:
        try:
            log.info(f"Submitting GTC Limit Buy to close short put {opt['symbol']} at {opt['limit']}")
            client.submit_order(
                LimitOrderRequest(
                    symbol=opt["symbol"],
                    qty=opt["qty"],
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                    limit_price=opt["limit"]
                )
            )
        except Exception as e:
            log.error(f"Failed to submit order for {opt['symbol']}: {e}")

    # 3. Sell stock positions using Limit orders with extended_hours=True
    # These will execute immediately in the current extended hours session.
    stocks_to_sell = [
        {"symbol": "ANET", "qty": 100, "limit": 168.00},
        {"symbol": "FCX", "qty": 100, "limit": 69.00},
        {"symbol": "UPS", "qty": 10, "limit": 106.00},
    ]

    for st in stocks_to_sell:
        try:
            log.info(f"Submitting Limit Sell for {st['symbol']} (qty={st['qty']}) at {st['limit']} (extended hours)")
            client.submit_order(
                LimitOrderRequest(
                    symbol=st["symbol"],
                    qty=st["qty"],
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=st["limit"],
                    extended_hours=True
                )
            )
        except Exception as e:
            log.error(f"Failed to submit sell order for {st['symbol']}: {e}")

    log.info("Manual liquidation execution completed.")

if __name__ == "__main__":
    main()
