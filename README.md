# Deep-oversold equity screen

Stage 1 of a deep-oversold / capitulation-reversal screen: find liquid stocks with RSI(14) < 30 that
clear every hard gate (free cash flow, Altman Z, leverage, EPS growth, forward PEG, analyst upside,
broker rating, falling-knife fences, existential news, and a five-year EV/EBITDA history test).

Latest run: `output/2026-11-14/hard_pass_report.md` (prices as of the 2026-11-13 close).

## Layout

- `data/` — the ticker universe supplied for the screen
- `screener/sources/` — one adapter per data provider (TradingView/FactSet, Zacks, stockanalysis,
  SEC EDGAR, Nasdaq, Finviz, FT Markets, FRED, Google News). All requests go through
  `screener/http.py`, which throttles per host, backs off on rate limits and caches responses in `.cache/`.
- `screener/technicals.py`, `gates.py`, `evebitda.py`, `scoring.py`, `seasonality.py` — the gate,
  scorecard, ranking and overlay logic (pure functions)
- `screener/collect.py` — gathers every provider's data for a ticker
- `screener/screen.py` — evaluates the hard gates; `screener/report.py` — writes the report

## Outputs per run (`output/<date>/`)

- `hard_pass_report.md` — result, funnel, gate-by-gate detail, failed-criteria log, near misses
- `gate_table_rsi_below_30.csv` — pass/fail per gate for every oversold name
- `evaluation.json` — every provider's value behind each gate decision
- `qualitative_gates.json` — the existential-news and thesis-filter judgments with their evidence
- `verification.json` — the independent re-check of the surviving names
- `rsi_recheck_borderline.csv` — RSI recomputed from two price sources for names just above 30

The adapters read only what each provider serves publicly; a source that answers with a rate limit,
login wall or bot challenge is reported as unavailable. This is a rules-based screen, not investment advice.
