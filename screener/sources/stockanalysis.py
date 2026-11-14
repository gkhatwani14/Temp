"""stockanalysis.com adapter (fundamentals supplied to the site by S&P Global
Market Intelligence; individual analyst actions by TipRanks).

Only what the site serves to an anonymous visitor is used:

  page data   https://stockanalysis.com/stocks/{sym}/<page>/__data.json
              (the SvelteKit data endpoint every page loads on navigation; it
              is strict JSON in "devalue" encoding and holds exactly what the
              rendered page shows -- nothing is scraped out of HTML)
  history     https://stockanalysis.com/api/symbol/s/{SYM}/history?range=..&period=..
              https://stockanalysis.com/api/symbol/s/{SYM}/history?type=chart[&series=a]
  screener    https://stockanalysis.com/_api/endpoints/screener/data-points?type=s&ids=a+b
  tooltips    https://stockanalysis.com/_api/endpoints/tooltip/{id}   (metric definitions)

Free-tier limits observed (2026-11-14) and respected here -- nothing behind
the Pro paywall is requested or worked around:
  * financial statements / ratios: annual = 5 fiscal years (+ a TTM column, or
    "Current" for ratios); quarterly and TTM views = 20 periods (5 years).
  * price table: Daily/Weekly up to 5Y, Monthly/Quarterly 10Y, Annual Max.
    The chart series (type=chart) is the full daily close history, close only.
  * forecast table: estimates beyond the next fiscal year / next two quarters
    are the literal string "[PRO]" -> returned as None with pro_only=True.
  * ratings page: only the newest 8 analyst actions are shown.

Conventions
  * every public function returns plain JSON-serialisable dicts with "ok",
    "error", "source" (provider + exact URL) and "retrieved_at";
  * a missing / unparseable / paywalled value is None -- never estimated;
  * values the module derives itself carry "derived" in their key or sit in
    a "derived" block that states the formula;
  * units: statistics() percentages are in percent units (117.21 = 117.21%);
    financials() rows keep the provider's raw numbers, where rows with
    format "percentage" are FRACTIONS (0.74674 = 74.674%); money is in
    absolute currency units (not millions).

CLI:  python3 -m screener.sources.stockanalysis NVDA LOPE [--fn statistics|...]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..http import FetchError, fetch

PROVIDER = "stockanalysis.com"
BASE = "https://stockanalysis.com"

DEFAULT_MAX_AGE = 20 * 3600          # same default as screener.http
DEFINITION_MAX_AGE = 30 * 24 * 3600  # tooltip texts change rarely
MIN_INTERVAL = 0.5                   # <= 2 network requests / second to the host
RETRIES = 3

_LSE_ALIASES = {"LSE", "LON", "L", "UK", "LONDON", "LONDON STOCK EXCHANGE"}
_US_ALIASES = {"", "US", "USA", "NYSE", "NASDAQ", "AMEX", "NYSE ARCA", "NYSE AMERICAN",
               "NYSE MKT", "NYSEARCA", "NYSEAMERICAN", "CBOE", "BATS", "ARCA"}


# =============================================================== HTTP plumbing
_gate = threading.Lock()
_last_net = 0.0


def _get(url: str, *, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """screener.http.fetch, serialised and limited to <= 2 network requests/s.

    All requests of this module go through one lock, so the host never sees
    more than one concurrent connection from it, however many threads call in.
    """
    global _last_net
    with _gate:
        wait = _last_net + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        res = fetch(url, max_age=max_age, retries=RETRIES)
        if time.time() - float(res.get("ts", 0)) < 2.0:      # came off the network, not the cache
            _last_net = time.monotonic()
        return res


def _source(url: str) -> str:
    return f"{PROVIDER} {url}"


_big_memo: dict[str, tuple[float, object, str | None]] = {}


def _get_json_memo(url: str, *, max_age: float | None) -> tuple[object, str | None]:
    """_get + json.loads with an in-process memo (for the large screener payloads
    that statistics() consults once per symbol)."""
    now = time.time()
    hit = _big_memo.get(url)
    if hit and max_age != 0 and (max_age is None or now - hit[0] < max_age):
        return hit[1], hit[2]
    res = _get(url, max_age=max_age)
    obj = json.loads(res["text"])
    _big_memo[url] = (float(res.get("ts", now)), obj, res.get("retrieved_at"))
    return obj, res.get("retrieved_at")


def _unavailable(status: int | None, detail: str = "") -> str:
    if status == 429:
        return "unavailable: HTTP 429 rate limit (not retried further)"
    if status in (401, 402, 403):
        return f"unavailable: HTTP {status} (login / paywall / bot challenge)"
    if status in (400, 404):
        return f"unavailable: HTTP {status} (unknown symbol or page not offered for it)"
    if status is None:
        return f"unavailable: network error {detail[:120]}".strip()
    return f"unavailable: HTTP {status}"


# ------------------------------------------------------------------- devalue
def _unflatten(values: list):
    """Decode SvelteKit's `devalue` flat encoding into plain Python objects.

    Slot 0 is the root; dicts/lists hold slot indices; negative indices are
    JS specials (undefined, hole, NaN, +/-Infinity, -0) -> None (-0 -> 0).
    """
    memo: dict[int, object] = {}

    def hyd(i):
        if not isinstance(i, int) or isinstance(i, bool):
            return None
        if i < 0:
            return 0 if i == -6 else None
        if i in memo:
            return memo[i]
        v = values[i]
        if isinstance(v, dict):
            out: dict = {}
            memo[i] = out
            for k, j in v.items():
                out[k] = hyd(j)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str):                 # tagged value
                tag = v[0]
                if tag == "Date":
                    return v[1] if len(v) > 1 else None
                if tag == "Set":
                    return [hyd(j) for j in v[1:]]
                if tag == "Map":
                    return {str(hyd(v[k])): hyd(v[k + 1]) for k in range(1, len(v) - 1, 2)}
                if tag == "null":
                    return {str(v[k]): hyd(v[k + 1]) for k in range(1, len(v) - 1, 2)}
                if tag == "BigInt":
                    try:
                        return int(v[1])
                    except (ValueError, IndexError, TypeError):
                        return None
                return None
            lst: list = []
            memo[i] = lst
            for j in v:
                lst.append(hyd(j))
            return lst
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    return hyd(0)


def _load(path: str, query: str = "", *, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Load one page-data endpoint. `path` like '/stocks/nvda/statistics/'.

    Returns {ok, error, http_status, url, page_url, retrieved_at, info, page,
    redirected_from}. Follows the site's own symbol redirects (e.g. FB->META).
    """
    out = {"ok": False, "error": None, "http_status": None, "url": None, "page_url": None,
           "retrieved_at": None, "info": None, "page": None, "redirected_from": None}
    hops = 0
    while True:
        url = f"{BASE}{path}__data.json{query}"
        out["url"], out["page_url"] = url, f"{BASE}{path}{query}"
        try:
            res = _get(url, max_age=max_age)
        except FetchError as e:
            out["http_status"] = e.status
            out["error"] = _unavailable(e.status, e.detail or "")
            return out
        out["http_status"], out["retrieved_at"] = res.get("status"), res.get("retrieved_at")
        try:
            doc = json.loads(res["text"])
        except ValueError:
            out["error"] = "unavailable: non-JSON response (consent wall / bot challenge / HTML error page)"
            return out
        if not isinstance(doc, dict):
            out["error"] = "unavailable: unexpected payload shape"
            return out
        loc = doc.get("location")
        if doc.get("type") == "redirect" and isinstance(loc, str) and loc.startswith("/") and hops < 2:
            out["redirected_from"] = out["redirected_from"] or out["page_url"]
            path = loc.split("?")[0]
            if not path.endswith("/"):
                path += "/"
            hops += 1
            continue
        break

    nodes = []
    for n in doc.get("nodes") or []:
        if isinstance(n, dict) and n.get("type") == "data" and isinstance(n.get("data"), list):
            try:
                nodes.append(_unflatten(n["data"]))
            except (IndexError, TypeError, RecursionError):
                nodes.append({"__error__": "undecodable node"})
        elif isinstance(n, dict) and n.get("type") == "error":
            msg = (n.get("error") or {}).get("message") if isinstance(n.get("error"), dict) else None
            nodes.append({"__error__": f"provider {n.get('status')}: {msg}"})
        else:
            nodes.append(None)
    if len(nodes) < 2:
        out["error"] = "unavailable: page data missing"
        return out
    layout, page = (nodes[1], nodes[-1]) if len(nodes) >= 3 else (None, nodes[-1])
    if isinstance(layout, dict) and "__error__" in layout:
        out["error"] = f"symbol not found on provider ({layout['__error__']})"
        return out
    if isinstance(layout, dict) and isinstance(layout.get("info"), dict):
        out["info"] = layout["info"]
    if not isinstance(page, dict):
        out["error"] = "unavailable: page data missing"
        return out
    if "__error__" in page:
        out["error"] = f"page not offered for this symbol ({page['__error__']})"
        return out
    out["page"], out["ok"] = page, True
    return out


# ============================================================ symbol mapping
def provider_symbol(symbol: str, exchange: str | None = None) -> dict:
    """Map a pipeline symbol to the provider's page path and API symbol.

    US   BRK.B / BRK-B / BRK/B -> page /stocks/brk.b/ , api s/BRK.B
    LSE  VOD, 'RR.' -> RR, 'BT.A' -> BT.A ; page /quote/lon/VOD/ , api a/LON-VOD
         (select with exchange='LSE' or a 'LSE:VOD' / 'LON:VOD' prefix)
    """
    s = (symbol or "").strip().upper()
    ex = (exchange or "").strip().upper()
    if ":" in s:
        pre, s = s.split(":", 1)
        ex = ex or pre
    if ex in _LSE_ALIASES:
        s = s.rstrip(".").replace("/", ".")
        return {"symbol": s, "market": "LSE", "page_base": f"/quote/lon/{s}/", "api": f"a/LON-{s}"}
    if ex not in _US_ALIASES:
        raise ValueError(f"unsupported exchange {exchange!r} (US listings and LSE only)")
    s = s.replace("/", ".").replace("-", ".").replace(" ", ".")
    return {"symbol": s, "market": "US", "page_base": f"/stocks/{s.lower()}/", "api": f"s/{s}"}


# ============================================================ value parsing
_NULLS = {"", "n/a", "na", "nan", "-", "--", "---", "—", "–", "none", "null", "[pro]",
          "upgrade", "pro", "n.a.", "undefined"}
_SUFFIX = {"K": 1e3, "k": 1e3, "M": 1e6, "m": 1e6, "B": 1e9, "b": 1e9, "T": 1e12, "t": 1e12}
_NUM_RE = re.compile(r"\d+(?:\.\d*)?|\.\d+")
_GROUPED_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d*)?[%xXKkMmBbTt]?$")
_CURRENCY_SIGNS = "$£€¥"          # $ GBP EUR JPY/CNY


def _split_number(text) -> tuple[float, int, float] | None:
    """-> (value, decimals shown, suffix multiplier) or None."""
    if text is None or isinstance(text, bool):
        return None
    s = str(text).strip()
    if s.lower() in _NULLS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = s.replace("−", "-")                 # unicode minus
    s = "".join(ch for ch in s if not ch.isspace() and ch not in _CURRENCY_SIGNS)
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        neg, s = not neg, s[1:]
    if "," in s:                                 # commas only as proper thousands separators
        if not _GROUPED_RE.match(s):
            return None
        s = s.replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    elif s.endswith(("x", "X")):
        s = s[:-1]
    mult = 1.0
    if s and s[-1] in _SUFFIX:
        mult, s = _SUFFIX[s[-1]], s[:-1]
    if not s or not _NUM_RE.fullmatch(s):
        return None
    decimals = len(s.split(".")[1]) if "." in s else 0
    val = float(s) * mult
    return (-val if neg else val), decimals, mult


def parse_number(x):
    """Robust number parse: thousands separators, %, K/M/B/T suffixes,
    parentheses for negatives, currency signs, 'n/a' / '-' / '--' / '[PRO]'.
    Returns int / float, or None when the text is not a clean number.
    A trailing % is dropped (the caller records the unit)."""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return x
    got = _split_number(x)
    if got is None:
        return None
    val, decimals, mult = got
    if decimals == 0 and mult == 1.0 and abs(val) < 9e15:
        return int(round(val))
    return val


def _num(x):
    """Numeric cell from provider JSON; any non-number ('[PRO]', '', ...) -> None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return x
    return None


def _is_pro(x) -> bool:
    return isinstance(x, str) and x.strip().upper() in ("[PRO]", "PRO", "UPGRADE")


def parse_date(x) -> str | None:
    """'Nov 17, 2026' / 'November 17, 2026' / '2026-11-17' / '8/26/2026' -> ISO date."""
    if not isinstance(x, str):
        return None
    s = x.strip()
    if s.lower() in _NULLS:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _text(x) -> str | None:
    if not isinstance(x, str):
        return None
    s = x.strip()
    return None if s.lower() in _NULLS else s


# ================================================================ definitions
_def_lock = threading.Lock()
_def_memo: dict[str, dict | None] = {}


def definition(metric_id: str, *, max_age: float | None = DEFINITION_MAX_AGE) -> dict | None:
    """The site's own tooltip for a metric id: {title, text, formula, source}.
    None when the site has no tooltip for it or the endpoint is unavailable."""
    key = (metric_id or "").strip().lower()
    if not key:
        return None
    with _def_lock:
        if key in _def_memo:
            return _def_memo[key]
    url = f"{BASE}/_api/endpoints/tooltip/{key}"
    out = None
    try:
        res = _get(url, max_age=max_age)
        d = json.loads(res["text"]).get("data")
        if isinstance(d, dict) and (d.get("text") or d.get("formula")):
            out = {"title": d.get("title"), "text": d.get("text"), "formula": d.get("formula"),
                   "source": _source(url), "retrieved_at": res.get("retrieved_at")}
    except (FetchError, ValueError, AttributeError):
        out = None
    with _def_lock:
        _def_memo[key] = out
    return out


# ================================================================= statistics
# provider item id -> canonical field name
_CANON = {
    "marketcap": "market_cap", "enterpriseValue": "enterprise_value",
    "pe": "pe_ttm", "peForward": "forward_pe", "pegRatio": "peg_ratio",
    "ps": "ps_ttm", "pb": "pb", "pfcf": "p_fcf", "pocf": "p_ocf",
    "evEbitda": "ev_ebitda", "evEbit": "ev_ebit", "evFcf": "ev_fcf", "evSales": "ev_sales",
    "evEarnings": "ev_earnings",
    "debtEquity": "debt_equity", "debtEbitda": "debt_ebitda", "debtFcf": "debt_fcf",
    "interestCoverage": "interest_coverage", "currentRatio": "current_ratio", "quickRatio": "quick_ratio",
    "roic": "roic_pct", "roe": "roe_pct", "roa": "roa_pct", "roce": "roce_pct",
    "revenue": "revenue_ttm", "gp": "gross_profit_ttm", "opinc": "operating_income_ttm",
    "pretax": "pretax_income_ttm", "netinc": "net_income_ttm", "ebitda": "ebitda_ttm",
    "ebit": "ebit_ttm", "eps": "eps_ttm",
    "ncfo": "operating_cash_flow_ttm", "capex": "capex_ttm", "fcf": "free_cash_flow_ttm",
    "fcfps": "fcf_per_share_ttm", "depreciationAmortization": "d_and_a_ttm",
    "totalcash": "total_cash", "cashandinvestments": "cash_and_investments",
    "debt": "total_debt", "netcash": "net_cash", "netcashpershare": "net_cash_per_share",
    "equity": "book_equity", "bvps": "book_value_per_share", "workingcapital": "working_capital",
    "zScore": "altman_z_score", "fScore": "piotroski_f_score",
    "analystRatings": "analyst_consensus", "priceTarget": "price_target",
    "priceTargetChange": "price_target_upside_pct", "analystCount": "analyst_count",
    "eps3y": "eps_growth_forecast_3y_pct", "revenue3y": "revenue_growth_forecast_3y_pct",
    "rsi": "rsi_14", "sma50": "sma_50", "sma200": "sma_200", "averageVolume": "avg_volume_20d",
    "beta": "beta_5y", "ch1y": "price_change_52w_pct",
    "sharesout": "shares_outstanding", "sharesOutClass": "shares_outstanding_class",
    "float": "float_shares", "sharesInstitutions": "institutional_ownership_pct",
    "sharesInsiders": "insider_ownership_pct", "sharesgrowthyoy": "shares_change_yoy_pct",
    "sharesgrowthqoq": "shares_change_qoq_pct",
    "earningsdate": "earnings_date", "exdivdate": "ex_dividend_date",
    "grossMargin": "gross_margin_pct", "operatingMargin": "operating_margin_pct",
    "profitMargin": "profit_margin_pct", "ebitdaMargin": "ebitda_margin_pct",
    "fcfMargin": "fcf_margin_pct", "fcfYield": "fcf_yield_pct", "earningsYield": "earnings_yield_pct",
    "dividendYield": "dividend_yield_pct", "payoutRatio": "payout_ratio_pct",
    "shortFloat": "short_pct_float", "shortShares": "short_pct_shares_out", "shortRatio": "short_ratio_days",
    "lastSplitDate": "last_split_date", "lastSplitType": "last_split_type", "splitRatio": "last_split_ratio",
}
_DATE_IDS = {"earningsdate", "exdivdate", "lastSplitDate"}
_TEXT_IDS = {"analystRatings", "lastSplitType", "splitRatio"}
# words the provider's title must contain for the canonical mapping to be trusted
_TITLE_GUARD = {
    "marketcap": "market cap", "enterpriseValue": "enterprise value", "pe": "pe ratio",
    "peForward": "forward pe", "pegRatio": "peg", "evEbitda": "ebitda", "evFcf": "fcf",
    "debtEquity": "equity", "debtEbitda": "ebitda", "debtFcf": "fcf", "interestCoverage": "interest coverage",
    "currentRatio": "current ratio", "roic": "invested capital", "revenue": "revenue", "ebitda": "ebitda",
    "ebit": "ebit", "ncfo": "operating cash flow", "capex": "capital expenditures", "fcf": "free cash flow",
    "totalcash": "cash", "debt": "debt", "netcash": "net cash", "zScore": "altman", "fScore": "piotroski",
    "analystRatings": "consensus", "priceTarget": "price target", "priceTargetChange": "price target",
    "analystCount": "analyst count", "rsi": "rsi", "sma50": "50-day", "sma200": "200-day",
    "averageVolume": "average volume", "sharesout": "shares outstanding", "sharesInstitutions": "institutions",
    "earningsdate": "earnings date", "eps3y": "eps growth forecast",
}
# metrics whose meaning is ambiguous enough that the site's own definition is attached
_DEFINE = ["pegRatio", "peForward", "pe", "evEbitda", "evFcf", "debtEquity", "debtEbitda", "debtFcf",
           "interestCoverage", "roic", "totalcash", "debt", "netcash", "fcf", "ebitda", "ebit",
           "eps3y", "revenue3y", "averageVolume", "sharesout", "rsi", "zScore", "fScore",
           "priceTarget", "analystRatings", "analystCount", "sharesInstitutions", "earningsdate"]
_EARN_RE = re.compile(
    r"The\s+(next confirmed|next estimated|next|last)\s+earnings date\s+(?:is|was)\s+"
    r"(?:[A-Za-z]+,\s+)?([A-Za-z]+ \d{1,2}, \d{4})(?:,\s*([^.]+))?\.", re.I)
# screener ids attached to statistics() so that both earnings dates and the
# historical EPS growth figures are present in one record (US listings only)
_STAT_SCREENER_IDS = ["lastEarningsDate", "nextEarningsDate", "earningsDate", "earningsTime",
                      "lastReportDate", "epsGrowth", "epsGrowthQ", "epsGrowth3Y", "epsGrowth5Y",
                      "eps3y", "rsi", "marketCap", "zScore", "fcf"]


def _stat_item(sec: str, item: dict, warnings: list[str]) -> dict:
    iid = item.get("id")
    raw_v, raw_h = item.get("value"), item.get("hover")
    rec = {"id": iid, "title": item.get("title"), "section": sec, "raw": raw_v, "hover": raw_h,
           "value": None, "unit": None}
    if item.get("proOnly"):
        rec["pro_only"] = True
        return rec
    if iid in _DATE_IDS:
        rec["value"], rec["unit"] = parse_date(raw_h) or parse_date(raw_v), "date"
        return rec
    if iid in _TEXT_IDS:
        rec["value"], rec["unit"] = _text(raw_v) or _text(raw_h), "text"
        return rec
    h, v = _split_number(raw_h), _split_number(raw_v)
    if h is not None and v is not None:
        tol = max(10.0 ** -h[1] * h[2], 10.0 ** -v[1] * v[2]) * 1.000001
        if abs(h[0] - v[0]) > tol:
            warnings.append(f"{iid}: displayed value {raw_v!r} and hover {raw_h!r} disagree -> None")
            return rec
        # the more precise of the two renderings
        best = raw_h if (10.0 ** -h[1] * h[2]) <= (10.0 ** -v[1] * v[2]) else raw_v
        rec["value"] = parse_number(best)
    elif h is not None:
        rec["value"] = parse_number(raw_h)
    elif v is not None:
        rec["value"] = parse_number(raw_v)
    joined = f"{raw_v or ''}{raw_h or ''}"
    rec["unit"] = "pct" if "%" in joined else "number"
    return rec


def _quote(info: dict | None) -> dict:
    q = (info or {}).get("quote") if isinstance(info, dict) else None
    q = q if isinstance(q, dict) else {}
    return {"price": _num(q.get("p")), "price_date": parse_date(q.get("td")),
            "prev_close": _num(q.get("cl")), "change": _num(q.get("c")), "change_pct": _num(q.get("cp")),
            "open": _num(q.get("o")), "high": _num(q.get("h")), "low": _num(q.get("l")),
            "volume": _num(q.get("v")), "high_52w": _num(q.get("h52")), "low_52w": _num(q.get("l52")),
            "updated": _text(q.get("u")), "market_status": _text(q.get("ms")),
            "listing_exchange": _text(q.get("ex"))}


def _meta(info: dict | None) -> dict:
    info = info if isinstance(info, dict) else {}
    curr = info.get("curr") if isinstance(info.get("curr"), dict) else {}
    return {"provider_ticker": info.get("ticker") or info.get("symbol"),
            "name": info.get("nameFull") or info.get("name"),
            "exchange": info.get("exchange"), "state": info.get("state"),
            "archived": info.get("archived"), "ipo_date": parse_date(info.get("ipoDate")),
            "cik": info.get("cik"),
            "currency": {"main": curr.get("main"), "price": curr.get("price"),
                         "financial": curr.get("financial"), "dividend": curr.get("dividend")}}


def statistics(symbol: str, exchange: str | None = None, *, definitions: bool = True,
               with_screener: bool = True, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """https://stockanalysis.com/stocks/{sym}/statistics/ as a record.

    fields      canonical flat dict (None when the site shows n/a or nothing).
                Percent fields end in _pct and are in percent units.
                Money is in info.currency.main (ADRs: converted to USD by the
                site, whereas financials() stays in the reporting currency).
    labels      canonical name -> the provider's own row title
    items       every row of the page by provider id (raw text, hover, parsed)
    earnings    the single "Earnings Date" of the page classified by its text
                as next_confirmed / next_estimated / last
    screener    (US only) last AND next earnings date + historical EPS growth
                from the public screener endpoint, with its own source
    definitions the site's tooltip text for ambiguous metrics (PEG etc.)
    """
    ps = provider_symbol(symbol, exchange)
    got = _load(ps["page_base"] + "statistics/", max_age=max_age)
    fields = {c: None for c in dict.fromkeys(_CANON.values())}
    fields.update({"last_earnings_date": None, "last_earnings_date_basis": None,
                   "next_earnings_date": None, "next_earnings_date_basis": None,
                   "earnings_date_kind": None, "earnings_time": None, "total_cash_basis": None})
    rec = {"symbol": symbol, "provider_symbol": ps["symbol"], "market": ps["market"],
           "ok": got["ok"], "error": got["error"], "source": _source(got["url"]),
           "page_url": BASE + ps["page_base"] + "statistics/", "retrieved_at": got["retrieved_at"],
           "redirected_from": got["redirected_from"], "data_provider": None,
           **_meta(got["info"]), "quote": _quote(got["info"]),
           "fields": fields, "labels": {}, "earnings": None, "screener": None,
           "derived": {}, "items": {}, "texts": {}, "definitions": {}, "warnings": []}
    if not got["ok"]:
        return rec
    page, warnings = got["page"], rec["warnings"]
    if got["redirected_from"]:
        warnings.append(f"provider redirected {got['redirected_from']} -> {got['page_url']}: "
                        f"data is for {rec.get('provider_ticker')}")
    trust = page.get("trust") if isinstance(page.get("trust"), dict) else {}
    srcs = [s.get("name") for s in (trust.get("sources") or []) if isinstance(s, dict)]
    rec["data_provider"] = ", ".join(x for x in srcs if x) or None

    for sec, block in page.items():
        if not (isinstance(block, dict) and isinstance(block.get("data"), list)):
            continue
        rec["texts"][sec] = block.get("text")
        for item in block["data"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            it = _stat_item(sec, item, warnings)
            rec["items"][it["id"]] = it
            canon = _CANON.get(it["id"])
            if not canon:
                continue
            guard = _TITLE_GUARD.get(it["id"])
            if guard and guard not in str(it["title"] or "").lower():
                warnings.append(f"{it['id']}: unexpected title {it['title']!r} -> {canon} left None")
                continue
            fields[canon] = it["value"]
            rec["labels"][canon] = it["title"]

    # The balance-sheet block shows EITHER "Cash & Cash Equivalents" (id totalcash = cash +
    # short-term investments) OR "Cash & Investments" (id cashandinvestments; banks and
    # companies holding long-term investments).  Net Cash is computed by the site from
    # whichever one is shown, so total_cash follows it and total_cash_basis names it.
    if fields["total_cash"] is not None:
        fields["total_cash_basis"] = rec["labels"].get("total_cash")
    elif fields["cash_and_investments"] is not None:
        fields["total_cash"] = fields["cash_and_investments"]
        fields["total_cash_basis"] = rec["labels"].get("cash_and_investments")
        rec["labels"]["total_cash"] = rec["labels"].get("cash_and_investments")

    # the page shows ONE earnings date; its sentence says whether it is the next or the last
    dates_text = rec["texts"].get("dates") or ""
    m = _EARN_RE.search(dates_text)
    if m:
        kind = m.group(1).lower().replace(" ", "_")
        kind = "next_unspecified" if kind == "next" else kind
        d = parse_date(m.group(2))
        rec["earnings"] = {"date": d, "kind": kind, "timing": (m.group(3) or "").strip() or None,
                           "text": dates_text}
        if d and fields["earnings_date"] and d != fields["earnings_date"]:
            warnings.append("earnings date in sentence differs from table value")
        fields["earnings_date_kind"] = kind
        if kind == "last":
            fields["last_earnings_date"] = d
            fields["last_earnings_date_basis"] = "statistics page (last reported)"
        else:
            fields["next_earnings_date"] = d
            fields["next_earnings_date_basis"] = f"statistics page ({kind.replace('_', ' ')})"
    elif fields["earnings_date"]:
        warnings.append("earnings date present but its kind (next/last) could not be read")

    if with_screener and ps["market"] == "US":
        b = bulk(_STAT_SCREENER_IDS, symbols=[ps["symbol"]], max_age=max_age)
        row = (b.get("data") or {}).get(ps["symbol"]) if b.get("ok") else None
        rec["screener"] = {"ok": bool(row), "error": b.get("error") if not row else None,
                           "source": b.get("source"), "retrieved_at": b.get("retrieved_at"),
                           "note": "percent units; epsGrowth = TTM YoY, epsGrowthQ = latest quarter YoY, "
                                   "epsGrowth3Y/5Y = historical CAGR, eps3y = analyst forecast CAGR next 3Y",
                           "values": row}
        if not row and b.get("ok"):
            rec["screener"]["error"] = "symbol absent from screener universe"
        if row:
            for src_key, dst in (("lastEarningsDate", "last_earnings_date"),
                                 ("nextEarningsDate", "next_earnings_date")):
                d = parse_date(row.get(src_key))
                if fields[dst] is None:
                    fields[dst] = d
                    if d:
                        fields[dst + "_basis"] = (f"screener {src_key}"
                                                  + (" (not stated as confirmed)" if "next" in dst else ""))
                elif d and d != fields[dst]:
                    warnings.append(f"{dst}: statistics page {fields[dst]} vs screener {d}")
            fields["earnings_time"] = _text(row.get("earningsTime"))

    fpe, peg = fields.get("forward_pe"), fields.get("peg_ratio")
    if isinstance(fpe, (int, float)) and isinstance(peg, (int, float)) and peg > 0 and fpe > 0:
        rec["derived"]["peg_implied_lt_eps_growth_pct"] = {
            "value": round(fpe / peg, 3),
            "formula": "forward_pe / peg_ratio (the site does not publish the 5Y growth figure itself)"}

    if definitions:
        for iid in _DEFINE:
            canon = _CANON.get(iid)
            if canon and iid in rec["items"]:
                rec["definitions"][canon] = definition(iid)
    return rec


# ================================================================= financials
_STATEMENTS = {
    "income": "income-statement", "income-statement": "income-statement", "is": "income-statement",
    "balance": "balance-sheet", "balance-sheet": "balance-sheet", "bs": "balance-sheet",
    "cash-flow": "cash-flow-statement", "cash-flow-statement": "cash-flow-statement",
    "cashflow": "cash-flow-statement", "cf": "cash-flow-statement",
    "ratios": "ratios",
}
_PERIODS = {"annual": ("", "annual"), "quarterly": ("?p=quarterly", "quarterly"),
            "ttm": ("?p=trailing", "ttm"), "trailing": ("?p=trailing", "ttm")}
_UNIT_BY_FORMAT = {"percentage": "fraction", "ratio": "ratio", "pershare": "currency_per_share",
                   "divpershare": "currency_per_share", "reduce_precision": "absolute"}
_KEY_ROWS = {  # rows the pipeline needs, for a quick presence report
    "income-statement": ["revenue", "gp", "opinc", "ebitda", "ebit", "interestExpense", "netinc", "epsdil",
                         "sharesDiluted", "fcf", "grossMargin", "operatingMargin"],
    "balance-sheet": ["cashneq", "totalcash", "debt", "assets", "liabilities", "retearn", "workingcapital",
                      "sharesOutTotalCommon", "equity", "assetsc", "currentLiabilities"],
    "cash-flow-statement": ["ncfo", "capex", "fcf", "totalDepAmorCF"],
    "ratios": ["marketcap", "ev", "evebitda", "pe", "peForward", "pegRatio", "debtebitda", "currentratio"],
}


def financials(symbol: str, statement: str = "income", period: str = "annual",
               exchange: str | None = None, *, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """One financial statement table exactly as the free site serves it.

    statement  income | balance-sheet | cash-flow-statement | ratios
    period     annual | quarterly | ttm
    rows       {row_id: {title, format, unit, values: {column key: number|None}}}
               column keys are period-end dates (ISO); the extra first column
               of annual tables (and of every ratios table) is keyed "TTM".
    columns    metadata per column key, newest first, as on the site.
               "TTM" column: statements = trailing twelve months ended
               ttm_period_end (balance sheet = latest quarter-end balance);
               ratios = the site's "Current" column: price of price_as_of
               over TTM financials.
    units      format 'percentage' rows are FRACTIONS; money is absolute, in
               `currency` (reporting currency -- CNY for BZ, not USD).
    prior      the one older period the site ships to compute YoY growth of
               the oldest displayed column (fiscal year label only, no date).
    """
    slug = _STATEMENTS.get((statement or "").strip().lower())
    per = _PERIODS.get((period or "").strip().lower())
    ps = provider_symbol(symbol, exchange)
    rec = {"symbol": symbol, "provider_symbol": ps["symbol"], "market": ps["market"],
           "statement": slug, "period": per[1] if per else None, "ok": False, "error": None,
           "source": None, "page_url": None, "retrieved_at": None, "currency": None,
           "price_currency": None, "fiscal_year": None, "reporting_frequency": None, "data_source": None,
           "ttm_period_end": None, "price_as_of": None, "columns": [], "n_periods": 0,
           "has_ttm_column": False, "provider_full_count": None, "first_period_end": None,
           "last_period_end": None, "rows": {}, "key_rows_missing": [], "prior": None, "ttm_prior": None,
           "warnings": []}
    if not slug or not per:
        rec["error"] = f"bad arguments: statement={statement!r} period={period!r}"
        return rec
    got = _load(f"{ps['page_base']}financials/{slug}/", per[0], max_age=max_age)
    rec.update({"ok": got["ok"], "error": got["error"], "source": _source(got["url"]),
                "page_url": got["page_url"], "retrieved_at": got["retrieved_at"],
                "redirected_from": got["redirected_from"]})
    if not got["ok"]:
        return rec
    page, warnings = got["page"], rec["warnings"]
    if got["redirected_from"]:
        warnings.append(f"provider redirected {got['redirected_from']} -> {got['page_url']}")
    fd = page.get("financialData")
    if page.get("statement") != slug or page.get("period") != per[1] or not isinstance(fd, dict):
        rec.update({"ok": False, "error": f"unexpected payload: statement={page.get('statement')!r} "
                                          f"period={page.get('period')!r}"})
        return rec
    keys = fd.get("datekey")
    if not isinstance(keys, list) or not keys:
        rec.update({"ok": False, "error": "no period columns in payload"})
        return rec

    details = page.get("details") if isinstance(page.get("details"), dict) else {}
    curr = (got["info"] or {}).get("curr") if isinstance(got["info"], dict) else {}
    curr = curr if isinstance(curr, dict) else {}
    rec["currency"] = details.get("currency") or curr.get("financial")
    rec["price_currency"] = curr.get("price")
    rec["fiscal_year"] = details.get("fiscalYear")
    rec["reporting_frequency"] = details.get("reportingFrequency") or "quarterly"
    rec["data_source"] = page.get("source")
    rec["ttm_period_end"] = parse_date(details.get("lastTrailingDate"))
    rec["price_as_of"] = parse_date(details.get("lastTradingDay"))
    rec["provider_full_count"] = _num(page.get("full_count"))

    n = len(keys)
    fy = fd.get("fiscalYear") if isinstance(fd.get("fiscalYear"), list) else [None] * n
    fq = fd.get("fiscalQuarter") if isinstance(fd.get("fiscalQuarter"), list) else [None] * n
    col_keys: list[str | None] = []
    for i, k in enumerate(keys):
        is_ttm = isinstance(k, str) and k.strip().upper() == "TTM"
        iso = None if is_ttm else parse_date(k)
        if not is_ttm and iso is None:
            warnings.append(f"column {i}: unreadable period key {k!r} dropped")
            col_keys.append(None)
            continue
        if is_ttm:
            col = {"key": "TTM", "is_ttm_column": True,
                   "label": "Current" if slug == "ratios" else "TTM",
                   "period_end": rec["price_as_of"] if slug == "ratios" else rec["ttm_period_end"],
                   "financials_through": rec["ttm_period_end"]}
            rec["has_ttm_column"] = True
        else:
            basis = {"annual": "FY", "quarterly": "Q", "ttm": "TTM"}[per[1]]
            col = {"key": iso, "is_ttm_column": False, "label": basis, "period_end": iso}
        col["fiscal_year"] = fy[i] if i < len(fy) else None
        col["fiscal_quarter"] = fq[i] if i < len(fq) else None
        rec["columns"].append(col)
        col_keys.append(col["key"])
    dated = [c["key"] for c in rec["columns"] if not c["is_ttm_column"]]
    if len(set(dated)) != len(dated):
        rec.update({"ok": False, "error": "duplicate period columns in payload"})
        return rec
    rec["n_periods"] = len(dated)
    rec["first_period_end"], rec["last_period_end"] = (min(dated), max(dated)) if dated else (None, None)

    titles = {m.get("id"): m for m in (page.get("map") or []) if isinstance(m, dict)}
    order = [m.get("id") for m in (page.get("map") or []) if isinstance(m, dict)]
    pro_cells = 0
    for rid in order + [k for k in fd if k not in titles]:
        if rid in ("datekey", "fiscalYear", "fiscalQuarter") or rid not in fd or rid in rec["rows"]:
            continue
        vals = fd[rid]
        if not isinstance(vals, list) or len(vals) != n:
            warnings.append(f"row {rid}: length mismatch with period columns -> dropped")
            continue
        meta = titles.get(rid, {})
        fmt = meta.get("format")
        cells = {}
        for k, v in zip(col_keys, vals):
            if k is None:
                continue
            pro_cells += _is_pro(v)
            cells[k] = _num(v)
        rec["rows"][rid] = {"title": meta.get("title"), "format": fmt,
                            "unit": _UNIT_BY_FORMAT.get(fmt, "absolute"), "values": cells}
    if pro_cells:
        warnings.append(f"{pro_cells} paywalled cells returned as None")
    rec["key_rows_missing"] = [r for r in _KEY_ROWS.get(slug, []) if r not in rec["rows"]]

    prior = page.get("prior")
    if isinstance(prior, dict) and prior:
        pv = {k: _num(v[0]) for k, v in prior.items()
              if isinstance(v, list) and len(v) == 1 and k not in ("fiscalYear", "fiscalQuarter")}
        lab = lambda k: (prior.get(k) or [None])[0] if isinstance(prior.get(k), list) else None  # noqa: E731
        rec["prior"] = {"fiscal_year": lab("fiscalYear"), "fiscal_quarter": lab("fiscalQuarter"),
                        "note": "period before the oldest displayed column; shipped by the site for "
                                "YoY growth; no period-end date is given",
                        "values": pv}
    tp = page.get("ttmPrior")
    if isinstance(tp, dict) and tp:
        rec["ttm_prior"] = {"note": "the TTM/Current column one year earlier (site uses it for TTM growth)",
                            "values": {k: (v if isinstance(v, str) else _num(v)) for k, v in tp.items()
                                       if k in rec["rows"] or k in ("filingDateIS",)}}
    return rec


def ev_ebitda_history(symbol: str, exchange: str | None = None, *,
                      max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """The "EV/EBITDA Ratio" row of the ratios table, annual and quarterly.

    Quarterly points are period-end enterprise value over EBITDA of the four
    quarters ending that date (checked: ev / TTM ebitda reproduces the row);
    "current" is today's EV over the latest TTM EBITDA.
    """
    out = {"symbol": symbol, "ok": False, "error": None, "source": None, "retrieved_at": None,
           "row_title": None, "current": None, "current_as_of": None,
           "quarterly": [], "annual": [], "n_quarterly": 0, "n_annual": 0, "n_quarterly_non_null": 0,
           "n_annual_non_null": 0, "sources": {}}
    errs = []
    for per in ("quarterly", "annual"):
        f = financials(symbol, "ratios", per, exchange, max_age=max_age)
        out["sources"][per] = f["source"]
        out["retrieved_at"] = out["retrieved_at"] or f["retrieved_at"]
        row = (f.get("rows") or {}).get("evebitda")
        if not f["ok"] or not row:
            errs.append(f"{per}: {f['error'] or 'EV/EBITDA row absent'}")
            continue
        out["row_title"] = row.get("title")
        pts = []
        for c in f["columns"]:
            v = row["values"].get(c["key"])
            ev = ((f["rows"].get("ev") or {}).get("values") or {}).get(c["key"])
            mc = ((f["rows"].get("marketcap") or {}).get("values") or {}).get(c["key"])
            if c["is_ttm_column"]:
                if per == "quarterly" or out["current"] is None:
                    out["current"], out["current_as_of"] = v, c["period_end"]
                continue
            pts.append({"period_end": c["key"], "fiscal_year": c["fiscal_year"],
                        "fiscal_quarter": c["fiscal_quarter"], "ev_ebitda": v,
                        "enterprise_value": ev, "market_cap": mc})
        out[per] = pts
        out[f"n_{per}"] = len(pts)
        out[f"n_{per}_non_null"] = sum(p["ev_ebitda"] is not None for p in pts)
    out["ok"] = bool(out["quarterly"] or out["annual"])
    out["error"] = "; ".join(errs) or None
    out["source"] = _source(" | ".join(s.split(" ", 1)[1] for s in out["sources"].values() if s))
    return out


# ============================================================== price history
_TABLE_RANGES = {  # what the free history table offers per period type
    "Daily": ["3M", "6M", "YTD", "1Y", "5Y"],
    "Weekly": ["3M", "6M", "YTD", "1Y", "5Y"],
    "Monthly": ["10Y"],
    "Quarterly": ["10Y"],
    "Annual": ["MAX"],
}
_RANGE_DAYS = {"3M": 92, "6M": 184, "1Y": 366, "5Y": 5 * 366, "10Y": 10 * 366}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm_period(period: str) -> str | None:
    p = (period or "").strip().lower()
    return {"d": "Daily", "daily": "Daily", "1d": "Daily", "w": "Weekly", "weekly": "Weekly",
            "1w": "Weekly", "m": "Monthly", "monthly": "Monthly", "1m": "Monthly",
            "q": "Quarterly", "quarterly": "Quarterly", "a": "Annual", "annual": "Annual",
            "y": "Annual", "yearly": "Annual"}.get(p)


def _chart_series(api_sym: str, *, max_age: float | None) -> dict:
    """Full daily close (+ dividend-adjusted close) history from the chart endpoint."""
    out = {"ok": False, "error": None, "urls": [], "retrieved_at": None, "rows": []}
    series = {}
    for name, q in (("close", "?type=chart"), ("adj_close", "?type=chart&series=a")):
        url = f"{BASE}/api/symbol/{api_sym}/history{q}"
        out["urls"].append(url)
        try:
            res = _get(url, max_age=max_age)
            data = json.loads(res["text"]).get("data")
        except FetchError as e:
            if name == "close":
                out["error"] = _unavailable(e.status, e.detail or "")
                return out
            continue
        except (ValueError, AttributeError):
            if name == "close":
                out["error"] = "unavailable: non-JSON response"
                return out
            continue
        out["retrieved_at"] = out["retrieved_at"] or res.get("retrieved_at")
        pts = {}
        for p in data if isinstance(data, list) else []:
            if isinstance(p, list) and len(p) == 2 and isinstance(p[0], (int, float)) and _num(p[1]) is not None:
                d = _dt.datetime.fromtimestamp(p[0] / 1000, _dt.timezone.utc).date().isoformat()
                pts[d] = p[1]
        series[name] = pts
    close = series.get("close") or {}
    if not close:
        out["error"] = "unavailable: empty chart series"
        return out
    adj = series.get("adj_close") or {}
    out["rows"] = [{"date": d, "open": None, "high": None, "low": None, "close": close[d],
                    "adj_close": adj.get(d), "volume": None, "change_pct": None} for d in sorted(close)]
    out["ok"] = True
    return out


def _years(rng: str) -> float | None:
    m = re.fullmatch(r"(\d+)([YM])", rng)
    if not m:
        return None
    return int(m.group(1)) if m.group(2) == "Y" else int(m.group(1)) / 12.0


def _table_range_for(per: str, rng: str) -> str | None:
    """Smallest free table range covering the request, or None (-> chart series)."""
    opts = _TABLE_RANGES[per]
    if rng in opts:
        return rng
    yrs = _years(rng)
    if yrs is None:
        return None
    if per == "Annual":
        return "MAX"
    for o in opts:
        oy = _years(o)
        if oy is not None and oy >= yrs:
            return o
    return None


def price_history(symbol: str, range: str = "5Y", period: str = "Daily",  # noqa: A002
                  exchange: str | None = None, *, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Price history, rows ascending by date:
    {date, open, high, low, close, adj_close, volume, change_pct}.

    mode "table"  full OHLCV from the history table endpoint, free combinations
                  only: Daily/Weekly x 3M|6M|YTD|1Y|5Y, Monthly/Quarterly x 10Y,
                  Annual x MAX (other spans, e.g. Daily 4Y, fetch the next
                  longer free range and are cut to the request).  close is
                  split-adjusted, adj_close is also dividend-adjusted.  Weekly/Monthly/... rows are dated by the
                  FIRST trading day of the period, close = last close of it,
                  and the newest row is the period in progress.
    mode "chart"  anything longer (e.g. Daily 10Y/MAX, Monthly 16Y/MAX): built
                  from the full daily chart series -- close and adj_close only
                  (open/high/low/volume None).  Monthly/Weekly/Quarterly/Annual
                  rows are then the LAST trading day of each period (derived by
                  resampling the daily series; flagged derived=True).
    The server silently answers unknown ranges with 1Y and caps Pro ranges, so
    only ranges known to be free are ever sent; first_date/last_date/n_rows
    state what actually came back.
    """
    ps = provider_symbol(symbol, exchange)
    per = _norm_period(period)
    rng = (range or "").strip().upper()
    rec = {"symbol": symbol, "provider_symbol": ps["symbol"], "market": ps["market"],
           "range_requested": range, "period": per, "ok": False, "error": None, "source": None,
           "retrieved_at": None, "mode": None, "derived": False, "ohlcv": False,
           "price_basis": "split-adjusted close; adj_close additionally dividend-adjusted",
           "row_date_is": None, "last_period_in_progress": None,
           "n_rows": 0, "first_date": None, "last_date": None, "expected_first_date": None,
           "shorter_than_requested": None, "rows": [], "warnings": []}
    if per is None or not (rng in ("YTD", "MAX") or _years(rng)):
        rec["error"] = f"bad arguments: range={range!r} period={period!r}"
        return rec

    table_rng = _table_range_for(per, rng)
    if table_rng:
        api_range = "Max" if table_rng == "MAX" else table_rng
        url = f"{BASE}/api/symbol/{ps['api']}/history?range={api_range}&period={per}"
        rec.update({"source": _source(url), "mode": "table", "ohlcv": True,
                    "row_date_is": "trading day" if per == "Daily" else "first trading day of the period"})
        try:
            res = _get(url, max_age=max_age)
            data = json.loads(res["text"]).get("data")
        except FetchError as e:
            rec["error"] = _unavailable(e.status, e.detail or "")
            return rec
        except (ValueError, AttributeError):
            rec["error"] = "unavailable: non-JSON response"
            return rec
        rec["retrieved_at"] = res.get("retrieved_at")
        rows, bad = {}, 0
        for r in data if isinstance(data, list) else []:
            d = r.get("t") if isinstance(r, dict) else None
            if not (isinstance(d, str) and _DATE_RE.match(d)) or _num(r.get("c")) is None:
                bad += 1
                continue
            rows[d] = {"date": d, "open": _num(r.get("o")), "high": _num(r.get("h")),
                       "low": _num(r.get("l")), "close": _num(r.get("c")), "adj_close": _num(r.get("a")),
                       "volume": _num(r.get("v")), "change_pct": _num(r.get("ch"))}
        if bad:
            rec["warnings"].append(f"{bad} malformed rows skipped")
        ordered = [rows[d] for d in sorted(rows)]
        yrs = _years(rng)
        if ordered and table_rng != rng and yrs:        # a longer free range was fetched: cut to the request
            cut = (_dt.date.fromisoformat(ordered[-1]["date"])
                   - _dt.timedelta(days=int(round(yrs * 365.25)))).isoformat()
            ordered = [r for r in ordered if r["date"] >= cut]
            rec["table_range_fetched"] = table_rng
        rec["rows"] = ordered
        if per != "Daily":
            rec["last_period_in_progress"] = True
    else:
        ch = _chart_series(ps["api"], max_age=max_age)
        rec.update({"source": _source(" | ".join(ch["urls"])), "retrieved_at": ch["retrieved_at"],
                    "mode": "chart", "ohlcv": False})
        if not ch["ok"]:
            rec["error"] = ch["error"]
            return rec
        daily = ch["rows"]
        if per == "Daily":
            rows = daily
            rec["row_date_is"] = "trading day"
        else:
            def bucket(d: str) -> str:
                y, mth = d[:4], int(d[5:7])
                if per == "Monthly":
                    return d[:7]
                if per == "Quarterly":
                    return f"{y}-Q{(mth - 1) // 3 + 1}"
                if per == "Annual":
                    return y
                iso = _dt.date.fromisoformat(d).isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"
            last: dict[str, dict] = {}
            for r in daily:                                   # ascending -> last row of each bucket wins
                last[bucket(r["date"])] = r
            rows = [dict(r, period=b) for b, r in last.items()]
            rec.update({"derived": True, "last_period_in_progress": True,
                        "row_date_is": "last trading day of the period (resampled from the daily close series)"})
        yrs = _years(rng)
        if rows and (rng == "YTD" or yrs):
            end = _dt.date.fromisoformat(rows[-1]["date"])
            if rng == "YTD":
                start = _dt.date(end.year, 1, 1)
            else:
                start = end - _dt.timedelta(days=int(round(yrs * 365.25)))
            rows = [r for r in rows if r["date"] >= start.isoformat()]
        rec["rows"] = rows

    rows = rec["rows"]
    if not rows:
        rec["error"] = "unavailable: no price rows returned"
        return rec
    rec.update({"ok": True, "n_rows": len(rows), "first_date": rows[0]["date"], "last_date": rows[-1]["date"]})
    end = _dt.date.fromisoformat(rows[-1]["date"])
    exp = None
    if rng == "YTD":
        exp = _dt.date(end.year, 1, 1)
    elif rng in _RANGE_DAYS and rec["mode"] == "table" and not rec.get("table_range_fetched"):
        exp = end - _dt.timedelta(days=_RANGE_DAYS[rng])
    elif _years(rng):
        exp = end - _dt.timedelta(days=int(round(_years(rng) * 365.25)))
    if exp:
        rec["expected_first_date"] = exp.isoformat()
        slack = {"Daily": 10, "Weekly": 14, "Monthly": 45, "Quarterly": 100, "Annual": 370}[per]
        rec["shorter_than_requested"] = (
            _dt.date.fromisoformat(rows[0]["date"]) > exp + _dt.timedelta(days=slack))
    return rec


# =================================================================== forecast
_EST_KEYS = {  # provider row -> output key
    "revenue": "revenue", "revenueGrowth": "revenue_growth_pct",
    "eps": "eps_gaap", "epsGrowth": "eps_gaap_growth_pct",
    "adjustedEps": "eps_adjusted", "adjustedEpsGrowth": "eps_adjusted_growth_pct",
    "netIncome": "net_income", "operatingIncome": "operating_income", "grossProfit": "gross_profit",
    "grossMargin": "gross_margin_pct", "freeCashFlow": "free_cash_flow", "dps": "dividend_per_share",
    "dpsGrowth": "dividend_growth_pct", "peForward": "forward_pe", "analysts": "analysts",
    "ffo": "ffo", "ffoShare": "ffo_per_share", "pFFO": "p_ffo",
}


def _estimate_rows(tbl) -> list[dict]:
    if not isinstance(tbl, dict) or not isinstance(tbl.get("dates"), list):
        return []
    dates = tbl["dates"]
    last_actual = _num(tbl.get("lastDate"))
    out = []
    for i, d in enumerate(dates):
        row = {"period_end": parse_date(d),
               "fiscal_year": (tbl.get("fiscalYear") or [None] * len(dates))[i] or None,
               "fiscal_quarter": (tbl.get("fiscalQuarter") or [None] * len(dates))[i] or None,
               "is_estimate": (i > last_actual) if last_actual is not None else None,
               "pro_only": False}
        for src, dst in _EST_KEYS.items():
            col = tbl.get(src)
            v = col[i] if isinstance(col, list) and i < len(col) else None
            if _is_pro(v):
                row["pro_only"] = True
            row[dst] = _num(v)
        out.append(row)
    return out


def _rating_row(r: dict) -> dict:
    act = _text(r.get("action_rt"))
    return {"date": parse_date(r.get("date")), "time": _text(r.get("time")), "firm": _text(r.get("firm")),
            "analyst": _text(r.get("analyst")), "action": act,
            "direction": {"upgrades": "upgrade", "downgrades": "downgrade", "maintains": "maintain",
                          "reiterates": "reiterate", "initiates": "initiate"}.get((act or "").lower()),
            "rating_new": _text(r.get("rating_new")), "rating_old": _text(r.get("rating_old")),
            "price_target_new": _num(r.get("pt_now")), "price_target_old": _num(r.get("pt_old")),
            "currency": _text(r.get("curr"))}


def forecast(symbol: str, exchange: str | None = None, *, with_peg: bool = True,
             max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """https://stockanalysis.com/stocks/{sym}/forecast/

    consensus          S&P Global consensus label + strong buy/buy/hold/sell/
                       strong sell counts (the numbers the page headline uses)
    price_target       S&P Global: low / avg / median / high / count
    price_target_individual  TipRanks-based aggregate of individual analysts
                       (a different, smaller sample -- do not mix the two)
    recommendation_trend     monthly rating-count snapshots (12 months)
    estimates_annual / estimates_quarterly
                       actual and estimated periods; growth in percent units.
                       eps_gaap = row "eps", eps_adjusted = row "adjustedEps";
                       the page displays the adjusted row when eps_basis ==
                       "normalized".  Paywalled cells -> None, pro_only=True.
    this_next          the page's headline "EPS/Revenue This Year / Next Year"
    peg                PEG inputs (see `definition`): forward PE (NTM) over the
                       5Y long-term EPS growth consensus; that growth figure is
                       not published, only implied (derived) as forward_pe/peg.
    """
    ps = provider_symbol(symbol, exchange)
    got = _load(ps["page_base"] + "forecast/", max_age=max_age)
    rec = {"symbol": symbol, "provider_symbol": ps["symbol"], "market": ps["market"],
           "ok": got["ok"], "error": got["error"], "source": _source(got["url"]),
           "page_url": BASE + ps["page_base"] + "forecast/", "retrieved_at": got["retrieved_at"],
           "redirected_from": got["redirected_from"],
           "quote": _quote(got["info"]), "consensus": None, "price_target": None,
           "price_target_individual": None, "recommendation_trend": [], "estimates_source": None,
           "eps_basis": None, "displayed_eps_row": None, "estimates_annual": [], "estimates_quarterly": [],
           "this_next": None, "estimate_ranges": None, "latest_ratings": [], "peg": None, "warnings": []}
    if with_peg:
        rec["peg"] = _peg_block(symbol, exchange, max_age=max_age)
    if not got["ok"]:
        if "page not offered" in (got["error"] or ""):
            rec["error"] += " -- the site has no analyst forecast page for this symbol (no coverage)"
        return rec
    page = got["page"]
    price = rec["quote"]["price"]

    cr = page.get("currentRatings")
    if isinstance(cr, dict):
        rec["consensus"] = {"label": _text(cr.get("consensus")), "score": _num(cr.get("score")),
                            "analyst_count": _num(cr.get("count")), "strong_buy": _num(cr.get("strongBuy")),
                            "buy": _num(cr.get("buy")), "hold": _num(cr.get("hold")),
                            "sell": _num(cr.get("sell")), "strong_sell": _num(cr.get("strongSell")),
                            "data_source": cr.get("source")}

    def target(block, keymap) -> dict | None:
        if not isinstance(block, dict):
            return None
        t = {k: _num(block.get(src)) for k, src in keymap.items()}
        t["currency"] = _text(block.get("currency"))
        t["data_source"] = block.get("source")
        t["updated"] = parse_date(block.get("updated"))
        t["upside_pct_derived"] = None
        if isinstance(t.get("avg"), (int, float)) and isinstance(price, (int, float)) and price > 0:
            t["upside_pct_derived"] = round((t["avg"] / price - 1) * 100, 2)
            t["upside_formula"] = f"avg / quote.price({price}) - 1"
        return t

    rec["price_target"] = target(page.get("priceTargets"), {
        "low": "low", "avg": "avg", "median": "median", "high": "high", "count": "numPriceTargets"})
    rec["price_target_individual"] = target(page.get("targets"), {
        "low": "low", "avg": "average", "median": "median", "high": "high", "count": "count"})

    for r in page.get("recommendations") or []:
        if isinstance(r, dict):
            rec["recommendation_trend"].append({
                "month": _text(r.get("month")), "date": parse_date(r.get("date")),
                "strong_buy": _num(r.get("strongBuy")), "buy": _num(r.get("buy")), "hold": _num(r.get("hold")),
                "sell": _num(r.get("sell")), "strong_sell": _num(r.get("strongSell")),
                "total": _num(r.get("total")), "consensus": _text(r.get("consensus")),
                "score": _num(r.get("score"))})

    est = page.get("estimates") if isinstance(page.get("estimates"), dict) else {}
    rec["estimates_source"] = page.get("estimatesSource")
    rec["eps_basis"] = est.get("epsBasis")
    rec["displayed_eps_row"] = "eps_adjusted" if est.get("epsBasis") == "normalized" else "eps_gaap"
    tbl = est.get("table") if isinstance(est.get("table"), dict) else {}
    rec["estimates_annual"] = _estimate_rows(tbl.get("annual"))
    rec["estimates_quarterly"] = _estimate_rows(tbl.get("quarterly"))

    stats = est.get("stats") if isinstance(est.get("stats"), dict) else {}
    tn = {}
    for per in ("annual", "quarterly"):
        blk = stats.get(per) if isinstance(stats.get(per), dict) else {}
        tn[per] = {}
        for src, dst in (("epsThis", "eps_this"), ("epsNext", "eps_next"),
                         ("revenueThis", "revenue_this"), ("revenueNext", "revenue_next")):
            b = blk.get(src) if isinstance(blk.get(src), dict) else {}
            tn[per][dst] = {"value": _num(b.get("this")), "prior": _num(b.get("last")),
                            "growth_pct": _num(b.get("growth"))}
    rec["this_next"] = dict(tn, note="'this'/'next' = current / next fiscal year (annual) or quarter; "
                                     f"EPS basis = {rec['eps_basis']}")

    charts = page.get("estimatesCharts")
    if isinstance(charts, dict):
        rng = {}
        for metric, by_date in charts.items():
            if not isinstance(by_date, dict):
                continue
            rng[metric] = {}
            for d, v in by_date.items():
                if isinstance(v, dict):
                    rng[metric][d] = {"analysts": _num(v.get("no")), "avg": _num(v.get("avg")),
                                      "low": _num(v.get("low")), "high": _num(v.get("high")), "pro_only": False}
                else:
                    rng[metric][d] = {"analysts": None, "avg": None, "low": None, "high": None,
                                      "pro_only": _is_pro(v)}
        rec["estimate_ranges"] = rng

    rec["latest_ratings"] = [_rating_row(r) for r in page.get("ratings") or [] if isinstance(r, dict)]
    return rec


def _peg_block(symbol: str, exchange: str | None, *, max_age: float | None) -> dict:
    st = statistics(symbol, exchange, definitions=False, with_screener=False, max_age=max_age)
    f = st["fields"]
    fpe, peg = f.get("forward_pe"), f.get("peg_ratio")
    implied = None
    if isinstance(fpe, (int, float)) and isinstance(peg, (int, float)) and peg > 0 and fpe > 0:
        implied = round(fpe / peg, 3)
    return {"peg_ratio": peg, "forward_pe": fpe, "pe_ttm": f.get("pe_ttm"),
            "eps_growth_forecast_3y_cagr_pct": f.get("eps_growth_forecast_3y_pct"),
            "lt_eps_growth_5y_pct_implied_derived": implied,
            "derived_formula": "forward_pe / peg_ratio",
            "note": "PEG on this site = forward PE (next 12 months) / consensus long-term (5Y) EPS growth; "
                    "the 3Y forecast CAGR and the yearly forecast growth rates are different figures",
            "definition": definition("pegRatio"), "forward_pe_definition": definition("peForward"),
            "source": st["source"], "retrieved_at": st["retrieved_at"],
            "ok": st["ok"], "error": st["error"]}


def ratings(symbol: str, exchange: str | None = None, *, days: int = 90, as_of: str | None = None,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """https://stockanalysis.com/stocks/{sym}/ratings/ -- dated analyst actions.

    The free page lists only the newest rows (8 on 2026-11-14) of
    `total_available`; older ones are behind the Pro paywall.  `rows` are the
    visible actions within `days` of `as_of` (default: today, UTC).
    `window_fully_covered` is True only when the visible rows provably reach
    back past the start of the window (or nothing is hidden); None = unknown.
    Stocks with thin coverage have no ratings page at all: the rows then come
    from the forecast page's "Latest Forecasts" list (max 5) -- see rows_from.
    """
    ps = provider_symbol(symbol, exchange)
    got = _load(ps["page_base"] + "ratings/", max_age=max_age)
    end = _dt.date.fromisoformat(as_of) if as_of else _dt.datetime.now(_dt.timezone.utc).date()
    start = end - _dt.timedelta(days=days)
    rec = {"symbol": symbol, "provider_symbol": ps["symbol"], "market": ps["market"],
           "ok": got["ok"], "error": got["error"], "source": _source(got["url"]),
           "page_url": BASE + ps["page_base"] + "ratings/", "retrieved_at": got["retrieved_at"],
           "redirected_from": got["redirected_from"], "data_provider": "TipRanks", "window_start": start.isoformat(), "window_end": end.isoformat(),
           "rows_from": None, "total_available": None, "rows_visible": 0, "oldest_visible_date": None,
           "truncated_by_paywall": None, "window_fully_covered": None,
           "counts_in_window": None, "summary": None, "rows": []}
    rec["rows_from"] = "ratings page"
    if not got["ok"]:
        if "page not offered" not in (got["error"] or ""):
            return rec
        # thinly covered stocks have no /ratings/ page; the forecast page still lists
        # the latest (at most 5) analyst actions
        fc = _load(ps["page_base"] + "forecast/", max_age=max_age)
        if not fc["ok"]:
            rec["error"] = (f"no ratings page ({got['error']}) and no forecast page ({fc['error']}) "
                            "-- the site has no analyst coverage for this symbol")
            return rec
        rec.update({"ok": True, "error": None, "source": _source(fc["url"]),
                    "page_url": BASE + ps["page_base"] + "forecast/", "retrieved_at": fc["retrieved_at"],
                    "rows_from": "forecast page 'Latest Forecasts' (max 5 rows; no ratings page for this symbol)"})
        page = {"ratings": fc["page"].get("ratings"), "meta": None, "widget": None}
        cr = fc["page"].get("currentRatings") if isinstance(fc["page"].get("currentRatings"), dict) else {}
        pt = fc["page"].get("priceTargets") if isinstance(fc["page"].get("priceTargets"), dict) else {}
        page["widget"] = {"all": {"count": cr.get("count"), "consensus": cr.get("consensus"),
                                  "price_target": pt.get("avg"), "currency": pt.get("currency")}}
    else:
        page = got["page"]
    allrows = [_rating_row(r) for r in page.get("ratings") or [] if isinstance(r, dict)]
    allrows = [r for r in allrows if r["date"]]
    allrows.sort(key=lambda r: (r["date"], r["time"] or ""), reverse=True)
    total = _num((page.get("meta") or {}).get("total")) if isinstance(page.get("meta"), dict) else None
    rec["total_available"] = total
    rec["rows_visible"] = len(allrows)
    rec["oldest_visible_date"] = allrows[-1]["date"] if allrows else None
    reaches_back = bool(allrows and allrows[-1]["date"] < start.isoformat())
    if total is not None:
        rec["truncated_by_paywall"] = total > len(allrows)
        rec["window_fully_covered"] = (total <= len(allrows)) or reaches_back
    else:                                   # no total given: only provable when the rows reach past the window
        rec["truncated_by_paywall"] = None
        rec["window_fully_covered"] = True if reaches_back else None
    rows = [r for r in allrows if start.isoformat() <= r["date"] <= end.isoformat()]
    rec["rows"] = rows
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["direction"] or "other"] = counts.get(r["direction"] or "other", 0) + 1
    rec["counts_in_window"] = counts
    w = page.get("widget") if isinstance(page.get("widget"), dict) else {}
    allw = w.get("all") if isinstance(w.get("all"), dict) else {}
    rec["summary"] = {"analyst_count": _num(allw.get("count")), "consensus": _text(allw.get("consensus")),
                      "price_target": _num(allw.get("price_target")), "currency": _text(allw.get("currency"))}
    return rec


# ============================================================ screener (bulk)
BULK_DEFAULT_FIELDS = [
    "exchange", "price", "priceDate", "marketCap", "enterpriseValue", "rsi", "ma50", "ma200",
    "averageVolume", "zScore", "fScore", "fcf", "ebitda", "evEbitda", "peRatio", "peForward", "pegRatio",
    "debtEbitda", "debtEquity", "debtFcf", "interestCoverage", "currentRatio", "roic", "revenue",
    "operatingCF", "capex", "cash", "debt", "netCash", "sharesOut", "sharesInstitutions",
    "epsGrowth", "epsGrowthQ", "epsGrowth3Y", "epsGrowth5Y", "eps3y",
    "analystRatings", "analystCount", "priceTarget", "priceTargetChange",
    "lastEarningsDate", "nextEarningsDate", "earningsTime", "lastReportDate",
]
_BULK_CHUNK = 45
_bulk_lock = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_fields_memo: dict[str, tuple[float, dict]] = {}


def bulk_fields(*, max_age: float | None = DEFINITION_MAX_AGE) -> dict:
    """The data points the public stock screener offers: id, name, category,
    pro_only.  (https://stockanalysis.com/stocks/screener/)"""
    hit = _fields_memo.get("rec")
    if hit and max_age != 0 and (max_age is None or time.time() - hit[0] < max_age):
        return hit[1]
    got = _load("/stocks/screener/", max_age=max_age)
    rec = {"ok": False, "error": got["error"], "source": _source(got["url"]),
           "retrieved_at": got["retrieved_at"], "count": 0, "fields": {}}
    doc = got["page"] if got["ok"] else None
    pts = doc.get("dataPoints") if isinstance(doc, dict) else None
    if not isinstance(pts, list):
        rec["error"] = rec["error"] or "unavailable: screener data point list missing"
        return rec
    for p in pts:
        if isinstance(p, dict) and p.get("id"):
            rec["fields"][p["id"]] = {"name": p.get("name"), "category": p.get("cat"),
                                      "pro_only": bool(p.get("proOnly"))}
    rec["ok"], rec["count"] = True, len(rec["fields"])
    _fields_memo["rec"] = (time.time(), rec)
    return rec


def bulk(fields: list[str] | None = None, symbols: list[str] | None = None, *,
         max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Screener columns for ALL US-listed stocks (~5,600) in one request.

    GET /_api/endpoints/screener/data-points?type=s&ids=a+b+c -- the endpoint
    the public screener calls when a visitor adds columns; served anonymously.
    data   {SYMBOL: {field: value}}; class shares as BRK.B.  Percentages are
           in percent units, money in USD (ADRs converted), dates ISO.
           Missing values are None exactly as the provider sends them.
    Pro-only data points are never requested (listed in pro_only_skipped);
    unknown ids are listed in unknown_fields.  `symbols` only filters the
    returned dict (the request is always the whole universe).
    """
    want = list(dict.fromkeys(fields or BULK_DEFAULT_FIELDS))
    rec = {"ok": False, "error": None, "source": None, "retrieved_at": None, "universe": "US-listed stocks",
           "fields": [], "pro_only_skipped": [], "unknown_fields": [], "all_null_fields": None,
           "count": 0, "symbols_missing": [], "data": {}}
    cat = bulk_fields()
    known = cat["fields"] if cat["ok"] else None
    ids = []
    for f in want:
        if not isinstance(f, str) or not _ID_RE.match(f):
            rec["unknown_fields"].append(f)
        elif known is not None and f not in known:
            rec["unknown_fields"].append(f)
        elif known is not None and known[f]["pro_only"]:
            rec["pro_only_skipped"].append(f)
        else:
            ids.append(f)
    if known is None:
        rec["error"] = f"field catalogue unavailable ({cat['error']}); nothing requested"
        return rec
    rec["fields"] = ids
    if not ids:
        rec["error"] = "no requestable fields"
        return rec
    urls, data = [], {}
    with _bulk_lock:                       # one thread downloads, the others then hit the cache
        for i in range(0, len(ids), _BULK_CHUNK):
            chunk = ids[i:i + _BULK_CHUNK]
            url = f"{BASE}/_api/endpoints/screener/data-points?type=s&ids={'+'.join(chunk)}"
            urls.append(url)
            try:
                payload, retrieved = _get_json_memo(url, max_age=max_age)
                part = payload["data"]["data"]
            except FetchError as e:
                rec.update({"error": _unavailable(e.status, e.detail or ""), "source": _source(" | ".join(urls))})
                return rec
            except (ValueError, KeyError, TypeError):
                rec.update({"error": "unavailable: unexpected screener payload",
                            "source": _source(" | ".join(urls))})
                return rec
            rec["retrieved_at"] = rec["retrieved_at"] or retrieved
            if not isinstance(part, dict):
                rec.update({"error": "unavailable: unexpected screener payload",
                            "source": _source(" | ".join(urls))})
                return rec
            for sym, row in part.items():
                if isinstance(row, dict):
                    tgt = data.setdefault(sym, {})
                    for f in chunk:
                        v = row.get(f)
                        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                            v = None
                        tgt[f] = v
    rec["source"] = _source(" | ".join(urls))
    if symbols is not None:
        wanted = [provider_symbol(s)["symbol"] for s in symbols]
        rec["symbols_missing"] = [s for s in wanted if s not in data]
        data = {s: data[s] for s in wanted if s in data}
    for row in data.values():
        for f in ids:
            row.setdefault(f, None)
    if symbols is None and data:            # e.g. "n" (name) is a screener column this endpoint does not serve
        rec["all_null_fields"] = [f for f in ids if all(r.get(f) is None for r in data.values())]
    rec.update({"ok": True, "count": len(data), "data": data})
    return rec


# ====================================================================== batch
_FUNCS = {"statistics": statistics, "financials": financials, "price_history": price_history,
          "forecast": forecast, "ratings": ratings, "ev_ebitda_history": ev_ebitda_history}


def batch(fn: str, symbols: list[str], *, max_workers: int = 4, **kwargs) -> dict:
    """Run one of the per-symbol functions for many symbols -> {symbol: record}.
    Threads only overlap on cache hits; network requests stay serialised at
    <= 2/s by the module gate.  A failing symbol yields {ok: False, error}."""
    func = _FUNCS[fn]

    def one(sym: str):
        try:
            return sym, func(sym, **kwargs)
        except Exception as e:  # noqa: BLE001 - one bad symbol must not sink the batch
            return sym, {"symbol": sym, "ok": False, "error": f"{type(e).__name__}: {e}",
                         "source": None, "retrieved_at": None}

    with ThreadPoolExecutor(max_workers=max(1, min(4, max_workers))) as ex:
        return dict(ex.map(one, symbols))


# ======================================================================== CLI
def _compact_stats(r: dict) -> dict:
    keep = ("symbol", "provider_symbol", "ok", "error", "source", "retrieved_at", "name", "exchange",
            "currency", "quote", "fields", "earnings", "derived", "warnings")
    out = {k: r.get(k) for k in keep}
    sc = r.get("screener") or {}
    out["screener"] = {"values": sc.get("values"), "source": sc.get("source"),
                       "retrieved_at": sc.get("retrieved_at")}
    d = (r.get("definitions") or {}).get("peg_ratio") or {}
    out["peg_definition"] = d.get("formula") or d.get("text")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m screener.sources.stockanalysis",
                                 description="stockanalysis.com adapter; prints JSON")
    ap.add_argument("tickers", nargs="*", help="symbols, e.g. NVDA LOPE GEF.B (LSE: --exchange LSE VOD)")
    ap.add_argument("--fn", default="statistics",
                    choices=["statistics", "financials", "price_history", "forecast", "ratings",
                             "ev_ebitda_history", "bulk", "bulk_fields", "all"])
    ap.add_argument("--exchange", default=None, help="LSE for London listings (default: US)")
    ap.add_argument("--statement", default="income", help="income | balance-sheet | cash-flow-statement | ratios")
    ap.add_argument("--period", default=None, help="financials: annual|quarterly|ttm ; price: Daily|Weekly|Monthly")
    ap.add_argument("--range", default="5Y", help="price_history range: 3M 6M YTD 1Y 5Y 10Y 16Y MAX")
    ap.add_argument("--fields", default=None, help="bulk: comma separated screener ids")
    ap.add_argument("--days", type=int, default=90, help="ratings window")
    ap.add_argument("--tail", type=int, default=None, help="price_history: print only the last N rows")
    ap.add_argument("--compact", action="store_true", help="statistics: canonical fields only")
    ap.add_argument("--no-cache", action="store_true", help="re-fetch instead of using the on-disk cache")
    a = ap.parse_args(argv)
    age = 0 if a.no_cache else DEFAULT_MAX_AGE
    out: dict = {}
    if a.fn == "bulk_fields":
        out = bulk_fields(max_age=age if a.no_cache else DEFINITION_MAX_AGE)
    elif a.fn == "bulk":
        out = bulk(a.fields.split(",") if a.fields else None, a.tickers or None, max_age=age)
    else:
        if not a.tickers:
            ap.error("at least one ticker is required")
        for t in a.tickers:
            if a.fn in ("statistics", "all"):
                s = statistics(t, a.exchange, max_age=age)
                s = _compact_stats(s) if a.compact else s
                if a.fn == "statistics":
                    out[t] = s
                    continue
                out[t] = {"statistics": s}
            if a.fn in ("financials", "all"):
                if a.fn == "financials":
                    out[t] = financials(t, a.statement, a.period or "annual", a.exchange, max_age=age)
                    continue
                out[t]["financials"] = {
                    f"{st}:{p}": financials(t, st, p, a.exchange, max_age=age)
                    for st in ("income", "balance-sheet", "cash-flow-statement", "ratios")
                    for p in ("annual", "quarterly", "ttm")}
            if a.fn in ("price_history", "all"):
                ph = price_history(t, a.range, a.period or "Daily", a.exchange, max_age=age)
                if a.tail:
                    ph["rows"] = ph["rows"][-a.tail:]
                if a.fn == "price_history":
                    out[t] = ph
                    continue
                out[t]["price_history"] = ph
            if a.fn in ("forecast", "all"):
                fc = forecast(t, a.exchange, max_age=age)
                if a.fn == "forecast":
                    out[t] = fc
                    continue
                out[t]["forecast"] = fc
            if a.fn in ("ratings", "all"):
                rt = ratings(t, a.exchange, days=a.days, max_age=age)
                if a.fn == "ratings":
                    out[t] = rt
                    continue
                out[t]["ratings"] = rt
            if a.fn in ("ev_ebitda_history", "all"):
                ev = ev_ebitda_history(t, a.exchange, max_age=age)
                if a.fn == "ev_ebitda_history":
                    out[t] = ev
                    continue
                out[t]["ev_ebitda_history"] = ev
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
