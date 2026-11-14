"""Technical-regime gate and oversold-reversal scorecard (pure functions).

Input everywhere is a daily OHLCV DataFrame sorted ascending with columns
date, open, high, low, close, volume (close on a split-adjusted basis).

Definitions follow the screen spec ("MOMENTUM / TECHNICALS" module):
  hard gate        RSI(14) < 30 on the report date
  falling knives   RSI(14) < 15 ; price more than 25% below the 50-day MA
  scorecard (0-6)  RSI sweet spot (0/1/2) + bullish RSI divergence (1)
                   + price stabilisation (1) + capitulation-volume exhaustion (1)
                   + institutional accumulation (1, supplied by the caller)
Where the spec leaves an operational detail open, the choice made here is
stated in the docstring of the function and echoed in the output record.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


# ----------------------------------------------------------------- indicators
def wilder_rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI: first average = simple mean of the first n changes, then
    Wilder smoothing (alpha = 1/n). Matches TradingView / StockCharts."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = pd.Series(np.nan, index=close.index, dtype=float)
    avg_loss = pd.Series(np.nan, index=close.index, dtype=float)
    if len(close) <= n:
        return avg_gain
    g0, l0 = gain.iloc[1 : n + 1].mean(), loss.iloc[1 : n + 1].mean()
    avg_gain.iloc[n], avg_loss.iloc[n] = g0, l0
    g, l = g0, l0
    gv, lv = gain.to_numpy(), loss.to_numpy()
    out_g, out_l = avg_gain.to_numpy().copy(), avg_loss.to_numpy().copy()
    for i in range(n + 1, len(close)):
        g = (g * (n - 1) + gv[i]) / n
        l = (l * (n - 1) + lv[i]) / n
        out_g[i], out_l[i] = g, l
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = out_g / out_l
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi[np.isnan(out_g)] = np.nan
    rsi[(out_l == 0) & ~np.isnan(out_g)] = 100.0
    return pd.Series(rsi, index=close.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([(df["high"] - df["low"]), (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


# ------------------------------------------------------------------ hard gate
def technical_gate(df: pd.DataFrame) -> dict:
    """RSI(14) < 30 hard gate plus the two price-based falling-knife exclusions."""
    c = df["close"].astype(float)
    rsi = wilder_rsi(c)
    sma50 = c.rolling(50).mean()
    last_rsi, last_c, last_sma = float(rsi.iloc[-1]), float(c.iloc[-1]), float(sma50.iloc[-1])
    gap = last_c / last_sma - 1.0
    return {
        "as_of": str(df["date"].iloc[-1])[:10],
        "close": last_c,
        "rsi14": last_rsi,
        "sma50": last_sma,
        "gap_vs_sma50": gap,
        "rsi_below_30": last_rsi < 30.0,
        "knife_rsi_below_15": last_rsi < 15.0,
        "knife_more_than_25pct_below_sma50": gap < -0.25,
    }


# ------------------------------------------------------------------ scorecard
def rsi_sweet_spot_points(rsi: float) -> int:
    """20 <= RSI <= 25 -> 2 ; 25 < RSI < 30 -> 1 ; 15 <= RSI < 20 -> 0."""
    if 20.0 <= rsi <= 25.0:
        return 2
    if 25.0 < rsi < 30.0:
        return 1
    return 0


def bullish_divergence(df: pd.DataFrame, window: int = 20, k: int = 2, min_sep: int = 3) -> dict:
    """Bullish RSI divergence inside the last `window` sessions.

    Operationalisation: swing lows are closing lows -- a session whose close is
    the minimum of the closes within +/-k sessions (the right-hand side may be
    truncated at the latest bar, so the latest close can itself be a low).
    Divergence = the most recent swing low closes BELOW an earlier swing low
    (>= min_sep sessions before it, same window) while RSI(14) at the recent low
    is ABOVE RSI at that earlier low. Closes are used because RSI is a
    close-based oscillator.
    """
    c = df["close"].astype(float).reset_index(drop=True)
    rsi = wilder_rsi(c)
    n = len(c)
    lo = max(0, n - window)
    lows = []
    for i in range(lo, n):
        a, b = max(lo, i - k), min(n - 1, i + k)
        if c.iloc[i] <= c.iloc[a : b + 1].min():
            lows.append(i)
    # collapse runs of adjacent equal lows to the last one
    lows = [i for j, i in enumerate(lows) if j == len(lows) - 1 or lows[j + 1] - i > 1]
    out = {"divergence": False, "recent_low": None, "prior_low": None, "window_sessions": window}
    if len(lows) < 2:
        return out
    recent = lows[-1]
    for prior in reversed(lows[:-1]):
        if recent - prior < min_sep:
            continue
        if c.iloc[recent] < c.iloc[prior] and rsi.iloc[recent] > rsi.iloc[prior]:
            out.update(
                divergence=True,
                recent_low={"date": str(df["date"].iloc[recent])[:10], "close": float(c.iloc[recent]), "rsi": float(rsi.iloc[recent])},
                prior_low={"date": str(df["date"].iloc[prior])[:10], "close": float(c.iloc[prior]), "rsi": float(rsi.iloc[prior])},
            )
            return out
    out["recent_low"] = {"date": str(df["date"].iloc[recent])[:10], "close": float(c.iloc[recent]), "rsi": float(rsi.iloc[recent])}
    return out


def price_stabilisation(df: pd.DataFrame, lookback: int = 20, recent: int = 5) -> dict:
    """No new 20-session closing low in the last 5 sessions.

    A session sets a "new 20-session closing low" when its close is the lowest
    close of the 20 sessions ending on that session.
    """
    c = df["close"].astype(float).reset_index(drop=True)
    roll_min = c.rolling(lookback).min()
    new_low = c <= roll_min
    tail = new_low.iloc[-recent:]
    last_low_idx = new_low[new_low].index.max() if new_low.any() else None
    return {
        "stabilised": not bool(tail.any()),
        "new_low_sessions_in_last_5": int(tail.sum()),
        "last_new_20d_closing_low": None if last_low_idx is None else str(df["date"].iloc[int(last_low_idx)])[:10],
        "sessions_since_last_new_low": None if last_low_idx is None else int(len(c) - 1 - last_low_idx),
    }


def volume_exhaustion(df: pd.DataFrame, window: int = 20, avg_n: int = 90, min_age: int = 5) -> dict:
    """Capitulation-volume exhaustion.

    Flush day = highest-volume DOWN day (close < prior close) of the last 20
    sessions. Point requires: flush volume >= 2x the 90-session average volume
    (measured over the 90 sessions ending the day BEFORE the flush, so the flush
    does not dilute its own benchmark), the flush is >= 5 sessions old, and
    down-day volume has contracted since: every later down day is below the
    flush volume and the mean volume of the later half of those down days is
    below the mean of the earlier half (a single later down day just has to be
    below the flush; no later down day at all also counts as contracted).
    """
    d = df.reset_index(drop=True)
    c, v = d["close"].astype(float), d["volume"].astype(float)
    n = len(d)
    down = c < c.shift(1)
    idx = [i for i in range(max(1, n - window), n) if down.iloc[i]]
    out = {"exhaustion": False, "flush_date": None, "flush_volume": None, "avg90_before_flush": None,
           "flush_multiple": None, "flush_age_sessions": None, "down_days_since": None, "contracting": None}
    if not idx:
        return out
    f = max(idx, key=lambda i: v.iloc[i])
    avg90 = float(v.iloc[max(0, f - avg_n) : f].mean())
    age = n - 1 - f
    later = [i for i in idx if i > f]
    if not later:
        contracting = True
    elif len(later) == 1:
        contracting = bool(v.iloc[later[0]] < v.iloc[f])
    else:
        h = len(later) // 2
        contracting = bool(v.iloc[later[h:]].mean() < v.iloc[later[:h]].mean())
    mult = float(v.iloc[f] / avg90) if avg90 else float("nan")
    out.update(
        flush_date=str(d["date"].iloc[f])[:10], flush_volume=float(v.iloc[f]), avg90_before_flush=avg90,
        flush_multiple=mult, flush_age_sessions=int(age), down_days_since=len(later), contracting=contracting,
        exhaustion=bool(mult >= 2.0 and age >= min_age and contracting),
    )
    return out


def reversal_scorecard(df: pd.DataFrame, institutional_net_buying: bool | None) -> dict:
    """Oversold-reversal scorecard 0-6. `institutional_net_buying` None = data
    unavailable (scores 0 and is flagged)."""
    gate = technical_gate(df)
    div, stab, vol = bullish_divergence(df), price_stabilisation(df), volume_exhaustion(df)
    pts = {
        "rsi_sweet_spot": rsi_sweet_spot_points(gate["rsi14"]),
        "bullish_divergence": int(div["divergence"]),
        "price_stabilisation": int(stab["stabilised"]),
        "volume_exhaustion": int(vol["exhaustion"]),
        "institutional_accumulation": int(bool(institutional_net_buying)),
    }
    return {"score": sum(pts.values()), "max": 6, "points": pts, "detail": {"divergence": div, "stabilisation": stab, "volume": vol},
            "institutional_data_available": institutional_net_buying is not None}


# ------------------------------------------------- risk-adjusted performance
def sharpe_3y_weekly(df: pd.DataFrame, rf_annual_pct: float) -> dict:
    """3-year Sharpe from weekly (Friday-to-Friday) returns, annualised by
    sqrt(52); risk-free = 3-month T-bill yield as of the report date."""
    s = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
    end = s.index[-1]
    s = s[s.index > end - pd.DateOffset(years=3) - pd.Timedelta(days=7)]
    w = s.resample("W-FRI").last().dropna()
    r = w.pct_change().dropna()
    if len(r) < 100:
        return {"sharpe": None, "weeks": int(len(r)), "note": "fewer than 100 weekly returns"}
    rf_w = (1 + rf_annual_pct / 100.0) ** (1 / 52.0) - 1
    ex = r - rf_w
    sharpe = float(ex.mean() / ex.std(ddof=1) * math.sqrt(52))
    return {"sharpe": sharpe, "weeks": int(len(r)), "ann_return": float((1 + r.mean()) ** 52 - 1),
            "ann_vol": float(r.std(ddof=1) * math.sqrt(52)), "rf_annual_pct": rf_annual_pct}


def price_return(df: pd.DataFrame, years: int = 3) -> dict:
    s = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
    end = s.index[-1]
    past = s[s.index <= end - pd.DateOffset(years=years)]
    if past.empty:
        return {"return": None, "note": f"less than {years}y of history"}
    return {"return": float(s.iloc[-1] / past.iloc[-1] - 1), "from": str(past.index[-1])[:10], "to": str(end)[:10]}


# ------------------------------------ consolidation / "broken price" diagnostics
def consolidation_diagnostics(df: pd.DataFrame) -> dict:
    """Indicators from the user's notes: ATR compression, Bollinger squeeze,
    EMA confluence, deviation from the 200-day MA, death cross, down-day volume."""
    d = df.reset_index(drop=True)
    c, v = d["close"].astype(float), d["volume"].astype(float)
    atr = wilder_atr(d)
    atrp = atr / c
    bb_mid, bb_sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    bbw = (4 * bb_sd) / bb_mid
    ema = {n: c.ewm(span=n, adjust=False).mean() for n in (50, 100, 200)}
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    spread = (max(e.iloc[-1] for e in ema.values()) - min(e.iloc[-1] for e in ema.values())) / c.iloc[-1]
    cross = (sma50 < sma200) & (sma50.shift(1) >= sma200.shift(1))
    last_cross = cross[cross].index.max() if cross.any() else None
    down = c < c.shift(1)
    v20_down = v[down].iloc[-10:].mean() if down.any() else float("nan")
    v20_up = v[~down].iloc[-10:].mean()
    hist = bbw.iloc[-252:].dropna()
    hist_atr = atrp.iloc[-252:].dropna()
    return {
        "atr14_pct_of_price": float(atrp.iloc[-1]),
        "atr_pct_percentile_1y": float((hist_atr <= atrp.iloc[-1]).mean()) if len(hist_atr) else None,
        "atr_compressing": bool(atrp.iloc[-1] < atrp.iloc[-21]) if len(atrp) > 21 else None,
        "bollinger_width": float(bbw.iloc[-1]),
        "bollinger_width_percentile_1y": float((hist <= bbw.iloc[-1]).mean()) if len(hist) else None,
        "bollinger_squeeze": bool((hist <= bbw.iloc[-1]).mean() <= 0.10) if len(hist) else None,
        "ema50_100_200_spread_pct_of_price": float(spread),
        "ema_confluence": bool(spread < 0.05),
        "deviation_from_sma200": float(c.iloc[-1] / sma200.iloc[-1] - 1) if not math.isnan(sma200.iloc[-1]) else None,
        "sma50_below_sma200": bool(sma50.iloc[-1] < sma200.iloc[-1]) if not math.isnan(sma200.iloc[-1]) else None,
        "last_death_cross": None if last_cross is None else str(d["date"].iloc[int(last_cross)])[:10],
        "down_day_vs_up_day_volume_last10": float(v20_down / v20_up) if v20_up else None,
    }
