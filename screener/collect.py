"""Collect every provider's data for a list of tickers into one JSON per ticker.

    python3 -m screener.collect NVDA LOPE ...      (or no args = the work-list)

Output: .cache/collect/<TICKER>.json  with one key per provider call; a call
that raises is recorded as {"error": "..."} instead of aborting the run.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .http import CACHE_DIR
from .sources import finviz, nasdaq, news, sec_edgar, stockanalysis as sa, zacks

OUT = CACHE_DIR / "collect"
OUT.mkdir(parents=True, exist_ok=True)

HIST_FROM = "2022-09-01"   # > 4 years of daily bars for 3y Sharpe / 3y return


def _call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # keep going; the gap is recorded and reported
        return {"error": f"{type(e).__name__}: {e}"[:400], "trace": traceback.format_exc(limit=2)[-600:]}


def collect(ticker: str, company: str | None = None, *, prices_only: bool = False) -> dict:
    rec: dict = {"ticker": ticker, "company": company, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rec["sa_prices_daily"] = _call(sa.price_history, ticker, "5Y", "Daily")
    rec["nasdaq_prices_daily"] = _call(nasdaq.historical, ticker, HIST_FROM)
    if prices_only:
        return rec
    rec["sa_prices_monthly"] = _call(sa.price_history, ticker, "MAX", "Monthly")
    rec["sa_statistics"] = _call(sa.statistics, ticker)
    rec["sa_forecast"] = _call(sa.forecast, ticker)
    rec["sa_ratings"] = _call(sa.ratings, ticker)
    rec["sa_ev_ebitda_history"] = _call(sa.ev_ebitda_history, ticker)
    rec["sa_income_annual"] = _call(sa.financials, ticker, "income", "annual")
    rec["sa_ratios_annual"] = _call(sa.financials, ticker, "ratios", "annual")
    rec["zacks"] = _call(zacks.snapshot, ticker)
    rec["nasdaq"] = _call(nasdaq.snapshot, ticker, ("summary", "institutional", "earnings", "analyst"))
    rec["finviz"] = _call(finviz.all_tables, ticker)
    rec["sec_annual_eps"] = _call(sec_edgar.annual_eps, ticker)
    rec["sec_ttm"] = _call(sec_edgar.ttm_series, ticker)
    rec["sec_altman"] = _call(sec_edgar.altman_inputs, ticker)
    rec["sec_8k"] = _call(sec_edgar.recent_8k, ticker, "2026-08-01")
    rec["news"] = _call(news.existential_scan, company, ticker)
    return rec


def run(tickers: list[tuple[str, str | None]], *, prices_only: bool = False, workers: int = 4) -> None:
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(collect, t, n, prices_only=prices_only): t for t, n in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            rec = f.result()
            suffix = ".prices.json" if prices_only else ".json"
            (OUT / f"{t.replace('/', '_')}{suffix}").write_text(json.dumps(rec, default=str))
            errs = [k for k, v in rec.items() if isinstance(v, dict) and v.get("error")]
            print(f"[{i}/{len(futs)}] {t:6s} {time.time() - t0:6.0f}s  errors: {errs or '-'}", flush=True)


def load(ticker: str, *, prices_only: bool = False) -> dict:
    suffix = ".prices.json" if prices_only else ".json"
    return json.loads((OUT / f"{ticker.replace('/', '_')}{suffix}").read_text())


if __name__ == "__main__":
    args = sys.argv[1:]
    prices_only = "--prices-only" in args
    args = [a for a in args if not a.startswith("--")]
    run([(a, None) for a in args], prices_only=prices_only)
