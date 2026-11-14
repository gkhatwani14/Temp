"""Write the Stage-1 report: output/<date>/hard_pass_report.md, evaluation.json.

    python3 -m screener.report

Numeric gates come from .cache/eval_full.json (screener.screen); qualitative
gates (existential news, thesis filter) are analyst judgments recorded with
their dated evidence in output/<date>/qualitative_gates.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import scoring
from .http import CACHE_DIR

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "2026-11-14"
GATE_COLS = ["UNIV", "TECH", "FCF", "ALTZ", "SOLV", "EPS3", "PEG", "UPSD", "ABR", "EVEB"]
GATE_NAMES = {"UNIV": "universe (cap/liquidity)", "TECH": "technical (RSI<30, no falling knife)", "FCF": "FCF (TTM) > 0",
              "ALTZ": "Altman Z > 3", "SOLV": "ND/EBITDA < 2.5x & coverage > 5x", "EPS3": "3-yr EPS CAGR > 10%",
              "PEG": "forward PEG 0-1.5", "UPSD": "target upside >= 15%", "ABR": "broker rating < 2", "EVEB": "historic EV/EBITDA gate"}


def pct(x, d=1):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def money(x):
    if x is None:
        return "n/a"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(x) >= div:
            return f"${x / div:,.2f}{unit}"
    return f"${x:,.0f}"


def fnum(x, d=2):
    return "n/a" if x is None else f"{x:,.{d}f}"


def summary_row(e: dict) -> dict:
    def pf(v):
        return "?" if v is None else ("P" if v else "F")
    gt = e["gates"]
    tech_ok = e["tech_rsi_lt30"] and not e["knife_rsi_lt15"] and not e["knife_gap25"]
    row = {"ticker": e["ticker"], "company": e["company"], "in_list": e.get("in_user_list", True), "rsi": e["rsi14"], "gap50": e["gap50"],
           "UNIV": pf(e["universe_pass"]), "TECH": pf(tech_ok), "FCF": pf(gt["fcf"]["pass"]), "ALTZ": pf(gt["altman"]["pass"]),
           "SOLV": pf(gt["solvency"]["pass"]), "EPS3": pf(gt["eps_cagr"]["pass"]), "PEG": pf(gt["peg"]["pass"]),
           "UPSD": pf(gt["upside"]["pass"]), "ABR": pf(gt["abr"]["pass"]), "EVEB": pf(e["ev_gate"]["pass"])}
    row["fails"] = sum(row[k] == "F" for k in GATE_COLS)
    row["unknown"] = sum(row[k] == "?" for k in GATE_COLS)
    return row


def failed_log(e: dict) -> str:
    """Plain-language list of the failed / not-evaluable criteria with the deciding figure."""
    gt, out = e["gates"], []
    if not e["tech_rsi_lt30"]:
        out.append(f"RSI {e['rsi14']:.1f} not < 30")
    if e["knife_gap25"]:
        out.append(f"falling knife: {pct(e['gap50'])} vs 50-day MA (fence -25%)")
    if e["knife_rsi_lt15"]:
        out.append("falling knife: RSI < 15")
    v = gt["fcf"]
    if v["pass"] is False:
        out.append(f"FCF (TTM) {money(v['values'].get(v['decided_by']))} ({v['decided_by']})")
    v = gt["altman"]
    if v["pass"] is False:
        out.append(f"Altman Z {fnum(v['values'].get(v['decided_by']))} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("Altman Z not evaluable" + (" (financial; no sourced Solvency Score)" if e.get("is_financial") else ""))
    v = gt["solvency"]
    if v["pass"] is False:
        out.append(f"solvency: {v['notes'].get(v['decided_by'])} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("solvency not evaluable")
    v = gt["eps_cagr"]
    if v["pass"] is False:
        c = v["values"].get(v["decided_by"])
        out.append(f"3-yr EPS CAGR {pct(c) if isinstance(c, (int, float)) else c} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("3-yr EPS CAGR not evaluable")
    v = gt["peg"]
    if v["pass"] is False:
        out.append(f"PEG {fnum(v['values'].get(v['decided_by']))} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("forward PEG not published by any provider (no long-term growth estimate)")
    v = gt["upside"]
    if v["pass"] is False:
        out.append(f"target upside {pct(v['values'].get(v['decided_by']))} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("no analyst price target")
    v = gt["abr"]
    if v["pass"] is False:
        out.append(f"broker rating {fnum(v['values'].get(v['decided_by']))} ({v['decided_by']})")
    elif v["pass"] is None:
        out.append("no broker rating")
    ev = e["ev_gate"]
    if ev["pass"] is False:
        d = ev["detail"][ev["primary"]]
        out.append(f"EV/EBITDA {fnum(d['current'])}x at percentile {d['percentile_rank']:.0f}% of its own history (no option met)")
    elif ev["pass"] is None:
        out.append("EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series)")
    return "; ".join(out)


def provider_line(v: dict, fmt) -> str:
    return ", ".join(f"{p} {fmt(x)}" for p, x in v["values"].items() if x is not None and not isinstance(x, dict))


def card(e: dict, t2: dict, qual: dict, comp: dict | None) -> list[str]:
    gt, t = e["gates"], e["ticker"]
    tech = e["technical"]
    L = [f"### {t} — {e['company']}", "",
         f"{e['tier2_inputs'].get('industry') or e['industry']} (Zacks industry) · close ${e['price']:.2f} (2026-11-13) · market cap {money(e['mcap_usd'])} · "
         f"90-day average traded value {money(e['adv90_usd']['computed_from_prices'])}/day", ""]
    L += ["| Hard gate | Result | Deciding figure (provider) | Other providers |", "|---|---|---|---|"]
    rs = ", ".join(f"{k} {v['rsi14']:.2f}" for k, v in tech.items())
    L.append(f"| RSI(14) < 30, >= 15 | PASS | {e['rsi14']:.2f} (recomputed, Wilder, from stockanalysis and Nasdaq daily closes) | {rs} |")
    L.append(f"| Not > 25% below 50-day MA | PASS | {pct(e['gap50'])} (50-day SMA ${tech['stockanalysis']['sma50']:.2f}) | same on all three price sources |")
    v = gt["fcf"]
    ccy = e.get("sec_currency") or "USD"
    if ccy != "USD" and v["decided_by"] == "sec":
        lead = f"{ccy} {v['values']['sec'] / 1e9:,.2f}B for the latest fiscal year (SEC annual filing; no quarterly XBRL)"
        others = ", ".join(f"{p} {money(x)}" for p, x in v["values"].items() if x is not None and p != "sec") + " (TTM, USD)"
    else:
        lead, others = f"{money(v['values'][v['decided_by']])} ({v['decided_by']})", provider_line(v, money)
    L.append(f"| FCF (TTM) > 0 | {v['status']} | {lead} | {others} |")
    v = gt["altman"]
    L.append(f"| Altman Z > 3 | {v['status']} | {fnum(v['values'][v['decided_by']])} ({v['decided_by']}) | {provider_line(v, fnum)} |")
    v = gt["solvency"]
    q = e.get("sec_ebitda_quality") or {}
    L.append(f"| Net debt/EBITDA < 2.5x, coverage > 5x | {v['status']} | {v['notes'][v['decided_by']]} ({v['decided_by']}{'; EBITDA = ' + q['note'] if q.get('note') and v['decided_by'] == 'sec' else ''}) | "
             + "; ".join(f"{p}: {n}" for p, n in v["notes"].items() if p != v["decided_by"]) + " |")
    v = gt["eps_cagr"]
    pr = e["eps_pairs"].get(v["decided_by"]) or {}
    L.append(f"| 3-yr EPS CAGR > 10% | {v['status']} | {pct(v['values'][v['decided_by']])}: diluted EPS {pr.get('base')} (FY {str(pr.get('base_fy'))[:4]}) "
             f"to {pr.get('latest')} (FY {str(pr.get('latest_fy'))[:4]}) ({v['decided_by']}) | {provider_line(v, pct)} |")
    v = gt["peg"]
    pi = e["peg_inputs"]
    L.append(f"| Forward PEG 0–1.5 | {v['status']} | {fnum(v['values'][v['decided_by']])} = P/E (F1) {pi['zacks_forward_pe_f1']} / 3–5yr growth "
             f"{pi['zacks_exp_eps_growth_3_5yr_pct']}% ({v['decided_by']}) | {provider_line(v, fnum)} |")
    v = gt["upside"]
    tg = e["targets"]
    L.append(f"| Consensus target >= 15% above price | {v['status']} | {pct(v['values'][v['decided_by']])}: target ${tg['zacks_used']} from {tg['zacks_n']} analysts ({v['decided_by']}) | {provider_line(v, pct)} |")
    v = gt["abr"]
    ah = e["abr_history"]
    L.append(f"| Average broker rating < 2 | {v['status']} | {fnum(v['values'][v['decided_by']])} from {ah['num_brokers']} brokers; {ah['abr_1_month_ago']} a month ago, "
             f"{ah['abr_3_months_ago']} three months ago ({v['decided_by']}) | {provider_line(v, fnum)} |")
    ev = e["ev_gate"]
    d = ev["detail"][ev["primary"]]
    L.append(f"| Historic EV/EBITDA | {'PASS' if ev['pass'] else 'FAIL'} — Option {d.get('option_used')} | {d['current']:.2f}x vs 3-yr MA {d['ma_3y']:.2f}x, 5-yr median {d['median']:.2f}x, "
             f"25th pct {d['p25']:.2f}x ({d['frequency']}{'; SHORT SERIES' if d.get('short_series') else ''}) | "
             + "; ".join(f"{k}: {x['current']:.2f}x, pctile {x['percentile_rank']:.0f}%, pass={x['passed']}" for k, x in ev["detail"].items() if x.get("evaluable") and k != ev["primary"]) + " |")
    L.append(f"| Existential-news exclusion | {qual['existential']['status']} | {qual['existential']['summary']} | |")
    L.append(f"| Thesis filter (>= 1) | {qual['thesis']['status']} | {qual['thesis']['summary']} | |")
    L += ["", "`EV/EBITDA (TTM) = {:.2f} | 5Y Median = {:.2f} | Percentile = {:.0f}% | Z-Score = {:.2f} | Pass = {} | Value Trap = NO`".format(
        d["current"], d["median"], d["percentile_rank"], d["z_score"], "YES" if d["passed"] else "NO"), "",
        f"EV/EBITDA series statistics ({d['frequency']}, {d['first']} to {d['last']}, {d['n_obs']} observations): min {d['min']:.2f}, mean {d['mean']:.2f}, "
        f"median {d['median']:.2f}, st. dev. {d['stdev']:.2f}; current is {pct(d['pct_vs_min'])} above the minimum and {pct(d['pct_vs_ma_3y'])} versus the 3-year MA; "
        f"trailing 4-quarter EBITDA growth {pct(d.get('ebitda_growth_4q'))}.", ""]
    sc = e["scorecard"]
    p, dt = sc["points"], sc["detail"]
    inst = e["institutional"]
    L += [f"**Oversold-reversal scorecard: {sc['score']} / 6**", "",
          "| Signal | Points | Evidence |", "|---|---|---|",
          f"| RSI sweet spot | {p['rsi_sweet_spot']} / 2 | RSI {e['rsi14']:.1f} |",
          f"| Bullish RSI divergence | {p['bullish_divergence']} / 1 | most recent closing low {dt['divergence']['recent_low']['date']} at RSI {dt['divergence']['recent_low']['rsi']:.1f}; no earlier higher-price/lower-RSI low in the 20-session window |"
          if not p["bullish_divergence"] else f"| Bullish RSI divergence | 1 / 1 | lower close {dt['divergence']['recent_low']} vs {dt['divergence']['prior_low']} |",
          f"| Price stabilisation | {p['price_stabilisation']} / 1 | last new 20-session closing low {dt['stabilisation']['last_new_20d_closing_low']} ({dt['stabilisation']['sessions_since_last_new_low']} sessions ago; needs 5 clear sessions) |",
          f"| Capitulation-volume exhaustion | {p['volume_exhaustion']} / 1 | flush {dt['volume']['flush_date']}: {dt['volume']['flush_multiple']:.1f}x the prior 90-day average volume, {dt['volume']['flush_age_sessions']} sessions old, down-day volume contracting: {dt['volume']['contracting']} |",
          f"| Institutional accumulation | {p['institutional_accumulation']} / 1 | 13F net change {inst['net_share_change']:+,} shares (increased {inst['increased']:,} vs decreased {inst['decreased']:,}; mostly period ending {inst['report_date']}; Nasdaq) |", ""]
    L += [f"**Tier-2 scored criteria: {t2['score']} / {t2['max']}**", "", "| Criterion | Met | Evidence |", "|---|---|---|"]
    for k, c in t2["criteria"].items():
        L.append(f"| {k.replace('_', ' ')} | {'yes' if c['met'] else ('no' if c['met'] is not None else 'not sourced')} | {c['evidence']} |")
    L.append("")
    if comp:
        L += [f"**Composite rank score: {comp['composite']:.0f} / 100** — " + ", ".join(f"{k} {v:.0f}" for k, v in comp["components"].items()), ""]
    L += [f"**Reward:risk.** Default plan: target +12% (${e['price'] * 1.12:.2f}) and stop −8% (${e['price'] * 0.92:.2f}) from the last close, reward:risk 1.5:1. "
          f"Entry discipline: {qual['entry']}", ""]
    if qual.get("flags"):
        L += ["**Flags**", ""] + [f"- {f}" for f in qual["flags"]] + [""]
    return L


def main() -> None:
    ev = json.loads((CACHE_DIR / "eval_full.json").read_text())
    qual = json.loads((OUT / "qualitative_gates.json").read_text())
    rows = pd.DataFrame([summary_row(e) for e in ev.values()]).sort_values(["fails", "unknown", "rsi"])
    numeric_pass = rows[(rows.fails == 0) & (rows.unknown == 0)].ticker.tolist()
    qualified = [t for t in numeric_pass if qual[t]["existential"]["status"] == "PASS" and qual[t]["thesis"]["status"].startswith("PASS")]
    t2 = {t: scoring.tier2(ev[t]) for t in numeric_pass}

    # composite ranking among qualified names
    raw = {}
    for t in qualified:
        e = ev[t]
        d = e["ev_gate"]["detail"][e["ev_gate"]["primary"]]
        ti = e["tier2_inputs"]
        crit = t2[t]["criteria"]
        qcount = sum(bool(crit[k]["met"]) for k in ("profitability_score_gt_50", "fcf_positive_prior_fy_and_ttm", "debt_equity_lt_1_and_current_ratio_gt_1_2",
                                                    "earnings_beat_last_quarter", "expanding_margins", "earnings_within_5_weeks"))
        scount = sum(bool(crit[k]["met"]) for k in ("zacks_rank_1_or_2", "vgm_a_or_b", "net_upgrades_or_positive_revisions"))
        days = (pd.Timestamp(ti["next_earnings_date"]) - pd.Timestamp("2026-11-14")).days if ti.get("next_earnings_date") else 999
        raw[t] = {"technical": e["scorecard"]["score"], "upside": e["gates"]["upside"]["values"]["zacks"],
                  "quality": qcount + min(e["gates"]["altman"]["values"][e["gates"]["altman"]["decided_by"]], 10) / 10 + (scoring._first(ti["piotroski"]) or 0) / 9,
                  "valuation_percentile": d["percentile_rank"], "sentiment": scount + (5 - (ti.get("zacks_rank") or 5)) / 4,
                  "catalyst": -days + 30 * qual[t]["thesis"].get("filters_met", 1)}
    comp = scoring.composite(raw) if qualified else {}
    ranked = sorted(qualified, key=lambda t: -comp[t]["composite"])

    L: list[str] = []
    L += ["# Deep-oversold screen — Stage 1 hard-pass list", "",
          "Report date 2026-11-14 (Saturday); all prices are the 2026-11-13 close; data retrieved 2026-11-14 13:15–14:10 UTC.", "",
          qual["_headline"], ""]
    L += ["## Result", "", "| Rank | Ticker | Company | Status | RSI(14) | vs 50-day MA | Reversal scorecard | Target upside | PEG | Broker rating | EV/EBITDA vs own history | Composite |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, t in enumerate(ranked, 1):
        e = ev[t]
        d = e["ev_gate"]["detail"][e["ev_gate"]["primary"]]
        L.append(f"| {i} | **{t}** | {e['company']} | {qual[t]['status']} | {e['rsi14']:.1f} | {pct(e['gap50'])} | {e['scorecard']['score']} / 6 | "
                 f"{pct(e['gates']['upside']['values']['zacks'], 0)} | {fnum(e['gates']['peg']['values']['zacks'])} | {fnum(e['gates']['abr']['values']['zacks'])} | "
                 f"{d['current']:.1f}x, percentile {d['percentile_rank']:.0f}% | {comp[t]['composite']:.0f} |")
    for t in numeric_pass:
        if t in qualified:
            continue
        e = ev[t]
        d = e["ev_gate"]["detail"][e["ev_gate"]["primary"]]
        L.append(f"| — | {t} | {e['company']} | {qual[t]['status']} | {e['rsi14']:.1f} | {pct(e['gap50'])} | {e['scorecard']['score']} / 6 | "
                 f"{pct(e['gates']['upside']['values']['zacks'], 0)} | {fnum(e['gates']['peg']['values']['zacks'])} | {fnum(e['gates']['abr']['values']['zacks'])} | "
                 f"{d['current']:.1f}x, percentile {d['percentile_rank']:.0f}% | — |")
    L += ["", qual["_result_note"], ""]
    L += ["## Funnel", ""] + [f"- {x}" for x in qual["_funnel"]] + [""]
    L += ["## Qualified hard passes", ""]
    for t in ranked:
        L += card(ev[t], t2[t], qual[t], comp.get(t))
    L += ["## Passed every numeric gate but excluded", ""]
    for t in numeric_pass:
        if t in qualified:
            continue
        L += card(ev[t], t2[t], qual[t], None)
        L += [qual[t]["exclusion_detail"], ""]
    L += ["## Failed-criteria log: every name with RSI(14) < 30 in the universe", "",
          "P = pass, F = fail, ? = not evaluable from sourced data (a hard gate that cannot be evaluated cannot be passed). "
          "Rows marked † are NYSE-listed ADRs that are oversold but are not in the supplied ticker list.", "",
          "| Ticker | RSI | vs 50d MA | " + " | ".join(GATE_COLS) + " | Failed / not-evaluable criteria |", "|---|---|---|" + "---|" * (len(GATE_COLS) + 1)]
    for _, r in rows.iterrows():
        e = ev[r.ticker]
        note = failed_log(e) or ("all numeric gates pass — " + qual.get(r.ticker, {}).get("status", ""))
        L.append(f"| {r.ticker}{'' if r.in_list else ' †'} | {r.rsi:.1f} | {pct(r.gap50)} | " + " | ".join(r[c] for c in GATE_COLS) + f" | {note} |")
    L += ["", "Gate keys: " + "; ".join(f"{k} = {v}" for k, v in GATE_NAMES.items()) + ".", ""]
    for sec in ("_near_miss", "_overlays", "_verification", "_method"):
        L += qual[sec] + [""]
    (OUT / "hard_pass_report.md").write_text("\n".join(L))

    slim = {}
    for t, e in ev.items():
        slim[t] = {k: e.get(k) for k in ("ticker", "company", "exchange", "sector", "industry", "in_user_list", "price", "rsi14", "gap50", "rsi_all_sources",
                                         "gap50_all_sources", "mcap_usd", "adv90_usd", "universe_pass", "is_financial", "sec_latest_period", "sec_currency",
                                         "eps_pairs", "peg_inputs", "targets", "abr_history", "gates", "ev_gate", "institutional", "scorecard", "sharpe_3y",
                                         "return_3y", "consolidation", "tier2_inputs")}
        slim[t]["failed_criteria"] = failed_log(e)
        if t in t2:
            slim[t]["tier2"] = t2[t]
        if t in comp:
            slim[t]["composite"] = comp[t]
    (OUT / "evaluation.json").write_text(json.dumps(slim, indent=1, default=str))
    out_rows = rows.copy()
    out_rows["failed_criteria"] = [failed_log(ev[t]) for t in out_rows.ticker]
    out_rows.to_csv(OUT / "gate_table_rsi_below_30.csv", index=False)
    print("qualified:", ranked, "| numeric pass:", numeric_pass)
    print({t: round(comp[t]["composite"], 1) for t in ranked}, raw)


if __name__ == "__main__":
    main()
