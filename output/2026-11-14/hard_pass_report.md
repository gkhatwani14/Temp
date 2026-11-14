# Deep-oversold screen — Stage 1 hard-pass list

Report date 2026-11-14 (Saturday); all prices are the 2026-11-13 close; data retrieved 2026-11-14 13:15–14:10 UTC.

**Two stocks qualify as hard passes: Grand Canyon Education (LOPE) and Kanzhun (BZ).** NVIDIA (NVDA) clears every numeric gate but is excluded under the existential-news rule. Stage 2 confirmation gates (three-scenario DCF: base-case upside >= 15%, worst-case downside <= 8%) have not been run, so both names are screening-stage passes, not buy recommendations.

## Result

| Rank | Ticker | Company | Status | RSI(14) | vs 50-day MA | Reversal scorecard | Target upside | PEG | Broker rating | EV/EBITDA vs own history | Composite |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **BZ** | KANZHUN LIMITED - American Depository Shares | HARD PASS (caveats) | 28.4 | -17.8% | 2 / 6 | 69% | 1.09 | 1.29 | 5.2x, percentile 6% | 60 |
| 2 | **LOPE** | Grand Canyon Education, Inc. | HARD PASS | 25.9 | -19.3% | 3 / 6 | 55% | 0.80 | 1.50 | 8.5x, percentile 3% | 40 |
| — | NVDA | NVIDIA Corporation | Numeric pass — EXCLUDED (existential news) | 24.9 | -24.1% | 3 / 6 | 80% | 1.25 | 1.47 | 19.6x, percentile 2% | — |

Target upside, PEG and broker rating are the Zacks figures (the screen's designated provider); EV/EBITDA percentile is the current multiple's rank within its own five-year history. With only two qualifiers the rank-normalised composite is coarse: each component is simply 100 for the better name and 0 for the other.

## Funnel

- Supplied list: 30,000 global tickers; 5,294 are listed on the screen's exchanges (NASDAQ 2,387, NYSE 1,901, LSE 867, NYSE American/Arca 138, Cboe 1). Nine could not be matched to live data.
- Market cap > $1B and 90-day average traded value > $10M: 2,186 names (2,065 US, 121 LSE; GBP converted at 1.3208 USD, 2026-11-13).
- RSI(14) < 30 at the 2026-11-13 close: 36 names, all US-listed. No LSE name qualifies: the two oversold LSE names above $1B (Kainos, Coats) trade under $10M a day.
- Regime: SPY closed 1.3% below its 50-day MA (754.37 vs 764.43), far from the 10% systemic-panic trigger, so the 25% fence is a hard exclusion.
- Falling-knife fences: 13 names sit more than 25% below their 50-day MA and are excluded; none has RSI < 15. 23 remain.
- Fundamental, valuation, sentiment and EV/EBITDA-history gates: 3 names clear all of them (NVDA, LOPE, BZ).
- Existential-news exclusion and thesis filter: NVDA excluded; LOPE and BZ qualify.
- Cross-checks: recomputing Wilder RSI from two independent price histories reproduced the provider RSI to within 0.05 on all 74 names with RSI < 33, so no name sits on the wrong side of 30. Finviz's own RSI < 30 screen contains all 36 names; its nine extras are five names that fail the $10M liquidity floor and four NYSE ADRs absent from the list (PBR, PBR.A, YPF, VIST), which were evaluated anyway and fail.

## Qualified hard passes

### BZ — KANZHUN LIMITED - American Depository Shares

Internet - Software (Zacks industry) · close $12.96 (2026-11-13) · market cap $5.94B · 90-day average traded value $93.82M/day

| Hard gate | Result | Deciding figure (provider) | Other providers |
|---|---|---|---|
| RSI(14) < 30, >= 15 | PASS | 28.40 (recomputed, Wilder, from stockanalysis and Nasdaq daily closes) | factset_tv 28.40, stockanalysis 28.40, nasdaq 28.40 |
| Not > 25% below 50-day MA | PASS | -17.8% (50-day SMA $15.76) | same on all three price sources |
| FCF (TTM) > 0 | PASS | CNY 4.43B for the latest fiscal year (SEC annual filing; no quarterly XBRL) | stockanalysis $664.82M, zacks $659.16M (TTM, USD) |
| Altman Z > 3 | PASS | 5.08 (factset) | factset 5.08, stockanalysis 7.82 |
| Net debt/EBITDA < 2.5x, coverage > 5x | PASS | SOLVENCY PASS -- net cash / negligible debt (net debt <= 0) (factset) | sec: net debt or EBITDA unavailable; stockanalysis: SOLVENCY PASS -- net cash / negligible debt (net debt <= 0) |
| 3-yr EPS CAGR > 10% | PASS | 190.8%: diluted EPS 0.12 (FY 2022) to 2.95 (FY 2025) (sec) | sec 190.8%, factset 186.5%, stockanalysis 190.8%, finviz 186.5% |
| Forward PEG 0–1.5 | PASS | 1.09 = P/E (F1) 10.2 / 3–5yr growth 9.37% (zacks) | zacks 1.09, nasdaq 1.38, stockanalysis 0.99, finviz 0.83 |
| Consensus target >= 15% above price | PASS | 69.4%: target $21.95 from 13 analysts (zacks) | zacks 69.4%, factset 62.5%, stockanalysis 61.6%, nasdaq 59.0%, finviz 62.7% |
| Average broker rating < 2 | PASS | 1.29 from 14 brokers; 1.29 a month ago, 1.29 three months ago (zacks) | zacks 1.29, factset 1.23, finviz 1.46, stockanalysis 1.36 |
| Historic EV/EBITDA | PASS — Option A | 5.17x vs 3-yr MA 22.76x, 5-yr median 32.99x, 25th pct 17.56x (quarterly period-end (provider series) + current; SHORT SERIES) |  |
| Existential-news exclusion | PASS | ADS fell 25.3% on 2026-11-12. Headlines attribute it to softening demand, soft revenue guidance and founder share sales; none reports fraud, a restatement, withdrawn guidance or regulatory action. No company filing explains the move, so the trigger is known from headlines only. | |
| Thesis filter (>= 1) | PASS (contested) | Limb (c), growing demand: Q2 2026 revenue +14.1%, paid enterprise customers +10.8% to 7.2M, monthly active users above 70M for the first time, growth 'accelerating' (6-K 2026-08-25). November headlines citing softening demand cut against this; Q3 results on 2026-11-25 will settle it. | |

`EV/EBITDA (TTM) = 5.17 | 5Y Median = 32.99 | Percentile = 6% | Z-Score = -0.45 | Pass = YES | Value Trap = NO`

EV/EBITDA series statistics (quarterly period-end (provider series) + current, 2021-09-30 to 2026-11-13, 18 observations): min 5.17, mean 95.93, median 32.99, st. dev. 202.26; current is 0.0% above the minimum and -77.3% versus the 3-year MA; trailing 4-quarter EBITDA growth 80.0%.

**Oversold-reversal scorecard: 2 / 6**

| Signal | Points | Evidence |
|---|---|---|
| RSI sweet spot | 1 / 2 | RSI 28.4 |
| Bullish RSI divergence | 0 / 1 | most recent closing low 2026-11-12 at RSI 23.7; no earlier higher-price/lower-RSI low in the 20-session window |
| Price stabilisation | 0 / 1 | last new 20-session closing low 2026-11-12 (1 sessions ago; needs 5 clear sessions) |
| Capitulation-volume exhaustion | 0 / 1 | flush 2026-11-12: 3.5x the prior 90-day average volume, 1 sessions old, down-day volume contracting: True |
| Institutional accumulation | 1 / 1 | 13F net change +19,901,862 shares (increased 78,316,366 vs decreased 58,414,504; mostly period ending 2026-09-30; Nasdaq) |

**Tier-2 scored criteria: 8 / 14**

| Criterion | Met | Evidence |
|---|---|---|
| profitability score gt 50 | yes | SUBSTITUTE -- no provider Profitability Score is publicly retrievable; Piotroski F-Score 6/9 (>= 5 counts as > 50%) |
| fcf positive prior fy and ttm | yes | latest fiscal-year FCF 641.18 (USD m, Zacks statements, FY 2025-12-31); TTM gate PASS |
| debt equity lt 1 and current ratio gt 1 2 | yes | Debt/Equity 0.01, Current Ratio 4.189 ({'stockanalysis': 0.01, 'zacks': 0.0, 'finviz': 0.01}, {'stockanalysis': 4.189, 'zacks': 4.19, 'finviz': 4.19}) |
| earnings beat last quarter | yes | last-quarter EPS surprise 3.13% (Zacks) |
| expanding margins | yes | grossMargin TTM 85.9% vs 3-FY avg 83.5%; operatingMargin TTM 34.8% vs 3-FY avg 19.2% |
| earnings within 5 weeks | yes | next earnings date 2026-11-25 |
| pe ttm 12 to 25 | no | P/E (TTM) 9.043 ({'stockanalysis': 9.043, 'finviz': 9.18, 'factset': 9.178470254957508}) |
| industry rank top 25pct | no | Zacks Industry Rank 84 of 247 (Internet - Software) |
| zacks rank 1 or 2 | no | Zacks Rank 3 |
| vgm a or b | yes | VGM Score A |
| net upgrades or positive revisions | yes | analyst actions 90d: 1 upgrades vs 0 downgrades (Finviz); EPS estimate revisions 60d (FY1+FY2): 0 up / 0 down (Zacks) |
| sharpe 3y gt 0 7 | no | 3-yr weekly Sharpe 0.09 |
| sharpe 3y gt 1 0 bonus | no | bonus point |
| return 3y positive | no | 3-yr price return -13.8% |

**Composite rank score: 60 / 100** — technical 0, upside 100, quality 100, valuation 0, sentiment 100, catalyst 100

**Reward:risk.** Default plan: target +12% ($14.52) and stop −8% ($11.92) from the last close, reward:risk 1.5:1. Entry discipline: the 25% gap is one session old and the flush has not aged, so there is no stabilisation evidence yet; wait for five sessions without a close below $12.49, which runs into the 2026-11-25 results. A close below $12.49 after entry means the stop governs.

**Flags**

- Event risk: Q3 results on 2026-11-25 arrive before any stabilisation can be confirmed. A one-day 25% gap also shows the stock does not trade tightly enough to guarantee an 8% stop.
- Most analyst inputs pre-date the drop: the Zacks rating (1.29, 14 brokers) is unchanged for three months and the $21.95 average target is largely unrevised. The three post-drop actions are Citi $26 to $22 (11-12), Barclays $20 to $17 (11-13) and an 86Research upgrade with a $17 target (11-13); even $17 is 31% above the close, so the upside gate holds on fresh targets alone.
- Foreign private issuer reporting in RMB: SEC XBRL data is annual only, so TTM free cash flow, Altman Z and net cash come from providers (S&P via stockanalysis, FactSet, Zacks) rather than from filings.
- The EV/EBITDA gate rests on an 18-quarter provider series (listed June 2021): SHORT SERIES, quarterly not monthly.
- The 191% EPS CAGR is off a near-zero 2022 base (RMB 0.12 per share).
- Insiders have been sellers: nine open-market sales worth $122M and $381M of proposed sales (Form 144) in six months, per Finviz.
- Three-year price return is -13.8% and the three-year Sharpe ratio is 0.09.

### LOPE — Grand Canyon Education, Inc.

Schools (Zacks industry) · close $120.60 (2026-11-13) · market cap $3.08B · 90-day average traded value $49.77M/day

| Hard gate | Result | Deciding figure (provider) | Other providers |
|---|---|---|---|
| RSI(14) < 30, >= 15 | PASS | 25.93 (recomputed, Wilder, from stockanalysis and Nasdaq daily closes) | factset_tv 25.93, stockanalysis 25.93, nasdaq 25.93 |
| Not > 25% below 50-day MA | PASS | -19.3% (50-day SMA $149.49) | same on all three price sources |
| FCF (TTM) > 0 | PASS | $264.85M (sec) | sec $264.85M, factset $264.85M, stockanalysis $264.85M, zacks $264.85M |
| Altman Z > 3 | PASS | 16.32 (factset) | factset 16.32, stockanalysis 17.50, sec 14.39 |
| Net debt/EBITDA < 2.5x, coverage > 5x | PASS | SOLVENCY PASS -- net cash / negligible debt (net debt <= 0) (sec) | factset: SOLVENCY PASS -- net cash / negligible debt (net debt <= 0); stockanalysis: SOLVENCY PASS -- net cash / negligible debt (net debt <= 0) |
| 3-yr EPS CAGR > 10% | PASS | 10.4%: diluted EPS 5.73 (FY 2022) to 7.71 (FY 2025) (sec) | sec 10.4%, factset 10.4%, stockanalysis 10.4%, finviz 10.4% |
| Forward PEG 0–1.5 | PASS | 0.80 = P/E (F1) 11.95 / 3–5yr growth 15.0% (zacks) | zacks 0.80, nasdaq 0.80, stockanalysis 0.76, finviz 1.13 |
| Consensus target >= 15% above price | PASS | 55.3%: target $187.33 from 3 analysts (zacks) | zacks 55.3%, factset 52.8%, stockanalysis 52.8%, nasdaq 54.6%, finviz 52.8% |
| Average broker rating < 2 | PASS | 1.50 from 4 brokers; 1.5 a month ago, 1.5 three months ago (zacks) | zacks 1.50, factset 1.12, finviz 1.25, stockanalysis 1.33 |
| Historic EV/EBITDA | PASS — Option A | 8.49x vs 3-yr MA 14.12x, 5-yr median 13.07x, 25th pct 11.48x (monthly, point-in-time (SEC filings + month-end close)) | stockanalysis_quarterly: 8.42x, pctile 5%, pass=True |
| Existential-news exclusion | PASS | Decline is an earnings reaction: shares fell 18.9% on 2026-11-05 after Q3 results (2.4% revenue growth, Q4 EPS guide below consensus). Full-year 2026 guidance was issued, not withdrawn; no fraud, restatement, going-concern or regulatory trigger in the headlines or 8-Ks. | |
| Thesis filter (>= 1) | PASS (weak) | Limb (c), growing demand: university-partner enrollments +5.2% to 145,270, online +7.1%, off-campus sites +12.6%, two new sites opened in 2026 (Q3 release, 8-K 2026-11-04). No exposure to the listed growth themes and no specific incentive. | |

`EV/EBITDA (TTM) = 8.49 | 5Y Median = 13.07 | Percentile = 3% | Z-Score = -1.87 | Pass = YES | Value Trap = NO`

EV/EBITDA series statistics (monthly, point-in-time (SEC filings + month-end close), 2021-11-30 to 2026-11-13, 61 observations): min 8.09, mean 12.92, median 13.07, st. dev. 2.37; current is 5.0% above the minimum and -39.9% versus the 3-year MA; trailing 4-quarter EBITDA growth 19.7%.

**Oversold-reversal scorecard: 3 / 6**

| Signal | Points | Evidence |
|---|---|---|
| RSI sweet spot | 1 / 2 | RSI 25.9 |
| Bullish RSI divergence | 0 / 1 | most recent closing low 2026-11-09 at RSI 18.1; no earlier higher-price/lower-RSI low in the 20-session window |
| Price stabilisation | 0 / 1 | last new 20-session closing low 2026-11-09 (4 sessions ago; needs 5 clear sessions) |
| Capitulation-volume exhaustion | 1 / 1 | flush 2026-11-05: 3.8x the prior 90-day average volume, 6 sessions old, down-day volume contracting: True |
| Institutional accumulation | 1 / 1 | 13F net change +1,755,583 shares (increased 3,978,851 vs decreased 2,223,268; mostly period ending 2026-09-30; Nasdaq) |

**Tier-2 scored criteria: 6 / 14**

| Criterion | Met | Evidence |
|---|---|---|
| profitability score gt 50 | yes | SUBSTITUTE -- no provider Profitability Score is publicly retrievable; Piotroski F-Score 6/9 (>= 5 counts as > 50%) |
| fcf positive prior fy and ttm | yes | latest fiscal-year FCF 238.65 (USD m, Zacks statements, FY 2025-12-31); TTM gate PASS |
| debt equity lt 1 and current ratio gt 1 2 | yes | Debt/Equity 0.18, Current Ratio 2.641 ({'stockanalysis': 0.18, 'zacks': 0.0, 'finviz': 0.18}, {'stockanalysis': 2.641, 'zacks': 2.64, 'finviz': 2.64}) |
| earnings beat last quarter | no | last-quarter EPS surprise 0.0% (Zacks) |
| expanding margins | yes | grossMargin TTM 53.3% vs 3-FY avg 52.8%; operatingMargin TTM 27.3% vs 3-FY avg 26.8% |
| earnings within 5 weeks | no | next earnings date 2027-02-17 |
| pe ttm 12 to 25 | yes | P/E (TTM) 12.798 ({'stockanalysis': 12.798, 'finviz': 12.88, 'factset': 12.879799220376995}) |
| industry rank top 25pct | no | Zacks Industry Rank 160 of 247 (Schools) |
| zacks rank 1 or 2 | no | Zacks Rank 4 |
| vgm a or b | yes | VGM Score A |
| net upgrades or positive revisions | no | analyst actions 90d: 0 upgrades vs 0 downgrades (Finviz); EPS estimate revisions 60d (FY1+FY2): 0 up / 4 down (Zacks) |
| sharpe 3y gt 0 7 | no | 3-yr weekly Sharpe -0.08 |
| sharpe 3y gt 1 0 bonus | no | bonus point |
| return 3y positive | no | 3-yr price return -12.6% |

**Composite rank score: 40 / 100** — technical 100, upside 0, quality 0, valuation 100, sentiment 0, catalyst 0

**Reward:risk.** Default plan: target +12% ($135.07) and stop −8% ($110.95) from the last close, reward:risk 1.5:1. Entry discipline: the last new 20-session closing low was 2026-11-09, four sessions ago, so the stabilisation test (five clear sessions) is not yet met; wait for a fifth session without a new closing low below $116.16. A later close below that level means the stop governs.

**Flags**

- The 3-year EPS CAGR is 10.4%, barely above the 10% floor (GAAP diluted EPS $5.73 in 2022 to $7.71 in 2025; 2025 was flat on 2024). All four providers agree on the figure.
- Analyst coverage is thin: the Zacks broker rating rests on 4 brokers and the target on 3 analysts. All three targets were revised after the drop (Truist to $180 and Barrington $200 on 2026-11-05, BMO to $173 on 2026-11-06), so the upside is not a stale-target artefact.
- Sentiment is poor: Zacks Rank 4 (Sell), four downward and no upward EPS revisions in 60 days, Zacks industry rank 160 of 247.
- The thesis fit is the weakest part: a September headline attributes an earlier 28% drop to fears that AI disrupts online education, which is the structural bear case to weigh.
- No catalyst is near: next earnings are expected 2027-02-17.
- Three-year price return is -12.6% and the three-year Sharpe ratio is -0.08, so it is not the 'compounder caught in a selloff' archetype on price, though ROIC has risen every year to 44%.

## Passed every numeric gate but excluded

### NVDA — NVIDIA Corporation

Semiconductor - General (Zacks industry) · close $162.85 (2026-11-13) · market cap $3.92T · 90-day average traded value $28.81B/day

| Hard gate | Result | Deciding figure (provider) | Other providers |
|---|---|---|---|
| RSI(14) < 30, >= 15 | PASS | 24.86 (recomputed, Wilder, from stockanalysis and Nasdaq daily closes) | factset_tv 24.86, stockanalysis 24.86, nasdaq 24.86 |
| Not > 25% below 50-day MA | PASS | -24.1% (50-day SMA $214.65) | same on all three price sources |
| FCF (TTM) > 0 | PASS | $127.01B (sec) | sec $127.01B, factset $127.01B, stockanalysis $127.01B, zacks $127.01B |
| Altman Z > 3 | PASS | 37.34 (factset) | factset 37.34, stockanalysis 34.85, sec 30.31 |
| Net debt/EBITDA < 2.5x, coverage > 5x | PASS | SOLVENCY PASS -- net cash / negligible debt (ND/EBITDA 0.05x < 0.5x) (sec) | factset: SOLVENCY PASS -- net cash / negligible debt (net debt <= 0); stockanalysis: SOLVENCY PASS -- net cash / negligible debt (net debt <= 0) |
| 3-yr EPS CAGR > 10% | PASS | 206.6%: diluted EPS 0.17 (FY 2023) to 4.9 (FY 2026) (sec) | sec 206.6%, factset 204.1%, stockanalysis 206.6%, finviz 204.1% |
| Forward PEG 0–1.5 | PASS | 1.25 = P/E (F1) 17.54 / 3–5yr growth 14.0% (zacks) | zacks 1.25, nasdaq 1.25, stockanalysis 0.27, finviz 0.16 |
| Consensus target >= 15% above price | PASS | 80.1%: target $293.22 from 47 analysts (zacks) | zacks 80.1%, factset 75.7%, stockanalysis 71.7%, nasdaq 52.0%, finviz 75.7% |
| Average broker rating < 2 | PASS | 1.47 from 53 brokers; 1.13 a month ago, 1.14 three months ago (zacks) | zacks 1.47, factset 1.27, finviz 1.53, stockanalysis 1.62 |
| Historic EV/EBITDA | PASS — Option A | 19.55x vs 3-yr MA 42.88x, 5-yr median 47.86x, 25th pct 36.88x (monthly, point-in-time (SEC filings + month-end close)) | stockanalysis_quarterly: 19.42x, pctile 5%, pass=True |
| Existential-news exclusion | EXCLUDED | Drawdown follows the CEO's immediate resignation (reported 2026-11-12) amid a board special-committee investigation, a reported FBI inquiry, congressional subpoena threats and calls for tighter China export controls, and investor litigation. | |
| Thesis filter (>= 1) | PASS | AI hardware; sovereign-AI and export-policy exposure; new infrastructure partnerships. | |

`EV/EBITDA (TTM) = 19.55 | 5Y Median = 47.86 | Percentile = 2% | Z-Score = -1.11 | Pass = YES | Value Trap = NO`

EV/EBITDA series statistics (monthly, point-in-time (SEC filings + month-end close), 2021-11-30 to 2026-11-13, 61 observations): min 19.55, mean 57.68, median 47.86, st. dev. 34.23; current is 0.0% above the minimum and -54.4% versus the 3-year MA; trailing 4-quarter EBITDA growth 104.8%.

**Oversold-reversal scorecard: 3 / 6**

| Signal | Points | Evidence |
|---|---|---|
| RSI sweet spot | 2 / 2 | RSI 24.9 |
| Bullish RSI divergence | 0 / 1 | most recent closing low 2026-11-13 at RSI 24.9; no earlier higher-price/lower-RSI low in the 20-session window |
| Price stabilisation | 0 / 1 | last new 20-session closing low 2026-11-13 (0 sessions ago; needs 5 clear sessions) |
| Capitulation-volume exhaustion | 0 / 1 | flush 2026-11-12: 5.7x the prior 90-day average volume, 1 sessions old, down-day volume contracting: True |
| Institutional accumulation | 1 / 1 | 13F net change +139,013,818 shares (increased 606,833,886 vs decreased 467,820,068; mostly period ending 2026-09-30; Nasdaq) |

**Tier-2 scored criteria: 14 / 14**

| Criterion | Met | Evidence |
|---|---|---|
| profitability score gt 50 | yes | SUBSTITUTE -- no provider Profitability Score is publicly retrievable; Piotroski F-Score 5/9 (>= 5 counts as > 50%) |
| fcf positive prior fy and ttm | yes | latest fiscal-year FCF 96676.0 (USD m, Zacks statements, FY 2026-01-31); TTM gate PASS |
| debt equity lt 1 and current ratio gt 1 2 | yes | Debt/Equity 0.17, Current Ratio 4.589 ({'stockanalysis': 0.17, 'zacks': 0.14, 'finviz': 0.17}, {'stockanalysis': 4.589, 'zacks': 4.59, 'finviz': 4.59}) |
| earnings beat last quarter | yes | last-quarter EPS surprise 6.22% (Zacks) |
| expanding margins | yes | grossMargin TTM 74.7% vs 3-FY avg 72.9%; operatingMargin TTM 65.2% vs 3-FY avg 59.0% |
| earnings within 5 weeks | yes | next earnings date 2026-11-17 |
| pe ttm 12 to 25 | yes | P/E (TTM) 20.592 ({'stockanalysis': 20.592, 'finviz': 20.59, 'factset': 20.586301924000708}) |
| industry rank top 25pct | yes | Zacks Industry Rank 51 of 247 (Semiconductor - General) |
| zacks rank 1 or 2 | yes | Zacks Rank 2 |
| vgm a or b | yes | VGM Score B |
| net upgrades or positive revisions | yes | analyst actions 90d: 0 upgrades vs 13 downgrades (Finviz); EPS estimate revisions 60d (FY1+FY2): 6 up / 1 down (Zacks) |
| sharpe 3y gt 0 7 | yes | 3-yr weekly Sharpe 1.04 |
| sharpe 3y gt 1 0 bonus | yes | bonus point |
| return 3y positive | yes | 3-yr price return 234.9% |

**Reward:risk.** Default plan: target +12% ($182.39) and stop −8% ($149.82) from the last close, reward:risk 1.5:1. Entry discipline: not applicable while excluded.

**Flags**

- Thirteen analyst downgrades between 2026-11-09 and 2026-11-13 (Finviz); the Zacks broker rating moved from 1.13 to 1.47 in a week.
- Still printing new 20-session closing lows (four in the last five sessions); the 5.7x-volume flush on 2026-11-12 is one session old.
- Earnings on 2026-11-17 will be reported under an interim CEO.

**Why excluded.** The screen's rule removes names whose drawdown is driven by regulatory or legal action that plausibly reprices the business permanently, because trailing fundamentals cannot see such events. The headlines retrieved on 2026-11-14 (Reuters, WSJ, Yahoo Finance, Barron's via Finviz and Google News) describe an unresolved board investigation with outside counsel, a reported federal investigation, lawmakers pressing for export-control action and threatening subpoenas, and shareholder suits. That is a legal and regulatory overhang with an open-ended outcome, not an ordinary earnings miss, so the conservative reading is to exclude. This is a judgment call: on a narrow reading (no fraud, restatement, going-concern or withdrawn guidance has been reported) NVDA would be the top-ranked hard pass, clearing every numeric gate on every provider with no conflicts and scoring 14 of 14 on the Tier-2 criteria. Revisit after the 2026-11-17 results and once the investigation's scope is known.

## Failed-criteria log: every name with RSI(14) < 30 in the universe

P = pass, F = fail, ? = not evaluable from sourced data (a hard gate that cannot be evaluated cannot be passed). Rows marked † are NYSE-listed ADRs that are oversold but are not in the supplied ticker list.

| Ticker | RSI | vs 50d MA | UNIV | TECH | FCF | ALTZ | SOLV | EPS3 | PEG | UPSD | ABR | EVEB | Failed / not-evaluable criteria |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NVDA | 24.9 | -24.1% | P | P | P | P | P | P | P | P | P | P | all numeric gates pass — Numeric pass — EXCLUDED (existential news) |
| LOPE | 25.9 | -19.3% | P | P | P | P | P | P | P | P | P | P | all numeric gates pass — HARD PASS |
| BZ | 28.4 | -17.8% | P | P | P | P | P | P | P | P | P | P | all numeric gates pass — HARD PASS (caveats) |
| DT | 26.7 | -20.5% | P | P | P | P | P | P | F | P | P | P | PEG 1.79 (zacks) |
| VIST † | 28.3 | -18.3% | P | P | P | F | P | P | P | P | P | P | Altman Z 1.98 (factset) |
| DGII | 29.3 | -23.0% | P | P | P | P | P | P | ? | P | P | F | forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA 19.34x at percentile 59% of its own history (no option met) |
| HWC | 29.0 | -10.4% | P | P | P | ? | ? | F | P | P | P | ? | Altman Z not evaluable (financial; no sourced Solvency Score); solvency not evaluable; 3-yr EPS CAGR -1.8% (sec); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| RSI | 26.2 | -36.6% | P | F | P | P | P | F | P | P | P | P | falling knife: -36.6% vs 50-day MA (fence -25%); 3-yr EPS CAGR undefined (non-positive EPS) (sec) |
| PBR.A † | 26.2 | -18.6% | P | P | P | F | P | F | P | P | P | P | Altman Z 1.42 (factset); 3-yr EPS CAGR -18.5% (sec) |
| PBR † | 28.8 | -16.5% | P | P | P | F | P | F | P | P | P | P | Altman Z 1.51 (factset); 3-yr EPS CAGR -18.5% (sec) |
| DOX | 28.9 | -22.1% | P | P | P | P | P | F | P | P | F | P | 3-yr EPS CAGR -7.2% (sec); broker rating 2.67 (zacks) |
| NTCT | 29.1 | -15.6% | P | P | P | P | P | P | F | P | F | P | PEG 2.00 (finviz); broker rating 2.33 (zacks) |
| NEU | 21.0 | -16.7% | P | P | P | P | P | P | ? | F | F | P | forward PEG not published by any provider (no long-term growth estimate); target upside -8.2% (finviz); broker rating 2.00 (factset) |
| GEF | 24.5 | -12.1% | P | P | P | ? | P | F | P | P | F | P | Altman Z not evaluable; 3-yr EPS CAGR -9.8% (factset); broker rating 3.00 (zacks) |
| YPF † | 25.2 | -23.2% | P | P | P | F | P | F | ? | P | P | P | Altman Z 2.05 (stockanalysis); 3-yr EPS CAGR undefined (non-positive EPS) (factset); forward PEG not published by any provider (no long-term growth estimate) |
| TEAM | 28.0 | -21.9% | P | P | P | P | P | F | F | P | P | ? | 3-yr EPS CAGR undefined (non-positive EPS) (sec); PEG 2.03 (zacks); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| VCYT | 28.1 | -42.3% | P | F | P | P | P | F | ? | P | P | P | falling knife: -42.3% vs 50-day MA (fence -25%); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate) |
| GNL | 24.1 | -16.3% | P | P | P | ? | F | F | ? | P | P | P | Altman Z not evaluable (financial; no sourced Solvency Score); solvency: ND/EBITDA 7.63x, interest coverage 1.1x (sec); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate) |
| NRIX | 26.8 | -33.5% | P | F | P | P | P | F | ? | P | P | ? | falling knife: -33.5% vs 50-day MA (fence -25%); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| HLIO | 21.0 | -29.1% | P | F | P | P | P | F | F | P | P | P | falling knife: -29.1% vs 50-day MA (fence -25%); 3-yr EPS CAGR -21.7% (sec); PEG 1.69 (stockanalysis) |
| STRA | 22.0 | -28.1% | P | F | P | P | P | P | P | P | F | F | falling knife: -28.1% vs 50-day MA (fence -25%); broker rating 3.50 (zacks); EV/EBITDA 10.44x at percentile 61% of its own history (no option met) |
| CORT | 26.1 | -29.7% | P | F | P | P | P | F | P | P | F | P | falling knife: -29.7% vs 50-day MA (fence -25%); 3-yr EPS CAGR -2.0% (sec); broker rating 2.00 (zacks) |
| HALO | 26.7 | -12.5% | P | P | P | P | F | P | P | F | P | F | solvency: ND/EBITDA 3.15x, interest coverage 30.6x (sec); target upside 14.7% (zacks); EV/EBITDA 19.76x at percentile 46% of its own history (no option met) |
| IOSP | 29.8 | -14.3% | P | P | P | P | P | F | F | P | F | P | 3-yr EPS CAGR -4.3% (sec); PEG 3.20 (zacks); broker rating 2.00 (zacks) |
| AXON | 29.9 | -24.5% | P | P | P | P | F | F | F | P | P | P | solvency: ND/EBITDA 6.43x, interest coverage 0.6x (sec); 3-yr EPS CAGR -9.4% (sec); PEG 1.60 (zacks) |
| STWD | 28.6 | -15.1% | P | P | P | ? | F | F | P | P | P | F | Altman Z not evaluable (financial; no sourced Solvency Score); solvency: ND/EBITDA 14.67x, interest coverage 0.9x (sec); 3-yr EPS CAGR -25.1% (sec); EV/EBITDA 17.79x at percentile 95% of its own history (no option met) |
| SOUN | 22.5 | -32.7% | P | F | F | P | P | F | ? | P | P | ? | falling knife: -32.7% vs 50-day MA (fence -25%); FCF (TTM) $-129.25M (sec); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| ARWR | 25.9 | -24.5% | P | P | F | F | P | F | ? | P | P | ? | FCF (TTM) $-283.94M (sec); Altman Z 0.94 (factset); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| AGM | 29.8 | -14.2% | P | P | P | ? | F | F | P | P | F | ? | Altman Z not evaluable (financial; no sourced Solvency Score); solvency: ND/EBITDA 24.54x, interest coverage 1.3x (factset); 3-yr EPS CAGR 6.2% (sec); broker rating 2.00 (zacks); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| FFIV | 24.1 | -25.3% | P | F | P | P | P | P | F | P | F | F | falling knife: -25.3% vs 50-day MA (fence -25%); PEG 2.13 (zacks); broker rating 2.69 (zacks); EV/EBITDA 17.48x at percentile 57% of its own history (no option met) |
| CTVA | 25.4 | -29.4% | P | F | F | F | P | F | P | P | P | ? | falling knife: -29.4% vs 50-day MA (fence -25%); FCF (TTM) $-149.00M (sec); Altman Z 2.53 (factset); 3-yr EPS CAGR 0.4% (sec); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| ANDE | 28.1 | -14.8% | P | P | F | P | F | F | ? | P | P | F | FCF (TTM) $-293.91M (sec); solvency: ND/EBITDA 2.63x, interest coverage 19.3x (sec); 3-yr EPS CAGR -9.9% (sec); forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA 7.40x at percentile 61% of its own history (no option met) |
| IONS | 24.2 | -33.4% | P | F | F | F | P | F | F | P | P | ? | falling knife: -33.4% vs 50-day MA (fence -25%); FCF (TTM) $-616.26M (sec); Altman Z 0.83 (factset); 3-yr EPS CAGR undefined (non-positive EPS) (sec); PEG -0.85 (nasdaq); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| XIFR | 27.8 | -23.3% | P | P | F | F | F | F | ? | P | F | P | FCF (TTM) $-191.00M (sec); Altman Z 0.14 (factset); solvency: ND/EBITDA 8.59x, interest coverage 0.2x (sec); 3-yr EPS CAGR undefined (non-positive EPS) (factset); forward PEG not published by any provider (no long-term growth estimate); broker rating 2.98 (zacks) |
| URGN | 28.7 | -29.2% | P | F | F | F | F | F | ? | P | P | ? | falling knife: -29.2% vs 50-day MA (fence -25%); FCF (TTM) $-132.05M (sec); Altman Z -0.81 (factset); solvency: positive net debt with non-positive EBITDA (sec); 3-yr EPS CAGR undefined (non-positive EPS) (factset); forward PEG not published by any provider (no long-term growth estimate); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| OPEN | 24.9 | -45.8% | P | F | F | ? | F | F | ? | P | F | ? | falling knife: -45.8% vs 50-day MA (fence -25%); FCF (TTM) $-1.74B (sec); Altman Z not evaluable (financial; no sourced Solvency Score); solvency: positive net debt with non-positive EBITDA (sec); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate); broker rating 2.90 (zacks); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| CLMT | 22.9 | -33.9% | P | F | P | F | F | F | P | P | F | F | falling knife: -33.9% vs 50-day MA (fence -25%); Altman Z 1.92 (factset); solvency: ND/EBITDA 6.13x, interest coverage 0.9x (sec); 3-yr EPS CAGR undefined (non-positive EPS) (factset); broker rating 2.43 (zacks); EV/EBITDA 15.65x at percentile 55% of its own history (no option met) |
| OTTR | 25.7 | -13.1% | P | P | F | F | P | F | F | P | F | F | FCF (TTM) $-121.39M (sec); Altman Z 1.89 (factset); 3-yr EPS CAGR -1.1% (sec); PEG 1.56 (stockanalysis); broker rating 2.60 (zacks); EV/EBITDA 9.83x at percentile 89% of its own history (no option met) |
| PLUG | 28.9 | -34.5% | P | F | F | F | F | F | ? | P | F | ? | falling knife: -34.5% vs 50-day MA (fence -25%); FCF (TTM) $-485.54M (sec); Altman Z -4.35 (factset); solvency: positive net debt with non-positive EBITDA (sec); 3-yr EPS CAGR undefined (non-positive EPS) (sec); forward PEG not published by any provider (no long-term growth estimate); broker rating 2.77 (zacks); EV/EBITDA history not evaluable (EBITDA <= 0 or no sourced series) |
| FLNG | 27.3 | -13.7% | P | P | P | F | F | F | F | F | F | F | Altman Z 1.02 (factset); solvency: ND/EBITDA 5.45x, interest coverage 2.1x (sec); 3-yr EPS CAGR -26.7% (sec); PEG 1.68 (finviz); target upside 11.2% (zacks); broker rating 3.33 (zacks); EV/EBITDA 11.26x at percentile 71% of its own history (no option met) |

Gate keys: UNIV = universe (cap/liquidity); TECH = technical (RSI<30, no falling knife); FCF = FCF (TTM) > 0; ALTZ = Altman Z > 3; SOLV = ND/EBITDA < 2.5x & coverage > 5x; EPS3 = 3-yr EPS CAGR > 10%; PEG = forward PEG 0-1.5; UPSD = target upside >= 15%; ABR = broker rating < 2; EVEB = historic EV/EBITDA gate.

## Near misses (capitulation watchlist)

| Ticker | What it fails | Detail |
|---|---|---|
| DT (Dynatrace) | Forward PEG only | Zacks PEG 1.79 (P/E 23.65 on 13.2% growth) against a 1.5 cap; Nasdaq 3.13, stockanalysis 1.92, Finviz 1.41. Every other gate passes, EV/EBITDA is at the 8th percentile of its history. |
| DGII (Digi International) | EV/EBITDA history; PEG not published | EV/EBITDA 19.3x is at the 59th percentile of its own history on both series, so no option is met; no provider publishes a long-term growth estimate, so the PEG gate cannot be evaluated. |
| HALO (Halozyme) | Target upside, solvency, EV/EBITDA (provider-dependent) | Zacks consensus target $115 is 14.7% above the price (other providers 17–20%); net debt/EBITDA is 2.7–3.2x on as-reported EBITDA, which includes a 2025 acquired-R&D charge, but 1.9x on providers' adjusted EBITDA. |
| NTCT (NetScout) | Broker rating; PEG | Zacks rating 2.33 from 3 brokers (was 1.67 three months ago); no Zacks growth estimate, Finviz PEG 2.0. |
| FFIV (F5) | Falling-knife fence, PEG, rating | 25.3% below its 50-day MA on all three price sources, just through the fence; Zacks PEG and rating also fail. |
| HWC (Hancock Whitney) | 3-yr EPS CAGR | EPS $5.98 (2022) to $5.67 (2025), a -1.8% CAGR. As a bank its Altman Z is undefined and no provider solvency score could be sourced. |
| VIST (Vista Energy, not in the list) | Altman Z | FactSet Altman Z 1.98; passes the other numeric gates. |

## Overlays from the other two attachments

**Seasonality (seasonality prompt).** Sixteen years of monthly dividend-adjusted closes to October 2026, Kruskal–Wallis across calendar months, best 2–10 month window on demeaned log returns.

| Ticker | Kruskal–Wallis p | Best window | Avg window return | Win rate | Rest of year | Seasonal excess | Tier | Out-of-sample |
|---|---|---|---|---|---|---|---|---|
| LOPE | 0.96 | buy start of Feb, sell end of Mar | 6.7% | 10 of 16 | 9.2% | 4.5 pts | none | window fitted on 2010–2017 (Aug–Nov) did not hold in 2018–2026 |
| NVDA | 0.29 | buy start of May, sell end of Aug | 27.8% | 13 of 16 | 29.8% | 13.1 pts | none | fitted window held, but far behind buy-and-hold |
| BZ | not analysed | — | — | — | — | — | — | listed June 2021: only 5 observations per calendar month (10 required) |

Neither analysable name shows statistically real seasonality (p well above 0.05, so no q-value survives), and November is not a seasonally weak month for LOPE (average +3.0%). Seasonality gives no reason to time or avoid either entry.

**Broken price or broken business (notes, section 2).**

| Indicator | LOPE | BZ |
|---|---|---|
| ROIC high and stable (> 15%) | Yes: 35% (2022) rising every year to 44% TTM (S&P via stockanalysis) | Unclear: providers disagree (S&P 9.9%, Finviz 21%, FactSet 24%) |
| FCF margin robust | Yes: about 23% | Yes: about 50% |
| Gross margin flat or expanding | Yes: 53.3% TTM vs 52.8% three-year average | Yes: 85.9% vs 83.5% |
| Deeply oversold vs 200-day MA | RSI 25.9; 22% below the 200-day MA | RSI 28.4; 14% below the 200-day MA |
| Death cross with volume expansion on down days | 50-day below 200-day since 2025-12-10; down-day volume about equal to up-day volume (1.04x) | 50-day still above 200-day; down-day volume 1.21x up-day volume |
| Litmus test: revenue and cash flow still compounding | Yes: revenue +2.4% in Q3 with FCF and EBITDA up about 20% year on year, and 2026 EPS guided to $9.81–9.85 against $7.71 in 2025 | Through Q2 yes (revenue +14.1%); November headlines question it and Q3 is unreported |
| Reading | Broken price, on the evidence available | Undetermined until the 2026-11-25 results |

**Consolidation zone (notes, section 1).** Neither name has formed a base. For both, ATR and Bollinger-band width are at the top of their one-year range (volatility is expanding, not compressing), and LOPE's 50/100/200-day EMAs are spread across 8% of the price. BZ's EMAs are clustered within 2%, but the price has just gapped below all of them. A volume-profile reading needs intraday data, which was not collected.

## Method, sources and limits

- **Bulk first pass:** TradingView scanner (fundamentals and estimates supplied by FactSet) for every in-scope ticker: RSI, 50-day MA, market cap, traded value, and first-pass fundamentals.
- **Per-candidate verification of all 40 oversold names:** Zacks (PEG, broker rating, price target, rank, style scores, industry rank), stockanalysis.com (S&P Global Market Intelligence: Altman Z, FCF, debt, EBITDA, quarterly EV/EBITDA history), SEC EDGAR XBRL filings (FCF, EBITDA, debt, cash, shares, split-adjusted annual EPS), Nasdaq.com (daily prices, 13F position changes, analyst data), Finviz (snapshot, analyst actions, insider trades, news), FRED (3-month T-bill 4.14% on 2026-11-12) and Google News RSS headlines.
- **Gate decisions:** each gate is evaluated on every provider that reports it and decided by a stated hierarchy (filings first for FCF, solvency and EPS; Zacks first for PEG, target and broker rating; FactSet first for Altman Z). The evaluation file records every provider's value and flags disagreements.
- **EV/EBITDA history:** primary series is monthly and point-in-time, built from SEC filings (EBITDA = operating income + D&A over the trailing four quarters, debt and cash from the latest balance sheet public at each month-end) and month-end closes; the provider's 20-quarter series is the cross-check and the fallback for non-US filers.
- **Technicals:** Wilder RSI(14) and the 50-day SMA are recomputed from two independent daily price histories (stockanalysis and Nasdaq, split-adjusted closes).
- **Substitutions:** no provider 'Profitability Score' or 'Solvency Score' is publicly retrievable, so the Tier-2 profitability criterion uses the Piotroski F-Score (flagged) and financials' Altman-Z substitute is reported as not evaluable. Interest coverage is EBIT over interest expense.
- **Unavailable sources:** Yahoo Finance rate-limited this machine (HTTP 429), Macrotrends and Stockopedia answer with bot challenges, MSN serves its data through a keyed API, and FT Markets forecasts are subscriber-only. None was worked around. No LSE candidate reached the stage where the Stockopedia fallback would have been needed.
- **Known weak spots:** the SEC-derived EBITDA uses a single D&A tag and can understate D&A for companies that tag intangible amortisation separately (it understates HALO's by about $100M); wherever the filing-based and provider figures disagree the gate is marked as a conflict. Thesis-filter and existential-news results are analyst judgments made from headlines and filings, not article text.
- **Not done:** Stage 2 (CAPM, three-scenario DCF, the base-case and worst-case confirmation gates) for either qualifier.

This is a rules-based screen on public data as of one date, not investment advice. Analyst targets and ratings lag sharp price moves, and a screening pass says nothing about whether the Stage 2 valuation gates would hold.
