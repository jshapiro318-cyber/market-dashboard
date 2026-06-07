"""Fundamental analysis — company financials, earnings, analyst view.

Pulls from yfinance.Ticker.info / .calendar / .earnings_dates and scores
the company on growth, profitability, valuation, and analyst sentiment.
Cached aggressively (24h) since fundamentals change quarterly at most.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import yfinance as yf

_cache: dict[str, tuple[float, dict]] = {}
_TTL = 24 * 3600


def fetch_fundamentals(ticker: str) -> dict:
    """Fetch raw fundamentals dict. Returns empty fields on failure."""
    key = ticker.upper()
    now = time.time()
    if key in _cache and now - _cache[key][0] < _TTL:
        return _cache[key][1]

    out: dict = {"ticker": key}
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        out.update({
            "company_name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "business_summary": info.get("longBusinessSummary"),
            "country": info.get("country"),
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website"),

            # Valuation
            "market_cap": info.get("marketCap"),
            "enterprise_value": info.get("enterpriseValue"),
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("trailingPegRatio") or info.get("pegRatio"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "price_to_book": info.get("priceToBook"),

            # Profitability / quality
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "return_on_equity": info.get("returnOnEquity"),
            "return_on_assets": info.get("returnOnAssets"),

            # Growth (YoY)
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "earnings_quarterly_growth": info.get("earningsQuarterlyGrowth"),
            "revenue_quarterly_growth": info.get("revenueQuarterlyGrowth"),

            # Leverage / cash
            "debt_to_equity": info.get("debtToEquity"),
            "current_ratio": info.get("currentRatio"),
            "total_cash": info.get("totalCash"),
            "total_debt": info.get("totalDebt"),
            "free_cashflow": info.get("freeCashflow"),
            "operating_cashflow": info.get("operatingCashflow"),

            # Dividend / shareholder return
            "dividend_yield": info.get("dividendYield"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            
            # Insider / Institutional holdings & Short Interest
            "held_percent_insiders": info.get("heldPercentInsiders"),
            "held_percent_institutions": info.get("heldPercentInstitutions"),
            "short_percent_of_float": info.get("shortPercentOfFloat"),
            "short_ratio": info.get("shortRatio"),

            # Analyst consensus
            "analyst_rating_key": info.get("recommendationKey"),
            "analyst_rating_mean": info.get("recommendationMean"),
            "target_mean": info.get("targetMeanPrice"),
            "target_high": info.get("targetHighPrice"),
            "target_low": info.get("targetLowPrice"),
            "num_analysts": info.get("numberOfAnalystOpinions"),

            # 52-week range
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
        })

        # Next earnings date
        try:
            cal = t.calendar
            if cal is not None:
                if isinstance(cal, dict):
                    out["next_earnings_date"] = _fmt_date(cal.get("Earnings Date"))
                else:  # DataFrame
                    out["next_earnings_date"] = _fmt_date(cal.iloc[0].get("Earnings Date") if len(cal) else None)
        except Exception:
            pass

        # Recent earnings surprises (last 4 quarters)
        try:
            ed = t.earnings_dates
            if ed is not None and len(ed) > 0:
                past = ed[ed.index < datetime.now(ed.index.tz)] if ed.index.tz else ed[ed.index < datetime.now()]
                surprises = []
                for idx, row in past.head(4).iterrows():
                    est = row.get("EPS Estimate")
                    act = row.get("Reported EPS")
                    surp_pct = row.get("Surprise(%)")
                    if est is not None and act is not None:
                        surprises.append({
                            "date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
                            "estimate": float(est) if est == est else None,
                            "actual": float(act) if act == act else None,
                            "surprise_pct": float(surp_pct) if surp_pct == surp_pct else None,
                        })
                out["recent_earnings"] = surprises
        except Exception:
            pass
    except Exception as e:
        out["error"] = str(e)

    _cache[key] = (now, out)
    return out


def _fmt_date(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, list) and d:
        d = d[0]
    try:
        if hasattr(d, "strftime"):
            return d.strftime("%Y-%m-%d")
        return str(d)
    except Exception:
        return None


def fundamentals_factor_score(fund: dict, quote: dict) -> tuple[float, list[str]]:
    """Score fundamentals -10..+10 with reasons. Returns (score, reasons)."""
    score = 0.0
    reasons: list[str] = []

    # Earnings growth (most recent quarter YoY) — biggest swing
    eq = fund.get("earnings_quarterly_growth")
    if eq is not None:
        if eq > 0.30:
            score += 4; reasons.append(f"Q earnings growth +{eq*100:.0f}% YoY (very strong)")
        elif eq > 0.10:
            score += 2; reasons.append(f"Q earnings growth +{eq*100:.0f}% YoY")
        elif eq > 0:
            score += 1; reasons.append(f"Q earnings growth +{eq*100:.0f}% YoY (modest)")
        elif eq > -0.10:
            score -= 1; reasons.append(f"Q earnings declined {eq*100:.0f}% YoY")
        else:
            score -= 3; reasons.append(f"Q earnings down {eq*100:.0f}% YoY (sharp decline)")

    # Revenue growth
    rg = fund.get("revenue_growth")
    if rg is not None:
        if rg > 0.20:
            score += 2; reasons.append(f"Revenue +{rg*100:.0f}% YoY")
        elif rg > 0.05:
            score += 1
        elif rg < -0.05:
            score -= 2; reasons.append(f"Revenue {rg*100:.0f}% YoY (contracting)")

    # Forward P/E — lower is cheaper, but very-high P/E is a red flag
    fpe = fund.get("forward_pe")
    if fpe is not None and fpe > 0:
        if fpe < 12:
            score += 1; reasons.append(f"Forward P/E {fpe:.1f} (cheap)")
        elif fpe > 50:
            score -= 2; reasons.append(f"Forward P/E {fpe:.1f} (expensive)")
        elif fpe > 30:
            score -= 1

    # Analyst consensus rating: 1=Strong Buy ... 5=Strong Sell
    am = fund.get("analyst_rating_mean")
    if am is not None and fund.get("num_analysts", 0) and fund["num_analysts"] >= 3:
        if am < 1.7:
            score += 2; reasons.append(f"Analyst consensus Strong Buy ({am:.1f}/5)")
        elif am < 2.3:
            score += 1; reasons.append(f"Analyst consensus Buy ({am:.1f}/5)")
        elif am > 3.7:
            score -= 2; reasons.append(f"Analyst consensus Sell ({am:.1f}/5)")
        elif am > 3.3:
            score -= 1

    # Analyst target upside
    tm = fund.get("target_mean")
    last = quote.get("last")
    if tm and last:
        upside = (tm - last) / last * 100
        if upside > 25:
            score += 2; reasons.append(f"Analyst target ${tm:.0f} (+{upside:.0f}% upside)")
        elif upside > 10:
            score += 1
        elif upside < -10:
            score -= 2; reasons.append(f"Analyst target ${tm:.0f} ({upside:.0f}% downside)")

    # Return on equity — quality marker
    roe = fund.get("return_on_equity")
    if roe is not None:
        if roe > 0.25:
            score += 1; reasons.append(f"ROE {roe*100:.0f}% (high quality)")
        elif roe < 0:
            score -= 2; reasons.append(f"ROE {roe*100:.0f}% (unprofitable)")

    # Profit margin
    pm = fund.get("profit_margin")
    if pm is not None:
        if pm < 0:
            score -= 1
        elif pm > 0.25:
            score += 1

    # Recent earnings surprise streak
    rec = fund.get("recent_earnings") or []
    if rec:
        beats = sum(1 for r in rec if r.get("surprise_pct") and r["surprise_pct"] > 0)
        misses = sum(1 for r in rec if r.get("surprise_pct") and r["surprise_pct"] < 0)
        if beats >= 3:
            score += 1; reasons.append(f"Beat earnings {beats} of last {len(rec)} quarters")
        elif misses >= 2:
            score -= 1; reasons.append(f"Missed earnings {misses} of last {len(rec)} quarters")

    # Clamp
    return max(-10.0, min(10.0, score)), reasons
