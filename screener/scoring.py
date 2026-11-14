"""Tier-2 scored criteria and the composite ranking (applied to hard passes).

Each criterion scores 1 if met, 0 if not; None means the input could not be
sourced (scores 0 and is flagged). Substitutions are labelled.
"""
from __future__ import annotations

from datetime import date, timedelta

REPORT_DATE = date(2026, 11, 14)


def _first(d: dict | None):
    if not isinstance(d, dict):
        return None
    for v in d.values():
        if v is not None:
            return v
    return None


def _latest_fy_cols(values: dict | None, n: int = 3):
    cols = sorted(k for k in (values or {}) if k != "TTM" and values[k] is not None)
    return cols[-n:]


def tier2(e: dict) -> dict:
    t = e["tier2_inputs"]
    gt = e["gates"]
    crit: dict[str, dict] = {}

    pio = _first(t["piotroski"])
    crit["profitability_score_gt_50"] = {
        "met": None if pio is None else pio >= 5,
        "evidence": f"SUBSTITUTE -- no provider Profitability Score is publicly retrievable; Piotroski F-Score {pio}/9 (>= 5 counts as > 50%)",
        "substituted": True}

    fcf_ttm_ok = gt["fcf"]["pass"]
    fy = t.get("fcf_annual") or []
    prior = fy[0].get("free_cash_flow") if fy else None
    crit["fcf_positive_prior_fy_and_ttm"] = {
        "met": None if prior is None or fcf_ttm_ok is None else bool(prior > 0 and fcf_ttm_ok),
        "evidence": f"latest fiscal-year FCF {prior} (USD m, Zacks statements, FY {fy[0].get('fiscal_year_end') if fy else None}); TTM gate {gt['fcf']['status']}"}

    de, cr = _first(t["debt_equity"]), _first(t["current_ratio"])
    crit["debt_equity_lt_1_and_current_ratio_gt_1_2"] = {
        "met": None if de is None or cr is None else bool(de < 1.0 and cr > 1.2),
        "evidence": f"Debt/Equity {de}, Current Ratio {cr} ({t['debt_equity']}, {t['current_ratio']})"}

    sur = t.get("last_eps_surprise_pct")
    crit["earnings_beat_last_quarter"] = {"met": None if sur is None else sur > 0,
                                          "evidence": f"last-quarter EPS surprise {sur}% (Zacks)"}

    met_m, ev_m = None, []
    for m in ("grossMargin", "operatingMargin"):
        vals = (t.get("margins_annual") or {}).get(m) or {}
        cols = _latest_fy_cols(vals, 3)
        if vals.get("TTM") is not None and len(cols) == 3:
            avg = sum(vals[c] for c in cols) / 3
            k = 100.0 if abs(vals["TTM"]) <= 1.5 else 1.0   # provider serves margins as fractions
            ev_m.append(f"{m} TTM {vals['TTM'] * k:.1f}% vs 3-FY avg {avg * k:.1f}%")
            met_m = bool(met_m) or vals["TTM"] > avg
    crit["expanding_margins"] = {"met": met_m, "evidence": "; ".join(ev_m) or "margin history unavailable"}

    ned = t.get("next_earnings_date")
    within = None
    if ned:
        try:
            d = date.fromisoformat(str(ned)[:10])
            within = REPORT_DATE <= d <= REPORT_DATE + timedelta(weeks=5)
        except ValueError:
            pass
    crit["earnings_within_5_weeks"] = {"met": within, "evidence": f"next earnings date {ned}"}

    pe = _first(t["pe_ttm"])
    crit["pe_ttm_12_to_25"] = {"met": None if pe is None else 12 <= pe <= 25, "evidence": f"P/E (TTM) {pe} ({t['pe_ttm']})"}

    ir, tot = t.get("industry_rank"), t.get("industry_rank_total")
    crit["industry_rank_top_25pct"] = {"met": None if not ir or not tot else ir / tot <= 0.25,
                                       "evidence": f"Zacks Industry Rank {ir} of {tot} ({t.get('industry')})"}

    zr = t.get("zacks_rank")
    crit["zacks_rank_1_or_2"] = {"met": None if zr is None else zr in (1, 2), "evidence": f"Zacks Rank {zr}"}
    vgm = t.get("vgm")
    crit["vgm_a_or_b"] = {"met": None if vgm is None else vgm in ("A", "B"), "evidence": f"VGM Score {vgm}"}

    up, dn = t.get("upgrades_90d"), t.get("downgrades_90d")
    rev = t.get("revisions") or {}
    ups = sum((rev.get(k) or {}).get("up_last_60_days") or 0 for k in ("current_yr", "next_yr"))
    dns = sum((rev.get(k) or {}).get("down_last_60_days") or 0 for k in ("current_yr", "next_yr"))
    ratio = ups / (ups + dns) if (ups + dns) else None
    net_up = None if up is None or dn is None else up > dn
    crit["net_upgrades_or_positive_revisions"] = {
        "met": None if net_up is None and ratio is None else bool(net_up) or bool(ratio is not None and ratio > 0.5),
        "evidence": f"analyst actions 90d: {up} upgrades vs {dn} downgrades (Finviz); EPS estimate revisions 60d (FY1+FY2): {ups} up / {dns} down (Zacks)"}

    sh = (e.get("sharpe_3y") or {}).get("sharpe")
    crit["sharpe_3y_gt_0_7"] = {"met": None if sh is None else sh > 0.7, "evidence": f"3-yr weekly Sharpe {None if sh is None else round(sh, 2)}"}
    crit["sharpe_3y_gt_1_0_bonus"] = {"met": None if sh is None else sh > 1.0, "evidence": "bonus point"}
    r3 = (e.get("return_3y") or {}).get("return")
    crit["return_3y_positive"] = {"met": None if r3 is None else r3 > 0, "evidence": f"3-yr price return {None if r3 is None else round(r3 * 100, 1)}%"}

    score = sum(1 for c in crit.values() if c["met"])
    return {"score": score, "max": len(crit), "criteria": crit,
            "unsourced": [k for k, c in crit.items() if c["met"] is None]}


def rank_normalise(values: dict[str, float | None], higher_better: bool = True) -> dict[str, float]:
    """Percentile rank 0-100 across the candidates (ties share the average rank;
    a missing value ranks last; a single candidate scores 100)."""
    keys = list(values)
    if len(keys) == 1:
        return {keys[0]: 100.0}
    def key(k):
        v = values[k]
        if v is None:
            return float("-inf")
        return v if higher_better else -v
    order = sorted(keys, key=key)
    ranks: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and key(order[j + 1]) == key(order[i]):
            j += 1
        avg = (i + j) / 2.0
        for k in order[i : j + 1]:
            ranks[k] = avg / (len(order) - 1) * 100.0
        i = j + 1
    return ranks


WEIGHTS = {"technical": 0.25, "upside": 0.20, "quality": 0.20, "valuation": 0.15, "sentiment": 0.10, "catalyst": 0.10}


def composite(cands: dict[str, dict]) -> dict[str, dict]:
    """cands: ticker -> {technical, upside, quality, valuation_percentile (lower = cheaper),
    sentiment, catalyst} raw component values. Returns per-ticker component ranks + composite."""
    comp = {
        "technical": rank_normalise({k: v["technical"] for k, v in cands.items()}),
        "upside": rank_normalise({k: v["upside"] for k, v in cands.items()}),
        "quality": rank_normalise({k: v["quality"] for k, v in cands.items()}),
        "valuation": rank_normalise({k: v["valuation_percentile"] for k, v in cands.items()}, higher_better=False),
        "sentiment": rank_normalise({k: v["sentiment"] for k, v in cands.items()}),
        "catalyst": rank_normalise({k: v["catalyst"] for k, v in cands.items()}),
    }
    out = {}
    for k in cands:
        parts = {c: comp[c][k] for c in WEIGHTS}
        out[k] = {"components": parts, "composite": sum(parts[c] * w for c, w in WEIGHTS.items())}
    return out
