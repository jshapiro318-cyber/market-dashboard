import logging
import json
from datetime import datetime, timezone
from . import portfolio, db
from ..config import ALPACA_KEY, ALPACA_SECRET
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

log = logging.getLogger("liquidate")

def run():
    with db._lock, db.connect() as c:
        row = c.execute("SELECT 1 FROM auto_runs WHERE summary LIKE '%\"kind\": \"liquidate_opts\"%'").fetchone()
        if row:
            log.info("Options liquidation already completed. Skipping.")
            return

    client = TradingClient(api_key=ALPACA_KEY, secret_key=ALPACA_SECRET, paper=True)
    log.info("Starting one-time options liquidation...")
    
    # 1. Close all short puts to free up cash
    for pos in client.get_all_positions():
        if len(pos.symbol) > 10 and pos.symbol[-9] == 'P':
            try:
                log.info(f"Buying to close short put {pos.symbol}")
                client.submit_order(MarketOrderRequest(symbol=pos.symbol, qty=abs(float(pos.qty)), side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
            except Exception as e:
                log.error(f"Failed to close {pos.symbol}: {e}")

    # 2. Free up cash by selling 10 UPS if needed
    try:
        log.info("Selling 10 UPS to ensure cash for calls...")
        client.submit_order(MarketOrderRequest(symbol="UPS", qty=10, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
    except Exception as e:
        log.error(f"Failed to sell UPS: {e}")
        
    import time
    time.sleep(10) # wait for orders to fill and cash to settle
    
    # 3. Close all short calls
    for pos in client.get_all_positions():
        if len(pos.symbol) > 10 and pos.symbol[-9] == 'C':
            try:
                log.info(f"Buying to close short call {pos.symbol}")
                client.submit_order(MarketOrderRequest(symbol=pos.symbol, qty=abs(float(pos.qty)), side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
            except Exception as e:
                log.error(f"Failed to close {pos.symbol}: {e}")

    time.sleep(10)

    # 4. Sell ANET and FCX
    for ticker in ["ANET", "FCX"]:
        try:
            log.info(f"Selling {ticker} shares...")
            client.submit_order(MarketOrderRequest(symbol=ticker, qty=100, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
        except Exception as e:
            log.error(f"Failed to sell {ticker}: {e}")
            
    log.info("Options liquidation complete.")

    # Write to auto_runs to prevent running again
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "kind": "liquidate_opts",
        "status": "ok",
        "summary": {"info": "Options liquidation completed successfully."}
    }
    with db._lock, db.connect() as c:
        c.execute(
            "INSERT INTO auto_runs (ts, summary) VALUES (?, ?)",
            (summary["started_at"], json.dumps(summary))
        )

