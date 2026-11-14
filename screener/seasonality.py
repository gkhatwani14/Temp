"""Calendar-month seasonality overlay (the user's seasonality_analysis_prompt).

Pure functions over a monthly adjusted-close series:
  monthly returns across consecutive calendar months, |r| > 150% discarded
  Kruskal-Wallis across the 12 calendar-month groups (+ Benjamini-Hochberg
  across the family of stocks tested)
  best contiguous 2-10 month window on demeaned mean log returns
  per-year window returns, win rate, rest-of-year average, seasonal excess
  out-of-sample check: fit window on the first half, test on the second
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import kruskal

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def monthly_returns(monthly: pd.DataFrame) -> pd.DataFrame:
    """monthly: DataFrame(date, adj_close) one row per calendar month.
    Returns DataFrame(year, month, ret) only across consecutive months."""
    m = monthly.copy()
    m["date"] = pd.to_datetime(m["date"])
    m = m.sort_values("date")
    m["ym"] = m["date"].dt.year * 12 + (m["date"].dt.month - 1)
    m = m.drop_duplicates("ym", keep="last")
    m["ret"] = m["adj_close"] / m["adj_close"].shift(1) - 1.0
    m.loc[m["ym"].diff() != 1, "ret"] = np.nan
    m = m.dropna(subset=["ret"])
    m = m[m["ret"].abs() <= 1.5]
    return pd.DataFrame({"year": m["date"].dt.year.values, "month": m["date"].dt.month.values, "ret": m["ret"].values})


def _window_months(start: int, length: int) -> list[int]:
    return [((start - 1 + i) % 12) + 1 for i in range(length)]


def best_window(r: pd.DataFrame) -> tuple[int, int, float]:
    """(start_month, length, total demeaned mean log return) of the best 2-10 month run."""
    lr = np.log1p(r["ret"])
    overall = lr.mean()
    by_m = lr.groupby(r["month"].values).mean().reindex(range(1, 13)) - overall
    best = (1, 2, -math.inf)
    for length in range(2, 11):
        for start in range(1, 13):
            tot = float(by_m.loc[_window_months(start, length)].sum())
            if tot > best[2] + 1e-12:
                best = (start, length, tot)
    return best


def window_yearly(r: pd.DataFrame, start: int, length: int) -> pd.DataFrame:
    """Compound actual monthly returns inside the window for every window-year
    (a window that wraps over year-end is labelled by the year it starts in)."""
    months = _window_months(start, length)
    rows = []
    for y in sorted(r["year"].unique()):
        vals = []
        for i, mth in enumerate(months):
            yy = y + (1 if (start - 1 + i) >= 12 else 0)
            hit = r[(r["year"] == yy) & (r["month"] == mth)]["ret"]
            if hit.empty:
                vals = None
                break
            vals.append(float(hit.iloc[0]))
        if vals:
            rows.append({"year": int(y), "window_ret": float(np.prod([1 + v for v in vals]) - 1)})
    return pd.DataFrame(rows)


def rest_of_year(r: pd.DataFrame, start: int, length: int) -> float | None:
    months = set(_window_months(start, length))
    rest = r[~r["month"].isin(months)]
    if rest.empty:
        return None
    per_year = rest.groupby("year")["ret"].apply(lambda s: float(np.prod(1 + s) - 1))
    return float(per_year.mean())


def analyse(monthly: pd.DataFrame, min_obs: int = 10) -> dict:
    r = monthly_returns(monthly)
    counts = r.groupby("month")["ret"].count().reindex(range(1, 13)).fillna(0)
    if (counts < min_obs).any():
        return {"analysed": False, "reason": f"insufficient history: min {int(counts.min())} obs for a calendar month (< {min_obs})"}
    groups = [r[r["month"] == m]["ret"].values for m in range(1, 13)]
    h, p = kruskal(*groups)
    start, length, tot = best_window(r)
    wy = window_yearly(r, start, length)
    lr = np.log1p(r["ret"])
    pace = float(math.exp(lr.mean() * length) - 1)
    avg_w = float(wy["window_ret"].mean())
    win = float((wy["window_ret"] > 0).mean())
    excess = avg_w - pace
    tier = None
    if p < 0.02 and win >= 0.80 and excess >= 0.10:
        tier = "Strong"
    elif p < 0.05 and win >= 0.70 and excess >= 0.05:
        tier = "Moderate"
    end_month = _window_months(start, length)[-1]
    profile = r.groupby("month")["ret"].mean().reindex(range(1, 13))
    return {
        "analysed": True, "n_months": int(len(r)), "first_year": int(r["year"].min()), "last_year": int(r["year"].max()),
        "kw_p": float(p), "kw_h": float(h),
        "buy_month": f"start of {MONTHS[start - 1]}", "sell_month": f"end of {MONTHS[end_month - 1]}",
        "window_start": start, "window_length": length, "window_demeaned_log": tot,
        "avg_window_return": avg_w, "win_rate": win, "wins": int((wy["window_ret"] > 0).sum()), "years": int(len(wy)),
        "rest_of_year_avg": rest_of_year(r, start, length), "seasonal_excess": excess,
        "score": (win - 0.5) * excess, "tier": tier,
        "month_profile": {MONTHS[i - 1]: float(profile.loc[i]) for i in range(1, 13)},
        "backtest": backtest(r),
    }


def backtest(r: pd.DataFrame, cost: float = 0.001) -> dict:
    """Fit the window on the first half of the years, test it on the second half.
    Strategy: long in the window, cash otherwise, 0.10% per one-way trade."""
    years = sorted(r["year"].unique())
    if len(years) < 8:
        return {"run": False}
    split = years[len(years) // 2]
    fit, test = r[r["year"] < split], r[r["year"] >= split]
    if fit.groupby("month")["ret"].count().reindex(range(1, 13)).fillna(0).min() < 4:
        return {"run": False}
    start, length, _ = best_window(fit)

    def run(part: pd.DataFrame) -> dict:
        wy = window_yearly(part, start, length)
        strat = float(np.prod([(1 + w) * (1 - cost) ** 2 for w in wy["window_ret"]]) - 1) if len(wy) else None
        bh = float(np.prod(1 + part["ret"]) - 1)
        return {"strategy_total": strat, "buy_hold_total": bh, "years": int(len(wy)),
                "avg_window_return": float(wy["window_ret"].mean()) if len(wy) else None,
                "win_rate": float((wy["window_ret"] > 0).mean()) if len(wy) else None}

    ins, oos = run(fit), run(test)
    end_month = _window_months(start, length)[-1]
    return {"run": True, "split_year": int(split), "fit_window": f"{MONTHS[start - 1]}-{MONTHS[end_month - 1]}",
            "in_sample": ins, "out_of_sample": oos,
            "held_out_of_sample": bool(oos["avg_window_return"] is not None and oos["avg_window_return"] > 0 and (oos["win_rate"] or 0) >= 0.6)}


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    q = np.empty(n)
    prev = 1.0
    for rank, idx in enumerate(order[::-1]):
        k = n - rank
        prev = min(prev, p[idx] * n / k)
        q[idx] = prev
    return q.tolist()
