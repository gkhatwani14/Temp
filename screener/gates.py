"""Tier-1 hard-gate decision logic (pure functions).

Each gate is evaluated separately for every provider that returned a value,
then decided by the provider hierarchy for that metric. A gate whose deciding
provider passes but where another provider disagrees is flagged CONFLICT so it
can be adjudicated against filings rather than silently accepted.

Provider keys used throughout:
  zacks          Zacks (primary provider in the spec)
  factset        FactSet fundamentals/estimates as served by TradingView
  sec            computed here from the company's SEC XBRL filings
  stockanalysis  stockanalysis.com
  nasdaq         Nasdaq.com (analyst data on Nasdaq.com is supplied by Zacks)
  finviz         Finviz
  ft             FT Markets (LSEG/Refinitiv data)
"""
from __future__ import annotations

import math

# Hierarchy per metric: first provider with a value decides.
HIERARCHY = {
    # filing-derived quantities: the filings themselves outrank any aggregator
    "fcf_ttm": ["sec", "factset", "stockanalysis", "nasdaq", "ft"],
    "altman_z": ["factset", "stockanalysis", "sec"],
    "solvency": ["sec", "factset", "stockanalysis"],
    "eps_cagr_3y": ["sec", "factset", "stockanalysis", "finviz"],
    # estimate-derived quantities: Zacks is the spec's primary provider
    "peg_forward": ["zacks", "nasdaq", "stockanalysis", "finviz"],
    "target_upside": ["zacks", "factset", "stockanalysis", "nasdaq", "finviz", "ft"],
    "abr": ["zacks", "factset", "finviz", "ft", "stockanalysis"],
}


def _num(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _decide(metric: str, per_provider: dict[str, bool | None], values: dict) -> dict:
    decided_by, verdict = None, None
    for p in HIERARCHY[metric]:
        if per_provider.get(p) is not None:
            decided_by, verdict = p, per_provider[p]
            break
    others = {p: v for p, v in per_provider.items() if v is not None and p != decided_by}
    conflict = verdict is not None and any(v != verdict for v in others.values())
    return {"metric": metric, "pass": verdict, "decided_by": decided_by, "conflict": conflict,
            "per_provider": per_provider, "values": values,
            "status": "NOT EVALUABLE" if verdict is None else ("PASS" if verdict else "FAIL") + (" (CONFLICT)" if conflict else "")}


def gate_fcf(vals: dict) -> dict:
    """Positive free cash flow (TTM)."""
    v = {p: _num(x) for p, x in vals.items()}
    return _decide("fcf_ttm", {p: (None if x is None else x > 0) for p, x in v.items()}, v)


def gate_altman(vals: dict, is_financial: bool) -> dict:
    """Altman Z > 3 (TTM). Financials: Z is not defined -> substitute solvency test required."""
    v = {p: _num(x) for p, x in vals.items()}
    out = _decide("altman_z", {p: (None if x is None else x > 3.0) for p, x in v.items()}, v)
    if is_financial:
        out["status"] = "NOT EVALUABLE (financial: Altman Z undefined; no sourced Solvency Score available)"
        out["pass"] = None
        out["financial_substitution"] = True
    return out


def solvency_test(net_debt, ebitda, interest_coverage) -> tuple[bool | None, str]:
    """Net Debt/EBITDA < 2.5x AND Interest Coverage > 5x, with the net-cash exception:
    automatic pass if Net Debt <= 0, or (EBITDA > 0 and Net Debt/EBITDA < 0.5x)."""
    nd, e, ic = _num(net_debt), _num(ebitda), _num(interest_coverage)
    if nd is not None and nd <= 0:
        return True, "SOLVENCY PASS -- net cash / negligible debt (net debt <= 0)"
    if nd is not None and e is not None and e > 0 and nd / e < 0.5:
        return True, f"SOLVENCY PASS -- net cash / negligible debt (ND/EBITDA {nd / e:.2f}x < 0.5x)"
    if nd is None or e is None:
        return None, "net debt or EBITDA unavailable"
    if e <= 0:
        return False, "positive net debt with non-positive EBITDA"
    if ic is None:
        return None, f"ND/EBITDA {nd / e:.2f}x but interest coverage unavailable"
    ok = nd / e < 2.5 and ic > 5.0
    return ok, f"ND/EBITDA {nd / e:.2f}x, interest coverage {ic:.1f}x"


def gate_solvency(vals: dict) -> dict:
    """vals: provider -> {net_debt, ebitda, interest_coverage}."""
    per, notes = {}, {}
    for p, d in vals.items():
        ok, note = solvency_test(d.get("net_debt"), d.get("ebitda"), d.get("interest_coverage"))
        per[p], notes[p] = ok, note
    out = _decide("solvency", per, vals)
    out["notes"] = notes
    return out


def eps_cagr(latest, base, years: int = 3):
    a, b = _num(latest), _num(base)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (a / b) ** (1.0 / years) - 1.0


def gate_eps_cagr(vals: dict) -> dict:
    """3-yr EPS CAGR > 10%. vals: provider -> {latest, base, cagr (optional precomputed)}.
    A non-positive base or latest EPS makes the CAGR undefined -> fail (flagged)."""
    per, shown = {}, {}
    for p, d in vals.items():
        c = _num(d.get("cagr"))
        if c is None and d.get("latest") is not None and d.get("base") is not None:
            c = eps_cagr(d["latest"], d["base"])
            if c is None:  # values exist but CAGR is undefined (loss in either year)
                per[p], shown[p] = False, "undefined (non-positive EPS)"
                continue
        per[p] = None if c is None else c > 0.10
        shown[p] = c
    return _decide("eps_cagr_3y", per, shown)


def gate_peg(vals: dict) -> dict:
    """PEG (forward, 3-5yr growth) strictly between 0 and 1.5."""
    v = {p: _num(x) for p, x in vals.items()}
    return _decide("peg_forward", {p: (None if x is None else 0 < x < 1.5) for p, x in v.items()}, v)


def gate_upside(targets: dict, price: float) -> dict:
    """Consensus target >= 15% above current price."""
    v = {p: (None if _num(t) is None else _num(t) / price - 1.0) for p, t in targets.items()}
    return _decide("target_upside", {p: (None if x is None else x >= 0.15) for p, x in v.items()}, v)


def gate_abr(vals: dict) -> dict:
    """Average Broker Recommendation < 2 on a 1 (Strong Buy) - 5 (Strong Sell) scale."""
    v = {p: _num(x) for p, x in vals.items()}
    return _decide("abr", {p: (None if x is None else x < 2.0) for p, x in v.items()}, v)
