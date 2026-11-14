"""Stage-1 screen: evaluate every Tier-1 hard gate for the collected tickers,
then the Tier-2 scorecard for ranking.

    python3 -m screener.screen            -> output/<date>/gate_table.csv + facts JSON

Every gate carries the per-provider values it was decided from, so a reader
can see which provider said what and where they disagree.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import collect, evebitda, gates, technicals

ROOT = Path(__file__).resolve().parent.parent
REPORT_DATE = "2026-11-14"       # report date (a Saturday); prices as of the 2026-11-13 close
LAST_CLOSE = "2026-11-13"
FINANCIAL_SECTORS = {"Finance"}


def g(d, path, default=None):
    """Nested get: g(rec, 'zacks.quote.peg_ratio')."""
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur


def num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def price_frame(rec: dict, key: str) -> pd.DataFrame | None:
    rows = g(rec, f"{key}.rows")
    if not isinstance(rows, list) or len(rows) < 60:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return df[df["date"] <= LAST_CLOSE + " 23:59"]


# --------------------------------------------------------------- facts per gate
def mean_rating_from_counts(c: dict | None):
    if not isinstance(c, dict):
        return None
    w = [(c.get("strong_buy"), 1), (c.get("buy"), 2), (c.get("hold"), 3), (c.get("sell"), 4), (c.get("strong_sell"), 5)]
    tot = sum(n for n, _ in w if n)
    return None if not tot else sum(n * s for n, s in w if n) / tot


def sec_eps_pair(rec: dict):
    yrs = [y for y in (g(rec, "sec_annual_eps.years") or []) if y.get("eps_diluted") is not None]
    yrs.sort(key=lambda y: y["fiscal_year_end"])
    if len(yrs) < 4:
        return None
    return {"latest": yrs[-1]["eps_diluted"], "base": yrs[-4]["eps_diluted"],
            "latest_fy": yrs[-1]["fiscal_year_end"], "base_fy": yrs[-4]["fiscal_year_end"]}


def sa_eps_pair(rec: dict):
    vals = g(rec, "sa_income_annual.rows.epsdil.values") or {}
    cols = sorted(k for k in vals if k != "TTM" and vals[k] is not None)
    if len(cols) < 4:
        return None
    return {"latest": vals[cols[-1]], "base": vals[cols[-4]], "latest_fy": cols[-1], "base_fy": cols[-4]}


def tv_eps_pair(tv: pd.Series):
    e, fy = tv.get("earnings_per_share_diluted_fy_h"), tv.get("fiscal_period_fy_h")
    if not isinstance(e, (list, np.ndarray)) or len(e) < 4 or e[0] is None or e[3] is None:
        return None
    return {"latest": e[0], "base": e[3], "latest_fy": fy[0] if isinstance(fy, (list, np.ndarray)) else None,
            "base_fy": fy[3] if isinstance(fy, (list, np.ndarray)) else None}


def sec_altman_z(rec: dict, mcap: float | None):
    a = rec.get("sec_altman") or {}
    need = [a.get(k) for k in ("working_capital", "retained_earnings", "ebit_ttm", "total_assets", "total_liabilities", "revenue_ttm")]
    if (a.get("currency") or "USD") != "USD":
        return None  # filings in another currency: a USD market cap cannot be mixed in without sourced FX history
    if any(v is None for v in need) or not mcap or not a.get("total_assets") or not a.get("total_liabilities"):
        return None
    wc, re_, ebit, ta, tl, rev = need
    return 1.2 * wc / ta + 1.4 * re_ / ta + 3.3 * ebit / ta + 0.6 * mcap / tl + 1.0 * rev / ta


def evaluate(ticker: str, tv: pd.Series, rf_pct: float | None) -> dict:
    rec = collect.load(ticker)
    out: dict = {"ticker": ticker, "company": tv.get("description") or rec.get("company"), "exchange": tv.get("exchange"),
                 "sector": tv.get("sector"), "industry": tv.get("industry")}
    is_fin = (tv.get("sector") in FINANCIAL_SECTORS) or bool(g(rec, "sec_ttm.is_financial_sector"))
    out["is_financial"] = is_fin

    # ---------------------------------------------------------- technical gate
    px_sa, px_nq = price_frame(rec, "sa_prices_daily"), price_frame(rec, "nasdaq_prices_daily")
    px = px_sa if px_sa is not None else px_nq
    tech = {"factset_tv": {"rsi14": num(tv.get("RSI")), "sma50": num(tv.get("SMA50")), "close": num(tv.get("close"))}}
    for name, frame in (("stockanalysis", px_sa), ("nasdaq", px_nq)):
        if frame is not None:
            tech[name] = technicals.technical_gate(frame)
    own = [tech[k] for k in ("stockanalysis", "nasdaq") if k in tech]
    rsis = [t["rsi14"] for t in own] + [tech["factset_tv"]["rsi14"]]
    gaps = [t["gap_vs_sma50"] for t in own] + [tech["factset_tv"]["close"] / tech["factset_tv"]["sma50"] - 1]
    out["price"] = own[0]["close"] if own else tech["factset_tv"]["close"]
    out["rsi14"] = own[0]["rsi14"] if own else tech["factset_tv"]["rsi14"]
    out["gap50"] = own[0]["gap_vs_sma50"] if own else gaps[-1]
    out["rsi_all_sources"] = [round(r, 2) for r in rsis if r is not None]
    out["gap50_all_sources"] = [round(x, 4) for x in gaps if x is not None]
    rsi_pass = [r < 30 for r in rsis if r is not None]
    out["tech_rsi_lt30"] = all(rsi_pass)
    out["tech_rsi_conflict"] = len(set(rsi_pass)) > 1
    out["knife_rsi_lt15"] = any(r < 15 for r in rsis if r is not None)
    knife = [x < -0.25 for x in gaps if x is not None]
    out["knife_gap25"] = any(knife)
    out["knife_gap25_conflict"] = len(set(knife)) > 1
    out["technical"] = tech

    # ------------------------------------------------------------- universe
    mcap = num(tv.get("market_cap_basic"))
    out["mcap_usd"] = mcap
    out["mcap_sources"] = {"factset_tv": mcap, "stockanalysis": num(g(rec, "sa_statistics.fields.market_cap")),
                           "zacks": num(g(rec, "zacks.quote.market_cap"))}
    adv = None
    if px is not None:
        tail = px.tail(90)
        adv = float((tail["close"] * tail["volume"]).mean())
    out["adv90_usd"] = {"computed_from_prices": adv, "factset_tv": num(tv.get("AvgValue.Traded_90d"))}
    out["universe_pass"] = bool(mcap and mcap > 1e9 and (adv or num(tv.get("AvgValue.Traded_90d")) or 0) > 10e6)

    # ------------------------------------------------------------------ FCF
    q = (g(rec, "sec_ttm.quarters") or [])
    lastq = q[-1] if q else {}
    out["sec_latest_period"] = {"period_end": lastq.get("period_end"), "filed": lastq.get("filed"), "form": lastq.get("form")}
    zfcf = num(g(rec, "zacks.fundamentals.free_cash_flow_ttm"))
    fcf = gates.gate_fcf({"sec": lastq.get("fcf_ttm"), "factset": tv.get("free_cash_flow_ttm"),
                          "stockanalysis": g(rec, "sa_statistics.fields.free_cash_flow_ttm"),
                          "zacks": None if zfcf is None else zfcf * 1e6})
    # ------------------------------------------------------------- Altman Z
    alt = gates.gate_altman({"factset": tv.get("altman_z_score_ttm"), "stockanalysis": g(rec, "sa_statistics.fields.altman_z_score"),
                             "sec": sec_altman_z(rec, mcap)}, is_fin)
    # ------------------------------------------------------------- solvency
    sec_nd = None
    if lastq.get("total_debt") is not None and lastq.get("cash") is not None:
        sec_nd = lastq["total_debt"] - lastq["cash"] - (lastq.get("short_term_investments") or 0.0)
    sec_ic = None
    if num(lastq.get("ebit_ttm")) is not None and num(lastq.get("interest_expense_ttm")):
        sec_ic = lastq["ebit_ttm"] / lastq["interest_expense_ttm"]
    sa_nc = num(g(rec, "sa_statistics.fields.net_cash"))
    # --- quality checks on the filing-derived EBITDA
    sec_da, sa_da = num(lastq.get("depreciation_amortization_ttm")), num(g(rec, "sa_statistics.fields.d_and_a_ttm"))
    sec_ebitda, ebitda_note = num(lastq.get("ebitda_ttm")), None
    sec_bad_signs = (sec_da is not None and sec_da < 0) or (num(lastq.get("interest_expense_ttm")) or 0) < 0
    da_undercaptured = bool(sec_da is not None and sa_da and sa_da > 0 and sec_da < 0.7 * sa_da and not sec_bad_signs)
    if sec_bad_signs:      # IFRS filers report D&A / interest as negative numbers; the derived EBITDA is not usable
        sec_ebitda, sec_ic, sec_nd = None, None, None
        ebitda_note = "filing-derived EBITDA unusable (sign convention of the IFRS tags); providers decide"
    elif da_undercaptured and num(lastq.get("ebit_ttm")) is not None:
        sec_ebitda = lastq["ebit_ttm"] + sa_da
        ebitda_note = (f"as-reported operating income {lastq['ebit_ttm'] / 1e6:,.0f}M + provider D&A {sa_da / 1e6:,.0f}M "
                       f"(the filing's D&A tag, {sec_da / 1e6:,.0f}M, omits separately tagged intangible amortisation)")
    out["sec_ebitda_quality"] = {"sec_d_and_a_ttm": sec_da, "provider_d_and_a_ttm": sa_da, "d_and_a_undercaptured": da_undercaptured,
                                 "bad_signs": sec_bad_signs, "ebitda_used": sec_ebitda, "note": ebitda_note}
    solv = gates.gate_solvency({
        "sec": {"net_debt": sec_nd, "ebitda": sec_ebitda, "interest_coverage": sec_ic,
                "no_interest_expense_reported": lastq.get("interest_expense_ttm") in (None, 0)},
        "factset": {"net_debt": tv.get("net_debt_fq"), "ebitda": tv.get("ebitda_ttm"), "interest_coverage": tv.get("interst_cover_ttm")},
        "stockanalysis": {"net_debt": None if sa_nc is None else -sa_nc, "ebitda": g(rec, "sa_statistics.fields.ebitda_ttm"),
                          "interest_coverage": g(rec, "sa_statistics.fields.interest_coverage")},
    })
    # ---------------------------------------------------------- EPS CAGR 3y
    fv_cagr = num(g(rec, "finviz.snapshot.fields.eps_growth_past_3y_pct"))
    pairs = {"sec": sec_eps_pair(rec), "factset": tv_eps_pair(tv), "stockanalysis": sa_eps_pair(rec)}
    fy_end = {"sec": str((pairs["sec"] or {}).get("latest_fy") or "")[:7],
              "stockanalysis": str((pairs["stockanalysis"] or {}).get("latest_fy") or "")[:7]}
    newest = max([v for v in fy_end.values() if v] or [""])
    # A filer that has reported a fiscal year but not yet filed the annual report lags in the SEC data: drop any lagging pair.
    stale = [p for p, v in fy_end.items() if v and v < newest]
    out["eps_pairs"], out["eps_pairs_stale"] = pairs, stale
    eps = gates.gate_eps_cagr({k: v for k, v in {
        **{p: pr for p, pr in pairs.items() if p not in stale},
        "finviz": None if fv_cagr is None else {"cagr": fv_cagr / 100.0}}.items() if v})
    # ------------------------------------------------------------------ PEG
    peg = gates.gate_peg({"zacks": g(rec, "zacks.quote.peg_ratio"), "nasdaq": g(rec, "nasdaq.analyst.peg_ratio.peg"),
                          "stockanalysis": g(rec, "sa_statistics.fields.peg_ratio")})
    out["peg_inputs"] = {"zacks_forward_pe_f1": g(rec, "zacks.quote.forward_pe_f1"),
                         "finviz_peg_trailing_pe_context_only": num(g(rec, "finviz.snapshot.fields.peg")),
                         "zacks_exp_eps_growth_3_5yr_pct": g(rec, "zacks.quote.exp_eps_growth_3_5yr_pct"),
                         "factset_trailing_peg_context_only": num(tv.get("price_earnings_growth_ttm"))}
    # --------------------------------------------------------------- upside
    price = out["price"]
    z_head, z_faq = g(rec, "zacks.price_target.average_target"), g(rec, "zacks.price_target.faq_snapshot.average_target")
    faq_close = num(g(rec, "zacks.price_target.faq_snapshot.last_close_used"))
    # The Zacks page carries two snapshots; the FAQ block is the one computed on the latest close when the headline box lags a session.
    z_target = z_faq if (z_faq is not None and faq_close is not None and abs(faq_close - price) < 0.005) else (z_head if z_head is not None else z_faq)
    up = gates.gate_upside({"zacks": z_target, "factset": tv.get("price_target_average"),
                            "stockanalysis": g(rec, "sa_forecast.price_target.avg"), "nasdaq": g(rec, "nasdaq.analyst.price_target.mean"),
                            "finviz": g(rec, "finviz.ratings.target_price") or g(rec, "finviz.snapshot.fields.target_price")}, price)
    out["targets"] = {"zacks_used": z_target, "zacks_headline": z_head, "zacks_faq": z_faq,
                      "zacks_n": g(rec, "zacks.price_target.num_analysts"), "factset": num(tv.get("price_target_average")),
                      "stockanalysis_spg": g(rec, "sa_forecast.price_target.avg"), "nasdaq": g(rec, "nasdaq.analyst.price_target.mean"),
                      "finviz": g(rec, "finviz.ratings.target_price") or g(rec, "finviz.snapshot.fields.target_price")}
    # ------------------------------------------------------------------ ABR
    abr = gates.gate_abr({"zacks": g(rec, "zacks.broker_recs.abr_current"), "factset": tv.get("recommendation_mark"),
                          "finviz": g(rec, "finviz.ratings.recom") or g(rec, "finviz.snapshot.fields.recom"),
                          "stockanalysis": mean_rating_from_counts(g(rec, "sa_forecast.consensus"))})
    out["abr_history"] = {k: g(rec, f"zacks.broker_recs.{k}") for k in ("abr_current", "abr_1_week_ago", "abr_1_month_ago", "abr_3_months_ago", "num_brokers")}

    out["gates"] = {"fcf": fcf, "altman": alt, "solvency": solv, "eps_cagr": eps, "peg": peg, "upside": up, "abr": abr}

    # --------------------------------------------------- EV/EBITDA history gate
    ev = {}
    growth = lastq.get("ebitda_ttm_yoy_growth")
    sec_ccy = g(rec, "sec_ttm.currency") or "USD"
    out["sec_currency"] = sec_ccy
    if sec_ccy != "USD":
        ev["sec_monthly"] = {"evaluable": False, "reason": f"filings are in {sec_ccy} (foreign private issuer); no sourced FX history to build a USD series"}
    elif px_sa is not None and q:
        try:
            ser = evebitda.build_monthly_series(px_sa[["date", "close"]], q, years=5)
            if ser.empty:
                raise ValueError("no quarter with both EBITDA (TTM) and share count public in the window")
            hist = ser["ev_ebitda"].tolist()
            cur = hist[-1] if hist else None
            st = evebitda.gate_stats(hist, cur, ebitda_growth_4q=growth, points_per_year=12,
                                     series_years=(ser["date"].iloc[-1] - ser["date"].iloc[0]).days / 365.25 if len(ser) else 0)
            st["n_months"] = len(ser)
            st["n_negative_or_missing"] = int(ser["ev_ebitda"].isna().sum())
            st["first"], st["last"] = str(ser["date"].iloc[0])[:10], str(ser["date"].iloc[-1])[:10]
            st["frequency"] = "monthly, point-in-time (SEC filings + month-end close)"
            ev["sec_monthly"] = st
            ser.assign(date=ser["date"].astype(str)).to_json(collect.OUT / f"{ticker}.evseries.json", orient="records")
        except Exception as e:
            ev["sec_monthly"] = {"evaluable": False, "reason": f"series build failed: {e}"[:200]}
    else:
        ev["sec_monthly"] = {"evaluable": False, "reason": "no SEC quarterly series (foreign filer or XBRL gap)" if not q else "no price history"}
    h = rec.get("sa_ev_ebitda_history") or {}
    qs = sorted([x for x in (h.get("quarterly") or []) if x.get("ev_ebitda") is not None], key=lambda x: x["period_end"])
    if qs and h.get("current") is not None:
        vals = [x["ev_ebitda"] if x["ev_ebitda"] and x["ev_ebitda"] > 0 else float("nan") for x in qs] + [h["current"]]
        st = evebitda.gate_stats(vals, h["current"], ebitda_growth_4q=growth if growth is not None else None,
                                 points_per_year=4, series_years=len(qs) / 4.0)
        st["frequency"] = "quarterly period-end (provider series) + current"
        st["first"], st["last"] = qs[0]["period_end"], h.get("current_as_of")
        ev["stockanalysis_quarterly"] = st
    else:
        ev["stockanalysis_quarterly"] = {"evaluable": False, "reason": "provider series unavailable"}
    sec_series_ok = bool(ev["sec_monthly"].get("evaluable")) and not da_undercaptured and not sec_bad_signs
    if ev["sec_monthly"].get("evaluable") and not sec_series_ok:
        ev["sec_monthly"]["unreliable"] = ebitda_note or "filing-derived EBITDA failed its quality check"
    primary = ev["sec_monthly"] if sec_series_ok else ev["stockanalysis_quarterly"]
    out["ev_gate"] = {"primary": "sec_monthly" if sec_series_ok else "stockanalysis_quarterly",
                      "pass": primary.get("passed") if primary.get("evaluable") else None, "detail": ev,
                      "conflict": bool(ev["sec_monthly"].get("evaluable") and ev["stockanalysis_quarterly"].get("evaluable")
                                       and ev["sec_monthly"]["passed"] != ev["stockanalysis_quarterly"]["passed"])}

    # ------------------------------------------------------------- scorecard
    inst = g(rec, "nasdaq.institutional.net_share_change")
    out["institutional"] = {"net_share_change": inst, "increased": g(rec, "nasdaq.institutional.increased.shares"),
                            "decreased": g(rec, "nasdaq.institutional.decreased.shares"), "report_date": g(rec, "nasdaq.institutional.report_date"),
                            "finviz_inst_trans_pct": g(rec, "finviz.snapshot.fields.inst_trans_pct")}
    if px is not None:
        out["scorecard"] = technicals.reversal_scorecard(px, None if inst is None else inst > 0)
        out["scorecard_nasdaq_prices"] = technicals.reversal_scorecard(px_nq, None if inst is None else inst > 0)["points"] if px_nq is not None else None
        out["sharpe_3y"] = technicals.sharpe_3y_weekly(px, rf_pct) if rf_pct is not None else None
        out["return_3y"] = technicals.price_return(px, 3)
        out["consolidation"] = technicals.consolidation_diagnostics(px)

    # --------------------------------------------------------- tier-2 inputs
    z = rec.get("zacks") or {}
    f = g(rec, "finviz.snapshot.fields") or {}
    s = g(rec, "sa_statistics.fields") or {}
    out["tier2_inputs"] = {
        "zacks_rank": g(z, "quote.zacks_rank"), "vgm": g(z, "quote.style_scores.vgm"),
        "style_scores": g(z, "quote.style_scores"),
        "industry": g(z, "quote.industry"), "industry_rank": g(z, "quote.industry_rank"), "industry_rank_total": g(z, "quote.industry_rank_total"),
        "next_earnings_date": g(z, "quote.next_earnings_date") or g(rec, "nasdaq.earnings.next_earnings_date") or f.get("earnings_date"),
        "last_eps_surprise_pct": g(z, "quote.last_eps_surprise_pct"),
        "pe_ttm": {"stockanalysis": s.get("pe_ttm"), "finviz": f.get("pe_ttm"), "factset": num(tv.get("price_earnings_ttm"))},
        "debt_equity": {"stockanalysis": s.get("debt_equity"), "zacks": g(z, "fundamentals.debt_to_equity"), "finviz": f.get("debt_to_equity")},
        "current_ratio": {"stockanalysis": s.get("current_ratio"), "zacks": g(z, "fundamentals.current_ratio"), "finviz": f.get("current_ratio")},
        "piotroski": {"stockanalysis": s.get("piotroski_f_score"), "factset": num(tv.get("piotroski_f_score_ttm"))},
        "roic_pct": {"stockanalysis": s.get("roic_pct"), "finviz": f.get("roic_pct"), "factset": num(tv.get("return_on_invested_capital_fq"))},
        "upgrades_90d": g(rec, "finviz.ratings.upgrades_window"), "downgrades_90d": g(rec, "finviz.ratings.downgrades_window"),
        "revisions": {k: g(z, f"estimates.agreement.{k}") for k in ("current_qtr", "current_yr", "next_yr")},
        "fcf_annual": g(z, "fundamentals.derived.annual_free_cash_flow"),
        "margins_annual": {m: g(rec, f"sa_income_annual.rows.{m}.values") for m in ("grossMargin", "operatingMargin", "fcfMargin")},
        "roic_annual": g(rec, "sa_ratios_annual.rows.roic.values"),
        "insider_6m": g(rec, "finviz.insider.totals_window"),
    }
    out["why_moving"] = g(rec, "finviz.news.why_moving")
    out["recent_8k"] = (g(rec, "sec_8k.filings") or g(rec, "sec_8k.rows") or [])[:12]
    return out


def hard_gate_summary(e: dict) -> dict:
    """One row per ticker: P / F / ? per quantitative hard gate."""
    def pf(v):
        return "?" if v is None else ("P" if v else "F")
    gt = e["gates"]
    tech_ok = e["tech_rsi_lt30"] and not e["knife_rsi_lt15"] and not e["knife_gap25"]
    row = {
        "ticker": e["ticker"], "rsi": round(e["rsi14"], 1), "gap50%": round(e["gap50"] * 100, 1),
        "UNIV": pf(e["universe_pass"]), "TECH": pf(tech_ok), "FCF": pf(gt["fcf"]["pass"]), "ALTZ": pf(gt["altman"]["pass"]),
        "SOLV": pf(gt["solvency"]["pass"]), "EPS3": pf(gt["eps_cagr"]["pass"]), "PEG": pf(gt["peg"]["pass"]),
        "UPSD": pf(gt["upside"]["pass"]), "ABR": pf(gt["abr"]["pass"]), "EVEB": pf(e["ev_gate"]["pass"]),
    }
    keys = ["UNIV", "TECH", "FCF", "ALTZ", "SOLV", "EPS3", "PEG", "UPSD", "ABR", "EVEB"]
    row["fails"] = sum(row[k] == "F" for k in keys)
    row["unknown"] = sum(row[k] == "?" for k in keys)
    row["conflicts"] = ",".join(k for k, v in gt.items() if v.get("conflict")) + (",ev" if e["ev_gate"].get("conflict") else "") \
        + (",rsi" if e.get("tech_rsi_conflict") else "") + (",gap" if e.get("knife_gap25_conflict") else "")
    return row
