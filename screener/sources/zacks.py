"""Zacks adapter -- free public pages on www.zacks.com + quote-feed.zacks.com only.

Public functions (all return plain JSON-serialisable dicts):

    quote(symbol)         Zacks Rank, Style Scores, industry / sector rank, next
                          earnings date, forward P/E (F1), PEG, 3-5yr EPS growth,
                          ESP, beta, market cap, 20-day average volume.
    broker_recs(symbol)   Average Broker Rating (ABR) today / 1wk / 1m / 2m / 3m ago
                          and the Strong Buy..Strong Sell counts behind it.
    estimates(symbol)     consensus EPS / sales estimates, growth table, revision
                          agreement + magnitude, ESP, last-4-quarter surprises.
    price_target(symbol)  average / high / low target, upside %, analyst count.
    fundamentals(symbol)  ratios from "Financial Overview", Zacks' own free-cash-flow
                          / EPS chart series, and the income statement, balance
                          sheet and cash-flow statement tables.
    snapshot(symbol)      all five of the above.

Conventions
-----------
* Every record carries "source" (provider + exact URL of the primary page),
  "sources" (every URL touched, each with its own retrieved_at / availability)
  and "retrieved_at".
* A value that is missing, "NA", pending or unparseable is None. Nothing is
  estimated or defaulted. Anything this module computes itself lives under a
  "derived" / "checks" key and states its formula.
* If Zacks answers with a rate limit, a bot challenge, a login wall or a page
  whose structure / ticker does not match, the page is reported as unavailable
  ("available": False, "unavailable_reason": ...) and is never parsed as data.
* Class shares: pass BRK.B / BRK-B / BRK/B -- Zacks uses the dot form (BRK.B).

CLI:  python3 -m screener.sources.zacks [--only quote,broker_recs,...] TICKER [...]
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..http import FetchError, fetch

PROVIDER = "Zacks"
WWW = "https://www.zacks.com"
URLS = {
    "feed": "https://quote-feed.zacks.com/index?t={sym}",
    "quote": WWW + "/stock/quote/{sym}",
    "broker_recs": WWW + "/stock/research/{sym}/brokerage-recommendations",
    "price_target": WWW + "/stock/research/{sym}/price-target-stock-forecast",
    "estimates": WWW + "/stock/quote/{sym}/detailed-earning-estimates",
    "financial_overview": WWW + "/stock/quote/{sym}/financial-overview",
    "income_statement": WWW + "/stock/quote/{sym}/income-statement",
    "balance_sheet": WWW + "/stock/quote/{sym}/balance-sheet",
    "cash_flow": WWW + "/stock/quote/{sym}/cash-flow-statements",
    # JSON behind the "Fundamental Charts" pages (/stock/chart/{sym}/fundamental/...)
    "chart": WWW + "/data_handler/charts/?ticker={sym}&wrapper={metric}",
}

_ZACKS_HOSTS = {"www.zacks.com", "quote-feed.zacks.com"}
_MAX_AGE = 20 * 3600          # default cache lifetime handed to screener.http.fetch
_RETRIES = 3                  # polite: let the shared client back off a few times, then give up
_RECHECK_BLOCKED_AFTER = 15 * 60  # a cached blocked page older than this is requested once more

# ----------------------------------------------------------------------------
# Provider definitions (kept next to the numbers so the pipeline can cite them)
# ----------------------------------------------------------------------------
DEFINITIONS = {
    "zacks_rank": "Zacks Rank, 1-5: 1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell "
                  "(Zacks' short-term, 1-3 month, estimate-revision based timeliness rating).",
    "style_scores": "Zacks Style Scores, graded A (best) to F: Value, Growth, Momentum and the "
                    "combined VGM score.",
    "industry_rank": "Zacks Industry Rank: rank of the stock's Zacks X (expanded) industry by the "
                     "average Zacks Rank of its members; 1 = best. Shown as 'Top/Bottom N% (rank "
                     "out of total)'.",
    "sector_rank": "Zacks Sector Rank: same idea for the 16 Zacks sectors (taken from the Zacks "
                   "sector page reached via the stock's industry page).",
    "forward_pe_f1": "Quote page 'Forward PE' = 'P/E (F1)': latest price / Zacks consensus EPS "
                     "for the CURRENT fiscal year (F1). Not TTM.",
    "peg_ratio": "Zacks 'PEG Ratio' (quote page, estimates page, financial overview): P/E (F1) "
                 "divided by the expected long-term EPS growth rate ('Exp EPS Growth (3-5yr)' / "
                 "'Next 5 Years'). i.e. a FORWARD PEG on the 3-5 year growth horizon, even though "
                 "the label links to a chart named 'peg-ratio-ttm'. See checks.peg_recomputed.",
    "exp_eps_growth_3_5yr_pct": "Zacks 'Exp EPS Growth (3-5yr)': consensus long-term annual EPS "
                                "growth estimate, percent (same figure as the 'Next 5 Years' row "
                                "of the '% EPS Growth Estimates' table).",
    "earnings_esp_pct": "Zacks Earnings ESP = (Most Accurate Estimate - Zacks Consensus "
                        "Estimate) / Zacks Consensus Estimate, percent, current quarter.",
    "abr": "Average Brokerage Recommendation (ABR) on a 1-5 scale: 1=Strong Buy, 2=Buy, 3=Hold, "
           "4=Sell, 5=Strong Sell; average of the actual recommendations of the brokerage firms "
           "Zacks tracks. LOWER is more bullish.",
    "price_target": "Average of analysts' short-term (12-month) price targets collected by "
                    "Zacks. The headline 'Upside' is measured against the 'last closing price' "
                    "printed in the same block, which can lag the latest close by one session.",
    "debt_to_equity": "Zacks 'Debt-to-Equity' (financial overview), a ratio for the most recent "
                      "quarter Zacks has processed (see debt_to_equity_period_end). Zacks does "
                      "not print the formula; it tracks LONG-TERM debt / shareholders' equity "
                      "of the Zacks balance sheet (short-term debt excluded) -- compare "
                      "derived.lt_debt_to_equity_from_balance_sheet. Not meaningful when equity "
                      "is tiny or negative.",
    "free_cash_flow": "Zacks chart series 'Free Cash Flow (TTM)' / '(Quarterly)', USD millions: "
                      "net cash from operating activities minus capital expenditure "
                      "('Property & Equipment' line of the Zacks cash-flow statement).",
    "eps_diluted": "Zacks chart series 'EPS Diluted (TTM)' / '(Quarterly)': diluted EPS before "
                   "non-recurring items as in the Zacks income statement (USD per share). It "
                   "can differ from 'EPS (TTM)' on the estimates page, which sums the "
                   "as-reported quarterly EPS used for surprises.",
    "eps_growth_q": "Financial overview 'EPS Growth vs. Previous Year (Q0/Q-4)': most recent "
                    "reported quarter vs the same quarter a year earlier; '(Q0/Q-1)': vs the "
                    "prior quarter. Percent.",
    "last_eps_surprise_pct": "Detailed-estimates page 'Last EPS Surprise': (reported EPS - "
                             "Zacks consensus) / |consensus| for the most recently reported "
                             "quarter, percent.",
}

# ----------------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------------
_NA_TOKENS = {
    "", "na", "n/a", "nm", "n.a.", "n.a", "-", "--", "---", "—", "–", "null", "none",
    "nan", "$n/a", "n/a%", "pending next eps report", "pending",
}
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
_WORD_NUM = {
    "zero": 0, "no": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
_NUM_RE = re.compile(
    r"([+-]?)\s*\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)\s*([kKmMbBtT])?")


def parse_number(text) -> float | None:
    """'1,234.5' '$293.22' '76.44%' '3,924.69 B' '(1.2)' '-.03' -> float; anything else -> None."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return None if text != text else float(text)
    t = str(text).replace("\xa0", " ").replace("−", "-").strip()
    if t.lower() in _NA_TOKENS:
        return None
    neg = False
    m = re.fullmatch(r"\(\s*(.*?)\s*\)", t)
    if m:
        neg, t = True, m.group(1)
    if t.endswith("%"):
        t = t[:-1].strip()
    m = _NUM_RE.fullmatch(t)
    if not m:
        return None
    v = float(m.group(2).replace(",", ""))
    if m.group(3):
        v = round(v * _SUFFIX[m.group(3).lower()], 4)
    if m.group(1) == "-":
        v = -v
    return -v if neg else v


def parse_int(text) -> int | None:
    v = parse_number(text)
    if v is None or v != int(v):
        return None
    return int(v)


def _count_word(text) -> int | None:
    """'53' -> 53, 'three' -> 3 (Zacks spells out small counts in its prose)."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if t in _WORD_NUM:
        return _WORD_NUM[t]
    return parse_int(t)


def _iso_date(text) -> str | None:
    """'11/17/26' / '1/31/2026' -> '2026-11-17'; otherwise None."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*", str(text))
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if len(m.group(3)) == 2:
        # two-digit years: Zacks chart series reach back into the 1990s ('10/31/99'),
        # while forward-looking dates never lie more than ~2 years ahead
        y += 2000 if y <= (_dt.date.today().year + 2) % 100 else 1900
    try:
        return _dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _epoch_date(text) -> str | None:
    v = parse_int(text)
    if v is None or v <= 0:
        return None
    try:
        return _dt.datetime.fromtimestamp(v, _dt.timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _txt(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _slug(label: str) -> str:
    s = label.lower().replace("#", "num").replace("%", "pct").replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", label.replace("\xa0", " ")).strip().lower()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def zacks_symbol(symbol: str) -> str:
    """Pipeline symbol -> Zacks convention (class shares use a dot: BRK.B, GEF.B)."""
    s = str(symbol).strip().upper()
    s = s.replace("/", ".")
    if re.fullmatch(r"[A-Z]+-[A-Z]{1,2}", s):
        s = s.replace("-", ".")
    return s


def _round(v, n=4):
    return None if v is None else round(v, n)


# ----------------------------------------------------------------------------
# HTTP + "is this really data?" checks
# ----------------------------------------------------------------------------
_CHALLENGE_MARKERS = (
    "_incapsula_resource", "incapsula incident", "request unsuccessful", "captcha",
    "access denied", "pardon our interruption", "verify you are a human",
    "are you a robot", "unusual traffic", "just a moment", "cf-chl", "px-captcha",
    "datadome", "too many requests", "rate limit",
)


_HOME_REDIRECT = "Zacks redirected the request to its home page (page not served)"


def _block_reason(text: str | None, final_url: str, expect: str,
                  requested_url: str = "") -> str | None:
    """Why this response must not be parsed as data (None = looks like a normal page)."""
    u = urlparse(final_url or "")
    host, path = u.netloc.lower(), u.path.lower()
    if host and host not in _ZACKS_HOSTS:
        return f"redirected off-site to {host}"
    if path in ("", "/") and urlparse(requested_url or "").path not in ("", "/"):
        return _HOME_REDIRECT
    if "search.php" in path:
        return "symbol not found (Zacks redirected to its symbol search)"
    if expect == "html" and any(p in path for p in ("/registration", "/login", "/subscribe")):
        return f"login / subscription wall (redirected to {path})"
    low = (text or "").lower()
    if not low.strip():
        return "empty response"
    if "incapsula incident id" in low or "request unsuccessful. incapsula" in low:
        return "bot challenge (Imperva/Incapsula incident page)"
    if expect == "html":
        if len(low) < 30000 and any(m in low for m in _CHALLENGE_MARKERS):
            return "bot challenge / interstitial page instead of content"
        if "<html" not in low:
            return "non-HTML response"
    return None


def _error_reason(e: FetchError) -> str:
    detail = (e.detail or "")[:160]
    if e.status == 429:
        return "rate limited (HTTP 429)"
    if e.status in (401, 403):
        return f"access denied / bot challenge (HTTP {e.status})"
    if e.status == 404:
        return "not found (HTTP 404)"
    if e.status is None:
        if "search.php" in detail:
            return "symbol not found (Zacks redirected to its symbol search)"
        return f"network error: {detail}"
    if e.status >= 500:
        return f"provider server error (HTTP {e.status})"
    return f"HTTP {e.status}"


def _http(url: str, *, expect: str = "html", max_age: float | None = None) -> dict:
    """screener.http.fetch + block detection. Never raises.

    -> {"ok", "text", "url", "final_url", "status", "retrieved_at", "reason"}
    """
    max_age = _MAX_AGE if max_age is None else max_age
    out = {"ok": False, "text": None, "url": url, "final_url": None, "status": None,
           "retrieved_at": None, "reason": None}

    def attempt(age):
        try:
            res = fetch(url, max_age=age, retries=_RETRIES)
        except FetchError as e:
            out.update(status=e.status, retrieved_at=_now(), reason=_error_reason(e),
                       text=None, ok=False)
            return None
        reason = _block_reason(res.get("text"), res.get("url") or url, expect, url)
        out.update(status=res.get("status"), retrieved_at=res.get("retrieved_at"),
                   final_url=res.get("url") or url, reason=reason, ok=reason is None,
                   text=res.get("text") if reason is None else None)
        return res

    res = attempt(max_age)
    if res is not None and not out["ok"]:
        age = time.time() - float(res.get("ts") or time.time())
        if out["reason"] == _HOME_REDIRECT and (age < 5 or age > 60):
            # Zacks occasionally bounces a valid page to "/" (a soft server error, not a
            # block). One plain repeat of the same request, then give up.
            time.sleep(2.0 if age < 5 else 0.0)
            attempt(0)
        elif max_age != 0 and age > _RECHECK_BLOCKED_AFTER:
            attempt(0)  # the blocked copy came from the cache and is old: ask once more
    return out


def _src(page: dict) -> dict:
    d = {"provider": PROVIDER, "url": page["url"], "retrieved_at": page["retrieved_at"],
         "available": bool(page["ok"])}
    if page.get("final_url") and page["final_url"] != page["url"]:
        d["final_url"] = page["final_url"]
    if not page["ok"]:
        d["reason"] = page.get("reason")
    return d


def _record(symbol: str, kind: str) -> dict:
    return {"provider": PROVIDER, "kind": kind, "symbol": symbol,
            "zacks_symbol": zacks_symbol(symbol), "available": False,
            "unavailable_reason": None, "source": None, "retrieved_at": None,
            "sources": [], "warnings": []}


def _attach(rec: dict, page: dict, *, primary: bool = False) -> None:
    rec["sources"].append(_src(page))
    if primary or rec["source"] is None:
        rec["source"] = f"{PROVIDER} {page.get('final_url') or page['url']}"
        rec["retrieved_at"] = page["retrieved_at"]


def _fail(rec: dict, page: dict, reason: str | None = None) -> dict:
    """Mark the record's primary page as unavailable."""
    page = dict(page, ok=False, reason=reason or page.get("reason") or "unavailable")
    _attach(rec, page, primary=True)
    rec["available"] = False
    rec["unavailable_reason"] = page["reason"]
    return rec


# ----------------------------------------------------------------------------
# quote-feed JSON
# ----------------------------------------------------------------------------
def _feed(zsym: str, max_age: float | None = None) -> tuple[dict | None, dict]:
    """-> (feed dict for the ticker | None, page meta). None when unavailable/invalid."""
    page = _http(URLS["feed"].format(sym=zsym), expect="json", max_age=max_age)
    if not page["ok"]:
        return None, page
    try:
        obj = json.loads(page["text"])
    except ValueError:
        page.update(ok=False, reason="quote feed did not return JSON (possible challenge page)")
        return None, page
    d = obj.get(zsym) if isinstance(obj, dict) else None
    if not isinstance(d, dict):
        page.update(ok=False, reason="quote feed has no entry for this ticker")
        return None, page
    if str(d.get("error", "")).lower() == "true":
        page.update(ok=False, reason=f"quote feed: {d.get('reason') or 'error'}")
        return None, page
    if str(d.get("ticker") or "").upper() != zsym:
        page.update(ok=False, reason="quote feed returned a different ticker")
        return None, page
    return d, page


def _is_invalid_ticker(feed_page: dict) -> bool:
    return "invalid ticker" in str(feed_page.get("reason") or "").lower()


# ----------------------------------------------------------------------------
# Stock page fetch: soup + ribbon (ticker check, Zacks Rank, Style Scores, ...)
# ----------------------------------------------------------------------------
_RANK_LABELS = {1: "Strong Buy", 2: "Buy", 3: "Hold", 4: "Sell", 5: "Strong Sell"}
_RANK_POS_RE = re.compile(r"(Top|Bottom)\s+(\d+)\s*%\s*\(\s*(\d+)\s+out\s+of\s+(\d+)\s*\)", re.I)


def _rank_position(text: str) -> dict:
    """'Top 21% (51 out of 247)' -> parts (all None when it does not match)."""
    m = _RANK_POS_RE.search(text or "")
    if not m:
        return {"rank": None, "total": None, "position": None, "pct": None, "text": text or None}
    return {"rank": int(m.group(3)), "total": int(m.group(4)),
            "position": m.group(1).capitalize(), "pct": int(m.group(2)),
            "text": re.sub(r"\s+", " ", m.group(0))}


def _parse_ribbon(soup) -> dict | None:
    """The header ribbon every /stock/ page carries. None when it is absent."""
    rib = soup.find("section", id="quote_ribbon_v2")
    if rib is None:
        return None
    out: dict = {"ticker": None, "company_name": None}
    h1 = rib.find("h1")
    head = _txt(h1)
    m = re.match(r"^(.*?)\s*\(\s*([A-Za-z0-9.\-]+)\s*\)\s*$", head)
    if m:
        out["company_name"], out["ticker"] = m.group(1).strip(), m.group(2).upper()
    h2 = rib.find("h2")
    out["exchange_note"] = _txt(h2).strip("()") or None
    out["last_price"] = parse_number(_txt(rib.find(id="get_last_price")))
    out["volume"] = parse_number(_txt(rib.find(id="get_volume")))
    chg = _txt(rib.find(id="net_change"))
    m = re.match(r"^([+-]?[\d.,]+)\s*\(\s*([+-]?[\d.,]+)\s*%\s*\)$", chg)
    out["net_change"] = parse_number(m.group(1)) if m else None
    out["net_change_pct"] = parse_number(m.group(2)) if m else None
    ts = _txt(rib.find(id="timestamp"))
    out["price_updated"] = (ts + " ET") if ts else None

    # --- rank boxes: locate each by its title, never by position --------------
    rank = {"zacks_rank": None, "zacks_rank_label": None}
    styles = {"value": None, "growth": None, "momentum": None, "vgm": None}
    ind = {"industry": None, "industry_url": None}
    ind_rank = _rank_position("")
    for box in rib.select(".zr_rankbox"):
        title = _norm(_txt(box.select_one(".rank_title")))
        views = box.select("p.rank_view")
        if title.startswith("zacks rank"):
            for v in views:
                m = re.match(r"^([1-5])\s*-\s*(Strong Buy|Buy|Hold|Sell|Strong Sell)\b", _txt(v))
                if m and _RANK_LABELS[int(m.group(1))] == m.group(2):
                    rank = {"zacks_rank": int(m.group(1)), "zacks_rank_label": m.group(2)}
        elif title.startswith("style scores"):
            for v in views:
                t = _txt(v)
                for key, word in (("value", "Value"), ("growth", "Growth"),
                                  ("momentum", "Momentum"), ("vgm", "VGM")):
                    m = re.search(r"(?:^|[\s|])(NA|[A-DF])\s+" + word + r"\b", t)
                    if m and m.group(1) != "NA":
                        styles[key] = m.group(1)
        elif title.startswith("industry rank"):
            for v in views:
                a = v.find("a")
                t = _txt(v)
                if _RANK_POS_RE.search(t):
                    ind_rank = _rank_position(t)
                    if a is not None and a.get("href"):
                        ind["industry_url"] = urljoin(WWW, a["href"])
                m = re.match(r"^Industry:\s*(.+)$", t)
                if m:
                    ind["industry"] = m.group(1).strip()
                    if a is not None and a.get("href"):
                        ind["industry_url"] = urljoin(WWW, a["href"])
    out.update(rank)
    out["style_scores"] = styles
    out.update(ind)
    out["industry_rank"] = ind_rank["rank"]
    out["industry_rank_total"] = ind_rank["total"]
    out["industry_rank_position"] = ind_rank["position"]
    out["industry_rank_pct"] = ind_rank["pct"]
    out["industry_rank_text"] = ind_rank["text"]
    return out


def _stock_page(zsym: str, url: str, required_selector: str,
                max_age: float | None = None) -> tuple[BeautifulSoup | None, dict | None, dict]:
    """Fetch a /stock/ page and make sure it is the real page for this ticker.

    -> (soup | None, ribbon | None, page meta). soup is None when unavailable.
    Tooltip pop-ups are removed from the soup (they hold sample values such as
    'Value Score A' that must never be read as data); the earnings-date tooltip
    text is preserved in page["earnings_date_tooltip"].
    """
    page = _http(url, expect="html", max_age=max_age)
    if not page["ok"]:
        return None, None, page
    soup = BeautifulSoup(page["text"], "lxml")
    tips = {}
    for tid in ("exp_earning_date_pop", "genl_next_eps_pop"):
        el = soup.find(id=tid)
        if el is not None:
            tips[tid] = _txt(el)
    page["earnings_date_tooltip"] = tips.get("exp_earning_date_pop") or tips.get("genl_next_eps_pop")
    for el in soup.select(".tooltiptext, .tooltip-wrapper, script, style, noscript"):
        el.decompose()
    ribbon = _parse_ribbon(soup)
    if ribbon is None or not ribbon.get("ticker"):
        page.update(ok=False, text=None,
                    reason="unexpected page (no Zacks quote ribbon) -- possible challenge, "
                           "login wall or layout change")
        return None, None, page
    if ribbon["ticker"] != zsym:
        page.update(ok=False, text=None,
                    reason=f"page is for {ribbon['ticker']}, not {zsym}")
        return None, None, page
    if required_selector and soup.select_one(required_selector) is None:
        page.update(ok=False, text=None,
                    reason=f"expected section '{required_selector}' missing -- possible login "
                           "wall or layout change")
        return None, None, page
    return soup, ribbon, page


def _table(table) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """-> (header texts, [(row label, [cell texts])])."""
    headers: list[str] = []
    thead = table.find("thead")
    if thead is not None and thead.find("tr") is not None:
        headers = [_txt(c) for c in thead.find("tr").find_all(["th", "td"])]
    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if cells:
            rows.append((_txt(cells[0]), [_txt(c) for c in cells[1:]]))
    return headers, rows


def _dl_pairs(section) -> dict[str, str]:
    """<dl><dt>label</dt><dd>value</dd></dl> blocks -> {normalised label: value text}."""
    out: dict[str, str] = {}
    if section is None:
        return out
    for dl in section.find_all("dl"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if dt is None or dd is None:
            continue
        label = _norm(_txt(dt))
        if label and label not in out:
            out[label] = _txt(dd)
    return out


def _earnings_date(raw: str) -> dict:
    """'*AMC 11/17/26' -> {'date': '2026-11-17', 'time': 'AMC'}."""
    out = {"date": None, "time": None, "raw": raw or None}
    if not raw:
        return out
    m = re.search(r"\*\s*(AMC|BMO)", raw, re.I)
    if m:
        out["time"] = m.group(1).upper()
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", raw)
    if m:
        out["date"] = _iso_date(m.group(1))
    return out


def _confirmed(tooltip: str | None) -> bool | None:
    t = (tooltip or "").lower()
    if "confirmed date" in t:
        return True
    if "estimated date" in t or "expected date" in t or "projected date" in t:
        return False
    return None


# ----------------------------------------------------------------------------
# Industry / sector pages (cached by fetch; shared by every stock in the industry)
# ----------------------------------------------------------------------------
def _industry_sector(industry_url: str, max_age: float | None = None) -> tuple[dict, list[dict]]:
    """industry page -> sector name/link -> sector page -> Sector Rank."""
    out = {"sector": None, "sector_url": None, "sector_rank": None, "sector_rank_total": None,
           "sector_rank_position": None, "sector_rank_pct": None, "sector_rank_text": None}
    pages = []
    page = _http(industry_url, expect="html", max_age=max_age)
    pages.append(page)
    if not page["ok"]:
        return out, pages
    soup = BeautifulSoup(page["text"], "lxml")
    a = soup.select_one("#quote_ribbon_v2 p.ribbon_sector_name a")
    if a is None or not a.get("href"):
        page.update(ok=False, reason="industry page has no sector link (layout change?)")
        return out, pages
    out["sector"] = _txt(a) or None
    out["sector_url"] = urljoin(WWW, a["href"])
    spage = _http(out["sector_url"], expect="html", max_age=max_age)
    pages.append(spage)
    if not spage["ok"]:
        return out, pages
    ssoup = BeautifulSoup(spage["text"], "lxml")
    rib = ssoup.find("section", id="quote_ribbon_v2")
    name = _txt(rib.select_one("h1.sector_name")) if rib is not None else ""
    box = rib.select_one("div.industry_rank") if rib is not None else None
    if box is None or _norm(name) != _norm(out["sector"] or ""):
        spage.update(ok=False, reason="sector page does not match / has no Sector Rank block")
        return out, pages
    for el in box.select(".tooltiptext, .tooltip-wrapper"):
        el.decompose()
    t = _txt(box)
    if not t.lower().startswith("sector rank"):
        spage.update(ok=False, reason="sector page rank block is not labelled 'Sector Rank'")
        return out, pages
    pos = _rank_position(t)
    out.update(sector_rank=pos["rank"], sector_rank_total=pos["total"],
               sector_rank_position=pos["position"], sector_rank_pct=pos["pct"],
               sector_rank_text=pos["text"] if pos["rank"] is not None else None)
    return out, pages


# ----------------------------------------------------------------------------
# 1. quote()
# ----------------------------------------------------------------------------
def quote(symbol: str, *, with_sector: bool = True, with_estimates_summary: bool = True,
          max_age: float | None = None) -> dict:
    """Zacks quote page + quote-feed JSON (+ industry/sector pages, + estimates summary box).

    with_sector            also fetch the industry and sector pages for the Sector Rank
    with_estimates_summary also read 'Last EPS Surprise', 'EPS Last Quarter', 'ABR' and
                           'EPS (TTM)' from the detailed-estimates page (the quote page
                           itself does not show the last surprise)
    """
    rec = _record(symbol, "quote")
    zsym = rec["zacks_symbol"]
    rec["definitions"] = {k: DEFINITIONS[k] for k in (
        "zacks_rank", "style_scores", "industry_rank", "sector_rank", "forward_pe_f1",
        "peg_ratio", "exp_eps_growth_3_5yr_pct", "earnings_esp_pct", "last_eps_surprise_pct")}

    feed, fpage = _feed(zsym, max_age)
    if _is_invalid_ticker(fpage):
        rec["sources"].append(_src(fpage))
        return _fail(rec, {"url": URLS["quote"].format(sym=zsym), "final_url": None,
                           "retrieved_at": fpage["retrieved_at"]},
                     "ticker unknown to Zacks (quote feed: Invalid ticker); page not requested")

    soup, rib, page = _stock_page(zsym, URLS["quote"].format(sym=zsym),
                                  "#stock_key_earnings", max_age)
    if soup is None:
        rec["sources"].append(_src(fpage))
        return _fail(rec, page)
    _attach(rec, page, primary=True)
    rec["sources"].append(_src(fpage))
    rec["available"] = True
    w = rec["warnings"]

    # --- ribbon ---------------------------------------------------------------
    rec["company_name"] = rib["company_name"]
    rec["exchange_note"] = rib["exchange_note"]
    rec["last_price"] = rib["last_price"]
    rec["price_updated"] = rib["price_updated"]
    rec["net_change"] = rib["net_change"]
    rec["net_change_pct"] = rib["net_change_pct"]
    rec["volume"] = rib["volume"]
    rec["zacks_rank"] = rib["zacks_rank"]
    rec["zacks_rank_label"] = rib["zacks_rank_label"]
    rec["style_scores"] = rib["style_scores"]
    for k in ("industry", "industry_url", "industry_rank", "industry_rank_total",
              "industry_rank_position", "industry_rank_pct", "industry_rank_text"):
        rec[k] = rib[k]

    # --- Stock Activity -------------------------------------------------------
    act = _dl_pairs(soup.find("section", id="stock_activity"))
    rec["open"] = parse_number(act.get("open"))
    for key, label in (("day", "day low / high"), ("wk52", "52-wk low / high")):
        m = re.fullmatch(r"\s*([\d.,]+)\s*-\s*([\d.,]+)\s*", act.get(label, ""))
        rec[f"{key}_low"] = parse_number(m.group(1)) if m else None
        rec[f"{key}_high"] = parse_number(m.group(2)) if m else None
    rec["avg_volume_20d"] = parse_number(act.get("20-day avg vol"))
    rec["avg_volume_label"] = "20-Day Avg Vol (shares)"
    rec["market_cap"] = parse_number(act.get("market cap"))
    rec["market_cap_text"] = act.get("market cap") or None
    rec["market_cap_label"] = "Market Cap (USD; quote page shows it rounded, e.g. '3,924.69 B')"
    div = act.get("dividend", "")
    m = re.fullmatch(r"\s*([\d.,]+)\s*\(\s*([\d.,]+)\s*%\s*\)\s*", div)
    rec["dividend_annual"] = parse_number(m.group(1)) if m else None
    rec["dividend_yield_pct"] = parse_number(m.group(2)) if m else None
    rec["beta"] = parse_number(act.get("beta"))
    if not act:
        w.append("'Stock Activity' block not found on quote page")

    # --- Key Earnings Data ----------------------------------------------------
    ke = _dl_pairs(soup.find("section", id="stock_key_earnings"))
    rec["earnings_esp_pct"] = parse_number(ke.get("earnings esp"))
    rec["most_accurate_estimate_current_qtr"] = parse_number(ke.get("most accurate est"))
    rec["consensus_estimate_current_qtr"] = parse_number(ke.get("current qtr est"))
    rec["consensus_estimate_current_yr"] = parse_number(ke.get("current yr est"))
    rec["prior_year_eps"] = parse_number(ke.get("prior year eps"))
    rec["next_year_eps_growth_pct"] = parse_number(ke.get("next year eps growth"))
    rec["exp_eps_growth_3_5yr_pct"] = parse_number(ke.get("exp eps growth (3-5yr)"))
    rec["forward_pe_f1"] = parse_number(ke.get("forward pe"))
    rec["forward_pe_label"] = "Forward PE = P/E (F1): price / current-fiscal-year consensus EPS"
    rec["peg_ratio"] = parse_number(ke.get("peg ratio"))
    rec["peg_ratio_label"] = "PEG Ratio = P/E (F1) / Exp EPS Growth (3-5yr)  [forward, long-term growth]"
    ed_label = "earnings date" if "earnings date" in ke else (
        "exp earnings date" if "exp earnings date" in ke else None)
    ed = _earnings_date(ke.get(ed_label, "") if ed_label else "")
    rec["next_earnings_date"] = ed["date"]
    rec["next_earnings_time"] = ed["time"]
    rec["next_earnings_time_label"] = "AMC = after market close, BMO = before market open"
    rec["next_earnings_date_label"] = {"earnings date": "Earnings Date",
                                       "exp earnings date": "Exp Earnings Date"}.get(ed_label)
    tip = page.get("earnings_date_tooltip")
    rec["next_earnings_confirmed"] = _confirmed(tip) if ed["date"] else None
    rec["next_earnings_date_note"] = ((tip.split(".")[0].strip() + ".")
                                      if tip and ed["date"] else None)
    for label in ("earnings esp", "most accurate est", "current qtr est", "current yr est",
                  "prior year eps", "next year eps growth", "exp eps growth (3-5yr)",
                  "forward pe", "peg ratio"):
        if label not in ke:
            w.append(f"label '{label}' not found in 'Key Earnings Data' (layout change?)")
    if ed_label is None:
        w.append("no 'Earnings Date' label in 'Key Earnings Data'")

    # --- quote-feed JSON ------------------------------------------------------
    f: dict = {"available": feed is not None}
    if feed is None:
        f["unavailable_reason"] = fpage.get("reason")
    else:
        sg = (feed.get("source") or {}).get("sungard") or {}
        f.update({
            "last": parse_number(feed.get("last")),
            "updated": (str(feed.get("updated")) + " ET") if feed.get("updated") else None,
            "market_status": feed.get("market_status") or None,
            "net_change": parse_number(feed.get("net_change")),
            "percent_net_change": _round(parse_number(feed.get("percent_net_change"))),
            "volume": parse_number(feed.get("volume")),
            "prior_session_close": parse_number(sg.get("close")),
            "prior_session_close_label": "feed source.sungard.close = close of the session "
                                         "BEFORE the one in 'last' (last - net_change)",
            "market_cap": parse_number(sg.get("market_cap")),
            "market_cap_label": "feed source.sungard.market_cap, USD (unrounded)",
            "zacks_rank": parse_int(feed.get("zacks_rank")),
            "zacks_rank_label": feed.get("zacks_rank_text") or None,
            "pe_f1": parse_number(feed.get("pe_f1")),
            "pe_f1_label": "feed pe_f1 -- may be computed on the prior session's close; "
                           "prefer the quote page 'Forward PE'",
            "eps_ttm": parse_number(sg.get("earnings")),
            "dividend_yield_pct": parse_number(feed.get("dividend_yield")),
            "confirmed_reporting_date": _iso_date(feed.get("confirmed_reporting_date")),
            "expected_reporting_date": _epoch_date(feed.get("expected_reporting_date")),
            "name": feed.get("name") or None,
            "exchange_note": feed.get("exchange") or None,
            "ticker_type": feed.get("ticker_type") or None,
        })
        if f["zacks_rank"] is not None and f["zacks_rank_label"] != _RANK_LABELS.get(f["zacks_rank"]):
            w.append("quote feed zacks_rank number and text disagree")
        if f["zacks_rank"] != rec["zacks_rank"]:
            w.append(f"Zacks Rank differs: quote page {rec['zacks_rank']} vs feed {f['zacks_rank']}")
        if (f["last"] is not None and rec["last_price"] is not None
                and abs(f["last"] - rec["last_price"]) > 0.011):
            w.append(f"last price differs: quote page {rec['last_price']} vs feed {f['last']}")
        if (f["expected_reporting_date"] and rec["next_earnings_date"]
                and f["expected_reporting_date"] != rec["next_earnings_date"]):
            w.append("next earnings date differs: quote page "
                     f"{rec['next_earnings_date']} vs feed {f['expected_reporting_date']}")
        if (f["market_cap"] and rec["market_cap"]
                and abs(f["market_cap"] / rec["market_cap"] - 1) > 0.05):
            w.append(f"market cap differs: quote page {rec['market_cap']:.0f} vs feed "
                     f"{f['market_cap']:.0f} (the feed figure appears to count only the listed "
                     "share class for multi-class companies / ADRs; prefer the quote page)")
        if rec["next_earnings_confirmed"] is True and not f["confirmed_reporting_date"]:
            w.append("quote page tooltip says the earnings date is confirmed but the feed's "
                     "confirmed_reporting_date is empty")
    rec["feed"] = f

    # --- sector rank (industry page -> sector page) ---------------------------
    sector = {"sector": None, "sector_url": None, "sector_rank": None, "sector_rank_total": None,
              "sector_rank_position": None, "sector_rank_pct": None, "sector_rank_text": None}
    if with_sector and rec["industry_url"]:
        sector, pages = _industry_sector(rec["industry_url"], max_age)
        for p in pages:
            rec["sources"].append(_src(p))
            if not p["ok"]:
                w.append(f"sector lookup page unavailable: {p['url']} ({p.get('reason')})")
    rec.update(sector)

    # --- summary box of the detailed-estimates page ---------------------------
    rec["last_eps_surprise_pct"] = None
    rec["eps_last_quarter"] = None
    rec["eps_ttm"] = None
    rec["abr"] = None
    if with_estimates_summary:
        esoup, _erib, epage = _stock_page(zsym, URLS["estimates"].format(sym=zsym),
                                          "#detailed_estimate", max_age)
        rec["sources"].append(_src(epage))
        if esoup is None:
            w.append(f"detailed-estimates page unavailable ({epage.get('reason')}): "
                     "last_eps_surprise_pct / eps_last_quarter / abr not filled")
        else:
            s = _estimates_summary(esoup)
            rec["last_eps_surprise_pct"] = s["last_eps_surprise_pct"]
            rec["eps_last_quarter"] = s["eps_last_quarter"]
            rec["eps_ttm"] = s["eps_ttm"]
            rec["abr"] = s["abr"]
            rec["abr_label"] = "ABR as printed in the detailed-estimates summary box (1-5, lower = better)"

    # --- arithmetic cross-checks of the provider's own definitions ------------
    chk: dict = {"note": "derived by this module from the Zacks figures above; "
                         "for validating definitions only"}
    pe, g, peg = rec["forward_pe_f1"], rec["exp_eps_growth_3_5yr_pct"], rec["peg_ratio"]
    chk["peg_recomputed"] = _round(pe / g, 2) if pe is not None and g else None
    chk["peg_recomputed_formula"] = "forward_pe_f1 / exp_eps_growth_3_5yr_pct"
    chk["peg_matches_definition"] = (abs(chk["peg_recomputed"] - peg) <= max(0.03, 0.02 * abs(peg))
                                     if chk["peg_recomputed"] is not None and peg is not None
                                     else None)
    px, f1 = rec["last_price"], rec["consensus_estimate_current_yr"]
    chk["forward_pe_recomputed"] = _round(px / f1, 2) if px is not None and f1 else None
    chk["forward_pe_recomputed_formula"] = "last_price / consensus_estimate_current_yr"
    chk["forward_pe_matches_definition"] = (
        abs(chk["forward_pe_recomputed"] - pe) <= max(0.03, 0.02 * abs(pe))
        if chk["forward_pe_recomputed"] is not None and pe is not None else None)
    rec["checks"] = chk
    if chk["peg_matches_definition"] is False:
        w.append(f"PEG {peg} != P/E(F1) {pe} / 3-5yr growth {g} = {chk['peg_recomputed']}; "
                 "Zacks may have computed it on a different price/estimate snapshot")
    if rec["zacks_rank"] is None:
        w.append("no Zacks Rank shown for this ticker (unranked / not covered)")
    return rec


# ----------------------------------------------------------------------------
# 3. estimates()
# ----------------------------------------------------------------------------
_PERIODS = {"current qtr": "current_qtr", "next qtr": "next_qtr",
            "current year": "current_yr", "next year": "next_yr",
            # headers Zacks prints for tickers without estimates: 'This Quarter (ND)' ...
            "this quarter": "current_qtr", "next quarter": "next_qtr",
            "this year": "current_yr"}


def _period(header: str) -> tuple[str, str | None]:
    """'Current Qtr (10/2026)' -> ('current_qtr', '10/2026')."""
    m = re.match(r"^(.*?)\s*\(\s*(\d{1,2}/\d{4}|ND|NA)\s*\)\s*$", header)
    name = _norm(m.group(1) if m else header)
    period = m.group(2) if m and "/" in m.group(2) else None
    return _PERIODS.get(name, _slug(name)), period


def _estimates_summary(soup) -> dict:
    """The two small 'EPS Estimates' boxes at the top of the detailed-estimates page."""
    vals: dict[str, str] = {}
    sec = soup.find("section", id="detailed_estimate")
    for div in (sec.select("div.two_col") if sec is not None else []):
        for tr in div.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) == 2:
                vals.setdefault(_norm(_txt(cells[0])), _txt(cells[1]))
    ed_label = "earnings date" if "earnings date" in vals else (
        "exp earnings date" if "exp earnings date" in vals else None)
    ed = _earnings_date(vals.get(ed_label, "") if ed_label else "")
    return {
        "next_earnings_date": ed["date"],
        "next_earnings_time": ed["time"],
        "current_quarter_estimate": parse_number(vals.get("current quarter")),
        "eps_last_quarter": parse_number(vals.get("eps last quarter")),
        "last_eps_surprise_pct": parse_number(vals.get("last eps surprise")),
        "abr": parse_number(vals.get("abr")),
        "earnings_esp_pct": parse_number(vals.get("earnings esp")),
        "current_year_estimate": parse_number(vals.get("current year")),
        "next_year_estimate": parse_number(vals.get("next year")),
        "eps_ttm": parse_number(vals.get("eps (ttm)")),
        "pe_f1": parse_number(vals.get("p/e (f1)")),
        "labels_found": sorted(vals),
    }


_EST_ROWS = {
    "zacks consensus estimate": ("zacks_consensus_estimate", parse_number),
    "# of estimates": ("num_estimates", parse_int),
    "most recent consensus": ("most_recent_consensus", parse_number),
    "high estimate": ("high_estimate", parse_number),
    "low estimate": ("low_estimate", parse_number),
    "year ago eps": ("year_ago_eps", parse_number),
    "year ago sales": ("year_ago_sales", parse_number),
    "year over year growth est.": ("yoy_growth_est_pct", parse_number),
}


def _period_table(table, row_map: dict | None = None, value=parse_number) -> dict:
    """Table whose columns are Current Qtr / Next Qtr / Current Year / Next Year."""
    headers, rows = _table(table)
    cols = [_period(h) for h in headers[1:]]
    out: dict = {key: {"period_label": headers[i + 1], "period_end": pe}
                 for i, (key, pe) in enumerate(cols)}
    for label, cells in rows:
        if row_map is not None:
            if _norm(label) not in row_map:
                continue
            name, fn = row_map[_norm(label)]
        else:
            name, fn = _slug(label), value
        if not name:
            continue
        for (key, _pe), cell in zip(cols, cells):
            out[key][name] = fn(cell)
    return out


def estimates(symbol: str, *, max_age: float | None = None) -> dict:
    """https://www.zacks.com/stock/quote/{SYM}/detailed-earning-estimates"""
    rec = _record(symbol, "estimates")
    zsym = rec["zacks_symbol"]
    url = URLS["estimates"].format(sym=zsym)
    _feed_d, fpage = _feed(zsym, max_age)
    if _is_invalid_ticker(fpage):
        rec["sources"].append(_src(fpage))
        return _fail(rec, {"url": url, "final_url": None, "retrieved_at": fpage["retrieved_at"]},
                     "ticker unknown to Zacks (quote feed: Invalid ticker); page not requested")
    soup, _rib, page = _stock_page(zsym, url, "#detailed_estimate", max_age)
    if soup is None:
        return _fail(rec, page)
    _attach(rec, page, primary=True)
    rec["available"] = True
    w = rec["warnings"]
    rec["definitions"] = {
        "eps_estimates": "Zacks Consensus Estimate of EPS (before non-recurring items) per period; "
                         "'Current Qtr'/'Next Qtr' = fiscal quarters, 'Current Year' (F1) / 'Next "
                         "Year' (F2) = fiscal years; period_end is fiscal month/year.",
        "sales_estimates": "Zacks consensus sales, converted to plain USD (Zacks prints e.g. 108.92B).",
        "growth_estimates": "'% EPS Growth Estimates' table: percent growth for the stock, its "
                            "Zacks industry (IND) and the S&P 500. 'Next 5 Years' = long-term "
                            "(3-5 yr) expected annual EPS growth; 'Past 5 Years' = historical "
                            "annual EPS growth. 'PE' = P/E (F1); 'PEG Ratio' = P/E (F1) / Next 5 "
                            "Years growth.",
        "agreement": "number of analysts' estimate revisions up / down in the last 7, 30, 60 days",
        "magnitude": "Zacks consensus EPS now vs 7 / 30 / 60 / 90 days ago",
        "upside": "Most Accurate Estimate vs Zacks Consensus Estimate and the resulting Earnings ESP %",
        "surprise_history": "last four reported quarters: reported EPS, Zacks consensus estimate, "
                            "difference and surprise % (most recent first)",
        "abr": DEFINITIONS["abr"],
        "last_eps_surprise_pct": DEFINITIONS["last_eps_surprise_pct"],
    }

    summ = _estimates_summary(soup)
    labels_found = summ.pop("labels_found")
    tip = page.get("earnings_date_tooltip")
    summ["next_earnings_confirmed"] = _confirmed(tip) if summ["next_earnings_date"] else None
    rec["summary"] = summ
    for label in ("current quarter", "eps last quarter", "last eps surprise", "abr",
                  "earnings esp", "current year", "next year", "eps (ttm)", "p/e (f1)"):
        if label not in labels_found:
            w.append(f"label '{label}' not found in the estimates summary box (layout change?)")

    # --- % EPS Growth Estimates ----------------------------------------------
    growth: dict = {}
    gt = soup.select_one("#earnings_growth_estimates table")
    if gt is None:
        w.append("'% EPS Growth Estimates' table not found")
    else:
        headers, rows = _table(gt)
        colmap = []
        for h in headers[1:]:
            hn = _norm(h)
            colmap.append("stock" if hn == zsym.lower() else "industry" if hn == "ind"
                          else "sp500" if hn in ("s&p", "s&p 500") else None)
        if "stock" not in colmap:
            w.append(f"growth table has no '{zsym}' column (headers: {headers}); table ignored")
        else:
            for label, cells in rows:
                key, pe = _period(label)
                key = {"past_5_years": "past_5_years", "next_5_years": "next_5_years",
                       "pe": "pe_f1", "peg_ratio": "peg_ratio"}.get(key, key)
                row = {"label": label, "period_end": pe}
                for name, cell in zip(colmap, cells):
                    if name:
                        row[name] = parse_number(cell)
                growth[key] = row
    rec["growth_estimates"] = growth
    rec["growth_estimates_units"] = ("percent for the growth rows; plain ratios for pe_f1 and "
                                     "peg_ratio")
    rec["long_term_growth_next_5yr_pct"] = (growth.get("next_5_years") or {}).get("stock")
    rec["past_5yr_eps_growth_pct"] = (growth.get("past_5_years") or {}).get("stock")
    rec["peg_ratio"] = (growth.get("peg_ratio") or {}).get("stock")
    rec["peg_ratio_label"] = "PEG Ratio = P/E (F1) / Next 5 Years growth [forward, long-term growth]"
    rec["pe_f1"] = (growth.get("pe_f1") or {}).get("stock")

    # --- Sales / Earnings estimate tables ------------------------------------
    rec["eps_estimates"], rec["sales_estimates"] = {}, {}
    for sec in soup.find_all("section", id="detailed_earnings_estimates"):
        title = _norm(_txt(sec.find(["h1", "h2", "h3"])))
        table = sec.find("table")
        if table is None:
            continue
        if title.startswith("earnings estimates"):
            rec["eps_estimates"] = _period_table(table, _EST_ROWS)
        elif title.startswith("sales estimates"):
            rec["sales_estimates"] = _period_table(table, _EST_ROWS)
    if not rec["eps_estimates"]:
        w.append("'Earnings Estimates' table not found")
    rec["sales_estimates_units"] = "USD"

    # --- Agreement / Magnitude / Upside --------------------------------------
    def section_table(sec_id):
        sec = soup.find("section", id=sec_id)
        return sec.find("table") if sec is not None else None

    t = section_table("agreement_estimate")
    rec["agreement"] = _period_table(t, value=parse_int) if t is not None else {}
    t = section_table("magnitude_estimate")
    rec["magnitude"] = _period_table(t) if t is not None else {}
    t = section_table("quote_upside")
    up = _period_table(t) if t is not None else {}
    for v in up.values():
        if "earnings_esp" in v:
            v["earnings_esp_pct"] = v.pop("earnings_esp")
    rec["upside"] = up
    for name in ("agreement", "magnitude", "upside"):
        if not rec[name]:
            w.append(f"'{name}' table not found")

    # --- Surprise history ----------------------------------------------------
    hist, avg = [], {"reported": None, "estimate": None, "difference": None, "surprise_pct": None}
    t = section_table("surprised_reported")
    if t is None:
        w.append("'Surprise - Reported Earnings History' table not found")
    else:
        headers, rows = _table(t)
        cols = []
        for h in headers[1:]:
            m = re.match(r"^Quarter Ending\s*\(\s*(\d{1,2}/\d{4})\s*\)$", h)
            cols.append(m.group(1) if m else ("average" if _norm(h).startswith("average") else None))
        entries = [{"quarter_ending": c, "reported": None, "estimate": None,
                    "difference": None, "surprise_pct": None} for c in cols]
        names = {"reported": "reported", "estimate": "estimate", "difference": "difference",
                 "surprise": "surprise_pct"}
        for label, cells in rows:
            name = names.get(_norm(label))
            if not name:
                continue
            for entry, cell in zip(entries, cells):
                entry[name] = parse_number(cell)
        for e in entries:
            if e["quarter_ending"] == "average":
                avg = {k: e[k] for k in avg}
            elif e["quarter_ending"]:
                hist.append(e)
    rec["surprise_history"] = hist
    rec["surprise_average"] = avg
    rec["surprise_history_order"] = "most recent quarter first; quarter_ending is fiscal month/year"
    if hist and summ["last_eps_surprise_pct"] is not None and hist[0]["surprise_pct"] is not None \
            and abs(hist[0]["surprise_pct"] - summ["last_eps_surprise_pct"]) > 0.011:
        w.append("summary 'Last EPS Surprise' differs from the first surprise-history column")
    return rec


# ----------------------------------------------------------------------------
# 2 + 4. broker_recs() / price_target()  (one Zacks page serves both)
# ----------------------------------------------------------------------------
_BR_COLS = {"today": "today", "1 week ago": "1_week_ago", "1 month ago": "1_month_ago",
            "2 months ago": "2_months_ago", "3 months ago": "3_months_ago"}
_BR_ROWS = {"strong buy": "strong_buy", "buy": "buy", "hold": "hold", "sell": "sell",
            "strong sell": "strong_sell", "abr": "abr"}
_CNT = r"(\d+|[A-Za-z]+)"


def _pt_page(zsym: str, max_age: float | None, need: str):
    """Fetch the page that carries both the price targets and the broker-rating table.

    Zacks 301-redirects /stock/research/{SYM}/brokerage-recommendations to
    /stock/research/{SYM}/price-target-stock-forecast, so that URL is requested
    first; the older URL is only tried when the expected block is missing.
    """
    tried = []
    for key in ("price_target", "broker_recs"):
        soup, rib, page = _stock_page(zsym, URLS[key].format(sym=zsym), need, max_age)
        tried.append(page)
        if soup is not None:
            return soup, rib, page, tried
        if not str(page.get("reason") or "").startswith("expected section"):
            break  # blocked / not found: a second URL would not help
    return None, None, tried[-1], tried


def _parse_price_targets(soup, w: list) -> dict:
    out = {"average_target": None, "high_target": None, "low_target": None, "upside_pct": None,
           "num_analysts": None, "last_close_used": None}
    table = soup.select_one("section.key-expected-earnings-data table")
    if table is not None:
        headers = [_norm(_txt(c)) for c in table.select("thead tr th, thead tr td")]
        body = table.select_one("tbody tr")
        cells = [_txt(c) for c in body.find_all(["th", "td"])] if body is not None else []
        m = dict(zip(headers, cells))
        out["average_target"] = parse_number(m.get("average price target"))
        out["high_target"] = parse_number(m.get("highest price target"))
        out["low_target"] = parse_number(m.get("lowest price target"))
        out["upside_pct"] = parse_number(m.get("upside to average price target"))
    else:
        w.append("price-target summary table not found")
    box = soup.select_one("section.price-target-box")
    text = _txt(box.select_one(".target-text-box p")) if box is not None else ""
    out["text"] = text or None
    m = re.search(r"price targets offered by " + _CNT + r" analysts?", text)
    n_text = _count_word(m.group(1)) if m else None
    h2 = _txt(box.select_one(".target-chart-box h2")) if box is not None else ""
    m = re.search(r"Analyst Price Targets \((\d+)\)", h2)
    n_head = int(m.group(1)) if m else None
    out["num_analysts"] = n_head if n_head is not None else n_text
    if n_head is not None and n_text is not None and n_head != n_text:
        w.append(f"analyst count differs: heading {n_head} vs text {n_text}")
    m = re.search(r"last closing price of \$([\d,]+(?:\.\d+)?)", text)
    out["last_close_used"] = parse_number(m.group(1)) if m else None
    # sign of the upside comes from the wording, the table prints it unsigned-or-signed
    m = re.search(r"represents (?:an?|a)\s+(increase|decline|decrease) of ([\d.,]+)%", text)
    if m and out["upside_pct"] is not None:
        mag = parse_number(m.group(2))
        if mag is not None and abs(abs(out["upside_pct"]) - mag) < 0.011:
            out["upside_pct"] = mag if m.group(1) == "increase" else -mag
    if out["num_analysts"] in (None, 0) or out["average_target"] is None:
        # "does not have any price target": the chart block then prints 0.00 placeholders
        out.update(average_target=None, high_target=None, low_target=None, upside_pct=None,
                   num_analysts=0 if "does not have any price target" in text else out["num_analysts"])
    return out


def _parse_faq(soup) -> dict:
    """The FAQ block restates the consensus -- sometimes from a different (newer) snapshot."""
    out = {"abr": None, "num_brokers": None, "average_target": None, "num_reports": None,
           "low_target": None, "high_target": None, "last_close_used": None, "upside_pct": None}
    faq = soup.select_one("section.quote-faq")
    if faq is None:
        return out
    money = r"\$(-?[\d,]+(?:\.\d+)?)"
    for seg in faq.select(".accordion-segment"):
        q = _norm(_txt(seg.select_one(".accordion-title")))
        a = _txt(seg.select_one(".accordion-content"))
        if "what is the recommendation for" in q:
            m = re.search(r"average brokerage recommendation \(ABR\) of ([\d.]+) on a scale of 1 to 5", a)
            out["abr"] = parse_number(m.group(1)) if m else None
            m = re.search(r"made by " + _CNT + r" brokerage firms?", a)
            out["num_brokers"] = _count_word(m.group(1)) if m else None
        elif "12 month price target" in q:
            m = re.search(r"The average price target for .{0,160}? is " + money + r"\.(?:\s|$)", a)
            out["average_target"] = parse_number(m.group(1)) if m else None
            m = re.search(r"is based on " + _CNT + r" reports?", a)
            out["num_reports"] = _count_word(m.group(1)) if m else None
        elif "forecast for" in q:
            m = re.search(r"range from a low of " + money + r" to a high of " + money + r"\.(?:\s|$)", a)
            if m:
                out["low_target"] = parse_number(m.group(1))
                out["high_target"] = parse_number(m.group(2))
            m = re.search(r"from the last closing price of " + money + r"\.?\s*$", a)
            out["last_close_used"] = parse_number(m.group(1)) if m else None
        elif "upside potential" in q:
            m = re.search(r"The current (UPSIDE|DOWNSIDE) for .{0,160}? is (-?[\d.,]+)\s*%", a)
            if m:
                v = parse_number(m.group(2))
                if v is not None:
                    out["upside_pct"] = -abs(v) if m.group(1) == "DOWNSIDE" else v
    return out


def price_target(symbol: str, *, max_age: float | None = None) -> dict:
    """https://www.zacks.com/stock/research/{SYM}/price-target-stock-forecast"""
    rec = _record(symbol, "price_target")
    zsym = rec["zacks_symbol"]
    url = URLS["price_target"].format(sym=zsym)
    feed, fpage = _feed(zsym, max_age)
    if _is_invalid_ticker(fpage):
        rec["sources"].append(_src(fpage))
        return _fail(rec, {"url": url, "final_url": None, "retrieved_at": fpage["retrieved_at"]},
                     "ticker unknown to Zacks (quote feed: Invalid ticker); page not requested")
    soup, rib, page, tried = _pt_page(zsym, max_age, "section.key-expected-earnings-data")
    if soup is None:
        for p in tried[:-1]:
            rec["sources"].append(_src(p))
        return _fail(rec, page)
    _attach(rec, page, primary=True)
    rec["available"] = True
    w = rec["warnings"]
    rec["definitions"] = {"price_target": DEFINITIONS["price_target"]}
    pt = _parse_price_targets(soup, w)
    rec.update({k: pt[k] for k in ("average_target", "high_target", "low_target", "upside_pct",
                                   "num_analysts", "last_close_used")})
    rec["upside_pct_label"] = ("'Upside to Average Price Target' as printed by Zacks: average "
                               "target vs last_close_used (NOT necessarily the latest close)")
    rec["currency"] = "USD"
    rec["headline_text"] = pt["text"]
    faq = _parse_faq(soup)
    rec["faq_snapshot"] = {
        "average_target": faq["average_target"], "low_target": faq["low_target"],
        "high_target": faq["high_target"], "num_reports": faq["num_reports"],
        "last_close_used": faq["last_close_used"], "upside_pct": faq["upside_pct"],
        "note": "figures restated in the page's FAQ block; Zacks sometimes renders it from a "
                "different snapshot than the headline box (compare last_close_used with the "
                "latest close to see which is fresher). FAQ low/high are printed without cents.",
    }
    rec["ribbon_last_price"] = rib["last_price"]
    rec["ribbon_price_updated"] = rib["price_updated"]
    derived = {"note": "computed by this module, not printed by Zacks"}
    px = rib["last_price"]
    for name, tgt in (("headline", rec["average_target"]), ("faq", faq["average_target"])):
        derived[f"upside_pct_{name}_avg_vs_ribbon_last_price"] = (
            _round((tgt / px - 1) * 100, 2) if tgt is not None and px else None)
    derived["formula"] = "(average_target / ribbon_last_price - 1) * 100"
    rec["derived"] = derived
    if rec["average_target"] is not None and faq["average_target"] is not None \
            and abs(rec["average_target"] - faq["average_target"]) > 0.011:
        w.append(f"headline average target {rec['average_target']} differs from the FAQ block's "
                 f"{faq['average_target']} (two snapshots on one page)")
    if rec["last_close_used"] is not None and px is not None \
            and abs(rec["last_close_used"] - px) > 0.011:
        w.append(f"headline upside is measured against {rec['last_close_used']}, not the latest "
                 f"price {px}; use derived.upside_pct_*_vs_ribbon_last_price for the latest close")
    if rec["num_analysts"] == 0:
        w.append("Zacks shows no analyst price targets for this ticker")
    return rec


def broker_recs(symbol: str, *, max_age: float | None = None) -> dict:
    """Average Broker Rating (ABR) and the recommendation counts behind it.

    Requested from https://www.zacks.com/stock/research/{SYM}/price-target-stock-forecast,
    which is where /stock/research/{SYM}/brokerage-recommendations redirects.
    """
    rec = _record(symbol, "broker_recs")
    zsym = rec["zacks_symbol"]
    url = URLS["price_target"].format(sym=zsym)
    _feed_d, fpage = _feed(zsym, max_age)
    if _is_invalid_ticker(fpage):
        rec["sources"].append(_src(fpage))
        return _fail(rec, {"url": url, "final_url": None, "retrieved_at": fpage["retrieved_at"]},
                     "ticker unknown to Zacks (quote feed: Invalid ticker); page not requested")
    soup, _rib, page, tried = _pt_page(zsym, max_age, "#quote_brokerage_recomm table")
    if soup is None:
        for p in tried[:-1]:
            rec["sources"].append(_src(p))
        return _fail(rec, page)
    _attach(rec, page, primary=True)
    rec["available"] = True
    rec["redirect_note"] = ("https://www.zacks.com/stock/research/{SYM}/brokerage-recommendations "
                            "redirects to the price-target-stock-forecast page, which carries the "
                            "'Brokerage Recommendations' table parsed here")
    w = rec["warnings"]
    rec["definitions"] = {"abr": DEFINITIONS["abr"]}
    rec["abr_scale"] = "1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell (lower is better)"

    # --- the table: columns are located by header text, rows by label ---------
    headers, rows = _table(soup.select_one("#quote_brokerage_recomm table"))
    cols = [_BR_COLS.get(_norm(h)) for h in headers[1:]]
    hist: dict = {c: {"strong_buy": None, "buy": None, "hold": None, "sell": None,
                      "strong_sell": None, "total": None, "abr": None}
                  for c in _BR_COLS.values()}
    seen_rows = set()
    for label, cells in rows:
        name = _BR_ROWS.get(_norm(label))
        if not name or len(cells) != len(cols):
            continue
        seen_rows.add(name)
        for col, cell in zip(cols, cells):
            if col:
                hist[col][name] = parse_number(cell) if name == "abr" else parse_int(cell)
    missing_cols = [c for c in _BR_COLS.values() if c not in cols]
    missing_rows = [r for r in _BR_ROWS.values() if r not in seen_rows]
    if missing_cols or missing_rows:
        w.append(f"broker table incomplete: missing columns {missing_cols}, rows {missing_rows}")
    for col, d in hist.items():
        counts = [d[k] for k in ("strong_buy", "buy", "hold", "sell", "strong_sell")]
        d["total"] = sum(counts) if all(c is not None for c in counts) else None
        if d["abr"] is not None and not 1.0 <= d["abr"] <= 5.0:
            w.append(f"ABR {d['abr']} ({col}) outside the 1-5 scale; set to None")
            d["abr"] = None
    rec["history"] = hist
    rec["history_columns"] = "today, 1_week_ago, 1_month_ago, 2_months_ago, 3_months_ago"
    rec["abr_current"] = hist["today"]["abr"]
    rec["abr_1_week_ago"] = hist["1_week_ago"]["abr"]
    rec["abr_1_month_ago"] = hist["1_month_ago"]["abr"]
    rec["abr_2_months_ago"] = hist["2_months_ago"]["abr"]
    rec["abr_3_months_ago"] = hist["3_months_ago"]["abr"]
    rec["num_brokers"] = hist["today"]["total"]
    rec["num_brokers_label"] = "sum of today's Strong Buy..Strong Sell counts in the table"
    for k in ("strong_buy", "buy", "hold", "sell", "strong_sell"):
        rec[f"{k}_count"] = hist["today"][k]

    # --- the prose next to it (independent restatement, used as a check) ------
    text = _txt(soup.select_one("#broker_rating .broker-text-box"))
    rec["text"] = text or None
    tx = {"abr": None, "num_brokers": None, "abr_1_month_ago": None, "num_brokers_1_month_ago": None}
    m = re.search(r"average brokerage recommendation \(ABR\) of ([\d.]+) on a scale of 1 to 5", text)
    tx["abr"] = parse_number(m.group(1)) if m else None
    m = re.search(r"made by " + _CNT + r" brokerage firms?", text)
    tx["num_brokers"] = _count_word(m.group(1)) if m else None
    m = re.search(r"compares to an ABR of ([\d.]+) a month ago based on " + _CNT
                  + r" recommendations?", text)
    if m:
        tx["abr_1_month_ago"] = parse_number(m.group(1))
        tx["num_brokers_1_month_ago"] = _count_word(m.group(2))
    rec["text_restatement"] = tx
    no_cov = "does not currently have any recommendation" in text
    rec["no_broker_coverage"] = bool(no_cov) if text else None
    faq = _parse_faq(soup)
    rec["faq_restatement"] = {"abr": faq["abr"], "num_brokers": faq["num_brokers"]}

    # --- consistency checks (the ABR < 2 gate depends on this number) ---------
    chk: dict = {"note": "computed by this module to validate the parse"}
    t = hist["today"]
    if t["total"]:
        chk["abr_from_counts"] = _round(
            (t["strong_buy"] + 2 * t["buy"] + 3 * t["hold"] + 4 * t["sell"]
             + 5 * t["strong_sell"]) / t["total"], 2)
    else:
        chk["abr_from_counts"] = None
    chk["abr_from_counts_formula"] = ("(1*SB + 2*B + 3*H + 4*S + 5*SS) / total; Zacks' own ABR can "
                                      "differ slightly because it averages fractional broker ratings")
    abr = rec["abr_current"]
    chk["table_vs_text_abr_match"] = (abs(abr - tx["abr"]) < 0.011
                                      if abr is not None and tx["abr"] is not None else None)
    chk["table_vs_faq_abr_match"] = (abs(abr - faq["abr"]) < 0.011
                                     if abr is not None and faq["abr"] is not None else None)
    chk["table_vs_text_num_brokers_match"] = (t["total"] == tx["num_brokers"]
                                              if t["total"] is not None and tx["num_brokers"] is not None
                                              else None)
    chk["abr_vs_counts_within_0_25"] = (abs(abr - chk["abr_from_counts"]) <= 0.25
                                        if abr is not None and chk["abr_from_counts"] is not None
                                        else None)
    flags = [chk[k] for k in ("table_vs_text_abr_match", "table_vs_faq_abr_match",
                              "table_vs_text_num_brokers_match", "abr_vs_counts_within_0_25")]
    chk["abr_consistent"] = (None if abr is None or all(f is None for f in flags)
                             else all(f is not False for f in flags))
    rec["checks"] = chk
    if chk["abr_consistent"] is False:
        w.append("ABR cross-checks disagree (see checks) -- treat abr_current with caution")
    if abr is None:
        w.append("no current ABR on the page (no broker coverage on Zacks)" if no_cov
                 else "current ABR missing / unparseable")

    pt = _parse_price_targets(soup, [])
    rec["average_target_price"] = pt["average_target"]
    rec["average_target_price_label"] = ("headline 'Average Price Target' on the same page; see "
                                         "price_target() for range, upside and snapshot caveats")
    rec["num_price_target_analysts"] = pt["num_analysts"]
    rec["industry_abr"] = None
    rec["industry_abr_note"] = ("not available: the redirected page no longer prints an industry "
                                "ABR, and the 'Industry Price Targets Comparison' table is filled "
                                "by JavaScript, not present in the HTML")
    return rec


# ----------------------------------------------------------------------------
# 5. fundamentals()
# ----------------------------------------------------------------------------
_FO_SERIES = {  # caption on the financial-overview page -> (key, unit)
    "roe": ("roe_pct", "percent, trailing 12 months"),
    "roa": ("roa_pct", "percent, trailing 12 months"),
    "current ratio": ("current_ratio", "ratio, most recent quarter"),
    "quick ratio": ("quick_ratio", "ratio, most recent quarter"),
    "operating margin": ("operating_margin_pct", "percent, trailing 12 months"),
    "net margin": ("net_margin_pct", "percent, trailing 12 months"),
    "pre-tax margin": ("pretax_margin_pct", "percent, trailing 12 months"),
    "book value": ("book_value_per_share", "USD per share, most recent quarter"),
    "inventory turnover": ("inventory_turnover", "ratio, trailing 12 months"),
    "debt-to-equity": ("debt_to_equity", "ratio (long-term debt / equity), most recent quarter"),
    "debt to capital": ("debt_to_capital_pct", "percent, most recent quarter"),
}
_FO_SCALARS = {  # (caption, row label) -> key
    ("p/e", "p/e (f1)"): "pe_f1",
    ("p/e", "trailing 12 months"): "pe_ttm",
    ("p/e", "peg ratio"): "peg_ratio",
    ("eps growth", "vs. previous year (q0/q-4)"): "eps_growth_vs_prev_year_q_pct",
    ("eps growth", "vs. previous quarter (q0/q-1)"): "eps_growth_vs_prev_quarter_pct",
    ("sales growth", "vs. previous year (q0/q-4)"): "sales_growth_vs_prev_year_q_pct",
    ("sales growth", "vs. previous quarter (q0/q-1)"): "sales_growth_vs_prev_quarter_pct",
    ("price ratios", "price/book"): "price_to_book",
    ("price ratios", "price/cash flow"): "price_to_cash_flow",
    ("price ratios", "price / sales"): "price_to_sales",
}
_MISSING_SENTINELS = {-99999.0, -9999.0, -999999.0}  # Zacks' "no data" placeholders in statements
# Balance-sheet rows that cannot be negative. Zacks sometimes leaks a corrupted placeholder
# into them (e.g. 'Cash & Equivalents -99,802' in a half-filled quarter): negative -> None.
_NONNEGATIVE_ROWS = {
    "Cash & Equivalents", "Receivables", "Inventories", "Total Current Assets", "Total Assets",
    "Notes Payable", "Accounts Payable", "Current Portion Long-Term Debt",
    "Total Current Liabilities", "Convertible Debt", "Long-Term Debt", "Total Liabilities",
    "Shares Outstanding",
}

# Chart series pulled by default: (wrapper name, output key, unit)
_CHART_METRICS = (
    ("free_cash_flow_ttm", "free_cash_flow_ttm", "USD millions, trailing 12 months"),
    ("free_cash_flow", "free_cash_flow_quarterly", "USD millions, single fiscal quarter"),
    ("eps_diluted_ttm", "eps_diluted_ttm",
     "USD per share, trailing 12 months (diluted, before non-recurring items)"),
    ("eps_diluted", "eps_diluted_quarterly",
     "USD per share, single fiscal quarter (diluted, before non-recurring items)"),
)
# Statement rows returned unless full_statements=True
_KEEP_ROWS = {
    "income_statement": (
        "Sales", "Income After Depreciation & Amortization",
        "Income After SG&A, R&D, and Dept/Amort Expenses", "Non-Operating Income",
        "Interest Expense", "Pretax Income", "Income Taxes", "Net Income (GAAP)",
        "Depreciation & Amortization (Cash Flow)", "Income Before Depreciation & Amortization",
        "Diluted EPS Before Non-Recurring Items", "Diluted Net EPS (GAAP)"),
    "balance_sheet": (
        "Cash & Equivalents", "Total Current Assets", "Total Assets", "Notes Payable",
        "Current Portion Long-Term Debt", "Total Current Liabilities", "Convertible Debt",
        "Long-Term Debt", "Total Liabilities", "Total Shareholder's Equity",
        "Shares Outstanding"),
    "cash_flow": (
        "Net Income (Loss)", "Depreciation/Amortization & Depletion",
        "Net Cash From Operating Activities", "Property & Equipment",
        "Issuance (Repayment) of Debt", "Payment of Dividends & Other Distributions"),
}


def _parse_financial_overview(soup, w: list) -> dict:
    sec = soup.find("section", id="financial_overview_details")
    scalars: dict = {k: None for k in _FO_SCALARS.values()}
    series: dict = {}
    for table in sec.find_all("table"):
        cap = _norm(_txt(table.find("caption")))
        _h, rows = _table(table)
        if cap in _FO_SERIES:
            key, unit = _FO_SERIES[cap]
            hist = []
            for label, cells in rows:
                d = _iso_date(label)
                if d is None or not cells:
                    continue
                v = parse_number(cells[0])
                hist.append({"period_end": d, "value": v,
                             "status": None if v is not None else (cells[0] or "NA")})
            latest = next((h for h in hist if h["value"] is not None), None)
            series[key] = {"label": _txt(table.find("caption")), "unit": unit,
                           "latest": latest["value"] if latest else None,
                           "latest_period_end": latest["period_end"] if latest else None,
                           "history": hist}
        else:
            for label, cells in rows:
                k = _FO_SCALARS.get((cap, _norm(label)))
                if k and cells:
                    scalars[k] = parse_number(cells[0])
    for cap, (key, unit) in _FO_SERIES.items():
        if key not in series:
            series[key] = {"label": None, "unit": unit, "latest": None,
                           "latest_period_end": None, "history": []}
            w.append(f"financial overview: table '{cap}' not found")
    return {"scalars": scalars, "series": series}


def _parse_statement(soup, section_id: str, kind: str, full: bool, w: list) -> dict:
    """Annual + quarterly tabs of an income-statement / balance-sheet / cash-flow page."""
    out: dict = {}
    sec = soup.find("section", id=section_id)
    for freq in ("annual", "quarterly"):
        div = None
        for d in sec.find_all("div", recursive=False):
            if str(d.get("id") or "").startswith(freq + "_"):
                div = d
                break
        blk = {"unit_note": None, "periods": [], "pending_periods": [], "rows": {}}
        out[freq] = blk
        if div is None:
            w.append(f"{kind}: '{freq}' tab not found")
            continue
        notes = [_txt(p) for p in div.find_all("p")]
        blk["unit_note"] = " ".join(n for n in notes if n) or None
        raw_rows: dict[str, list[str]] = {}
        periods: list[str | None] = []
        for table in div.find_all("table"):
            headers, rows = _table(table)
            p = [_iso_date(h) for h in headers[1:]]
            if not periods:
                periods = p
            elif p != periods:
                w.append(f"{kind} {freq}: tables disagree on the period columns; later table skipped")
                continue
            for label, cells in rows:
                if not label or len(cells) != len(periods) or not any(c.strip() for c in cells):
                    continue
                if _iso_date(label) or label in raw_rows:
                    continue
                raw_rows[label] = cells
        if not periods or any(p is None for p in periods):
            w.append(f"{kind} {freq}: could not read the period header")
            continue
        # a not-yet-reported period shows 'NA' in most rows (plus placeholder 0.00 / -99,999):
        # blank the whole column rather than keep the placeholders
        pending = []
        for i in range(len(periods)):
            col = [cells[i] for cells in raw_rows.values()]
            na = sum(1 for c in col if c.strip().upper() in ("NA", "N/A", ""))
            if col and na >= max(2, 0.5 * len(col)):
                pending.append(i)
        blk["periods"] = periods
        blk["pending_periods"] = [periods[i] for i in pending]
        keep = None if full else set(_KEEP_ROWS[kind])
        for label, cells in raw_rows.items():
            if keep is not None and label not in keep:
                continue
            vals = []
            for i, c in enumerate(cells):
                v = None if i in pending else parse_number(c)
                if v is not None and v in _MISSING_SENTINELS:
                    v = None
                if (v is not None and v < 0 and kind == "balance_sheet"
                        and label in _NONNEGATIVE_ROWS):
                    w.append(f"{kind} {freq} {periods[i]}: '{label}' = {c} is an impossible "
                             "negative (Zacks placeholder); set to None")
                    v = None
                vals.append(v)
            blk["rows"][label] = vals
    return out


def _chart_series(zsym: str, metric: str, unit: str, max_age: float | None,
                  history: int) -> tuple[dict, dict]:
    url = URLS["chart"].format(sym=zsym, metric=metric)
    page = _http(url, expect="json", max_age=max_age)
    out = {"metric": metric, "unit": unit, "available": False, "latest": None,
           "latest_period_end": None, "pending_period_end": None, "history": [],
           "source": f"{PROVIDER} {url}", "retrieved_at": page["retrieved_at"]}
    if not page["ok"]:
        out["unavailable_reason"] = page["reason"]
        return out, page
    try:
        obj = json.loads(page["text"])
    except ValueError:
        page.update(ok=False, reason="chart endpoint did not return JSON (possible challenge page)")
        out["unavailable_reason"] = page["reason"]
        return out, page
    data = obj.get(metric) if isinstance(obj, dict) else None
    if not isinstance(data, dict) or not data:
        reason = (obj.get("Error") if isinstance(obj, dict) else None) or "no series in response"
        page.update(ok=False, reason=f"chart endpoint: {reason}")
        out["unavailable_reason"] = page["reason"]
        return out, page
    pts = []
    for k, v in data.items():
        d = _iso_date(k)
        if d is not None:
            pts.append({"period_end": d, "value": parse_number(v)})
    pts.sort(key=lambda p: p["period_end"], reverse=True)
    latest = next((p for p in pts if p["value"] is not None), None)
    out["available"] = latest is not None
    if latest is None:
        out["unavailable_reason"] = "series has no numeric values"
    else:
        out["latest"], out["latest_period_end"] = latest["value"], latest["period_end"]
        if pts and pts[0]["value"] is None:
            out["pending_period_end"] = pts[0]["period_end"]
    out["history"] = pts[:history]
    out["history_order"] = "most recent first"
    return out, page


def _first(vals):
    return vals[0] if vals else None


def fundamentals(symbol: str, *, charts: bool = True, statements: bool = True,
                 full_statements: bool = False, chart_history: int = 12,
                 max_age: float | None = None) -> dict:
    """What Zacks exposes free on solvency / cash flow / EPS history.

    Pages: financial-overview (ratios), /data_handler/charts JSON (Zacks' own
    free-cash-flow and diluted-EPS series), income-statement, balance-sheet,
    cash-flow-statements. Zacks publishes NO interest-coverage ratio on its free
    pages; one is derived here from the income statement and labelled as such.
    """
    rec = _record(symbol, "fundamentals")
    zsym = rec["zacks_symbol"]
    url = URLS["financial_overview"].format(sym=zsym)
    _feed_d, fpage = _feed(zsym, max_age)
    if _is_invalid_ticker(fpage):
        rec["sources"].append(_src(fpage))
        return _fail(rec, {"url": url, "final_url": None, "retrieved_at": fpage["retrieved_at"]},
                     "ticker unknown to Zacks (quote feed: Invalid ticker); page not requested")
    w = rec["warnings"]
    rec["definitions"] = {k: DEFINITIONS[k] for k in ("debt_to_equity", "free_cash_flow",
                                                      "eps_diluted", "eps_growth_q", "peg_ratio")}
    rec["definitions"]["interest_coverage"] = (
        "NOT published by Zacks on free pages. derived.interest_coverage_* are computed here "
        "from the Zacks income statement (fiscal-year figures).")

    # --- financial overview ---------------------------------------------------
    soup, _rib, page = _stock_page(zsym, url, "#financial_overview_details", max_age)
    if soup is None:
        _fail(rec, page)
        rec["financial_overview"] = None
    else:
        _attach(rec, page, primary=True)
        rec["available"] = True
        fo = _parse_financial_overview(soup, w)
        rec["financial_overview"] = fo
        s, ser = fo["scalars"], fo["series"]
        rec["current_ratio"] = ser["current_ratio"]["latest"]
        rec["current_ratio_period_end"] = ser["current_ratio"]["latest_period_end"]
        rec["quick_ratio"] = ser["quick_ratio"]["latest"]
        rec["quick_ratio_period_end"] = ser["quick_ratio"]["latest_period_end"]
        rec["debt_to_equity"] = ser["debt_to_equity"]["latest"]
        rec["debt_to_equity_period_end"] = ser["debt_to_equity"]["latest_period_end"]
        rec["debt_to_equity_label"] = DEFINITIONS["debt_to_equity"]
        rec["debt_to_capital_pct"] = ser["debt_to_capital_pct"]["latest"]
        rec["eps_growth_vs_prev_year_q_pct"] = s["eps_growth_vs_prev_year_q_pct"]
        rec["eps_growth_vs_prev_quarter_pct"] = s["eps_growth_vs_prev_quarter_pct"]
        rec["sales_growth_vs_prev_year_q_pct"] = s["sales_growth_vs_prev_year_q_pct"]
        rec["pe_f1"], rec["pe_ttm"], rec["peg_ratio"] = s["pe_f1"], s["pe_ttm"], s["peg_ratio"]
        rec["roe_pct"] = ser["roe_pct"]["latest"]

    # --- chart series (Zacks' own FCF / EPS numbers) -------------------------
    rec["charts"] = {}
    rec["free_cash_flow_ttm"] = None
    rec["free_cash_flow_ttm_period_end"] = None
    rec["free_cash_flow_unit"] = "USD millions"
    if charts:
        for metric, key, unit in _CHART_METRICS:
            series, cpage = _chart_series(zsym, metric, unit, max_age, chart_history)
            rec["charts"][key] = series
            rec["sources"].append(_src(cpage))
            if not series["available"]:
                w.append(f"chart series {metric} unavailable: {series.get('unavailable_reason')}")
        fcf = rec["charts"]["free_cash_flow_ttm"]
        rec["free_cash_flow_ttm"] = fcf["latest"]
        rec["free_cash_flow_ttm_period_end"] = fcf["latest_period_end"]
        if any(s["available"] for s in rec["charts"].values()):
            rec["available"] = True
            if rec["source"] is None or rec["unavailable_reason"]:
                good = next(s for s in rec["charts"].values() if s["available"])
                rec["source"], rec["retrieved_at"] = good["source"], good["retrieved_at"]

    # --- statements ----------------------------------------------------------
    rec["statements"] = {}
    derived: dict = {"note": "computed by this module from the Zacks statement rows below; "
                             "not Zacks-published ratios"}
    if statements:
        spec = (("income_statement", "income_statements_tabs"),
                ("balance_sheet", "income_statements_tabs"),
                ("cash_flow", "cash_flow_statements_tabs"))
        for kind, sec_id in spec:
            ssoup, _r, spage = _stock_page(zsym, URLS[kind].format(sym=zsym), "#" + sec_id, max_age)
            rec["sources"].append(_src(spage))
            if ssoup is None:
                rec["statements"][kind] = None
                w.append(f"{kind} page unavailable: {spage.get('reason')}")
                continue
            st = _parse_statement(ssoup, sec_id, kind, full_statements, w)
            st["source"] = f"{PROVIDER} {spage['url']}"
            st["retrieved_at"] = spage["retrieved_at"]
            rec["statements"][kind] = st
            rec["available"] = True
        if rec["statements"].get("cash_flow"):
            rec["statements"]["cash_flow"]["quarterly_note"] = (
                "quarterly cash-flow columns are fiscal-YEAR-TO-DATE cumulative (the fiscal "
                "year-end column equals the annual figure), not single quarters")

        inc = (rec["statements"].get("income_statement") or {}).get("annual") or {}
        rows, periods = inc.get("rows") or {}, inc.get("periods") or []
        ie, pti = rows.get("Interest Expense"), rows.get("Pretax Income")
        opi = rows.get("Income After Depreciation & Amortization")
        derived["interest_coverage_fiscal_year"] = _first(periods)
        i0, p0, o0 = _first(ie or []), _first(pti or []), _first(opi or [])
        derived["interest_expense_latest_fy"] = i0
        derived["interest_coverage_ebit"] = (
            _round((p0 + i0) / i0, 2) if i0 and p0 is not None and i0 > 0 else None)
        derived["interest_coverage_ebit_formula"] = (
            "(Pretax Income + Interest Expense) / Interest Expense, latest fiscal year; None "
            "when Zacks shows interest expense of 0 / NA")
        derived["interest_coverage_operating"] = (
            _round(o0 / i0, 2) if i0 and o0 is not None and i0 > 0 else None)
        derived["interest_coverage_operating_formula"] = (
            "Income After Depreciation & Amortization (operating income) / Interest Expense, "
            "latest fiscal year")
        eps_hist = []
        for label in ("Diluted EPS Before Non-Recurring Items", "Diluted Net EPS (GAAP)"):
            vals = rows.get(label)
            if not vals:
                continue
            for i, (p, v) in enumerate(zip(periods, vals)):
                prev = vals[i + 1] if i + 1 < len(vals) else None
                g = (_round((v - prev) / abs(prev) * 100, 2)
                     if v is not None and prev is not None and prev > 0 else None)
                eps_hist.append({"measure": label, "fiscal_year_end": p, "eps": v,
                                 "yoy_growth_pct": g})
        derived["annual_eps_history"] = eps_hist
        derived["annual_eps_history_formula"] = (
            "yoy_growth_pct = (EPS - prior FY EPS) / |prior FY EPS| * 100; None when the prior "
            "year EPS is <= 0 or missing")
        cf = (rec["statements"].get("cash_flow") or {}).get("annual") or {}
        crow, cper = cf.get("rows") or {}, cf.get("periods") or []
        cfo, capex = crow.get("Net Cash From Operating Activities"), crow.get("Property & Equipment")
        fcf_hist = []
        if cfo and capex:
            for p, a, b in zip(cper, cfo, capex):
                fcf_hist.append({"fiscal_year_end": p, "cash_from_operations": a,
                                 "property_and_equipment": b,
                                 "free_cash_flow": _round(a + b, 2)
                                 if a is not None and b is not None else None})
        derived["annual_free_cash_flow"] = fcf_hist
        derived["annual_free_cash_flow_formula"] = (
            "Net Cash From Operating Activities + Property & Equipment (capex, negative), USD "
            "millions, fiscal years")
        bs = (rec["statements"].get("balance_sheet") or {}).get("quarterly") or {}
        brow, bper = bs.get("rows") or {}, bs.get("periods") or []
        idx = next((i for i, p in enumerate(bper) if p not in (bs.get("pending_periods") or [])),
                   None)

        def cell(label):
            vals = brow.get(label)
            return vals[idx] if vals and idx is not None and idx < len(vals) else None

        tca, tcl = cell("Total Current Assets"), cell("Total Current Liabilities")
        ltd, eq = cell("Long-Term Debt"), cell("Total Shareholder's Equity")
        derived["balance_sheet_period_end"] = bper[idx] if idx is not None else None
        derived["balance_sheet_latest_usd_millions"] = {
            "note": "raw Zacks balance-sheet rows for balance_sheet_period_end (not derived)",
            "cash_and_equivalents": cell("Cash & Equivalents"),
            "total_current_assets": tca, "total_current_liabilities": tcl,
            "notes_payable": cell("Notes Payable"),
            "current_portion_long_term_debt": cell("Current Portion Long-Term Debt"),
            "long_term_debt": ltd, "convertible_debt": cell("Convertible Debt"),
            "total_liabilities": cell("Total Liabilities"),
            "total_shareholders_equity": eq,
        }
        derived["current_ratio_from_balance_sheet"] = (
            _round(tca / tcl, 2) if tca is not None and tcl else None)
        derived["current_ratio_from_balance_sheet_formula"] = (
            "Total Current Assets / Total Current Liabilities, latest reported quarter")
        derived["lt_debt_to_equity_from_balance_sheet"] = (
            _round(ltd / eq, 2) if ltd is not None and eq and eq > 0 else None)
        derived["lt_debt_to_equity_from_balance_sheet_formula"] = (
            "Long-Term Debt / Total Shareholder's Equity, latest reported quarter")
    rec["derived"] = derived

    if not rec["available"] and not rec["unavailable_reason"]:
        rec["unavailable_reason"] = "no Zacks fundamentals page could be read"
    if rec["available"]:
        rec["unavailable_reason"] = None
    rec["not_published_free"] = ["interest coverage ratio", "Altman Z-score", "net debt / EBITDA"]
    return rec


# ----------------------------------------------------------------------------
# everything at once + CLI
# ----------------------------------------------------------------------------
FUNCTIONS = {"quote": quote, "broker_recs": broker_recs, "estimates": estimates,
             "price_target": price_target, "fundamentals": fundamentals}


def snapshot(symbol: str, *, only: tuple[str, ...] | None = None,
             max_age: float | None = None) -> dict:
    """All (or `only`) of quote / broker_recs / estimates / price_target / fundamentals."""
    out = {"provider": PROVIDER, "symbol": symbol, "zacks_symbol": zacks_symbol(symbol)}
    for name, fn in FUNCTIONS.items():
        if only and name not in only:
            continue
        try:
            out[name] = fn(symbol, max_age=max_age)
        except Exception as e:  # a parser bug must not take the other records down
            rec = _record(symbol, name)
            rec["unavailable_reason"] = f"adapter error: {type(e).__name__}: {e}"
            rec["retrieved_at"] = _now()
            rec["source"] = f"{PROVIDER} (see sources)"
            out[name] = rec
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    only = None
    indent = 2
    tickers = []
    it = iter(args)
    for a in it:
        if a == "--only":
            only = tuple(x for x in next(it, "").split(",") if x)
        elif a.startswith("--only="):
            only = tuple(x for x in a.split("=", 1)[1].split(",") if x)
        elif a == "--compact":
            indent = None
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            tickers.append(a)
    if not tickers:
        print("usage: python3 -m screener.sources.zacks [--only quote,broker_recs,estimates,"
              "price_target,fundamentals] [--compact] TICKER [TICKER ...]", file=sys.stderr)
        return 2
    bad = [x for x in (only or ()) if x not in FUNCTIONS]
    if bad:
        print(f"unknown --only value(s): {bad}; choose from {sorted(FUNCTIONS)}", file=sys.stderr)
        return 2
    out = {t: snapshot(t, only=only) for t in tickers}
    json.dump(out, sys.stdout, indent=indent, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
