"""TradingView scanner API (fundamentals supplied to TradingView by FactSet).

POST https://scanner.tradingview.com/{market}/scan with a JSON body returns
any of ~3,800 columns for every listed instrument in one request, which makes
it the bulk first-pass provider for the screen. `*_h` columns are history
slices (most-recent first).
"""
from __future__ import annotations

import json

import pandas as pd

from ..http import fetch

SCAN_URL = "https://scanner.tradingview.com/{market}/scan"

# Columns pulled for the whole universe.
BULK_COLUMNS = [
    # identity
    "name", "description", "exchange", "type", "typespecs", "is_primary",
    "currency", "fundamental_currency_code", "country", "sector", "industry",
    # price / technicals (daily)
    "close", "volume", "RSI", "RSI[1]", "SMA50", "SMA200", "EMA50", "EMA100", "EMA200",
    "ATR", "BB.upper", "BB.lower", "BB.basis",
    "average_volume_90d_calc", "AvgValue.Traded_90d", "market_cap_basic",
    "price_52_week_high", "price_52_week_low",
    "Perf.1M", "Perf.3M", "Perf.Y", "Perf.3Y",
    # quality / solvency
    "free_cash_flow_ttm", "free_cash_flow_fy", "altman_z_score_ttm", "altman_z_score_fy",
    "net_debt_fq", "total_debt_fq", "cash_n_short_term_invest_fq", "ebitda_ttm",
    "net_debt_to_ebitda_fq", "ebitda_interst_cover_ttm", "interst_cover_ttm",
    "debt_to_equity_fq", "current_ratio_fq", "piotroski_f_score_ttm",
    "return_on_invested_capital_fq", "gross_margin_ttm", "operating_margin_ttm",
    "free_cash_flow_margin_ttm",
    # growth / valuation
    "earnings_per_share_diluted_ttm", "last_annual_eps", "earnings_per_share_diluted_fy_h",
    "fiscal_period_fy_h", "price_earnings_ttm", "price_earnings_growth_ttm",
    "price_earnings_forward_fy", "enterprise_value_ebitda_ttm", "enterprise_value_current",
    # sentiment
    "price_target_average", "price_target_median", "recommendation_mark",
    "recommendation_total", "recommendation_buy", "recommendation_over",
    "recommendation_hold", "recommendation_under", "recommendation_sell",
    # catalysts
    "earnings_release_next_date", "earnings_release_date", "eps_surprise_percent_fq",
]


def scan(market: str, columns: list[str], *, filters: list[dict] | None = None,
         tickers: list[str] | None = None, max_age: float | None = 6 * 3600,
         page: int = 5000) -> tuple[pd.DataFrame, str]:
    """Run a scanner query; returns (DataFrame indexed by 'EXCH:SYM', retrieved_at)."""
    rows, start, retrieved = [], 0, ""
    while True:
        body: dict = {"columns": columns, "range": [start, start + page],
                      "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
                      "options": {"lang": "en"}}
        if filters:
            body["filter"] = filters
        if tickers:
            body["symbols"] = {"tickers": tickers}
        res = fetch(SCAN_URL.format(market=market), method="POST", json_body=body,
                    headers={"Content-Type": "application/json"}, max_age=max_age)
        payload = json.loads(res["text"])
        retrieved = res["retrieved_at"]
        data = payload.get("data") or []
        rows.extend(data)
        start += page
        if tickers or start >= payload.get("totalCount", 0) or not data:
            break
    df = pd.DataFrame([r["d"] for r in rows], columns=columns, index=[r["s"] for r in rows])
    df.index.name = "tv_id"
    return df, retrieved


def bulk_universe(max_age: float | None = 6 * 3600) -> tuple[pd.DataFrame, str]:
    """All US + UK listed stocks with the BULK_COLUMNS set."""
    stock_filter = [{"left": "type", "operation": "in_range", "right": ["stock", "dr", "fund"]}]
    us, ts = scan("america", BULK_COLUMNS, filters=stock_filter + [
        {"left": "exchange", "operation": "in_range", "right": ["NYSE", "NASDAQ", "AMEX", "CBOE"]}],
        max_age=max_age)
    uk, _ = scan("uk", BULK_COLUMNS, filters=stock_filter + [
        {"left": "exchange", "operation": "in_range", "right": ["LSE"]}], max_age=max_age)
    us["market"], uk["market"] = "america", "uk"
    return pd.concat([us, uk]), ts
