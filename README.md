# Market Intel Dashboard

A research dashboard + autonomous paper-trading agent. Runs technical analysis,
candlestick + chart patterns, news sentiment, fundamentals; aggregates into a
7-factor signal; routes real-time paper trades through Alpaca with hard-cap
position sizing, limit orders, and bracketed stop-loss / take-profit.

> **Not financial advice.** Paper trading only by default. No system reliably
> predicts markets.

## Quick start

### 1. Set up Alpaca keys

1. Sign in at <https://app.alpaca.markets/paper/dashboard/overview>.
2. Generate **paper trading** API keys (Settings → API Keys).
3. Copy the template and paste the keys:
   ```bash
   cd ~/market-dashboard
   cp .env.example .env
   # then edit .env and set APCA_API_KEY_ID and APCA_API_SECRET_KEY
   ```
4. The `.env` file is git-ignored. Never commit real keys.

### 2. Run

```bash
./run.sh
```

Then open <http://127.0.0.1:8000>.

The first run creates a virtualenv and installs dependencies (~30 s).

## What it does

### Data
- **Live OHLCV** via `yfinance` (free, no API key — Yahoo Finance under the hood).
- **News** via `yfinance.Ticker.news` (Yahoo's recent articles per ticker).

### Indicators (all computed from raw OHLCV in `analysis/indicators.py`)
- Trend: SMA(20/50/200), EMA(9/21/50/200)
- Momentum: RSI(14), MACD(12/26/9), Stochastic(14,3)
- Volatility: Bollinger Bands(20,2), ATR(14) + BB-width 60-day percentile
- Volume: OBV slope, cumulative VWAP, relative volume

### Patterns (`analysis/patterns.py`)
Candlestick: Doji · Hammer · Shooting Star · Marubozu · Bullish/Bearish Engulfing ·
Morning/Evening Star · Tweezer Top/Bottom · Three White Soldiers / Three Black Crows

Chart context: Bollinger squeeze · 20-day breakout/breakdown · Bullish/Bearish
RSI divergence · Golden/Death cross · Pivot-based support/resistance

### Sentiment (`analysis/sentiment.py`)
VADER analyzer extended with a finance-specific lexicon (beat, miss, downgrade,
buyback, lawsuit, etc.). Articles are weighted by recency with a 24-hour half-life.

### Signal aggregation (`analysis/signals.py`)
Six factors, each scored −10..+10 with a written rationale and a fixed weight:

| Factor      | Weight |
|-------------|--------|
| Trend       | 25%    |
| Momentum    | 20%    |
| Volume      | 15%    |
| Volatility  | 10%    |
| Patterns    | 15%    |
| Sentiment   | 15%    |

The aggregate is a weighted sum. **Confidence** combines (a) magnitude of the
sum and (b) agreement between factors — a strong, unanimous signal scores
higher than a strong but contradicted one.

The bias buckets: `bullish` / `lean bullish` / `neutral` / `lean bearish` / `bearish`.

### Risk flags
Always-on warnings the UI surfaces: RSI extremes, unusual volatility, factor
disagreement, and the standing "past patterns don't guarantee future results"
reminder.

## API

| Endpoint                       | Description                               |
|--------------------------------|-------------------------------------------|
| `GET /api/analyze/{ticker}`    | Full analysis (quote, indicators, patterns, news, signal) |
| `GET /api/candles/{ticker}`    | OHLCV bars for charting                   |
| `GET /api/watchlist?tickers=`  | Multi-ticker scan, sorted by score        |
| `GET /api/health`              | Liveness probe                            |
| `GET /docs`                    | FastAPI auto-generated OpenAPI docs       |

Example: <http://127.0.0.1:8000/api/analyze/NVDA>

## What this is NOT

- Not a real-time tick-data feed (yfinance is 15-min delayed for most US equities)
- Not a backtesting engine (yet — see "Extending" below)
- Not a paper trading account (yet)
- Not a forecasting model — no ML training is happening; signals are
  deterministic rules on real indicators

## Extending honestly

Things you can add without crossing into pseudoscience:

1. **Backtester** — replay the signal logic over historical bars to measure
   hit-rate / Sharpe / drawdown. This tells you whether the *rules themselves*
   would have worked historically (with realistic caveats about overfitting).
2. **Paper trading state** — persist a virtual portfolio to SQLite, track P&L.
3. **Better news** — swap yfinance.news for NewsAPI / Polygon / RSS aggregator.
4. **FinBERT** — replace VADER with a finance-tuned transformer for headline
   sentiment (≈400 MB model download, but more accurate on financial language).
5. **Options flow** — IBKR or Polygon options endpoints; flag unusual activity.
6. **Intraday signals** — pass `interval=15m` to `/api/analyze` and the indicators
   apply to the new timeframe.
7. **Custom watchlists** — `?tickers=AAPL,NVDA,…` on `/api/watchlist`.

## Project layout

```
market-dashboard/
├── backend/
│   ├── app.py                # FastAPI routes
│   ├── analysis/
│   │   ├── indicators.py     # SMA/EMA/RSI/MACD/BB/ATR/etc.
│   │   ├── patterns.py       # Candlestick + chart patterns
│   │   ├── sentiment.py      # VADER + finance lexicon
│   │   └── signals.py        # Factor scoring + aggregation
│   ├── data/
│   │   ├── prices.py         # yfinance wrapper + cache
│   │   └── news.py           # yfinance news wrapper + cache
│   └── requirements.txt
├── frontend/
│   └── index.html            # Single-page dashboard (no build step)
├── run.sh                    # Bootstrap venv + start server
└── README.md
```
