"""Finviz free public pages (https://finviz.com): quote page + free screener.

Only what finviz serves to an ordinary anonymous visitor is read:

* quote page   https://finviz.com/quote.ashx?t={SYM}&p=d  (redirects to /stock?t=...)
  -> snapshot table, analyst-ratings table, news table, insider-trading table and
     the JSON blobs the page itself embeds for its charts (daily bars, earnings /
     ratings chart events, annual + quarterly EPS / sales, monthly insider totals).
* screener     https://finviz.com/screener.ashx?v={view}&f={filters}&r={1,21,41..}
  -> the server-rendered JSON island each result page carries (20 rows a page).
* help page    https://finviz.com/help/screener.ashx  (finviz's own definitions).

No Elite / export features are touched.  All HTTP goes through
``screener.http.fetch`` (disk cache, back-off) plus a module-level limiter that
keeps finviz to <= 1 network request per second, one at a time.

Conventions
-----------
* A value finviz does not show ("-", empty, locked) or that does not parse
  strictly is ``None`` -- nothing is estimated or defaulted.
* Percentages are returned in percent units (``-18.04`` means -18.04 %) under
  keys ending in ``_pct``.  Money / share counts with K/M/B/T suffixes are
  expanded to absolute numbers (``market_cap`` is USD, not $B).
* Every returned record has ``source`` (provider + exact URL), ``retrieved_at``
  (UTC, from the HTTP cache), ``available`` and ``error``.  A 429 / login wall /
  bot challenge / 404 gives ``available: False`` with the reason in ``error``.
* Class shares: the pipeline passes ``BRK.B``; finviz uses ``BRK-B``.

CLI
---
    python3 -m screener.sources.finviz NVDA LOPE             # all four tables
    python3 -m screener.sources.finviz --only snapshot NVDA
    python3 -m screener.sources.finviz --universe            # RSI<30 & cap>$1B cross-check
    python3 -m screener.sources.finviz --screener cap_smallover,ta_rsi_os30 --view 171
    python3 -m screener.sources.finviz --definitions
"""
from __future__ import annotations

import argparse
import copy
import html as _html
import json
import re
import sys
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from ..http import FetchError, fetch

PROVIDER = "finviz"
BASE = "https://finviz.com/"
QUOTE_URL = BASE + "quote.ashx?t={sym}&p=d"
SCREENER_URL = BASE + "screener.ashx?v={view}&f={filters}{extra}&r={start}"
HELP_URL = BASE + "help/screener.ashx"

ET = ZoneInfo("America/New_York")  # finviz dates / times are US Eastern
MIN_INTERVAL = 1.05                # seconds between network requests to finviz
DEFAULT_MAX_AGE = 20 * 3600        # cache lifetime for quote / screener pages
HELP_MAX_AGE = 7 * 24 * 3600
RATINGS_TABLE_CAP = 20             # the quote page lists at most 20 analyst actions

# The pipeline's work-list (all RSI(14) < 30 on the primary provider, 2026-11-13 close).
WORKLIST = (
    "HLIO NEU STRA SOUN CLMT GNL FFIV IONS GEF NVDA OPEN OTTR LOPE ARWR CORT RSI HALO DT "
    "NRIX FLNG CTVA XIFR TEAM VCYT ANDE BZ STWD URGN PLUG DOX HWC NTCT DGII IOSP AGM AXON"
).split()


# --------------------------------------------------------------------------- #
# symbols
# --------------------------------------------------------------------------- #
def finviz_symbol(symbol: str) -> str:
    """Pipeline symbol -> finviz convention (BRK.B / BRK/B -> BRK-B)."""
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def pipeline_symbol(fv_symbol: str) -> str:
    """Finviz symbol -> pipeline convention (BRK-B -> BRK.B)."""
    return fv_symbol.strip().upper().replace("-", ".")


# --------------------------------------------------------------------------- #
# HTTP (always through screener.http.fetch)
# --------------------------------------------------------------------------- #
_net_lock = threading.Lock()
_last_net = [0.0]
_failures: dict[str, tuple[float, FetchError]] = {}  # url -> (when, error); failures are not on disk
FAILURE_TTL = 900.0                                  # do not re-request a refused URL for 15 minutes


def _get(url: str, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Cached GET.  Network requests are serialised and spaced >= MIN_INTERVAL; a URL that
    was refused (404 / 429 / wall) is not requested again within FAILURE_TTL."""

    def cached():
        if max_age == 0:
            return None
        try:  # retries=0 -> screener.http.fetch only consults its disk cache
            return fetch(url, max_age=max_age, retries=0)
        except FetchError:
            return None

    hit = cached()
    if hit is not None:
        return hit
    with _net_lock:
        hit = cached()  # another thread may have fetched it meanwhile
        if hit is not None:
            return hit
        failed = _failures.get(url)
        if failed and time.monotonic() - failed[0] < FAILURE_TTL:
            raise failed[1]
        wait = _last_net[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return fetch(url, max_age=max_age, retries=3)
        except FetchError as e:
            _failures[url] = (time.monotonic(), e)
            raise
        finally:
            _last_net[0] = time.monotonic()


def _describe(e: FetchError) -> str:
    st = e.status
    if st == 429:
        return "unavailable: finviz rate limit (HTTP 429) -- not retried further"
    if st in (401, 402, 403, 451):
        return f"unavailable: access denied / login or bot challenge (HTTP {st})"
    if st == 404:
        return "unavailable: finviz has no page for this symbol (HTTP 404)"
    if st is None:
        return f"unavailable: network error {e.detail}".strip()
    return f"unavailable: HTTP {st}"


_WALL_RE = re.compile(
    r"just a moment|cf-challenge|captcha|verify you are (a )?human|access denied|"
    r"unusual traffic|enable javascript and cookies", re.I)


def _wall_reason(html: str, what: str) -> str:
    if _WALL_RE.search(html[:30000]):
        return f"unavailable: finviz answered with a challenge / wall page instead of {what}"
    return f"unavailable: {what} not found in the page (layout changed or content withheld)"


# --------------------------------------------------------------------------- #
# strict parsing helpers
# --------------------------------------------------------------------------- #
_MISSING = {"", "-", "--", "---", "—", "–", "n/a", "na", "nan", "none", "null", "n.a."}
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_NUM_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


def _clean(text) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).replace("\xa0", " ").replace("−", "-")).strip()


def is_missing(text) -> bool:
    return _clean(text).lower() in _MISSING


def parse_number(text, *, allow_pct: bool = True, allow_suffix: bool = True):
    """'3924.69B' -> 3.92469e12, '365,800,054' -> 365800054.0, '-0.68%' -> -0.68,
    '(1.2)' -> -1.2, '$170' -> 170.0, '-', 'N/A', '' -> None.  Anything else -> None."""
    s = _clean(text)
    if s.lower() in _MISSING:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = s.replace(",", "").replace("$", "").replace(" ", "")
    if s.endswith("%"):
        if not allow_pct:
            return None
        s = s[:-1]
    mult = 1.0
    if s and s[-1].upper() in _SUFFIX:
        if not allow_suffix:
            return None
        mult, s = _SUFFIX[s[-1].upper()], s[:-1]
    if not _NUM_RE.fullmatch(s):
        return None
    v = float(s) * mult
    return -v if neg else v


def _pct(text):
    """Strict percent: the text must end with '%'."""
    s = _clean(text)
    return parse_number(s) if s.endswith("%") else None


def _plain(text):
    """Strict plain number (no % sign); K/M/B/T suffix allowed."""
    s = _clean(text)
    return None if s.endswith("%") else parse_number(s, allow_pct=False)


def _int(text):
    v = _plain(text)
    return int(round(v)) if v is not None else None


_DATE_FORMATS = ("%b %d, %Y", "%b %d %Y", "%b-%d-%y", "%b %d '%y", "%Y-%m-%d", "%m/%d/%Y")


def _parse_date(text) -> date | None:
    s = _clean(text)
    if s.lower() in _MISSING:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _iso(d) -> str | None:
    return d.isoformat() if d is not None else None


def _utc_to_et(retrieved_at: str | None) -> datetime | None:
    if not retrieved_at:
        return None
    try:
        return datetime.strptime(retrieved_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ET)
    except ValueError:
        return None


def _ts_et(ts) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ts), ET)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _months_back(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    day = d.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def _text(node) -> str:
    return _clean(node.get_text(" ", strip=True)) if node is not None else ""


def _tooltip(node) -> str | None:
    tip = node.get("data-boxover-html") if isinstance(node, Tag) else None
    if not tip:
        return None
    tip = re.sub(r"<br\s*/?>", " ", tip)
    return _clean(_html.unescape(re.sub(r"<[^>]+>", "", tip))) or None


def _own_rows(table) -> list:
    """Rows belonging to this table itself (not to tables nested inside its cells)."""
    rows = []
    for child in table.find_all(["thead", "tbody", "tfoot", "tr"], recursive=False):
        rows.extend([child] if child.name == "tr" else child.find_all("tr", recursive=False))
    return rows


def _json_script(soup, selector):
    el = soup.select_one(selector)
    if el is None:
        return None
    try:
        return json.loads(el.string or el.get_text())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# quote page loader (one page serves snapshot / ratings / news / insider)
# --------------------------------------------------------------------------- #
_page_cache: OrderedDict = OrderedDict()
_page_lock = threading.Lock()


def _quote(symbol: str, max_age: float | None):
    """-> (meta, page).  page is None when unavailable; meta always has source info."""
    fv = finviz_symbol(symbol)
    req = QUOTE_URL.format(sym=fv)
    meta = {
        "symbol": symbol, "finviz_symbol": fv, "provider": PROVIDER,
        "source": f"{PROVIDER} {req}", "url": None, "requested_url": req,
        "retrieved_at": None, "available": False, "error": None,
    }
    try:
        res = _get(req, max_age)
    except FetchError as e:
        meta["error"] = _describe(e)
        return meta, None
    meta.update(url=res["url"], source=f"{PROVIDER} {res['url']}", retrieved_at=res["retrieved_at"])
    key = (fv, res.get("ts"))
    with _page_lock:
        page = _page_cache.get(key)
    if page is None:
        soup = BeautifulSoup(res["text"], "lxml")
        page = {"soup": soup, "html": res["text"], "chart": _chart_data(res["text"], fv)}
        with _page_lock:
            _page_cache[key] = page
            while len(_page_cache) > 6:
                _page_cache.popitem(last=False)
    soup = page["soup"]
    h1 = soup.select_one("h1[data-ticker]") or soup.select_one("h1")
    shown = (h1.get("data-ticker") or _text(h1)).upper() if h1 is not None else None
    if not soup.select("table.snapshot-table2"):
        meta["error"] = _wall_reason(page["html"], "the quote snapshot table")
        return meta, None
    if shown and shown != fv:
        meta["error"] = f"unavailable: finviz served the page of {shown}, not {fv}"
        return meta, None
    meta["available"] = True
    return meta, page


def _chart_data(html: str, fv: str):
    """The daily-chart JSON the quote page embeds (`var data = {...}`)."""
    i = html.find('var data = {"')
    if i < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(html[i + len("var data = "):])
    except ValueError:
        return None
    if not isinstance(d, dict) or str(d.get("ticker", "")).upper() != fv:
        return None
    return d


def _page_today(meta, page) -> date | None:
    """finviz's own 'today' (ET) for the page, else the ET date of retrieval."""
    isl = _json_script(page["soup"], 'script[data-ssr-props^="ssr-island-analyst-ratings"]') if page else None
    d = _parse_date(isl.get("today")) if isinstance(isl, dict) else None
    if d is None:
        et = _utc_to_et(meta.get("retrieved_at"))
        d = et.date() if et else None
    return d


def _as_of(as_of, meta, page) -> date | None:
    if as_of:
        return as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))
    return _page_today(meta, page)


# --------------------------------------------------------------------------- #
# definitions (finviz help page, verbatim)
# --------------------------------------------------------------------------- #
_HELP_HEADINGS = {
    "peg": "PEG",
    "forward_pe": "Forward P/E",
    "pe_ttm": "P/E",
    "eps_ttm": "EPS (ttm)",
    "eps_growth_past_5y_pct": "EPS growth past 5 years",
    "eps_growth_next_5y_pct": "EPS growth next 5 years",
    "sales_growth_past_5y_pct": "Sales growth past 5 years",
    "eps_growth_qoq_pct": "EPS growth qtr over qtr",
    "recom": "Analyst Recommendation",
    "debt_to_equity": "Debt/Equity",
    "lt_debt_to_equity": "Long Term Debt/Equity",
    "current_ratio": "Current Ratio",
    "quick_ratio": "Quick Ratio",
    "roic_pct": "Return on Invested Capital",
    "insider_trans_pct": "Insider Transactions",
    "inst_trans_pct": "Institutional Transactions",
    "short_float_pct": "Short Float",
    "rsi14": "RSI (14)",
    "sma_dist_pct": "Simple Moving Average",
    "volatility_pct": "Volatility",
    "atr14": "ATR",
    "beta": "Beta (5 Years)",
    "avg_volume": "Average Volume",
    "earnings_date": "Earnings Date",
    "perf_pct": "Performance",
}

# How this module reads the three definitions the screen depends on.  The verbatim
# finviz wording is returned next to these notes by definitions() / snapshot().
_NOTES = {
    "peg": ("Forward PEG: finviz help states 'PEG = (Forward P/E) / Annual EPS Growth', measured "
            "'against its projected 3-5 year growth rate', i.e. Forward P/E (next fiscal year) divided "
            "by 'EPS next 5Y' (analysts' long-term annual EPS growth estimate).  It is NOT trailing P/E "
            "over historical growth.  snapshot()['checks'] recomputes it per ticker."),
    "eps_growth_past_3y_pct": ("Snapshot tooltip: 'Annual EPS growth past 3 and 5 years' -- an ANNUALISED "
                               "(compound) rate over the last 3 / 5 completed fiscal years of diluted EPS, "
                               "not a cumulative change.  snapshot()['checks'] recomputes the CAGR from the "
                               "annual EPS series embedded in the same page and reports the basis.  CAUTION: "
                               "when both endpoint years are loss-making finviz still prints a figure -- the "
                               "annualised change in the size of the loss with the sign flipped (positive = "
                               "losses shrinking); with a sign change between the endpoints it shows '-'.  "
                               "Use checks['eps_growth_past_3y']['endpoints_positive'] before treating the "
                               "figure as earnings growth."),
    "recom": ("Analysts' mean recommendation on a 1-5 scale: 1.0 Strong Buy, 2.0 Buy, 3.0 Hold, 4.0 Sell, "
              "5.0 Strong Sell (lower is more bullish)."),
}


def _parse_help(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for h2 in soup.select("h2"):
        parts = []
        for sib in h2.next_siblings:
            if isinstance(sib, Tag):
                if sib.name == "h2" or (sib.name == "span" and "help-small" in (sib.get("class") or [])):
                    break
                parts.append(sib.get_text(" ", strip=True))
            else:
                parts.append(str(sib))
        txt = _clean(" ".join(parts))
        if txt:
            out.setdefault(_text(h2), txt)
    return out


def definitions(max_age: float | None = HELP_MAX_AGE) -> dict:
    """finviz's own wording for the metrics used by the screen (help page, verbatim)."""
    out = {"provider": PROVIDER, "source": f"{PROVIDER} {HELP_URL}", "url": None,
           "retrieved_at": None, "available": False, "error": None, "definitions": {}, "notes": dict(_NOTES)}
    try:
        res = _get(HELP_URL, max_age)
    except FetchError as e:
        out["error"] = _describe(e)
        return out
    out.update(url=res["url"], source=f"{PROVIDER} {res['url']}", retrieved_at=res["retrieved_at"])
    helps = _parse_help(res["text"])
    if not helps:
        out["error"] = _wall_reason(res["text"], "the help definitions")
        return out
    out["available"] = True
    out["definitions"] = {k: {"heading": h, "text": helps.get(h)} for k, h in _HELP_HEADINGS.items()}
    return out


# --------------------------------------------------------------------------- #
# 1. snapshot
# --------------------------------------------------------------------------- #
# label -> (kind, key or (key1, key2), unit)
_SPEC = {
    "Index": ("index", "index", "index membership"),
    "Market Cap": ("num", "market_cap", "USD"),
    "Enterprise Value": ("num", "enterprise_value", "USD"),
    "Income": ("num", "income_ttm", "USD (net income, ttm)"),
    "Sales": ("num", "sales_ttm", "USD (revenue, ttm)"),
    "Book/sh": ("num", "book_per_share", "USD per share (mrq)"),
    "Cash/sh": ("num", "cash_per_share", "USD per share (mrq)"),
    "Dividend Est.": ("div", ("dividend_est", "dividend_est_yield_pct"), "USD per share / %"),
    "Dividend TTM": ("div", ("dividend_ttm", "dividend_ttm_yield_pct"), "USD per share / %"),
    "Dividend Ex-Date": ("date", "dividend_ex_date", "date"),
    "Dividend Gr. 3/5Y": ("pair_pct", ("dividend_growth_3y_pct", "dividend_growth_5y_pct"), "% per year"),
    "Payout": ("pct", "payout_ratio_pct", "%"),
    "Employees": ("int", "employees", "full-time employees"),
    "IPO": ("date", "ipo_date", "date"),
    "P/E": ("num", "pe_ttm", "x (trailing twelve months)"),
    "Forward P/E": ("num", "forward_pe", "x (next fiscal year EPS estimate)"),
    "PEG": ("num", "peg", "x (Forward P/E / EPS next 5Y)"),
    "P/S": ("num", "ps_ttm", "x"),
    "P/B": ("num", "pb_mrq", "x"),
    "P/C": ("num", "pc_mrq", "x"),
    "P/FCF": ("num", "pfcf_ttm", "x"),
    "EV/EBITDA": ("num", "ev_ebitda", "x"),
    "EV/Sales": ("num", "ev_sales", "x"),
    "Quick Ratio": ("num", "quick_ratio", "x (mrq)"),
    "Current Ratio": ("num", "current_ratio", "x (mrq)"),
    "Debt/Eq": ("num", "debt_to_equity", "x (total debt / equity, mrq)"),
    "LT Debt/Eq": ("num", "lt_debt_to_equity", "x (long-term debt / equity, mrq)"),
    "Option/Short": ("optshort", ("optionable", "shortable"), "bool"),
    "EPS (ttm)": ("num", "eps_ttm", "USD per share (diluted, ttm)"),
    "EPS next Q": ("num", "eps_next_q_estimate", "USD per share (analyst estimate)"),
    "EPS this Y": ("pct", "eps_growth_this_y_pct", "% (current fiscal year vs prior)"),
    "EPS next 5Y": ("pct", "eps_growth_next_5y_pct", "% per year (long-term analyst estimate)"),
    "EPS past 3/5Y": ("pair_pct", ("eps_growth_past_3y_pct", "eps_growth_past_5y_pct"), "% per year (annualised)"),
    "Sales past 3/5Y": ("pair_pct", ("sales_growth_past_3y_pct", "sales_growth_past_5y_pct"),
                        "% per year (annualised)"),
    "EPS Y/Y TTM": ("pct", "eps_growth_yoy_ttm_pct", "% (ttm vs prior ttm)"),
    "Sales Y/Y TTM": ("pct", "sales_growth_yoy_ttm_pct", "% (ttm vs prior ttm)"),
    "EPS Q/Q": ("pct", "eps_growth_qoq_pct", "% (latest quarter vs same quarter a year ago)"),
    "Sales Q/Q": ("pct", "sales_growth_qoq_pct", "% (latest quarter vs same quarter a year ago)"),
    "Earnings": ("earnings", ("earnings_date", "earnings_time"), "date + BMO/AMC"),
    "EPS/Sales Surpr.": ("pair_pct", ("eps_surprise_pct", "sales_surprise_pct"), "% (last reported quarter)"),
    "Insider Own": ("pct", "insider_own_pct", "%"),
    "Insider Trans": ("pct", "insider_trans_pct", "% (6-month change in insider ownership)"),
    "Inst Own": ("pct", "inst_own_pct", "%"),
    "Inst Trans": ("pct", "inst_trans_pct", "% (3-month change in institutional ownership)"),
    "ROA": ("pct", "roa_pct", "% (ttm)"),
    "ROE": ("pct", "roe_pct", "% (ttm)"),
    "ROIC": ("pct", "roic_pct", "% (ttm)"),
    "Gross Margin": ("pct", "gross_margin_pct", "% (ttm)"),
    "Oper. Margin": ("pct", "oper_margin_pct", "% (ttm)"),
    "Profit Margin": ("pct", "profit_margin_pct", "% (net, ttm)"),
    "SMA20": ("pct", "sma20_dist_pct", "% distance of price from the 20-day SMA"),
    "SMA50": ("pct", "sma50_dist_pct", "% distance of price from the 50-day SMA"),
    "SMA200": ("pct", "sma200_dist_pct", "% distance of price from the 200-day SMA"),
    "Trades": ("skip", "trades", "Elite-only field (locked)"),
    "Shs Outstand": ("num", "shares_outstanding", "shares"),
    "Shs Float": ("num", "shares_float", "shares"),
    "Short Float": ("pct", "short_float_pct", "% of float"),
    "Short Ratio": ("num", "short_ratio", "days to cover"),
    "Short Interest": ("short_interest", "short_interest", "shares"),
    "52W High": ("val_pct", ("high_52w", "high_52w_dist_pct"), "USD / % distance of price from it"),
    "52W Low": ("val_pct", ("low_52w", "low_52w_dist_pct"), "USD / % distance of price from it"),
    "Volatility": ("pair_pct", ("volatility_week_pct", "volatility_month_pct"),
                   "% (average daily high/low range: week, month)"),
    "ATR (14)": ("num", "atr14", "USD"),
    "RSI (14)": ("num", "rsi14", "0-100"),
    "Beta": ("num", "beta", "x"),
    "Rel Volume": ("num", "rel_volume", "x (volume / 3-month average)"),
    "Avg Volume": ("num", "avg_volume", "shares per day (3-month average)"),
    "Volume": ("int", "volume", "shares (last session)"),
    "Perf Week": ("pct", "perf_week_pct", "%"),
    "Perf Month": ("pct", "perf_month_pct", "%"),
    "Perf Quarter": ("pct", "perf_quarter_pct", "%"),
    "Perf Half Y": ("pct", "perf_half_y_pct", "%"),
    "Perf YTD": ("pct", "perf_ytd_pct", "%"),
    "Perf Year": ("pct", "perf_year_pct", "%"),
    "Perf 3Y": ("pct", "perf_3y_pct", "%"),
    "Perf 5Y": ("pct", "perf_5y_pct", "%"),
    "Perf 10Y": ("pct", "perf_10y_pct", "%"),
    "Recom": ("num", "recom", "1-5 (1 Strong Buy ... 5 Strong Sell)"),
    "Target Price": ("num", "target_price", "USD (analysts' mean)"),
    "Prev Close": ("num", "prev_close", "USD"),
    "Price": ("num", "price", "USD (last close / current)"),
    "Change %": ("pct", "change_pct", "% (last session)"),
    # "EPS next Y" appears twice (estimate and growth) -- handled in _snapshot_pairs
    "EPS next Y [estimate]": ("num", "eps_next_y_estimate", "USD per share (analyst estimate, next fiscal year)"),
    "EPS next Y [growth]": ("pct", "eps_growth_next_y_pct", "% (next fiscal year vs current)"),
}


def _all_keys() -> list[str]:
    keys = []
    for kind, key, _ in _SPEC.values():
        keys.extend(key if isinstance(key, tuple) else [key])
    return keys


def _two_tokens(raw: str):
    toks = raw.split()
    return toks if len(toks) == 2 else None


def _snapshot_pairs(soup):
    """[(label, tooltip, raw_value_text, value_td)] over all snapshot label/value cells."""
    out = []
    for table in soup.select("table.snapshot-table2"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            for i in range(0, len(tds) - 1, 2):
                label = _text(tds[i])
                if label:
                    out.append((label, _tooltip(tds[i]), _text(tds[i + 1]), tds[i + 1]))
    return out


def _resolve_earnings(raw: str, today: date | None, chart) -> dict:
    """'Nov 17 AMC' -> date (year from the page's own earnings chart events, else the
    occurrence of that month/day nearest to `today`) + BMO / AMC."""
    out = {"raw": raw, "date": None, "time": None, "year_source": None, "is_upcoming": None}
    if is_missing(raw):
        return out
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2})(?:,? (\d{4}))?(?: (BMO|AMC))?", raw)
    if not m:
        return out
    mon, day, year, when = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    out["time"] = when
    try:
        month = datetime.strptime(mon, "%b").month
    except ValueError:
        return out
    d = None
    if year:
        try:
            d = date(int(year), month, day)
            out["year_source"] = "stated"
        except ValueError:
            return out
    if d is None and today is not None:
        cands = []
        for ev in (chart or {}).get("chartEvents") or []:
            if ev.get("eventType") == "chartEvent/earnings":
                dt = _ts_et(ev.get("dateTimestamp"))
                if dt and dt.month == month and dt.day == day:
                    cands.append(dt.date())
        if cands:
            d = min(cands, key=lambda c: abs((c - today).days))
            out["year_source"] = "page chart earnings event"
        else:
            near = []
            for y in (today.year - 1, today.year, today.year + 1):
                try:
                    near.append(date(y, month, day))
                except ValueError:
                    pass
            if near:
                d = min(near, key=lambda c: abs((c - today).days))
                out["year_source"] = "nearest occurrence to retrieval date"
    if d is not None:
        out["date"] = d.isoformat()
        if today is not None:
            out["is_upcoming"] = d >= today
    return out


_FA_SERIES = ("eps_diluted", "sales_musd", "shares_outstanding_m")  # order of the page's three mini charts


def _fundamentals_series(soup, fields) -> dict | None:
    """Annual + quarterly EPS / sales / shares series from the page's fundamentals charts.

    The JSON carries no series names: the page draws three charts in a fixed order
    (EPS, Sales, Shares Outstanding).  A series is returned only when, in addition to
    its position, its latest point (TTM / MRQ, else the last fiscal year) agrees with
    the corresponding snapshot value; otherwise it is listed under `unverified`."""
    fa = _json_script(soup, 'script[id^="fa-init-data"]')
    if not isinstance(fa, dict):
        return None
    try:
        ann, qtr = fa["annual"]["values"], fa["quarterly"]["values"]
    except (KeyError, TypeError):
        return None
    if not (isinstance(ann, list) and isinstance(qtr, list) and len(ann) == len(qtr) == len(_FA_SERIES)):
        return None

    def points(series):
        pts = OrderedDict()
        for p in series if isinstance(series, list) else []:
            if isinstance(p, dict) and "name" in p:
                v = p.get("value")
                pts[str(p["name"])] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        return pts

    def latest(pts):
        for name in ("TTM", "MRQ"):
            if pts.get(name) is not None:
                return pts[name]
        vals = [v for v in pts.values() if v is not None]
        return vals[-1] if vals else None

    def close(a, b, rel, abs_):
        return a is not None and b is not None and abs(a - b) <= max(abs_, rel * abs(b))

    px, mc, sh = fields.get("price"), fields.get("market_cap"), fields.get("shares_outstanding")
    refs = {
        "eps_diluted": [(fields.get("eps_ttm"), 0.015, 0.02)],
        "sales_musd": [(fields["sales_ttm"] / 1e6 if fields.get("sales_ttm") else None, 0.005, 6.0)],
        # multi-class companies: the chart counts all classes, 'Shs Outstand' may be one class
        "shares_outstanding_m": [(sh / 1e6 if sh else None, 0.015, 0.02),
                                 (mc / px / 1e6 if mc and px else None, 0.015, 0.02)],
    }
    out = {"units": {"eps_diluted": "USD per share (diluted; fiscal years, TTM last)",
                     "sales_musd": "USD millions", "shares_outstanding_m": "millions of shares"},
           "labelling": "position in the page's fundamentals charts (EPS, Sales, Shares Outstanding), kept only "
                        "when the latest point agrees with the snapshot value",
           "annual": {}, "quarterly": {}, "unverified": []}
    for i, name in enumerate(_FA_SERIES):
        a_pts, q_pts = points(ann[i]), points(qtr[i])
        if any(close(latest(a_pts), ref, rel, abs_) for ref, rel, abs_ in refs[name]):
            out["annual"][name], out["quarterly"][name] = a_pts, q_pts
        else:
            out["unverified"].append(name)
    return out if out["annual"] else None


def _cagr_check(series: dict | None, years: int, shown):
    """Recompute finviz's annualised growth over `years` fiscal years from the page's own
    annual series, and say what kind of number it is.  Observed finviz behaviour:
    both endpoints > 0 -> ordinary CAGR; both < 0 -> the annualised change in the size of
    the loss with the sign flipped (positive = losses shrinking); mixed signs -> '-'."""
    if not series:
        return None
    fy = [(k, v) for k, v in series.items() if re.fullmatch(r"\d{4}", k)]
    if len(fy) <= years:
        return None
    (k0, v0), (k1, v1) = fy[-1 - years], fy[-1]
    res = {"from": {k0: v0}, "to": {k1: v1}, "basis": None, "endpoints_positive": None,
           "recomputed_pct": None, "finviz_pct": shown, "matches": None}
    if v0 is None or v1 is None:
        return res
    res["endpoints_positive"] = v0 > 0 and v1 > 0
    if v0 > 0 and v1 > 0:
        res["basis"] = "both endpoints positive: compound annual growth rate"
        res["recomputed_pct"] = round(((v1 / v0) ** (1.0 / years) - 1) * 100, 2)
    elif v0 < 0 and v1 < 0:
        res["basis"] = ("both endpoints NEGATIVE: finviz prints the annualised change in the size of the loss "
                        "with the sign flipped (positive = losses shrinking) -- not growth of positive earnings")
        res["recomputed_pct"] = round(-((abs(v1) / abs(v0)) ** (1.0 / years) - 1) * 100, 2)
    else:
        res["basis"] = "sign change or zero endpoint: no meaningful growth rate (finviz normally shows '-')"
    if res["recomputed_pct"] is not None and shown is not None:
        res["matches"] = abs(res["recomputed_pct"] - shown) <= max(0.6, 0.02 * abs(shown))
    return res


def _income_statement(soup, hist) -> dict | None:
    """Last two fiscal years of the income-statement breakdown the page embeds
    (revenue, operating income, interest expense, net income, ...)."""
    ib = _json_script(soup, 'script[id^="income-breakdown-init-data"]')
    st = ib.get("statement") if isinstance(ib, dict) else None
    data = st.get("data") if isinstance(st, dict) else None
    periods = data.get("Period") if isinstance(data, dict) else None
    if not isinstance(periods, list) or not periods:
        return None
    items = OrderedDict()
    for label, vals in data.items():
        if label != "Period" and isinstance(vals, list) and len(vals) == len(periods):
            items[label] = [parse_number(v, allow_pct=False, allow_suffix=False) for v in vals]
    out = {"currency": st.get("currency"), "unit": "millions of `currency`", "unit_verified": None,
           "periods": [str(p) for p in periods], "items": items}
    # the unit is not stated in the page: confirm it against the (USD millions) annual sales series
    sales = (hist or {}).get("annual", {}).get("sales_musd") or {}
    m = re.fullmatch(r"(\d{4})FY", str(periods[0]))
    rev = (items.get("Total Revenue") or [None])[0]
    ref = sales.get(m.group(1)) if m else None
    if rev is not None and ref is not None:
        out["unit_verified"] = abs(rev - ref) <= max(0.5, 0.005 * abs(ref))
    return out


def snapshot(symbol: str, *, max_age: float | None = DEFAULT_MAX_AGE, with_definitions: bool = True) -> dict:
    """Whole finviz snapshot table as numbers.

    Returns {symbol, source, retrieved_at, available, error, company, sector, industry,
    country, exchange, price_as_of, fields{...}, raw{label: text}, field_info{key:
    {label, tooltip, unit}}, earnings{...}, earnings_events[...], fundamentals_history,
    checks{...}, definitions{...}, unmapped_labels[], parse_warnings[]}.
    """
    meta, page = _quote(symbol, max_age)
    out = dict(meta)
    out["fields"] = {k: None for k in _all_keys() if k != "trades"}
    out["fields"].update(index_list=None, short_interest_settlement_date=None)
    if page is None:
        return out
    soup, chart = page["soup"], page["chart"]
    today = _page_today(meta, page)
    f = out["fields"]
    raw, info, unmapped, warns = OrderedDict(), {}, [], []

    cats = {}
    for a in soup.select(".quote-header_categories a[href]"):
        m = re.search(r"[?&]f=([a-z]+)_", a["href"])
        if m:
            cats.setdefault(m.group(1), _text(a))
    comp = soup.select_one(".quote-header_ticker-wrapper_company") or soup.select_one("h2")
    out.update(company=_text(comp) or None, sector=cats.get("sec"), industry=cats.get("industry") or cats.get("ind"),
               country=cats.get("geo"), exchange=cats.get("exch"))

    def put(key, value, label, tip, unit):
        f[key] = value
        info[key] = {"label": label, "tooltip": tip, "unit": unit}

    earnings = _resolve_earnings("", today, chart)
    seen_next_y = 0
    for label, tip, text, td in _snapshot_pairs(soup):
        spec_label = label
        missing = is_missing(text)
        if label == "EPS next Y":  # shown twice: the estimate (USD) and its growth (%)
            if tip:
                growth = "growth" in tip.lower()
            elif not missing:
                growth = text.endswith("%")
            else:
                growth = seen_next_y >= 1
            seen_next_y += 1
            spec_label = "EPS next Y [growth]" if growth else "EPS next Y [estimate]"
        if spec_label in raw:  # a label repeated unexpectedly: keep the first, never overwrite
            raw[f"{spec_label} #{len(raw)}"] = text
            unmapped.append(label)
            continue
        raw[spec_label] = text
        spec = _SPEC.get(spec_label)
        if spec is None:
            unmapped.append(label)
            continue
        kind, key, unit = spec
        if kind == "skip":
            continue
        ok = True  # False -> a non-missing cell that did not parse strictly
        if kind == "num":
            v = _plain(text)
            put(key, v, label, tip, unit)
            ok = v is not None
        elif kind == "pct":
            v = _pct(text)
            put(key, v, label, tip, unit)
            ok = v is not None
        elif kind == "int":
            v = _int(text)
            put(key, v, label, tip, unit)
            ok = v is not None
        elif kind == "date":
            v = _iso(_parse_date(text))
            put(key, v, label, tip, unit)
            ok = v is not None
        elif kind == "index":
            put(key, None if missing else text, label, tip, unit)
            f["index_list"] = None if missing else [s.strip() for s in text.split(",") if s.strip()]
        elif kind in ("pair_pct", "val_pct"):
            # two tokens, e.g. '204.08% 95.27%', '- 18.00%', '- -' or '243.37 -33.09%'
            toks = _two_tokens(text)
            first = _pct if kind == "pair_pct" else _plain
            a, b = (first(toks[0]), _pct(toks[1])) if toks else (None, None)
            put(key[0], a, label, tip, unit)
            put(key[1], b, label, tip, unit)
            ok = toks is not None and all(x is not None or is_missing(t) for x, t in zip((a, b), toks))
        elif kind == "div":
            m = re.fullmatch(r"(\S+) ?\( ?(\S+%) ?\)", text)  # '0.78 (0.48%)' / '0.76 ( 11.55% )'
            a, b = (_plain(m.group(1)), _pct(m.group(2))) if m else (None, None)
            put(key[0], a, label, tip, unit)
            put(key[1], b, label, tip, unit)
            ok = a is not None and b is not None
        elif kind == "optshort":
            m = re.fullmatch(r"(Yes|No) / (Yes|No)", text)
            a, b = ((m.group(1) == "Yes", m.group(2) == "Yes") if m else (None, None))
            put(key[0], a, label, tip, unit)
            put(key[1], b, label, tip, unit)
            ok = m is not None
        elif kind == "earnings":
            earnings = _resolve_earnings(text, today, chart)
            put(key[0], earnings["date"], label, tip, unit)
            put(key[1], earnings["time"], label, tip, unit)
            ok = earnings["date"] is not None
        elif kind == "short_interest":
            v = _plain(text)
            put(key, v, label, tip, unit)
            ok = v is not None
            b = td.find(attrs={"data-boxover-html": True})
            m = re.search(r"Settlement Date:\s*(\S+)", _tooltip(b) or "") if b is not None else None
            f["short_interest_settlement_date"] = _iso(_parse_date(m.group(1))) if m else None
        if not ok and not missing:
            warns.append(f"{label}: could not parse {text!r} strictly -> None")

    out["raw"], out["field_info"] = raw, info
    out["unmapped_labels"], out["parse_warnings"] = unmapped, warns
    out["earnings"] = earnings

    # --- what date the price refers to (from the page's own daily chart data) ---
    bar = None
    if chart:
        ld = str(chart.get("lastDate") or "")
        bar = {
            "last_date": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if re.fullmatch(r"\d{8}", ld) else None,
            "last_close": chart.get("lastClose"), "last_open": chart.get("lastOpen"),
            "last_high": chart.get("lastHigh"), "last_low": chart.get("lastLow"),
            "last_volume": chart.get("lastVolume"), "prev_close": chart.get("prevClose"),
            "last_trade_time_et": _iso(_ts_et(chart.get("lastTime"))),
            "after_hours_close": chart.get("afterClose"),
            "after_hours_time_et": _iso(_ts_et(chart.get("afterTime"))) if chart.get("afterClose") else None,
        }
        for k in ("last_close", "last_open", "last_high", "last_low", "prev_close", "after_hours_close"):
            if isinstance(bar[k], (int, float)):
                bar[k] = round(float(bar[k]), 4)
            else:
                bar[k] = None
    out["price_as_of"] = bar
    hdr = soup.select_one(".quote-price")
    out["header_quote"] = _text(hdr) or None

    # --- earnings history (reported + next scheduled) from the page's chart events ---
    events = []
    for ev in (chart or {}).get("chartEvents") or []:
        if ev.get("eventType") != "chartEvent/earnings":
            continue
        dt = _ts_et(ev.get("dateTimestamp"))
        fe = _ts_et(ev.get("fiscalEndDate"))

        def g(k, ev=ev):
            v = ev.get(k)
            return float(v) if isinstance(v, (int, float)) else None
        events.append({
            "datetime_et": _iso(dt), "date": _iso(dt.date()) if dt else None,
            "fiscal_period": ev.get("fiscalPeriod"), "fiscal_end_date": _iso(fe.date()) if fe else None,
            "eps_actual": g("epsActual"), "eps_estimate": g("epsEstimate"),
            "eps_reported_actual": g("epsReportedActual"), "eps_reported_estimate": g("epsReportedEstimate"),
            "sales_actual_musd": g("salesActual"), "sales_estimate_musd": g("salesEstimate"),
            "reported": ev.get("epsActual") is not None or ev.get("salesActual") is not None,
        })
    events.sort(key=lambda e: e["datetime_et"] or "")
    out["earnings_events"] = events
    out["earnings_events_note"] = ("from the quote page's chart events; eps_actual / eps_estimate are the "
                                   "adjusted (street) figures finviz uses for 'EPS/Sales Surpr.', "
                                   "eps_reported_* are GAAP; sales in USD millions")

    # --- fundamentals series + consistency checks ---
    hist = _fundamentals_series(soup, f)
    out["fundamentals_history"] = hist
    out["income_statement_fy"] = _income_statement(soup, hist)
    checks = {}
    fpe, pe, g5, peg = f.get("forward_pe"), f.get("pe_ttm"), f.get("eps_growth_next_5y_pct"), f.get("peg")

    def ratio(n, d):
        return round(n / d, 2) if n is not None and d not in (None, 0) and d > 0 else None

    r_fwd, r_ttm = ratio(fpe, g5), ratio(pe, g5)
    checks["peg"] = {
        "finviz_peg": peg,
        "forward_pe_over_eps_next_5y": r_fwd,
        "trailing_pe_over_eps_next_5y": r_ttm,
        "matches_forward_definition": (abs(r_fwd - peg) <= max(0.02, 0.03 * abs(peg))) if None not in (r_fwd, peg) else None,
        "matches_trailing_definition": (abs(r_ttm - peg) <= max(0.02, 0.03 * abs(peg))) if None not in (r_ttm, peg) else None,
    }
    eps_series = (hist or {}).get("annual", {}).get("eps_diluted")
    sales_series = (hist or {}).get("annual", {}).get("sales_musd")
    checks["eps_growth_past_3y"] = _cagr_check(eps_series, 3, f.get("eps_growth_past_3y_pct"))
    checks["eps_growth_past_5y"] = _cagr_check(eps_series, 5, f.get("eps_growth_past_5y_pct"))
    checks["sales_growth_past_3y"] = _cagr_check(sales_series, 3, f.get("sales_growth_past_3y_pct"))
    checks["sales_growth_past_5y"] = _cagr_check(sales_series, 5, f.get("sales_growth_past_5y_pct"))
    px, sh, mc = f.get("price"), f.get("shares_outstanding"), f.get("market_cap")
    checks["market_cap"] = {
        "finviz_market_cap": mc,
        "price_x_shares_outstanding": round(px * sh, 0) if None not in (px, sh) else None,
        "rel_diff_pct": round((px * sh / mc - 1) * 100, 2) if None not in (px, sh, mc) and mc else None,
    }
    checks["price_equals_chart_last_close"] = (
        abs(px - bar["last_close"]) < 0.006 if bar and None not in (px, bar["last_close"]) else None)
    out["checks"] = checks

    # --- finviz's own definitions for the ambiguous metrics ---
    if with_definitions:
        helps = definitions()
        hd = helps.get("definitions") or {}

        def d(key, help_key=None):
            i = info.get(key) or {}
            h = hd.get(help_key or key) or {}
            return {"label": i.get("label"), "tooltip": i.get("tooltip"),
                    "help_heading": h.get("heading"), "help_text": h.get("text"),
                    "note": _NOTES.get(key)}
        out["definitions"] = {
            "source": helps.get("source"), "retrieved_at": helps.get("retrieved_at"),
            "peg": d("peg"),
            "forward_pe": d("forward_pe"),
            "eps_growth_past_3y_pct": d("eps_growth_past_3y_pct", "eps_growth_past_5y_pct"),
            "eps_growth_past_5y_pct": dict(d("eps_growth_past_5y_pct"), note=_NOTES["eps_growth_past_3y_pct"]),
            "eps_growth_next_5y_pct": d("eps_growth_next_5y_pct"),
            "recom": d("recom"),
            "avg_volume": d("avg_volume"),
            "insider_trans_pct": d("insider_trans_pct"),
            "inst_trans_pct": d("inst_trans_pct"),
            "debt_to_equity": d("debt_to_equity"),
            "roic_pct": d("roic_pct"),
            "sma50_dist_pct": d("sma50_dist_pct", "sma_dist_pct"),
            "rsi14": d("rsi14"),
        }
    return out


# --------------------------------------------------------------------------- #
# 2. analyst ratings
# --------------------------------------------------------------------------- #
_ARROW_RE = re.compile(r"\s*(?:→|&rarr;|->|=>)\s*")


def _split_change(text: str):
    """'Buy -> Hold' -> ('Buy', 'Hold'); 'Buy' -> (None, 'Buy'); '' -> (None, None)."""
    s = _clean(_html.unescape(text or ""))
    if not s:
        return None, None
    parts = [p.strip() for p in _ARROW_RE.split(s)]
    if len(parts) == 2:
        return parts[0] or None, parts[1] or None
    if len(parts) == 1:
        return None, parts[0]
    return None, None


def _rating_row(d: date | None, action, firm, rating, target, origin) -> dict:
    rating = _clean(_html.unescape(rating or ""))
    target = _clean(_html.unescape(target or ""))
    r_from, r_to = _split_change(rating)
    t_from, t_to = _split_change(target)
    return {
        "date": _iso(d), "action": _clean(action) or None, "firm": _clean(_html.unescape(firm or "")) or None,
        "rating_change": rating or None, "rating_from": r_from, "rating_to": r_to,
        "price_target_change": target or None,
        "price_target_from": parse_number(t_from) if t_from else None,
        "price_target_to": parse_number(t_to) if t_to else None,
        "origin": origin,
    }


def _ratings_from_table(soup) -> list[dict] | None:
    for table in soup.find_all("table"):
        trs = _own_rows(table)
        head = [_text(c).lower() for c in trs[0].find_all(["th", "td"], recursive=False)] if trs else []
        if "analyst" not in head or "rating change" not in head:
            continue
        col = {name: i for i, name in enumerate(head)}
        rows = []
        for tr in trs[1:]:
            tds = tr.find_all("td", recursive=False)
            if len(tds) < len(head):
                continue
            firm = copy.copy(tds[col["analyst"]])  # work on a copy: the parsed page is shared
            for junk in firm.select("span.company-ticker"):
                junk.extract()  # logo placeholder letter, not part of the firm name
            rows.append(_rating_row(
                _parse_date(_text(tds[col["date"]])) if "date" in col else None,
                _text(tds[col["action"]]) if "action" in col else None,
                _text(firm),
                _text(tds[col["rating change"]]),
                _text(tds[col["price target change"]]) if "price target change" in col else "",
                "ratings table (HTML)"))
        return rows
    return None


def ratings(symbol: str, *, as_of: str | date | None = None, window_days: int = 90,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Analyst ratings table of the quote page + upgrade/downgrade counts in the last
    `window_days` days up to `as_of` (default: finviz's 'today' for the page).

    The table lists at most the 20 most recent actions; `window_fully_covered` says
    whether that reaches back past the window start.  `chart_events` are the
    upgrade/downgrade days the page embeds for its chart (about two years) and act as
    an independent cross-check / extension of the counts.
    """
    meta, page = _quote(symbol, max_age)
    out = dict(meta)
    out.update(as_of=None, window_days=window_days, window_start=None, rows=[], row_count=0,
               counts_window=None, upgrades_window=None, downgrades_window=None,
               net_upgrades_minus_downgrades=None, window_fully_covered=None, oldest_row_date=None,
               upgrades_window_union=None, downgrades_window_union=None, html_table_agrees_with_json=None,
               chart_events=None, recom=None, target_price=None)
    if page is None:
        return out
    soup = page["soup"]
    today = _as_of(as_of, meta, page)
    start = today - timedelta(days=window_days) if today else None
    out.update(as_of=_iso(today), window_start=_iso(start))

    isl = _json_script(soup, 'script[data-ssr-props^="ssr-island-analyst-ratings"]')
    rows = None
    if isinstance(isl, dict) and isinstance(isl.get("ratings"), list):
        rows = [_rating_row(_parse_date(r.get("date")), r.get("action"), r.get("firm"), r.get("rating"),
                            r.get("priceTarget"), "ratings island (JSON in page)")
                for r in isl["ratings"] if isinstance(r, dict)]
        out["recom"] = isl.get("recom") if isinstance(isl.get("recom"), (int, float)) else None
        out["target_price"] = isl.get("targetPrice") if isinstance(isl.get("targetPrice"), (int, float)) else None
    html_rows = _ratings_from_table(soup)
    if rows is None:
        rows = html_rows
    elif html_rows is not None:
        same = [(r["date"], r["action"], r["firm"]) for r in rows] == \
               [(r["date"], r["action"], r["firm"]) for r in html_rows]
        out["html_table_agrees_with_json"] = same
    if rows is None:
        rows = []
        out["note"] = "no analyst ratings table on the page (finviz lists none for this symbol)"
    out["rows"], out["row_count"] = rows, len(rows)
    dated = [date.fromisoformat(r["date"]) for r in rows if r["date"]]
    out["oldest_row_date"] = _iso(min(dated)) if dated else None

    def count(items):
        c = {}
        for r in items:
            d = date.fromisoformat(r["date"]) if r.get("date") else None
            if d is None or start is None or not (start <= d <= today):
                continue
            c[r["action"] or "?"] = c.get(r["action"] or "?", 0) + 1
        return c

    if start is not None:
        c = count(rows)
        out["counts_window"] = c
        out["upgrades_window"] = c.get("Upgrade", 0)
        out["downgrades_window"] = c.get("Downgrade", 0)
        out["net_upgrades_minus_downgrades"] = out["upgrades_window"] - out["downgrades_window"]
        out["window_fully_covered"] = (len(rows) < RATINGS_TABLE_CAP) or (bool(dated) and min(dated) < start)

    # chart events: days carrying an upgrade/downgrade, ~2 years of history
    chart = page["chart"]
    if chart and isinstance(chart.get("chartEvents"), list):
        ev_rows = []
        for ev in chart["chartEvents"]:
            if ev.get("eventType") != "chartEvent/ratings":
                continue
            dt = _ts_et(ev.get("dateTimestamp"))
            for r in ev.get("ratings") or []:
                ev_rows.append(_rating_row(dt.date() if dt else None, r.get("action"), r.get("analyst"),
                                           r.get("rating"), r.get("targetPrice"), "chart event (JSON in page)"))
        ev_rows.sort(key=lambda r: r["date"] or "", reverse=True)
        ce = {"note": "only days on which finviz charts an upgrade/downgrade marker; other actions on "
                      "those days ride along, initiations/reiterations on other days are absent",
              "rows": ev_rows, "upgrades_window": None, "downgrades_window": None,
              "agrees_with_table": None}
        if start is not None:
            c = count(ev_rows)
            ce["upgrades_window"], ce["downgrades_window"] = c.get("Upgrade", 0), c.get("Downgrade", 0)
            if out["window_fully_covered"]:
                ce["agrees_with_table"] = (ce["upgrades_window"] == out["upgrades_window"]
                                           and ce["downgrades_window"] == out["downgrades_window"])
            # union of both lists (deduplicated) -- the fullest count the page supports
            seen, up, down = set(), 0, 0
            for r in rows + ev_rows:
                d = date.fromisoformat(r["date"]) if r.get("date") else None
                k = (r["date"], r["action"], (r["firm"] or "").lower())
                if d is None or not (start <= d <= today) or k in seen:
                    continue
                seen.add(k)
                up += r["action"] == "Upgrade"
                down += r["action"] == "Downgrade"
            out["upgrades_window_union"], out["downgrades_window_union"] = up, down
        out["chart_events"] = ce
    return out


# --------------------------------------------------------------------------- #
# 3. news
# --------------------------------------------------------------------------- #
_NEWS_TS_RE = re.compile(r"(?:(Today|[A-Z][a-z]{2}-\d{2}-\d{2})\s+)?(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def news(symbol: str, *, limit: int | None = None, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """News table of the quote page (finviz shows up to ~100 rows): datetime (US Eastern),
    headline, source, url, and -- where finviz tags a story with the stock's move -- that
    move (price_reaction_pct; price_reaction_date only when finviz marks the badge with a
    date, i.e. a next-session reaction -- otherwise the session is not stated and stays
    None).  Also finviz's 'why is it moving' note when the page has one."""
    meta, page = _quote(symbol, max_age)
    out = dict(meta)
    out.update(rows=[], row_count=0, newest=None, oldest=None, why_moving=None)
    if page is None:
        return out
    soup = page["soup"]
    today = _page_today(meta, page)
    table = soup.select_one("table#news-table") or soup.select_one("table.news-table")
    rows, cur = [], None
    for tr in (table.find_all("tr") if table is not None else []):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        stamp = _text(tds[0])
        m = _NEWS_TS_RE.fullmatch(stamp)
        tm = None
        if m:
            if m.group(1):
                cur = today if m.group(1).lower() == "today" else _parse_date(m.group(1))
            try:
                tm = datetime.strptime(m.group(2).replace(" ", "").upper(), "%I:%M%p").time()
            except ValueError:
                tm = None
        a = tds[1].select_one("a.tab-link-news") or tds[1].find("a", href=True)
        if a is None:
            continue  # ad / placeholder row (its date stamp, if any, was still consumed above)
        right = tds[1].select_one(".news-link-right")
        spans = right.find_all("span", recursive=False) if right is not None else []
        source = re.sub(r"^\((.*)\)$", r"\1", _text(spans[0])) if spans else ""
        # optional badge: the stock's move finviz attaches to the story (title = the session it refers to)
        badge = next((sp for sp in spans[1:] if "fv-label" in (sp.get("class") or [])), None)
        dt = datetime.combine(cur, tm, ET) if (cur is not None and tm is not None) else None
        rows.append({
            "datetime": _iso(dt), "date": _iso(cur) if m else None,
            "time_et": tm.strftime("%H:%M") if tm else None, "stamp_raw": stamp,
            "headline": _text(a) or None, "source": source or None,
            "url": urljoin(BASE, a.get("href", "")) if a.get("href") else None,
            "price_reaction_pct": _pct(_text(badge)) if badge is not None else None,
            "price_reaction_date": _iso(_parse_date(badge.get("title"))) if badge is not None else None,
        })
    if table is None:
        out["note"] = "no news table on the page"
    out["row_count_total"] = len(rows)
    dts = [r["datetime"] for r in rows if r["datetime"]]
    out["newest"], out["oldest"] = (max(dts), min(dts)) if dts else (None, None)
    out["rows"] = rows[:limit] if limit else rows
    out["row_count"] = len(out["rows"])
    wm = _json_script(soup, 'script[id^="why-stock-moving-init-data"]')
    wm = wm.get("whyMoving") if isinstance(wm, dict) else None
    if isinstance(wm, dict) and wm.get("headline"):
        out["why_moving"] = {"datetime": wm.get("dateTime"), "headline": wm.get("headline"),
                             "summary": wm.get("summary"), "sentiment": wm.get("sentiment"),
                             "kind": wm.get("source")}
    return out


# --------------------------------------------------------------------------- #
# 4. insider trading
# --------------------------------------------------------------------------- #
def _filed_iso(raw: str, traded: date | None) -> str | None:
    """'Sep 23 04:56 PM' (no year) -> first such date on/after the transaction date."""
    m = re.fullmatch(r"([A-Z][a-z]{2}) (\d{1,2}) (\d{1,2}:\d{2} [AP]M)", _clean(raw))
    if not m or traded is None:
        return None
    for y in (traded.year, traded.year + 1):
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {y} {m.group(3)}", "%b %d %Y %I:%M %p")
        except ValueError:
            continue
        if dt.date() >= traded:
            return dt.isoformat(timespec="minutes")
    return None


def insider(symbol: str, *, as_of: str | date | None = None, months: int = 6,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Insider-trading table of the quote page + buy/sell totals for the last `months`
    months up to `as_of`.  'Proposed Sale' rows are Form 144 notices (intent, usually
    followed by the matching 'Sale') and are totalled separately, never added to sales.
    `monthly_aggregates` is the page's own per-calendar-month chart data (Buy / Sale only)."""
    meta, page = _quote(symbol, max_age)
    out = dict(meta)
    out.update(as_of=None, months=months, window_start=None, rows=[], row_count=0, oldest_row_date=None,
               totals_window=None, monthly_aggregates=None, table_matches_monthly_aggregates=None)
    if page is None:
        return out
    soup = page["soup"]
    today = _as_of(as_of, meta, page)
    start = _months_back(today, months) if today else None
    out.update(as_of=_iso(today), window_start=_iso(start))

    rows, found = [], False
    for table in soup.find_all("table"):
        trs = _own_rows(table)
        head = [_text(c).lower() for c in trs[0].find_all(["th", "td"], recursive=False)] if trs else []
        if "insider trading" not in head or "relationship" not in head:
            continue
        found = True
        col = {name: i for i, name in enumerate(head)}

        def cell(tds, name):
            i = col.get(name)
            return tds[i] if i is not None and i < len(tds) else None
        for tr in trs[1:]:
            tds = tr.find_all("td", recursive=False)
            if len(tds) != len(head):
                continue
            d = _parse_date(_text(cell(tds, "date")))
            f4 = cell(tds, "sec form 4")
            link = f4.find("a", href=True) if f4 is not None else None
            who = cell(tds, "insider trading")
            rows.append({
                "date": _iso(d), "insider": _text(who) or None,
                "relationship": _text(cell(tds, "relationship")) or None,
                "transaction": _text(cell(tds, "transaction")) or None,
                "cost": _plain(_text(cell(tds, "cost"))),
                "shares": _int(_text(cell(tds, "#shares"))),
                "value": _plain(_text(cell(tds, "value ($)"))),
                "shares_total": _int(_text(cell(tds, "#shares total"))),
                "sec_form4_filed_raw": _text(f4) or None,
                "sec_form4_filed": _filed_iso(_text(f4), d),
                "sec_form4_url": link["href"] if link is not None else None,
            })
        break
    if not found:
        out["note"] = "no insider trading table on the page (finviz lists no insider transactions)"
    out["rows"], out["row_count"] = rows, len(rows)
    dated = [date.fromisoformat(r["date"]) for r in rows if r["date"]]
    out["oldest_row_date"] = _iso(min(dated)) if dated else None

    if start is not None:
        by, buyers, sellers = {}, set(), set()
        for r in rows:
            d = date.fromisoformat(r["date"]) if r["date"] else None
            if d is None or not (start <= d <= today):
                continue
            t = by.setdefault(r["transaction"] or "?", {"count": 0, "shares": 0, "value": 0.0, "unparsed_rows": 0})
            t["count"] += 1
            if r["shares"] is None or r["value"] is None:
                t["unparsed_rows"] += 1
            t["shares"] += r["shares"] or 0
            t["value"] += r["value"] or 0.0
            if r["transaction"] == "Buy":
                buyers.add((r["insider"] or "").lower())
            elif r["transaction"] == "Sale":
                sellers.add((r["insider"] or "").lower())
        zero = {"count": 0, "shares": 0, "value": 0.0, "unparsed_rows": 0}
        buys, sales = by.get("Buy", dict(zero)), by.get("Sale", dict(zero))
        out["totals_window"] = {
            "buys": buys, "sales": sales,
            "option_exercises": by.get("Option Exercise", dict(zero)),
            "proposed_sales_form144": by.get("Proposed Sale", dict(zero)),
            "by_transaction": by,
            "distinct_buyers": len(buyers), "distinct_sellers": len(sellers),
            "net_buy_minus_sale_value": buys["value"] - sales["value"],
            "net_buy_minus_sale_shares": buys["shares"] - sales["shares"],
            "note": "open-market 'Buy' vs 'Sale' rows dated within the window; values in USD as shown by finviz",
        }

    agg = _json_script(soup, 'script[id^="insider-init-data"]')
    if isinstance(agg, list):
        months_out, ok = [], True
        for b in agg:
            ms = b.get("date") if isinstance(b, dict) else None
            dt = _ts_et(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None
            if dt is None:
                continue
            ym = dt.strftime("%Y-%m")
            entry = {"month": ym, "buy_value": b.get("buyAggregated"), "buy_count": b.get("buyTransactionCount"),
                     "sale_value": b.get("saleAggregated"), "sale_count": b.get("saleTransactionCount")}
            months_out.append(entry)
            # compare with the table for months inside the window
            if start is not None and ym >= start.strftime("%Y-%m") and ym <= today.strftime("%Y-%m"):
                for kind, vk, ck in (("Buy", "buy_value", "buy_count"), ("Sale", "sale_value", "sale_count")):
                    sel = [r for r in rows if r["transaction"] == kind and (r["date"] or "")[:7] == ym]
                    tv = sum(r["value"] or 0.0 for r in sel)
                    av, ac = entry[vk] or 0.0, entry[ck] or 0
                    if len(sel) != ac or abs(tv - av) > max(2.0 * max(len(sel), 1), 0.001 * abs(av)):
                        ok = False
        out["monthly_aggregates"] = months_out
        out["table_matches_monthly_aggregates"] = ok if (months_out and start is not None) else None
        out["monthly_aggregates_note"] = ("page chart data: Buy / Sale value (USD) and transaction count per "
                                          "calendar month; table_matches_monthly_aggregates compares every "
                                          "month touching the window with the table rows (True = the table is "
                                          "complete for the window)")
    return out


# --------------------------------------------------------------------------- #
# extra: daily bars the quote page embeds (independent price / RSI cross-check)
# --------------------------------------------------------------------------- #
def price_history(symbol: str, *, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Daily OHLCV bars embedded in the quote page (about two years, unadjusted as charted)."""
    meta, page = _quote(symbol, max_age)
    out = dict(meta)
    out.update(bars=[], bar_count=0, last_date=None)
    if page is None:
        return out
    c = page["chart"]
    cols = [c.get(k) for k in ("date", "open", "high", "low", "close", "volume")] if c else []
    if not c or any(not isinstance(x, list) for x in cols) or len({len(x) for x in cols}) != 1:
        out["note"] = "no usable chart data in the page"
        return out
    bars = []
    for ts, o, h, l, cl, v in zip(*cols):
        dt = _ts_et(ts)
        bars.append({"date": _iso(dt.date()) if dt else None, "open": o, "high": h, "low": l, "close": cl, "volume": v})
    out.update(bars=bars, bar_count=len(bars), last_date=bars[-1]["date"] if bars else None,
               timeframe=c.get("timeframe"))
    return out


# --------------------------------------------------------------------------- #
# 5. screener
# --------------------------------------------------------------------------- #
_ISLAND_RE = re.compile(r'<script type="application/json" data-ssr-props="[^"]*">(.*?)</script>', re.S)

# finviz screener JSON key -> unit (rows keep finviz's own key names)
SCREENER_UNITS = {
    "marketCap": "USD millions (also returned as market_cap in USD)",
    "rsi": "RSI(14), 0-100",
    "sma20": "% distance of price from 20-day SMA", "sma50": "% distance of price from 50-day SMA",
    "sma200": "% distance of price from 200-day SMA",
    "high52w": "% distance from 52-week high", "low52w": "% distance from 52-week low",
    "averageVolume": "shares per day (3-month average)", "volume": "shares (last session)",
    "price": "USD", "prevClose": "USD", "change": "% (last session)",
    "averageTrueRange": "USD (ATR 14)", "beta": "x",
    "sharesOutstanding2": "millions of shares", "sharesFloat": "millions of shares",
    "insiderOwn": "%", "insiderTrans": "%", "instOwn": "%", "instTrans": "%",
    "shortInterestShare": "% of float", "shortInterestRatio": "days to cover",
}


def _screener_island(html: str):
    for m in _ISLAND_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("data"), dict) and isinstance(d["data"].get("rows"), list):
            return d
    return None


def screener(filters, view: int = 171, *, order: str | None = None, signal: str | None = None,
             max_rows: int = 2000, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Free finviz screener, all result pages (20 rows each, r=1,21,41,...).

    filters: list or comma string of finviz filter codes, e.g. ["cap_smallover", "ta_rsi_os30"].
    view: 111 overview, 121 valuation, 131 ownership, 141 performance, 161 financial, 171 technical.
    Rows are the page's own JSON: text fields + the numeric columns of that view under
    finviz's key names (see SCREENER_UNITS); `market_cap` is added in USD.
    """
    flt = [s.strip() for s in (filters.split(",") if isinstance(filters, str) else filters) if s and s.strip()]
    extra = (f"&o={order}" if order else "") + (f"&s={signal}" if signal else "")
    first_req = SCREENER_URL.format(view=view, filters=",".join(flt), extra=extra, start=1)
    out = {"provider": PROVIDER, "source": f"{PROVIDER} {first_req}", "urls": [], "retrieved_at": None,
           "available": False, "error": None, "filters": flt, "view": view, "total_count": None,
           "page_size": None, "rows": [], "row_count": 0, "complete": False, "units": SCREENER_UNITS}
    rows, seen, start, total = [], set(), 1, None
    while True:
        req = SCREENER_URL.format(view=view, filters=",".join(flt), extra=extra, start=start)
        try:
            res = _get(req, max_age)
        except FetchError as e:
            out["error"] = _describe(e) + f" (page r={start})"
            break
        isl = _screener_island(res["text"])
        if isl is None:
            out["error"] = _wall_reason(res["text"], "the screener result data") + f" (page r={start})"
            break
        data = isl["data"]
        total, size, first_row = data.get("totalCount"), data.get("pageSize"), data.get("firstRow")
        if not isinstance(total, int) or not isinstance(size, int) or size <= 0 or first_row != start - 1:
            out["error"] = f"unavailable: unexpected paging data on page r={start} (firstRow={first_row!r})"
            break
        if isl.get("screenerType") not in (None, "stocks"):
            out["error"] = f"unavailable: unexpected screener type {isl.get('screenerType')!r}"
            break
        src = f"{PROVIDER} {res['url']}"
        out["urls"].append(res["url"])
        out["retrieved_at"] = out["retrieved_at"] or res["retrieved_at"]
        out.update(total_count=total, page_size=size)
        for r in data["rows"]:
            text = r.get("text") if isinstance(r.get("text"), dict) else {}
            nums = r.get("numbers") if isinstance(r.get("numbers"), dict) else {}
            tkr = text.get("ticker")
            if not tkr or tkr in seen:
                continue
            seen.add(tkr)
            row = {"ticker": pipeline_symbol(tkr), "finviz_ticker": tkr,
                   "company": text.get("company"), "sector": text.get("sector"),
                   "industry": text.get("industry"), "country": text.get("country"),
                   "security_type": text.get("securityType")}
            for k, v in nums.items():
                if k != "order":
                    row[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            mc = nums.get("marketCap")
            row["market_cap"] = round(mc * 1e6, 0) if isinstance(mc, (int, float)) else None
            row["source"], row["retrieved_at"] = src, res["retrieved_at"]
            rows.append(row)
        start += size
        if start > total or len(rows) >= max_rows or not data["rows"]:
            break
    out["rows"], out["row_count"] = rows, len(rows)
    out["complete"] = out["error"] is None and total is not None and len(rows) == total
    out["available"] = bool(out["urls"]) and out["error"] is None
    if out["urls"]:
        out["source"] = f"{PROVIDER} {out['urls'][0]}" + (f" (+{len(out['urls']) - 1} more pages, see urls)"
                                                         if len(out["urls"]) > 1 else "")
    return out


def _round(v, nd):
    return round(float(v), nd) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def oversold_universe(*, min_market_cap: float = 1e9, rsi_below: float = 30.0, worklist=None, universe=None,
                      explain_missing: bool = True, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """All finviz stocks with RSI(14) < 30 ('ta_rsi_os30') and market cap > $1B.

    finviz's free cap filters have no $1B break, so 'cap_smallover' (> $300M) is read and
    filtered locally.  The technical view (v=171: RSI, SMA distances, price, market cap) is
    joined with the ownership view (v=131: average volume).  With `worklist`, also reports
    which finviz names are missing from it and which work-list names finviz does not show
    below 30 (with their finviz RSI / market cap from the quote page when explain_missing).
    `universe` (optional iterable of pipeline symbols) tags each extra finviz name with
    `in_universe`, i.e. whether the pipeline's own ticker list contains it at all.
    """
    flt = ["cap_smallover", "ta_rsi_os30"]
    tech = screener(flt, 171, max_age=max_age)
    own = screener(flt, 131, max_age=max_age)
    out = {"provider": PROVIDER, "source": tech["source"], "sources": {"technical_v171": tech["urls"],
                                                                         "ownership_v131": own["urls"]},
           "retrieved_at": tech["retrieved_at"], "available": tech["available"], "error": tech["error"],
           "criteria": f"finviz filters {','.join(flt)}; then market cap > {min_market_cap:,.0f} USD "
                       f"and RSI(14) < {rsi_below:g}",
           "finviz_filter_total": tech["total_count"], "complete": tech["complete"],
           "avg_volume_available": own["available"] and own["complete"],
           "rows": [], "row_count": 0, "excluded_below_min_cap": [], "worklist_crosscheck": None}
    if not tech["available"]:
        return out
    if not own["available"]:
        out["avg_volume_error"] = own["error"]
    avg = {r["finviz_ticker"]: r for r in own["rows"]}
    for r in tech["rows"]:
        o = avg.get(r["finviz_ticker"], {})
        row = {"ticker": r["ticker"], "finviz_ticker": r["finviz_ticker"], "company": r["company"],
               "industry": r["industry"], "country": r["country"], "security_type": r["security_type"],
               # RSI / SMA distances keep finviz's full precision (threshold tests); the price is a
               # float32 in the page JSON (27.84000015...) and is rounded to 4 dp
               "rsi14": r.get("rsi"), "price": _round(r.get("price"), 4), "market_cap": r.get("market_cap"),
               "sma20_dist_pct": r.get("sma20"), "sma50_dist_pct": r.get("sma50"),
               "sma200_dist_pct": r.get("sma200"), "avg_volume": _round(o.get("averageVolume"), 0),
               "volume": r.get("volume"), "change_pct": _round(r.get("change"), 4),
               "source": r["source"], "retrieved_at": r["retrieved_at"]}
        if row["rsi14"] is None or row["market_cap"] is None or not row["rsi14"] < rsi_below:
            continue
        if row["market_cap"] > min_market_cap:
            out["rows"].append(row)
        else:
            out["excluded_below_min_cap"].append(row["ticker"])
    out["rows"].sort(key=lambda r: r["ticker"])
    out["row_count"] = len(out["rows"])

    if worklist:
        wl = [pipeline_symbol(finviz_symbol(s)) for s in worklist]
        fv = {r["ticker"]: r for r in out["rows"]}
        below_cap = set(out["excluded_below_min_cap"])
        extra = [t for t in fv if t not in wl]
        missing = [t for t in wl if t not in fv]
        uni = {pipeline_symbol(finviz_symbol(s)) for s in universe} if universe else None
        cc = {"worklist_size": len(wl), "finviz_oversold_count": len(fv),
              "in_both": [t for t in wl if t in fv],
              "finviz_oversold_not_in_worklist": [
                  dict({k: fv[t][k] for k in ("ticker", "company", "industry", "country", "security_type",
                                              "rsi14", "price", "market_cap", "sma50_dist_pct", "avg_volume")},
                       in_universe=(t in uni) if uni is not None else None)
                  for t in sorted(extra)],
              "worklist_not_oversold_on_finviz": []}
        for t in missing:
            item = {"ticker": t, "reason": None, "finviz_rsi14": None, "finviz_market_cap": None,
                    "finviz_price": None, "source": None, "retrieved_at": None}
            if t in below_cap:
                item["reason"] = "finviz RSI < 30 but finviz market cap <= minimum"
            if explain_missing:
                s = snapshot(t, max_age=max_age, with_definitions=False)
                item.update(source=s["source"], retrieved_at=s["retrieved_at"])
                if s["available"]:
                    item.update(finviz_rsi14=s["fields"]["rsi14"], finviz_market_cap=s["fields"]["market_cap"],
                                finviz_price=s["fields"]["price"])
                    if item["reason"] is None:
                        rsi = s["fields"]["rsi14"]
                        item["reason"] = ("finviz RSI(14) >= 30" if rsi is not None and rsi >= rsi_below
                                          else "not in finviz screener result (see values)")
                else:
                    item["reason"] = item["reason"] or s["error"]
            cc["worklist_not_oversold_on_finviz"].append(item)
        out["worklist_crosscheck"] = cc
    return out


# --------------------------------------------------------------------------- #
# convenience + CLI
# --------------------------------------------------------------------------- #
def all_tables(symbol: str, *, max_age: float | None = DEFAULT_MAX_AGE, news_limit: int | None = None) -> dict:
    """snapshot + ratings + news + insider for one symbol (one HTTP request, cached)."""
    return {"snapshot": snapshot(symbol, max_age=max_age), "ratings": ratings(symbol, max_age=max_age),
            "news": news(symbol, max_age=max_age, limit=news_limit), "insider": insider(symbol, max_age=max_age)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m screener.sources.finviz", description=__doc__.split("\n")[0])
    ap.add_argument("tickers", nargs="*", help="symbols (pipeline convention, e.g. NVDA BRK.B)")
    ap.add_argument("--only", help="comma list of: snapshot,ratings,news,insider,prices")
    ap.add_argument("--news-limit", type=int, default=None, help="keep only the newest N news rows")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD reference date for the ratings / insider windows")
    ap.add_argument("--universe", action="store_true", help="RSI<30 & market cap>$1B list + work-list cross-check")
    ap.add_argument("--screener", metavar="FILTERS", help="raw screener run, e.g. cap_smallover,ta_rsi_os30")
    ap.add_argument("--view", type=int, default=171)
    ap.add_argument("--definitions", action="store_true", help="finviz's own metric definitions")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE, help="cache lifetime in seconds (0 = refetch)")
    ap.add_argument("--compact", action="store_true", help="no indentation")
    a = ap.parse_args(argv)
    result: dict = {}
    if a.definitions:
        result["definitions"] = definitions()
    if a.screener:
        result["screener"] = screener(a.screener, a.view, max_age=a.max_age)
    if a.universe:
        try:  # tag extra names with membership of the pipeline's own ticker list, if it loads
            from ..universe import load_universe
            uni = [s for s in load_universe()["symbol"].tolist() if isinstance(s, str)]
        except Exception:
            uni = None
        result["oversold_universe"] = oversold_universe(worklist=a.tickers or WORKLIST, universe=uni,
                                                        max_age=a.max_age)
    elif a.tickers:
        only = [s.strip() for s in a.only.split(",")] if a.only else ["snapshot", "ratings", "news", "insider"]
        for t in a.tickers:
            rec = {}
            if "snapshot" in only:
                rec["snapshot"] = snapshot(t, max_age=a.max_age)
            if "ratings" in only:
                rec["ratings"] = ratings(t, as_of=a.as_of, max_age=a.max_age)
            if "news" in only:
                rec["news"] = news(t, limit=a.news_limit, max_age=a.max_age)
            if "insider" in only:
                rec["insider"] = insider(t, as_of=a.as_of, max_age=a.max_age)
            if "prices" in only:
                rec["prices"] = price_history(t, max_age=a.max_age)
            result[t] = rec
    if not result:
        ap.print_help()
        return 2
    try:
        json.dump(result, sys.stdout, indent=None if a.compact else 2, ensure_ascii=False, allow_nan=False)
        sys.stdout.write("\n")
    except BrokenPipeError:  # e.g. piped into `head`
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
