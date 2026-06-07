"""Discounted Cash Flow (DCF) valuation and financial modeling suite.

Implements DCF valuation, WACC estimation, sensitivity analysis, Monte Carlo simulation,
and scenario planning.
"""
from __future__ import annotations

import math
import numpy as np
from datetime import datetime, timezone
import yfinance as yf

from ..data import prices as price_mod
from . import fundamentals as fund_mod

def estimate_wacc(ticker: str, fund: dict) -> float:
    """Estimate Weighted Average Cost of Capital (WACC) using CAPM."""
    # Risk-free rate (approximate 10-year US Treasury yield)
    rf = 0.042
    
    # Beta
    beta = fund.get("beta")
    if beta is None or beta != beta:  # NaN check
        beta = 1.0
    beta = max(0.5, min(2.5, beta))
    
    # Equity Risk Premium (standard ERP)
    erp = 0.055
    
    # Cost of Equity
    cost_of_equity = rf + beta * erp
    
    # Cost of Debt (estimated pretax cost of debt)
    cost_of_debt = 0.055
    tax_rate = 0.21
    after_tax_debt = cost_of_debt * (1 - tax_rate)
    
    # Capital structure weights
    debt_to_equity_pct = fund.get("debt_to_equity")
    if debt_to_equity_pct is None or debt_to_equity_pct != debt_to_equity_pct:
        de_ratio = 0.30
    else:
        de_ratio = debt_to_equity_pct / 100.0
        
    de_ratio = max(0.0, min(3.0, de_ratio))
    
    weight_equity = 1.0 / (1.0 + de_ratio)
    weight_debt = de_ratio / (1.0 + de_ratio)
    
    wacc = (cost_of_equity * weight_equity) + (after_tax_debt * weight_debt)
    return float(max(0.05, min(0.20, wacc)))

def calculate_dcf(
    fcf_0: float,
    shares: float,
    cash: float,
    debt: float,
    growth_rate: float,
    terminal_growth: float,
    wacc: float
) -> float:
    """Core mathematical DCF formula: enterprise value, net debt, equity value, implied price."""
    if wacc <= terminal_growth:
        return 0.0
        
    # Project 5 years of free cash flows
    fcfs = []
    curr = fcf_0
    for _ in range(5):
        curr *= (1 + growth_rate)
        fcfs.append(curr)
        
    # Discount cash flows
    pv_fcfs = 0.0
    for t, fcf in enumerate(fcfs, start=1):
        pv_fcfs += fcf / ((1 + wacc) ** t)
        
    # Terminal value
    tv = fcfs[-1] * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_tv = tv / ((1 + wacc) ** 5)
    
    # Enterprise Value
    ev = pv_fcfs + pv_tv
    
    # Equity Value
    equity_val = ev + cash - debt
    
    # Implied price per share
    if shares <= 0:
        return 0.0
    implied_price = equity_val / shares
    return float(max(0.0, implied_price))

def run_dcf_valuation(
    ticker: str,
    custom_growth: float | None = None,
    custom_terminal: float | None = None,
    custom_wacc: float | None = None
) -> dict:
    """Run full DCF, Sensitivity analysis, Monte Carlo simulation, and Scenario planning."""
    ticker = ticker.upper().strip()
    fund = fund_mod.fetch_fundamentals(ticker)
    quote = price_mod.fetch_quote(ticker)
    
    last_price = quote.get("last") or 100.0
    market_cap = fund.get("market_cap")
    if not market_cap or market_cap != market_cap:
        market_cap = last_price * 100000000.0  # estimate
        
    shares = market_cap / last_price
    
    # Free Cash Flow base
    fcf_0 = fund.get("free_cashflow")
    op_cash = fund.get("operating_cashflow") or 0.0
    
    # yfinance freeCashflow is frequently under-reported or missing.
    # If operating cash flow is available, FCF is typically around 70% of it.
    est_fcf_from_op = op_cash * 0.70
    if fcf_0 is None or fcf_0 <= 0 or fcf_0 != fcf_0 or est_fcf_from_op > (fcf_0 or 0):
        fcf_0 = est_fcf_from_op if est_fcf_from_op > 0 else fcf_0
        
    # Fallback to revenue-based estimation if FCF is still missing/negative
    profit_margin = fund.get("profit_margin") or 0.10
    if fcf_0 is None or fcf_0 <= 0 or fcf_0 != fcf_0:
        # Estimate FCF as 5% of market cap (5% cash flow yield)
        fcf_0 = market_cap * max(0.02, min(0.12, profit_margin))
        
    cash = fund.get("total_cash") or 0.0
    debt = fund.get("total_debt") or 0.0
    
    # Parameters
    if custom_wacc is not None:
        wacc = custom_wacc
    else:
        wacc = estimate_wacc(ticker, fund)
        
    if custom_growth is not None:
        growth_rate = custom_growth
    else:
        growth_rate = fund.get("revenue_growth") or 0.08
        if growth_rate <= 0:
            growth_rate = 0.05
            
    if custom_terminal is not None:
        terminal_growth = custom_terminal
    else:
        terminal_growth = 0.02
        
    # 1. Base Case
    base_price = calculate_dcf(fcf_0, shares, cash, debt, growth_rate, terminal_growth, wacc)
    upside = ((base_price - last_price) / last_price * 100) if last_price else 0.0
    
    # 2. Sensitivity Analysis Grid (5x5 matrix)
    wacc_steps = [wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02]
    g_steps = [terminal_growth - 0.01, terminal_growth - 0.005, terminal_growth, terminal_growth + 0.005, terminal_growth + 0.01]
    
    sensitivity_grid = []
    for w in wacc_steps:
        row = []
        for g in g_steps:
            price = calculate_dcf(fcf_0, shares, cash, debt, growth_rate, g, w)
            row.append(round(price, 2))
        sensitivity_grid.append(row)
        
    # 3. Scenario Planning
    best_growth = growth_rate + 0.03
    best_wacc = max(0.04, wacc - 0.01)
    best_term = terminal_growth + 0.005
    best_price = calculate_dcf(fcf_0, shares, cash, debt, best_growth, best_term, best_wacc)
    
    worst_growth = max(0.01, growth_rate - 0.04)
    worst_wacc = wacc + 0.015
    worst_term = max(0.005, terminal_growth - 0.005)
    worst_price = calculate_dcf(fcf_0, shares, cash, debt, worst_growth, worst_term, worst_wacc)
    
    scenarios = {
        "best": {"growth": round(best_growth * 100, 2), "wacc": round(best_wacc * 100, 2), "price": round(best_price, 2), "upside": round(((best_price - last_price)/last_price*100), 2)},
        "base": {"growth": round(growth_rate * 100, 2), "wacc": round(wacc * 100, 2), "price": round(base_price, 2), "upside": round(upside, 2)},
        "worst": {"growth": round(worst_growth * 100, 2), "wacc": round(worst_wacc * 100, 2), "price": round(worst_price, 2), "upside": round(((worst_price - last_price)/last_price*100), 2)},
    }
    
    # 4. Monte Carlo Simulation (1,000 iterations)
    np.random.seed(42)
    mc_prices = []
    iterations = 1000
    
    sim_growths = np.random.normal(growth_rate, 0.03, iterations)
    sim_waccs = np.random.normal(wacc, 0.015, iterations)
    sim_terms = np.random.normal(terminal_growth, 0.005, iterations)
    
    for i in range(iterations):
        sg = max(0.01, min(0.25, sim_growths[i]))
        sw = max(0.04, min(0.20, sim_waccs[i]))
        st = max(0.005, min(0.04, sim_terms[i]))
        
        # Enforce mathematical stability (WACC > terminal growth)
        if sw <= st:
            sw = st + 0.01
            
        p = calculate_dcf(fcf_0, shares, cash, debt, sg, st, sw)
        mc_prices.append(p)
        
    mc_arr = np.array(mc_prices)
    mc_mean = float(np.mean(mc_arr))
    mc_median = float(np.median(mc_arr))
    mc_std = float(np.std(mc_arr))
    ci_90_lower = float(np.percentile(mc_arr, 5))
    ci_90_upper = float(np.percentile(mc_arr, 95))
    ci_95_lower = float(np.percentile(mc_arr, 2.5))
    ci_95_upper = float(np.percentile(mc_arr, 97.5))
    
    # Probability that implied DCF value is greater than the current stock price
    prob_undervalued = float(np.mean(mc_arr > last_price) * 100)
    
    return {
        "ticker": ticker,
        "company_name": fund.get("company_name", ticker),
        "last_price": round(last_price, 2),
        "implied_price": round(base_price, 2),
        "upside_pct": round(upside, 2),
        "wacc": round(wacc * 100, 2),
        "growth_rate": round(growth_rate * 100, 2),
        "terminal_growth": round(terminal_growth * 100, 2),
        "fcf_base": round(fcf_0 / 1e6, 2), # in millions
        "shares_outstanding": round(shares / 1e6, 2), # in millions
        "net_debt": round((debt - cash) / 1e6, 2), # in millions
        "sensitivity": {
            "wacc_steps": [round(x * 100, 1) for x in wacc_steps],
            "terminal_growth_steps": [round(x * 100, 2) for x in g_steps],
            "grid": sensitivity_grid
        },
        "scenarios": scenarios,
        "monte_carlo": {
            "mean": round(mc_mean, 2),
            "median": round(mc_median, 2),
            "std_dev": round(mc_std, 2),
            "confidence_90": [round(ci_90_lower, 2), round(ci_90_upper, 2)],
            "confidence_95": [round(ci_95_lower, 2), round(ci_95_upper, 2)],
            "probability_undervalued_pct": round(prob_undervalued, 2)
        }
    }
