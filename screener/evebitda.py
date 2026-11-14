"""Historic EV/EBITDA (TTM) gate -- series construction and gate statistics.

Series construction (point-in-time, sourced data only):
  EV(t)      = market cap(t) + total debt - cash & equivalents, where the
               balance-sheet items are the most recent ones PUBLIC at date t
               (filing date <= t) and market cap(t) = month-end close x shares
               outstanding on the same split basis.
  EBITDA(t)  = trailing four reported quarters public at date t
               (operating income + depreciation & amortisation, as reported).
Gate (one option must hold):
  A  current <= 3-year moving average AND <= 5-year median
  B  current <= 25th percentile of the 5-year distribution
  C  current >= 20% below the 3-year MA AND trailing 4-quarter EBITDA growth > 0
"""
from __future__ import annotations

import math
from bisect import bisect_right

import numpy as np
import pandas as pd


def build_monthly_series(prices: pd.DataFrame, quarters: list[dict], *, years: int = 5,
                         split_events: list[tuple[str, float]] | None = None) -> pd.DataFrame:
    """Monthly point-in-time EV/EBITDA.

    prices   daily DataFrame (date, close) with close split-adjusted to today's basis.
    quarters list of dicts sorted by period_end with keys: period_end, filed,
             ebitda_ttm, total_debt, cash, shares (as reported at the time).
    split_events [(effective_date, ratio)] -- shares reported BEFORE a split's
             effective date are multiplied by the ratio so they sit on the same
             basis as the split-adjusted price.
    """
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"])
    px = px.set_index("date")["close"].astype(float)
    month_end = px.resample("ME").last().dropna()
    end = px.index[-1]
    month_end = month_end[month_end.index > end - pd.DateOffset(years=years, days=5)]
    # The final observation is the report-date close, not a calendar month-end.
    month_end = month_end[month_end.index < end]
    month_end.loc[end] = px.iloc[-1]

    q = [x for x in quarters if x.get("filed")]
    q.sort(key=lambda x: x["filed"])
    filed_dates = [pd.Timestamp(x["filed"]) for x in q]
    splits = [(pd.Timestamp(d), float(r)) for d, r in (split_events or [])]

    rows = []
    for dt, close in month_end.items():
        i = bisect_right(filed_dates, dt) - 1
        if i < 0:
            continue
        # most recent period_end among filings public at dt (restatements included)
        known = max(q[: i + 1], key=lambda x: (x["period_end"], x["filed"]))
        shares = known.get("shares")
        if shares is None or known.get("ebitda_ttm") is None:
            continue
        factor = 1.0
        for sdate, ratio in splits:
            if pd.Timestamp(known["shares_as_of"] if known.get("shares_as_of") else known["period_end"]) < sdate:
                factor *= ratio
        mcap = close * shares * factor
        debt = known.get("total_debt") or 0.0
        cash = known.get("cash") or 0.0
        ev = mcap + debt - cash
        ebitda = known["ebitda_ttm"]
        rows.append({"date": dt, "close": close, "market_cap": mcap, "total_debt": debt, "cash": cash, "ev": ev,
                     "ebitda_ttm": ebitda, "ev_ebitda": ev / ebitda if ebitda and ebitda > 0 else np.nan,
                     "fundamentals_period": known["period_end"], "fundamentals_filed": known["filed"]})
    return pd.DataFrame(rows)


def gate_stats(history: list[float], current: float | None, *, ebitda_growth_4q: float | None,
               points_per_year: int = 12, series_years: float = 5.0) -> dict:
    """Evaluate Options A/B/C on a history ordered oldest -> newest whose last
    element is the current value. `points_per_year` = 12 (monthly) or 4 (quarterly)."""
    vals = np.array([v for v in history if v is not None and not (isinstance(v, float) and math.isnan(v))], dtype=float)
    out: dict = {"n_obs": int(len(vals)), "evaluable": False}
    if current is None or (isinstance(current, float) and math.isnan(current)) or current <= 0:
        out["reason"] = "current EV/EBITDA not meaningful (EBITDA <= 0 or unavailable)"
        return out
    if len(vals) < 3 * points_per_year:
        out["reason"] = f"only {len(vals)} sourced observations (< 3 years)"
        return out
    ma3 = float(vals[-3 * points_per_year:].mean())
    med5, mean5, sd5, mn = float(np.median(vals)), float(vals.mean()), float(vals.std(ddof=1)), float(vals.min())
    p25 = float(np.percentile(vals, 25))
    pct_rank = float((vals <= current).mean() * 100.0)
    a = current <= ma3 and current <= med5
    b = current <= p25
    c = (current <= 0.8 * ma3) and (ebitda_growth_4q is not None and ebitda_growth_4q > 0)
    out.update(
        evaluable=True, current=float(current), min=mn, mean=mean5, median=med5, stdev=sd5, p25=p25, ma_3y=ma3,
        percentile_rank=pct_rank, z_score=float((current - mean5) / sd5) if sd5 else None,
        pct_vs_min=float(current / mn - 1.0), pct_vs_ma_3y=float(current / ma3 - 1.0),
        ebitda_growth_4q=ebitda_growth_4q, option_a=bool(a), option_b=bool(b), option_c=bool(c),
        passed=bool(a or b or c), option_used=("A" if a else "B" if b else "C" if c else None),
        short_series=bool(series_years < 4.75),
    )
    return out


def one_line(stats: dict, value_trap: bool) -> str:
    if not stats.get("evaluable"):
        return f"EV/EBITDA (TTM) = n/a | NOT EVALUABLE ({stats.get('reason')})"
    return (f"EV/EBITDA (TTM) = {stats['current']:.2f} | 5Y Median = {stats['median']:.2f} | "
            f"Percentile = {stats['percentile_rank']:.0f}% | Z-Score = {stats['z_score']:.2f} | "
            f"Pass = {'YES' if stats['passed'] else 'NO'} | Value Trap = {'YES' if value_trap else 'NO'}")
