"""Equity Research Report generator plugin integration.

Aggregates technical, fundamental, DCF valuation, scenarios, and insider activity,
and formats it into an institutional-grade research report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from ..trading import strategy
from . import dcf as dcf_mod

def generate_research_report(ticker: str) -> dict:
    ticker = ticker.upper().strip()
    
    # 1. Gather core datasets using existing engines
    analysis = strategy.analyze_one(ticker)
    quote = analysis["quote"]
    ind = analysis["indicators"]
    candles = analysis["candle_patterns"]
    charts = analysis["chart_patterns"]
    news_agg = analysis["news_agg"]
    news_items = analysis["news_items"]
    fund = analysis["fundamentals"]
    signal = analysis["signal"]
    
    # 2. Run DCF Valuation
    dcf_res = dcf_mod.run_dcf_valuation(ticker)
    dcf_price = dcf_res["implied_price"]
    dcf_upside = dcf_res["upside_pct"]
    wacc = dcf_res["wacc"]
    growth_rate = dcf_res["growth_rate"]
    
    # 3. Determine Investment Rating & Conviction
    # Buy if positive upside > 15%, Sell if downside < -15%, else Hold
    if dcf_upside > 15.0 and signal["score"] >= 0:
        rating = "BUY"
    elif dcf_upside < -15.0 or signal["bias"] in ("bearish", "lean bearish"):
        rating = "SELL"
    else:
        rating = "HOLD"
        
    if abs(dcf_upside) > 30.0 or signal["confidence"] > 0.60:
        conviction = "High"
    elif abs(dcf_upside) > 10.0 or signal["confidence"] > 0.30:
        conviction = "Medium"
    else:
        conviction = "Low"
        
    # Position Sizing
    if rating == "BUY" and conviction == "High":
        position_size = "4 - 5%"
    elif rating == "BUY" and conviction == "Medium":
        position_size = "3%"
    elif rating == "BUY" and conviction == "Low":
        position_size = "1 - 2%"
    elif rating == "HOLD":
        position_size = "1 - 2%"
    else:
        position_size = "0% (Avoid / Hedge)"
        
    # 4. Valuation Scenarios (Base, Bull, Bear)
    # Target values
    bull_target = dcf_price * 1.25
    bear_target = dcf_price * 0.75
    
    # Probability weighted value
    expected_value = (dcf_price * 0.55) + (bull_target * 0.25) + (bear_target * 0.20)
    expected_upside = ((expected_value - quote["last"]) / quote["last"] * 100) if quote["last"] else 0.0
    
    # 5. Technical Context
    ema9 = ind.get("ema_9")
    ema21 = ind.get("ema_21")
    ema50 = ind.get("ema_50")
    ema200 = ind.get("ema_200")
    rsi_val = ind.get("rsi_14") or 50.0
    
    ma_stack = []
    if ema9 and ema21:
        ma_stack.append("Bullish 9/21-day short-term crossover" if ema9 > ema21 else "Bearish 9/21-day short-term crossover")
    if ema50 and ema200:
        ma_stack.append("Bullish golden cross structure (50 > 200 EMA)" if ema50 > ema200 else "Bearish death cross structure (50 < 200 EMA)")
    if quote["last"] and ema200:
        ma_stack.append(f"Trading above long-term 200-day support (${ema200:.2f})" if quote["last"] > ema200 else f"Trading below long-term 200-day resistance (${ema200:.2f})")
        
    rsi_status = "Oversold (mean-reversion bounce candidate)" if rsi_val <= 30 else "Overbought (exhaustion risk)" if rsi_val >= 70 else "Neutral consolidation zone"
    
    # 6. Insider/Institutional ownership
    insiders_pct = fund.get("held_percent_insiders")
    insts_pct = fund.get("held_percent_institutions")
    short_float = fund.get("short_percent_of_float")
    short_ratio = fund.get("short_ratio")
    
    insiders_str = f"{insiders_pct * 100:.2f}%" if insiders_pct is not None else "—"
    insts_str = f"{insts_pct * 100:.2f}%" if insts_pct is not None else "—"
    short_float_str = f"{short_float * 100:.2f}%" if short_float is not None else "—"
    short_ratio_str = f"{short_ratio:.2f}" if short_ratio is not None else "—"
    
    # 7. Catalysts & News
    news_lines = []
    for item in news_items[:3]:
        title = item.get("title") or "Market Update"
        publisher = item.get("publisher") or "Media Source"
        news_lines.append(f"- **{title}** ({publisher})")
    news_str = "\n".join(news_lines) if news_lines else "- No fresh corporate news catalysts identified."
    
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")
    ma_stack_str = "\n  ".join([f"- {ma}" for ma in ma_stack]) if ma_stack else "- Moving average trends are mixed/flat"
    
    report = f"""# {fund.get('company_name', ticker).upper()} ({ticker}) - EQUITY RESEARCH REPORT
*Generated on: {date_str}*

---

## 1. EXECUTIVE SUMMARY
**RATING: {rating}**  
**Conviction:** {conviction}  
**12-Month Target Price:** ${dcf_price:.2f}  
**Upside Potential:** {dcf_upside:+.2f}%  
**Recommended Position Sizing:** {position_size}  

*Executive Commentary:*  
{fund.get('company_name', ticker)} is rated a **{rating}** with **{conviction}** conviction. Fundamental DCF valuation projects an implied price of **${dcf_price:.2f}**, representing an upside of **{dcf_upside:+.2f}%** from the current close of **${quote['last']:.2f}**. Sentiment signals are {news_agg.get('label', 'neutral').upper()} and technical setups suggest a {signal['bias'].upper()} posture, forming a balanced entry zone with a solid margin of safety.

---

## 2. FUNDAMENTAL ANALYSIS
* **YoY Revenue Growth:** {fund.get('revenue_growth', 0.0)*100:+.1f}%
* **YoY Earnings Growth:** {fund.get('earnings_growth', 0.0)*100:+.1f}%
* **Profit Margin:** {fund.get('profit_margin', 0.0)*100:.1f}%
* **Operating Margin:** {fund.get('operating_margin', 0.0)*100:.1f}%
* **Return on Equity (ROE):** {fund.get('return_on_equity', 0.0)*100:.1f}%
* **Trailing P/E Ratio:** {fund.get('trailing_pe') or '—'}
* **PEG Ratio:** {fund.get('peg_ratio') or '—'}

*Business Profile:*  
{fund.get('business_summary', 'Detailed business summary not available.')[:600]}...

---

## 3. VALUATION & PRICE TARGET SCENARIOS
* **Base Case (55% Probability):** **${dcf_price:.2f}** (implied DCF price based on WACC of {wacc:.2f}% and revenue growth baseline of {growth_rate:.2f}%)
* **Bull Case (25% Probability):** **${bull_target:.2f}** (+25% premium representing accelerated expansion and margin improvement)
* **Bear Case (20% Probability):** **${bear_target:.2f}** (-25% haircut reflecting macro headwinds and capital constraints)
* **Expected Value (Probability-Weighted):** **${expected_value:.2f}** ({expected_upside:+.2f}% expected upside)

---

## 4. TECHNICAL & OPTIONS CONTEXT
* **Last Close:** ${quote['last']:.2f}
* **RSI (14-day):** {rsi_val:.1f} ({rsi_status})
* **Relative Volume:** {quote.get('relative_volume', 1.0)}x average
* **Moving Average Stack:**
  {ma_stack_str}

---

## 5. MARKET POSITIONING & INSIDER SIGNALS
* **Insider Ownership:** {insiders_str}
* **Institutional Ownership:** {insts_str}
* **Short Interest (% of Float):** {short_float_str}
* **Short Ratio (Days to Cover):** {short_ratio_str}

*Ownership Breakdown:*  
Institutions hold **{insts_str}** of the outstanding shares, reflecting high institutional backing. Insider ownership stands at **{insiders_str}**. Short sellers account for **{short_float_str}** of the float, implying a short ratio of **{short_ratio_str}** days to cover.

---

## 6. FRESH CORPORATE NEWS CATALYSTS
{news_str}

---

## 7. RISK ASSESSMENT
1. **Valuation Sensitivity:** Valuation models are highly dependent on assumptions: WACC ({wacc:.2f}%) and Terminal Growth ({dcf_res.get('terminal_growth', 2.0):.2f}%).
2. **Leverage & Debt Structure:** Debt-to-Equity stands at {fund.get('debt_to_equity', '—')}%, which might introduce credit/solvency risks.
3. **Macro Volatility:** Beta of {fund.get('beta', '1.0')} indicates the stock's sensitivity to market fluctuations.

---

## 8. RECOMMENDATION SUMMARY TABLE
| Metric | Target Value |
| :--- | :--- |
| **Investment Rating** | **{rating}** |
| **Conviction Strength** | **{conviction}** |
| **Price Target (12mo)** | **${dcf_price:.2f}** |
| **Model Expected Value** | **${expected_value:.2f}** |
| **DCF Price Upside** | **{dcf_upside:+.2f}%** |
| **Position Sizing Limit** | **{position_size}** |

---

*Disclaimer: This report is generated dynamically by an automated quantitative script for educational and simulation research purposes only. It does not constitute investment advice.*
"""

    return {
        "status": "ok",
        "ticker": ticker,
        "rating": rating,
        "conviction": conviction,
        "upside_pct": round(dcf_upside, 1),
        "implied_price": round(dcf_price, 2),
        "report_markdown": report
    }
