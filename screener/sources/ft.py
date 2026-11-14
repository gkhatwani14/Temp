"""FT Markets public equity tearsheets (host markets.ft.com; pages carry the
attribution "Data Provided by LSEG", short-selling gauge "Provided by S&P
Global Market Intelligence").

What an anonymous visitor is served (checked 2026-11-14):

  summary   /data/equities/tearsheet/summary?s=SYM    PUBLIC  -> summary()
  profile   /data/equities/tearsheet/profile?s=SYM    PUBLIC  -> profile()
  search    /data/search?query=...                    PUBLIC  -> resolve()
  forecasts /data/equities/tearsheet/forecasts?s=SYM  PAYWALL -> forecasts()
  financials/data/equities/tearsheet/financials?s=SYM PAYWALL -> financials()
  (historical prices and directors & dealings are behind the same barrier)

The paywalled views answer HTTP 200 with a "BarrierInlineApp" block
("Director dealings, financials and forecasts are now only available to
FT.com subscribers").  Those functions therefore return
available=False / unavailable_reason="paywall" with every data field None.
Nothing here tries to get around the barrier.  The forecast / financial
parsers that would run if FT ever served the content again use strict
patterns and are flagged layout_verified=False because they could not be
exercised against live content.

FT symbol convention: BASE:EXCH with EXCH = NSQ / NMQ / NAQ (NASDAQ Global
Select / Global Market / Capital Market), NYQ (NYSE consolidated), ASQ (NYSE
American consolidated), BTQ (Cboe BZX), LSE.
US class shares use a space ("BRK B:NYQ", "GEF B:NYQ"); LSE symbols keep
their dot ("RR.:LSE", "BT.A:LSE").

Every record carries "source" (provider + exact URL) and "retrieved_at".
A value that is missing or does not parse is None - never estimated.
Anything computed by this adapter sits under a key named "derived...".
"""
from __future__ import annotations

import datetime
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..http import FetchError, fetch

HOST = "https://markets.ft.com"
PROVIDER = "FT Markets (markets.ft.com)"
TEARSHEET_URL = HOST + "/data/equities/tearsheet/{view}?s={sym}"
SEARCH_URL = HOST + "/data/search?query={q}"
DEFAULT_MAX_AGE = 20 * 3600

NASDAQ_SUFFIXES = ("NSQ", "NMQ", "NAQ")
US_SUFFIXES = NASDAQ_SUFFIXES + ("NYQ", "ASQ", "BTQ")
SUFFIX_EXCHANGE = {
    "NSQ": "NASDAQ Global Select (consolidated)",
    "NMQ": "NASDAQ Global Market (consolidated)",
    "NAQ": "NASDAQ Capital Market (consolidated)",
    "NYQ": "NYSE (consolidated)",
    "ASQ": "NYSE American (consolidated)",
    "BTQ": "Cboe BZX",
    "LSE": "London Stock Exchange",
}
# pipeline / TradingView exchange label -> FT suffixes to try, in order
HINT_SUFFIXES = {
    "NASDAQ": NASDAQ_SUFFIXES,     # tier unknown -> resolved through FT search
    "NSQ": ("NSQ",),
    "NMQ": ("NMQ",),
    "NAQ": ("NAQ",),
    "NYSE": ("NYQ",),
    "NYQ": ("NYQ",),
    "NYSE ARCA": ("ASQ", "NYQ"),
    "NYSE AMERICAN": ("ASQ",),
    "NYSE MKT": ("ASQ",),
    "NYSE-MKT": ("ASQ",),
    "AMEX": ("ASQ",),
    "ASQ": ("ASQ",),
    "CBOE": ("BTQ",),
    "BATS": ("BTQ",),
    "BTQ": ("BTQ",),
    "LSE": ("LSE",),
    "LONDON": ("LSE",),
    "UK": ("LSE",),
    "US": US_SUFFIXES,
}

PAYWALLED_VIEWS_NOTE = (
    "FT serves this view to subscribers only (anonymous visitors get the "
    "'BarrierInlineApp' subscribe/sign-in block); treated as unavailable"
)

# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------
_NULLS = {"", "-", "--", "---", "n/a", "na", "n.a.", "nm", "n/m", "none", "null",
          "\u2014", "\u2013"}
_SUFFIX_MULT = {
    "k": Decimal(10) ** 3,
    "m": Decimal(10) ** 6, "mn": Decimal(10) ** 6, "mm": Decimal(10) ** 6,
    "b": Decimal(10) ** 9, "bn": Decimal(10) ** 9,
    "t": Decimal(10) ** 12, "tn": Decimal(10) ** 12, "trn": Decimal(10) ** 12,
}
_NUM_RE = re.compile(r"([+-]?)\s*(\d+(?:\.\d*)?|\.\d+)\s*([A-Za-z]{0,3})")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_number(text) -> float | int | None:
    """'365.80m' -> 365800000, '4.01tn' -> 4010000000000, '13,982.00' -> 13982.0,
    '-2.01%' -> -2.01, '(1.5)' -> -1.5, '--' / 'N/A' / '' -> None.
    Anything that is not exactly one number (plus optional k/m/bn/tn suffix) -> None.
    """
    if text is None:
        return None
    s = str(text).replace("\xa0", " ").replace("\u2212", "-").strip()
    if s.lower() in _NULLS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    s = s.replace(",", "")
    m = _NUM_RE.fullmatch(s)
    if not m:
        return None
    sign, digits, suffix = m.groups()
    try:
        val = Decimal(digits)
    except InvalidOperation:
        return None
    scaled = False
    if suffix:
        mult = _SUFFIX_MULT.get(suffix.lower())
        if mult is None:
            return None
        val, scaled = val * mult, True
    if neg or sign == "-":
        val = -val
    if scaled and val == val.to_integral_value():
        return int(val)
    return float(val)


def parse_date(text) -> str | None:
    """'Nov 12 2026' / 'Nov 12, 2026' / '27 Aug 2026' -> '2026-11-12'; else None."""
    if not text:
        return None
    s = " ".join(str(text).replace(",", " ").split())
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]* (\d{1,2}) (\d{4})", s)
    if m:
        mon, day, year = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})", s)
        if not m:
            return None
        day, mon, year = m.group(1), m.group(2), m.group(3)
    mi = _MONTHS.get(mon.lower())
    if not mi:
        return None
    try:
        return datetime.date(int(year), mi, int(day)).isoformat()
    except ValueError:
        return None


def _txt(node) -> str | None:
    if node is None:
        return None
    return " ".join(node.get_text(" ", strip=True).split())


def _decimals(raw: str | None) -> int | None:
    """Number of decimals in a displayed figure ('21.01' -> 2, '0.5394' -> 4)."""
    if not raw:
        return None
    m = re.search(r"\d+(?:\.(\d+))?", raw.replace(",", ""))
    if not m:
        return None
    return len(m.group(1) or "")


def _display_step(raw: str | None) -> float | None:
    """Half the last displayed digit, in absolute units ('24.10bn' -> 5e6)."""
    d = _decimals(raw)
    if d is None:
        return None
    m = re.search(r"([A-Za-z]{1,3})\s*$", raw.strip())
    mult = float(_SUFFIX_MULT.get(m.group(1).lower(), 1)) if m else 1.0
    return 0.5 * 10 ** (-d) * mult


# --------------------------------------------------------------------------
# page access
# --------------------------------------------------------------------------
def _sym_url(view: str, ft_symbol: str, extra: str = "") -> str:
    return TEARSHEET_URL.format(view=view, sym=quote(ft_symbol, safe=":.")) + extra


def _source(url: str) -> str:
    return f"{PROVIDER} {url}"


def _get(url: str, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Fetch one FT page and classify it.

    status: ok | not_found | paywall | rate_limited | blocked | http_error |
            unexpected_page
    """
    out = _classify(url, max_age)
    if out["status"] == "unexpected_page" and max_age != 0 and out.pop("age", 0) > 120:
        # a stale cached copy of an odd page (e.g. a one-off challenge): ask once more
        out = _classify(url, 0)
    out.pop("age", None)
    return out


def _classify(url: str, max_age: float | None) -> dict:
    out = {"status": "ok", "soup": None, "url": url, "final_url": url,
           "retrieved_at": None, "detail": None, "age": 0}
    try:
        res = fetch(url, max_age=max_age)
    except FetchError as e:
        if e.status == 429:
            out["status"] = "rate_limited"
        elif e.status in (401, 402, 403, 451):
            out["status"] = "blocked"
        elif e.status == 404:
            out["status"] = "not_found"
        else:
            out["status"] = "http_error"
        out["detail"] = f"HTTP {e.status} {(e.detail or '')[:80]}".strip()
        return out
    out["final_url"], out["retrieved_at"] = res["url"], res["retrieved_at"]
    out["age"] = time.time() - res.get("ts", time.time())
    soup = BeautifulSoup(res["text"], "lxml")
    out["soup"] = soup
    is_search = "/data/search" in url
    if not is_search and "/data/search" in res["url"]:
        # FT redirects an unknown tearsheet symbol to its search page
        out["status"], out["detail"] = "not_found", "FT redirected the symbol to its search page"
        return out
    barrier = soup.select_one('[data-module-name="BarrierInlineApp"], .markets-barrier-inline')
    if barrier is not None:
        out["status"] = "paywall"
        out["detail"] = _txt(barrier.select_one(".markets-barrier-inline__header__img__text")) \
            or "FT subscriber barrier"
        return out
    marker = '[data-module-name="ResultsApp"], [data-module-name="SearchApp"]' if is_search \
        else ".mod-tearsheet-overview__header__name"
    if soup.select_one(marker) is None:
        out["status"] = "unexpected_page"
        out["detail"] = ("page has no FT tearsheet/search content (consent wall, bot "
                         "challenge or layout change): " + (_txt(soup.title) or "")[:80])
    return out


def _attribution(soup) -> str | None:
    node = soup.find(string=re.compile(r"Data Provided by", re.I)) if soup else None
    return " ".join(str(node).split()) if node else None


def _header(soup) -> dict:
    """Company name, FT symbol shown on the page, and its listing row from the
    'Select symbol' menu (exchange + country)."""
    name = _txt(soup.select_one(".mod-tearsheet-overview__header__name"))
    trig = soup.select_one(".mod-ui-symbol-chain__trigger")
    shown = _txt(trig)
    exchange = country = None
    listings = []
    cur_country = None
    for li in soup.select(".mod-ui-symbol-chain__menu li"):
        if "mod-ui-symbol-chain__country" in (li.get("class") or []):
            cur_country = _txt(li)
            continue
        spans = li.find_all("span")
        if len(spans) >= 2:
            sym, exch = _txt(spans[0]), _txt(spans[1])
            listings.append({"ft_symbol": sym, "exchange": exch, "country": cur_country})
            if shown and sym == shown:
                exchange, country = exch, cur_country
    return {"name": name, "ft_symbol_shown": shown, "exchange": exchange,
            "country": country, "listings": listings}


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------
def _hint_suffixes(exchange_hint: str | None) -> tuple[str, ...]:
    if not exchange_hint:
        return ()
    return HINT_SUFFIXES.get(" ".join(str(exchange_hint).upper().split()), ())


def ft_base(symbol: str, lse: bool = False) -> str:
    """Pipeline symbol -> FT base symbol.  US class shares: 'BRK.B' / 'BRK-B' /
    'BRK/B' -> 'BRK B'.  LSE symbols are kept as listed ('RR.', 'BT.A')."""
    s = symbol.strip().upper()
    if lse:
        return s
    return re.sub(r"\s+", " ", re.sub(r"[./-]", " ", s)).strip()


def _search(query: str, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    url = SEARCH_URL.format(q=quote(query, safe=""))
    page = _get(url, max_age)
    rows = []
    if page["status"] == "ok":
        res = page["soup"].select_one('[data-module-name="ResultsApp"]')
        panel = (res.select_one("#equity-panel") or res) if res else None
        for tr in (panel.select("tbody tr") if panel else []):
            tds = tr.find_all("td")
            a = tr.find("a", href=True)
            if len(tds) < 4 or not a or "/data/equities/tearsheet/" not in a["href"]:
                continue
            tag = tds[2].select_one(".primary-tag")
            if tag is not None:
                tag.extract()
            rows.append({"name": _txt(tds[0]), "ft_symbol": _txt(tds[1]),
                         "exchange": _txt(tds[2]), "country": _txt(tds[3]),
                         "primary": tr.get("data-mod-primary") == "true" or tag is not None})
    return {"url": url, "status": page["status"], "detail": page["detail"],
            "retrieved_at": page["retrieved_at"], "rows": rows}


def resolve(symbol: str, exchange_hint: str | None = None, *,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Find the FT symbol for a pipeline ticker.

    exchange_hint: 'NASDAQ' | 'NYSE' | 'NYSE Arca' | 'AMEX' | 'CBOE' | 'LSE' (or an
    FT suffix such as 'NSQ').  A hint that maps to one FT suffix (NYSE -> NYQ,
    LSE -> LSE, ...) is probed directly (that request is the summary page, so it
    is reused by summary()).  'NASDAQ' or no hint goes through the FT search
    page (FT splits NASDAQ into NSQ / NMQ / NAQ by listing tier); remaining
    suffixes are probed as a last resort.  A US ticker is preferred over an LSE
    line unless the hint says LSE (or the ticker ends with '.'), so pass 'LSE'
    for London names: 'VOD' without a hint is the NASDAQ ADR.  A symbol that
    already contains ':' is taken as an FT symbol and verified.  Always compare
    the returned name with the company you expect.
    """
    sym_in = symbol.strip()
    rec = {"symbol": symbol, "exchange_hint": exchange_hint, "ft_symbol": None,
           "name": None, "exchange": None, "country": None, "method": None,
           "tried": [], "available": False, "unavailable_reason": None,
           "provider": PROVIDER, "source": None, "retrieved_at": None}

    def probe(cand: str, method: str) -> bool | None:
        """True = resolved, False = no such symbol, None = provider trouble."""
        url = _sym_url("summary", cand)
        page = _get(url, max_age)
        rec["tried"].append({"ft_symbol": cand, "url": url, "result": page["status"]})
        if page["status"] == "ok":
            head = _header(page["soup"])
            rec.update(ft_symbol=head["ft_symbol_shown"] or cand, name=head["name"],
                       exchange=head["exchange"], country=head["country"], method=method,
                       available=True, source=_source(page["final_url"]),
                       retrieved_at=page["retrieved_at"])
            if rec["ft_symbol"].endswith(":LSE") and not lse_only and ":" not in sym_in:
                rec["warning"] = ("no US listing matched; this is the LSE line with the same "
                                  "ticker - check that the company name is the one you expect")
            return True
        if page["status"] == "not_found":
            return False
        rec.update(unavailable_reason=f"{page['status']}: {page['detail']}",
                   source=_source(url), retrieved_at=page["retrieved_at"])
        return None

    hinted = _hint_suffixes(exchange_hint)
    if not hinted and sym_in.endswith(".") and ":" not in sym_in:
        hinted = ("LSE",)       # a trailing dot ('RR.', 'BP.') only exists on LSE tickers
        rec["hint_inferred"] = "LSE (ticker ends with '.')"
    lse_only = hinted == ("LSE",)

    if ":" in sym_in:
        base, _, sfx = sym_in.upper().rpartition(":")
        ok = probe(f"{base}:{sfx}", "explicit FT symbol, verified on summary page")
        if ok is False:
            rec["unavailable_reason"] = "not_found: FT has no equity tearsheet for this symbol"
            rec["source"] = _source(rec["tried"][-1]["url"])
        return rec

    base = ft_base(sym_in, lse=lse_only)
    done: set[str] = set()

    # an unambiguous hint (NYSE, LSE, CBOE, ...) is probed directly; that request
    # is the summary tearsheet itself, so nothing is wasted
    if len(hinted) == 1:
        done.add(hinted[0])
        ok = probe(f"{base}:{hinted[0]}", f"probed hinted suffix :{hinted[0]}")
        if ok is not False:
            return rec

    # FT search page: tells NASDAQ tiers apart and covers FT-specific spellings
    allowed = ("LSE",) if lse_only else tuple(dict.fromkeys(hinted + US_SUFFIXES)) + ("LSE",)
    srch = _search(sym_in if lse_only else base, max_age)
    rec["tried"].append({"search_url": srch["url"], "result": srch["status"],
                         "equity_rows": len(srch["rows"])})
    if srch["status"] == "ok":
        want = {base, sym_in.upper(), base.replace(" ", ""), base.replace(" ", ".")}
        hits = []
        for row in srch["rows"]:
            b, _, sfx = (row["ft_symbol"] or "").rpartition(":")
            if sfx in allowed and b.upper() in want and not (sfx in done and b.upper() == base):
                hits.append(((allowed.index(sfx), 0 if row["primary"] else 1), row))
        hits.sort(key=lambda h: h[0])
        for _, row in hits:
            sfx = row["ft_symbol"].rpartition(":")[2]
            if sfx == "LSE" and not lse_only and hinted:
                continue        # a US-hinted ticker is never silently mapped to an LSE line
            done.add(sfx)
            ok = probe(row["ft_symbol"],
                       "FT search result (exact ticker match), verified on summary page")
            if ok is not False:
                if hinted and sfx not in hinted:
                    rec["hint_mismatch"] = (f"hint {exchange_hint!r} expected "
                                            f"{'/'.join(hinted)} but FT lists :{sfx}")
                return rec
    elif srch["status"] != "not_found":
        rec.update(unavailable_reason=f"{srch['status']}: {srch['detail']}",
                   source=_source(srch["url"]), retrieved_at=srch["retrieved_at"])
        return rec

    # last resort: probe the remaining suffixes (hinted ones first)
    order = [] if lse_only else list(dict.fromkeys(hinted + US_SUFFIXES))
    if lse_only or not hinted:
        order.append("LSE")
    for sfx in order:
        if sfx in done:
            continue
        done.add(sfx)
        cand_base = ft_base(sym_in, lse=True) if sfx == "LSE" else base
        ok = probe(f"{cand_base}:{sfx}", f"probed default suffix :{sfx}")
        if ok is not False:
            if ok and hinted and sfx not in hinted:
                rec["hint_mismatch"] = (f"hint {exchange_hint!r} expected "
                                        f"{'/'.join(hinted)} but FT lists :{sfx}")
            return rec

    rec["unavailable_reason"] = "not_found: no FT equity tearsheet matched this ticker"
    rec["source"] = _source(srch["url"])
    rec["retrieved_at"] = srch["retrieved_at"]
    return rec


_resolve_memo: dict[tuple, dict] = {}


def _resolved(symbol: str, exchange_hint: str | None, max_age) -> dict:
    """resolve() with a per-process memo of successful resolutions (the pages
    behind it are disk-cached anyway; this only saves re-parsing)."""
    key = (symbol.strip().upper(), (exchange_hint or "").strip().upper())
    if max_age != 0 and key in _resolve_memo:
        return _resolve_memo[key]
    res = resolve(symbol, exchange_hint, max_age=max_age)
    if res["ft_symbol"]:
        _resolve_memo[key] = res
    return res


def _base_record(symbol: str, res: dict, view: str, url: str | None) -> dict:
    return {"symbol": symbol, "ft_symbol": res.get("ft_symbol"), "name": res.get("name"),
            "view": view, "available": False, "unavailable_reason": None,
            "provider": PROVIDER, "attribution": None,
            "source": _source(url) if url else res.get("source"),
            "retrieved_at": None}


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------
_KEY_STATS = {
    "open": ("open", "num"), "high": ("day_high", "num"), "low": ("day_low", "num"),
    "bid": ("bid", "num"), "offer": ("offer", "num"),
    "previous close": ("previous_close", "num"),
    "average volume": ("average_volume", "num"),
    "shares outstanding": ("shares_outstanding", "num"),
    "free float": ("free_float", "num"),
    "p/e (ttm)": ("pe_ttm", "num"),
    "market cap": ("market_cap", "num"),
    "eps (ttm)": ("eps_ttm", "num"),
    "annual div (ady)": ("annual_dividend", "num"),
    "annual div yield (ady)": ("annual_dividend_yield_pct", "num"),
    "div ex-date": ("div_ex_date", "date"), "div pay-date": ("div_pay_date", "date"),
    "next div ex-date": ("next_div_ex_date", "date"),
    "next div pay-date": ("next_div_pay_date", "date"),
}
_SUMMARY_FIELDS = [
    "price", "price_currency", "price_as_of", "price_as_of_raw", "change", "change_pct",
    "shares_traded", "one_year_change_pct", "beta",
    "open", "day_high", "day_low", "bid", "offer", "previous_close", "average_volume",
    "shares_outstanding", "free_float", "pe_ttm", "market_cap", "market_cap_currency",
    "eps_ttm", "eps_currency", "annual_dividend", "annual_dividend_currency",
    "annual_dividend_yield_pct", "div_ex_date", "div_pay_date", "next_div_ex_date",
    "next_div_pay_date", "week52_low", "week52_low_date", "week52_high", "week52_high_date",
    "short_selling_activity", "next_earnings_date", "last_reported_earnings",
]
_DEFINITIONS = {
    "price": "FT 'Price (CCY)': last confirmed trade price, delayed >=15 min (LSE >=20 min); "
             "LSE prices are quoted in GBX = pence",
    "pe_ttm": "FT 'P/E (TTM)' as displayed; FT usually computes it on the PREVIOUS close "
              "(see derived.ft_pe_basis)",
    "eps_ttm": "FT 'EPS (TTM)', trailing twelve months, in eps_currency",
    "market_cap": "FT 'Market cap' as displayed (2 decimals at k/m/bn/tn scale), company level "
                  "(all share classes); often based on the PREVIOUS close "
                  "(see derived.ft_market_cap_basis; profile().key_ratios.market_cap is "
                  "'Data as of' the latest close)",
    "shares_outstanding": "FT 'Shares outstanding' as displayed (rounded to 2 decimals at "
                          "k/m/bn scale); company-level total for multi-class companies",
    "free_float": "FT 'Free float' (number of shares, rounded as displayed)",
    "beta": "FT 'Beta' (historical volatility vs a relevant index; horizon not stated)",
    "annual_dividend": "FT 'Annual div (ADY)'; LSE names are in GBX (pence)",
    "short_selling_activity": "FT Low/Med/High gauge, 'Provided by S&P Global Market Intelligence'",
    "next_earnings_date": "not shown on FT's public summary page (the earnings calendar "
                          "sentence lives on the paywalled forecasts view) -> always None here",
    "last_reported_earnings": "most recent 'reported earnings of X for the period of the year "
                              "YYYYQn' item in FT's Events & Activity feed, if one is listed",
    "press_releases / ft_news / ic_news": "headline feeds as FT attaches them to the symbol; FT "
                                          "sometimes attaches other companies' releases",
}


def _basis(shown, shown_raw, cand_last, cand_prev, rel_extra):
    """Which candidate reproduces FT's displayed figure within display rounding."""
    if shown is None or shown == 0:
        return None
    step = _display_step(shown_raw) or 0.0
    tol = step / abs(shown) + rel_extra + 1e-9

    def hit(c):
        return c is not None and abs(c - shown) / abs(shown) <= tol
    a, b = hit(cand_last), hit(cand_prev)
    if a and b:
        return "ambiguous (both last price and previous close fit)"
    if a:
        return "last_price"
    if b:
        return "previous_close"
    if cand_last is None and cand_prev is None:
        return None
    return "neither (e.g. multi-class share count, ADR ratio or currency mismatch)"


def _parse_summary(soup) -> dict:
    d: dict = {k: None for k in _SUMMARY_FIELDS}
    raw: dict = {}
    other: dict = {}

    # quote bar
    quote_el = soup.select_one(".mod-tearsheet-overview__quote")
    for li in (quote_el.select("ul li") if quote_el else []):
        label = _txt(li.select_one(".mod-ui-data-list__label"))
        value = _txt(li.select_one(".mod-ui-data-list__value"))
        if not label:
            continue
        raw[label] = value
        low = label.lower()
        m = re.fullmatch(r"price \(([A-Za-z]{3})\)", low)
        if m:
            d["price"], d["price_currency"] = parse_number(value), m.group(1).upper()
        elif low == "today's change":
            parts = [p.strip() for p in (value or "").split("/")]
            if len(parts) == 2:
                d["change"], d["change_pct"] = parse_number(parts[0]), parse_number(parts[1])
        elif low == "shares traded":
            d["shares_traded"] = parse_number(value)
        elif low == "1 year change":
            d["one_year_change_pct"] = parse_number(value)
        elif low == "beta":
            d["beta"] = parse_number(value)
        else:
            other[label] = value
    disc = _txt(quote_el.select_one(".mod-disclaimer")) if quote_el else None
    if disc:
        raw["quote disclaimer"] = disc
        m = re.search(r"as of ([A-Za-z]{3} \d{1,2} \d{4}) (\d{1,2}):(\d{2}) ([A-Z]{2,4})", disc)
        if m:
            d["price_as_of_raw"] = f"{m.group(1)} {m.group(2)}:{m.group(3)} {m.group(4)}"
            iso = parse_date(m.group(1))
            if iso and m.group(4) in ("GMT", "UTC"):
                d["price_as_of"] = f"{iso}T{int(m.group(2)):02d}:{m.group(3)}:00Z"

    # key statistics
    ks = soup.select_one(".mod-tearsheet-key-stats")
    for tr in (ks.select("table tr") if ks else []):
        th, td = tr.find("th"), tr.find("td")
        if th is None or td is None:
            continue
        label = _txt(th)
        cur_el = td.select_one(".mod-format__currency")
        cur = _txt(cur_el)
        if cur_el is not None:
            cur_el.extract()
        value = _txt(td)
        raw[label] = value if not cur else f"{value} {cur}"
        spec = _KEY_STATS.get((label or "").lower())
        if not spec:
            other[label] = raw[label]
            continue
        key, kind = spec
        d[key] = parse_number(value) if kind == "num" else parse_date(value)
        if key == "market_cap":
            d["market_cap_currency"] = cur
        elif key == "eps_ttm":
            d["eps_currency"] = cur if d[key] is not None else None
        elif key == "annual_dividend":
            d["annual_dividend_currency"] = cur
    if ks:
        lo = ks.select_one(".mod-ui-range-bar__container__label--lo")
        hi = ks.select_one(".mod-ui-range-bar__container__label--hi")
        for tag, el in (("low", lo), ("high", hi)):
            if el is None:
                continue
            val = _txt(el.select_one(".mod-ui-range-bar__container__value"))
            spans = [s for s in el.find_all("span")
                     if "mod-ui-range-bar__container__value" not in (s.get("class") or [])]
            date = _txt(spans[0]) if spans else None
            raw[f"52-week {tag}"] = f"{val} ({date})"
            d[f"week52_{tag}"] = parse_number(val)
            d[f"week52_{tag}_date"] = parse_date(date)
        act = ks.select_one(".mod-ui-rating-bar__section--active")
        d["short_selling_activity"] = _txt(act)
        d["key_stats_text"] = _txt(ks.select_one(".mod-ui-smart-text"))
    else:
        d["key_stats_text"] = None

    # derived cross-checks (adapter arithmetic on FT-displayed figures)
    price, prev, eps = d["price"], d["previous_close"], d["eps_ttm"]
    sh, mc = d["shares_outstanding"], d["market_cap"]
    px_factor = None
    if d["price_currency"] and d["market_cap_currency"]:
        if d["price_currency"] == d["market_cap_currency"]:
            px_factor = 1.0
        elif d["price_currency"] == "GBX" and d["market_cap_currency"] == "GBP":
            px_factor = 0.01
    eps_factor = None
    if d["price_currency"] and d["eps_currency"]:
        if d["price_currency"] == d["eps_currency"]:
            eps_factor = 1.0
        elif d["price_currency"] == "GBX" and d["eps_currency"] == "GBP":
            eps_factor = 0.01
    mc_last = price * px_factor * sh if None not in (price, px_factor, sh) else None
    mc_prev = prev * px_factor * sh if None not in (prev, px_factor, sh) else None
    pe_last = price * eps_factor / eps if None not in (price, eps_factor, eps) and eps else None
    pe_prev = prev * eps_factor / eps if None not in (prev, eps_factor, eps) and eps else None
    sh_rel = (_display_step(raw.get("Shares outstanding")) or 0.0) / sh if sh else 0.0
    eps_rel = (_display_step((raw.get("EPS (TTM)") or "").split(" ")[0]) or 0.0) / abs(eps) if eps else 0.0
    d["derived"] = {
        "label": "computed by this adapter from FT-displayed (rounded) figures; not FT values",
        "market_cap_last_price_x_shares": round(mc_last) if mc_last is not None else None,
        "market_cap_prev_close_x_shares": round(mc_prev) if mc_prev is not None else None,
        "market_cap_currency": d["market_cap_currency"] if mc_last is not None else None,
        "pe_last_price_over_eps_ttm": round(pe_last, 2) if pe_last is not None else None,
        "pe_prev_close_over_eps_ttm": round(pe_prev, 2) if pe_prev is not None else None,
        "ft_market_cap_basis": _basis(mc, (raw.get("Market cap") or "").split(" ")[0],
                                      mc_last, mc_prev, sh_rel),
        "ft_pe_basis": _basis(d["pe_ttm"], raw.get("P/E (TTM)"), pe_last, pe_prev, eps_rel)
        if d["pe_ttm"] is not None else None,
    }

    # news / events (headlines are provider text, passed through untouched)
    def items(sel):
        out = []
        for li in soup.select(sel):
            a, t = li.find("a", href=True), li.find("time")
            title = _txt(li.select_one(".o-teaser__heading")) or _txt(a) or _txt(li)
            out.append({"date": (t.get("datetime") if t else None),
                        "title": title,
                        "standfirst": _txt(li.select_one(".o-teaser__standfirst"))
                        or (a.get("title") if a else None),
                        "url": (a["href"] if a and a["href"].startswith("http")
                                else (HOST + a["href"] if a else None))})
        return out
    d["ft_news"] = items("#ftnews-panel li.o-teaser-collection__item")[:10]
    d["ic_news"] = items("#investorschroniclenews-panel li.mod-ui-news__item")[:6]
    d["press_releases"] = items("#pressreleases-panel li.mod-ui-news__item")[:6]
    events = []
    for li in soup.select("#eventsactivity-panel li"):
        t = li.find("time")
        txt = _txt(li.select_one(".mod-news__display"))
        events.append({"date": t.get("datetime") if t else None, "text": txt})
    d["events_activity"] = events
    d["last_reported_earnings"] = None
    for ev in sorted(events, key=lambda e: e["date"] or "", reverse=True):
        m = re.search(r"reported earnings of (-?[\d,]+(?:\.\d+)?) for the period of the year "
                      r"(\d{4}Q[1-4])", ev["text"] or "")
        if m:
            d["last_reported_earnings"] = {
                "eps": parse_number(m.group(1)), "fiscal_period": m.group(2),
                "event_time": ev["date"], "text": ev["text"],
                "note": "from FT's 'Events & Activity' feed (only about a week of events is "
                        "shown); event_time is the feed timestamp (UTC), usually the night "
                        "after the announcement; EPS basis (GAAP/adjusted) is not stated"}
            break
    d["investors_chronicle_view"] = _txt(soup.select_one(
        '[data-module-name="ICViewApp"] .mod-ic-view__recommendation span'))
    d["other_key_stats"] = other
    d["raw"] = raw
    d["definitions"] = _DEFINITIONS
    return d


def summary(symbol: str, exchange_hint: str | None = None, *,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Price, market cap, P/E, EPS (TTM), shares outstanding, free float, 52-week
    range, beta, dividends, short-selling gauge, FT/IC headlines and FT's
    'Events & Activity' feed from the public summary tearsheet."""
    res = _resolved(symbol, exchange_hint, max_age)
    if not res["ft_symbol"]:
        rec = _base_record(symbol, res, "summary", None)
        rec.update({k: None for k in _SUMMARY_FIELDS})
        rec["unavailable_reason"] = res["unavailable_reason"]
        rec["retrieved_at"] = res["retrieved_at"]
        return rec
    url = _sym_url("summary", res["ft_symbol"])
    rec = _base_record(symbol, res, "summary", url)
    page = _get(url, max_age)
    rec["retrieved_at"] = page["retrieved_at"]
    if page["status"] != "ok":
        rec.update({k: None for k in _SUMMARY_FIELDS})
        rec["unavailable_reason"] = f"{page['status']}: {page['detail']}"
        return rec
    head = _header(page["soup"])
    rec.update(available=True, source=_source(page["final_url"]),
               attribution=_attribution(page["soup"]), name=head["name"],
               exchange=head["exchange"], country=head["country"])
    rec.update(_parse_summary(page["soup"]))
    if rec["price"] is None:
        rec["available"] = False
        rec["unavailable_reason"] = "unexpected_page: summary tearsheet had no parseable price"
    return rec


# --------------------------------------------------------------------------
# profile (public): key ratios from the peer-analysis table
# --------------------------------------------------------------------------
# provider column label -> (key, unit as displayed)
_PEER_COLS = {
    "revenue (ttm)": ("revenue_ttm", "reporting currency"),
    "net income (ttm)": ("net_income_ttm", "reporting currency"),
    "market cap": ("market_cap", "reporting currency"),
    "employees": ("employees", "count"),
    "price/earnings (ttm)": ("pe_ttm", "x"),
    "price/book value (mrq)": ("price_to_book_mrq", "x"),
    "price/cash flow (ttm)": ("price_to_cash_flow_ttm", "x"),
    "price/sales (ttm)": ("price_to_sales_ttm", "x"),
    "eps (excl. extraordinary items, ttm)": ("eps_excl_extraordinary_ttm", "reporting currency per share"),
    "eps (incl. extraordinary items, ttm)": ("eps_incl_extraordinary_ttm", "reporting currency per share"),
    "revenue per share (ttm)": ("revenue_per_share_ttm", "reporting currency per share"),
    "book value per share (mrq)": ("book_value_per_share_mrq", "reporting currency per share"),
    "asset turnover (ttm)": ("asset_turnover_ttm", "x"),
    "inventory turnover (ttm)": ("inventory_turnover_ttm", "x"),
    "receivables turnover (ttm)": ("receivables_turnover_ttm", "x"),
    "revenues per employee (ttm)": ("revenue_per_employee_ttm", "reporting currency"),
    "return on avg assets (ttm)": ("return_on_avg_assets_ttm_pct", "%"),
    "return on avg assets (5 yr avg)": ("return_on_avg_assets_5y_avg_pct", "%"),
    "return on investment (ttm)": ("return_on_investment_ttm_pct", "%"),
    "return on investment (5 yr avg)": ("return_on_investment_5y_avg_pct", "%"),
    "gross margin (ttm)": ("gross_margin_ttm_pct", "%"),
    "gross margin (5 yr avg)": ("gross_margin_5y_avg_pct", "%"),
    "net profit margin (ttm)": ("net_profit_margin_ttm_pct", "%"),
    "net profit margin (5 yr avg)": ("net_profit_margin_5y_avg_pct", "%"),
    "quick ratio (mrq)": ("quick_ratio_mrq", "x"),
    "interest coverage (ttm)": ("interest_coverage_ttm", "x"),
    "total debt to capital (mrq)": ("total_debt_to_capital_mrq", "ratio as displayed by FT"),
    "payout ratio (5 yr avg)": ("payout_ratio_5y_avg_pct", "%"),
    "revenue (yoy change %)": ("revenue_yoy_change_pct", "% (last fiscal year vs prior)"),
    "revenue (5 yr growth rate)": ("revenue_5y_growth_rate_pct", "% p.a."),
    "net income (yoy change %)": ("net_income_yoy_change_pct", "% (last fiscal year vs prior)"),
    "net income (5 yr growth rate)": ("net_income_5y_growth_rate_pct", "% p.a."),
    "capital expenditure (5 yr growth rate)": ("capex_5y_growth_rate_pct", "% p.a."),
    "dividend (5 yr growth rate)": ("dividend_5y_growth_rate_pct", "% p.a."),
}
_PROFILE_FIELDS = ["description", "revenue_ttm", "revenue_currency", "net_income_ttm",
                   "net_income_currency", "incorporated", "employees", "website",
                   "location", "reporting_currency", "key_ratios_as_of", "key_ratios",
                   "income_statement_yoy", "institutional_holders",
                   "pct_held_by_top_holders"]


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _peer_table(soup, ft_symbol: str, include_peers: bool) -> dict:
    out = {"key_ratios": None, "key_ratios_as_of": None, "reporting_currency": None,
           "reporting_currency_note": None, "peer_table_note": None, "peers": None}
    app = soup.select_one('[data-module-name="PeerAnalysisApp"]')
    if app is None:
        return out
    note = _txt(app.select_one(".mod-disclaimer"))
    out["peer_table_note"] = note
    if note:
        m = re.search(r"Data as of ([A-Za-z]{3} \d{1,2} \d{4})", note)
        out["key_ratios_as_of"] = parse_date(m.group(1)) if m else None
        m = re.search(r"reporting currency:.*?\b([A-Z]{3})\s*$", note)
        out["reporting_currency"] = m.group(1) if m else None
        if out["reporting_currency"] == "GBX":
            out["reporting_currency_note"] = (
                "FT words the peer-table currency of LSE lines as 'UK Pound GBX', but the "
                "magnitudes are pounds (the table's market cap equals the summary page's GBP "
                "market cap); treat currency figures here as GBP")
    table = app.find("table")
    if table is None:
        return out
    heads = [(_txt(th), th.get("data-mod-view-category")) for th in table.select("thead th")]

    def row_dict(tr):
        tds = tr.find_all("td")
        if len(tds) != len(heads):
            return None
        vals = {}
        for (label, cat), td in zip(heads[1:], tds[1:]):
            key, unit = _PEER_COLS.get((label or "").lower(), (_slug(label or "col"), "as displayed"))
            rawv = _txt(td)
            vals[key] = {"value": parse_number(rawv), "raw": rawv, "label": label,
                         "unit": unit, "tab": cat}
        return vals

    peers = []
    target = ft_symbol.upper()
    for tr in table.select("tbody tr"):
        a = tr.find("a", href=True)
        sym = None
        if a:
            m = re.search(r"[?&]s=([^&]+)", a["href"])
            sym = m.group(1).replace("+", " ").replace("%20", " ").replace("%3A", ":") if m else None
        vals = row_dict(tr)
        if vals is None:
            continue
        if sym and sym.upper() == target:
            out["key_ratios"] = vals
        elif include_peers:
            peers.append({"name": _txt(tr.find("td")), "ft_symbol": sym,
                          "metrics": {k: v["value"] for k, v in vals.items()}})
    if include_peers:
        out["peers"] = peers
    return out


_TN = r"(-?[\d,]+\.\d+(?:k|m|bn|tn)?)"
_PCT = r"(-?[\d,]+\.\d+)%"
_REV_PATTERNS = [
    # 'grew revenues 4.11% from 805.90m to 839.00m', "'s revenues fell -13.08% from A to B",
    # 'had revenues fall -4.47% from A to B'
    re.compile(r"revenues (?:fell |fall |rose |rise )?" + _PCT + r" from " + _TN + r" to " + _TN),
    # 'despite a 11.28% increase in revenues from 3.93bn to 4.38bn'
    re.compile(_PCT + r" (?:increase|decrease) in revenues from " + _TN + r" to " + _TN),
    # 'relatively flat revenues (4.19bn to 4.14bn)', 'revenue growth (705.14m to 943.71m)'
    re.compile(r"(?:flat revenues|revenue growth) \(()" + _TN + r" to " + _TN + r"\)"),
]
_NI_PATTERNS = [
    # 'net income improved 24.10% from 39.00m to 48.40m', 'grew net income 20.62% from A to B',
    # 'had net income fall -69.23% from A to B', "'s net income deteriorated -9.44% from A to B",
    # 'net income from a loss of 222.00m to a smaller loss of 33.80m',
    # 'net income improved from a loss of 366.92m to a gain of 95.53m',
    # 'had net income fall 36.62% from a loss of 193.57m to a larger loss of 264.46m'
    re.compile(r"net income (?:[a-z]+ )?(?:" + _PCT + r" )?from (a loss of |a gain of )?" + _TN
               + r" to (a smaller loss of |a larger loss of |a loss of |a gain of )?" + _TN),
    # 'narrowed profitability losses from -256.69m to -53.83m'
    re.compile(r"profitability (losses) from ()" + _TN + r" to ()" + _TN),
]


def _pct_consistent(pct: float, a: float, b: float, a_raw: str, b_raw: str) -> bool:
    """Does the stated % change agree in size with the two (rounded) figures?
    a / b are the signed values (losses negative); FT states falls sometimes with
    and sometimes without a minus sign, so only the magnitude is compared."""
    sa, sb = _display_step(a_raw), _display_step(b_raw)
    if None in (a, b, sa, sb) or a == 0 or abs(a) <= sa:
        return False
    diff = abs(b - a)
    lo = max(0.0, diff - sa - sb) / (abs(a) + sa) * 100
    hi = (diff + sa + sb) / (abs(a) - sa) * 100
    return lo - 0.02 <= abs(pct) <= hi + 0.02


def _income_tease(soup) -> dict | None:
    """FT's one-sentence 'Income statement ... Year on year' teaser on the public
    profile page: revenue and net income for the last two fiscal years.  Only
    the sentence shapes listed above are parsed; a stated % change must agree
    with the two figures or the pair is dropped.  Losses are returned negative."""
    app = soup.select_one('[data-module-name="FinancialsTeaseApp"]')
    if app is None:
        return None
    text = _txt(app.select_one(".mod-ui-smart-text"))
    cur = _txt(app.select_one(".mod-tearsheet-financials-tease__header-currency"))
    cur = cur.strip("() ") if cur else None
    out = {"text": text, "currency": cur,
           "basis": "FT smart text on the profile page: last reported fiscal year vs prior "
                    "fiscal year (figures rounded as displayed; fiscal-year labels not shown)",
           "revenue_prior_fy": None, "revenue_last_fy": None, "revenue_change_pct_stated": None,
           "net_income_prior_fy": None, "net_income_last_fy": None,
           "net_income_change_pct_stated": None,
           "pct_note": "the *_pct_stated values are copied as FT words them; FT writes a fall "
                       "sometimes as '-9.44%' and sometimes as 'fall 36.62%' - use the two "
                       "fiscal-year figures for the sign"}
    if not text or not text.startswith("Year on year"):
        return out
    for pat in _REV_PATTERNS:
        hits = pat.findall(text)
        if len(hits) != 1:
            continue
        pct_raw, a_raw, b_raw = hits[0]
        a, b = parse_number(a_raw), parse_number(b_raw)
        pct = parse_number(pct_raw) if pct_raw else None
        if a is None or b is None or (pct is not None
                                      and not _pct_consistent(pct, a, b, a_raw, b_raw)):
            continue
        out.update(revenue_prior_fy=a, revenue_last_fy=b, revenue_change_pct_stated=pct)
        break
    for i, pat in enumerate(_NI_PATTERNS):
        hits = pat.findall(text)
        if len(hits) != 1:
            continue
        pct_raw, pre_a, a_raw, pre_b, b_raw = hits[0]
        if i == 1:                  # 'profitability losses from A to B': both are losses
            pct_raw, pre_a, pre_b = "", "loss", "loss"
        a, b = parse_number(a_raw), parse_number(b_raw)
        if a is None or b is None:
            continue
        if "loss" in pre_a:
            a = -abs(a)
        if "loss" in pre_b:
            b = -abs(b)
        pct = parse_number(pct_raw) if pct_raw else None
        if pct is not None and not _pct_consistent(pct, a, b, a_raw, b_raw):
            continue
        out.update(net_income_prior_fy=a, net_income_last_fy=b, net_income_change_pct_stated=pct)
        break
    return out


def _parse_profile(soup, ft_symbol: str, include_peers: bool) -> dict:
    d: dict = {k: None for k in _PROFILE_FIELDS}
    about = soup.select_one('[data-module-name="AboutTheCompanyApp"]')
    raw = {}
    if about is not None:
        d["description"] = _txt(about.select_one(".mod-tearsheet-profile-description"))
        for li in about.select("li"):
            label = _txt(li.select_one(".mod-ui-data-list__label"))
            vnode = li.select_one(".mod-ui-data-list__value")
            value = _txt(vnode)
            if not label:
                continue
            raw[label] = value
            m = re.fullmatch(r"(Revenue|Net income) in ([A-Z]{3})(?: \(TTM\))?", label)
            if m:
                key = "revenue" if m.group(1) == "Revenue" else "net_income"
                d[f"{key}_ttm"], d[f"{key}_currency"] = parse_number(value), m.group(2)
            elif label == "Incorporated":
                d["incorporated"] = int(value) if value and value.isdigit() else None
            elif label == "Employees":
                emp = parse_number(value)
                d["employees"] = int(emp) if emp is not None and float(emp).is_integer() else emp
            elif label == "Website":
                d["website"] = value
            elif label == "Location":
                d["location"] = [_txt(s) for s in vnode.select("address span")] or value
    d.update(_peer_table(soup, ft_symbol, include_peers))
    if not include_peers:
        d.pop("peers", None)
    d["income_statement_yoy"] = _income_tease(soup)
    inst = soup.select_one('[data-module-name="InstitutionalShareholdersApp"]')
    if inst is not None:
        cap = inst.select_one(".mod-institutional-shareholders__chart-container__caption strong")
        d["pct_held_by_top_holders"] = parse_number(_txt(cap))
        holders = []
        panel = inst.select_one("#topholders-panel") or inst
        for tr in panel.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            first = _txt(tds[0]) or ""
            m = re.search(r"\s*as of (\d{1,2} [A-Za-z]{3} \d{4})\s*$", first)
            holders.append({"holder": first[: m.start()].strip() if m else first,
                            "as_of": parse_date(m.group(1)) if m else None,
                            "shares": parse_number(_txt(tds[1])),
                            "pct_held": parse_number(_txt(tds[2]))})
        d["institutional_holders"] = holders
    d["raw"] = raw
    d["definitions"] = {
        "key_ratios": "own-company row of FT's 'Peer analysis' table; each item keeps FT's column "
                      "label (TTM = trailing twelve months, MRQ = most recent quarter, 5 yr avg). "
                      "Currency figures are normalised to the company's REPORTING currency "
                      "(reporting_currency), which can differ from the listing currency "
                      "(ADRs, LSE names reporting in USD/EUR).",
        "revenue_ttm / net_income_ttm": "FT 'About the company' figures 'Revenue in CCY (TTM)' / "
                                        "'Net income in CCY' (TTM)",
        "total_debt_to_capital_mrq": "FT 'Total debt to capital (MRQ)' as displayed - a fraction "
                                     "(e.g. 0.1272 = 12.72% of total capital), not a percentage",
        "revenue_yoy_change_pct": "last reported fiscal year vs the prior fiscal year (matches "
                                  "income_statement_yoy), NOT quarterly or TTM growth",
    }
    return d


def profile(symbol: str, exchange_hint: str | None = None, *, include_peers: bool = False,
            max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Public profile tearsheet: revenue / net income (TTM), employees, FT's
    peer-analysis ratios for the company (valuation, margins, returns, quick
    ratio, interest coverage, total debt to capital, growth rates), the
    year-on-year income-statement teaser and top institutional holders."""
    res = _resolved(symbol, exchange_hint, max_age)
    if not res["ft_symbol"]:
        rec = _base_record(symbol, res, "profile", None)
        rec.update({k: None for k in _PROFILE_FIELDS})
        rec["unavailable_reason"] = res["unavailable_reason"]
        rec["retrieved_at"] = res["retrieved_at"]
        return rec
    url = _sym_url("profile", res["ft_symbol"])
    rec = _base_record(symbol, res, "profile", url)
    page = _get(url, max_age)
    rec["retrieved_at"] = page["retrieved_at"]
    if page["status"] != "ok":
        rec.update({k: None for k in _PROFILE_FIELDS})
        rec["unavailable_reason"] = f"{page['status']}: {page['detail']}"
        return rec
    rec.update(available=True, source=_source(page["final_url"]),
               attribution=_attribution(page["soup"]))
    rec.update(_parse_profile(page["soup"], res["ft_symbol"], include_peers))
    return rec


# --------------------------------------------------------------------------
# paywalled views: forecasts / financials
# --------------------------------------------------------------------------
_paywall_lock = threading.Lock()
_paywall_seen: dict[str, list[dict]] = {}
PAYWALL_SHORT_CIRCUIT = 3   # consecutive barrier answers before we stop asking


def _paywalled_get(view: str, url: str, max_age, force: bool) -> dict:
    """_get() for a subscriber-only view.  After PAYWALL_SHORT_CIRCUIT consecutive
    barrier responses in this process the remaining symbols are not requested
    (the barrier is site-wide) unless force=True."""
    with _paywall_lock:
        seen = list(_paywall_seen.get(view, []))
    if not force and len(seen) >= PAYWALL_SHORT_CIRCUIT:
        return {"status": "paywall", "soup": None, "url": url, "final_url": url,
                "retrieved_at": seen[-1]["retrieved_at"], "not_fetched": True,
                "detail": (f"not requested: the last {len(seen)} '{view}' pages fetched in this "
                           f"run were all the FT subscriber barrier ({seen[-1]['detail']})"),
                "evidence": seen}
    page = _get(url, max_age)
    with _paywall_lock:
        if page["status"] == "paywall":
            _paywall_seen.setdefault(view, []).append(
                {"url": url, "retrieved_at": page["retrieved_at"], "detail": page["detail"]})
        elif page["status"] == "ok":
            _paywall_seen[view] = []
    return page


RATING_WEIGHTS = {"buy": 1, "outperform": 2, "hold": 3, "underperform": 4, "sell": 5}
_REC_PERIODS = {"latest": "latest", "4 weeks ago": "4_weeks_ago", "2 months ago": "2_months_ago",
                "3 months ago": "3_months_ago", "last year": "1_year_ago", "1 year ago": "1_year_ago"}


def mean_rating_from_counts(counts: dict | None) -> float | None:
    """1-5 mean rating DERIVED from FT recommendation counts
    (Buy=1, Outperform=2, Hold=3, Underperform=4, Sell=5).  None unless all five
    counts are present and at least one analyst is counted."""
    if not counts or any(counts.get(k) is None for k in RATING_WEIGHTS):
        return None
    n = sum(counts[k] for k in RATING_WEIGHTS)
    if n <= 0:
        return None
    return round(sum(RATING_WEIGHTS[k] * counts[k] for k in RATING_WEIGHTS) / n, 2)


def _empty_forecasts() -> dict:
    return {
        "consensus_recommendation": None, "consensus_text": None, "analysts_polled": None,
        "recommendation_counts": {p: None for p in
                                  ["latest", "4_weeks_ago", "2_months_ago", "3_months_ago", "1_year_ago"]},
        "derived_mean_rating": {"label": "derived by adapter from FT counts: Buy=1, Outperform=2, "
                                         "Hold=3, Underperform=4, Sell=5 (not an FT figure)",
                                "latest": None, "4_weeks_ago": None, "2_months_ago": None,
                                "3_months_ago": None, "1_year_ago": None},
        "price_target": {"horizon": "12 month", "analysts": None, "median": None, "high": None,
                         "low": None, "median_vs_last_price_pct": None, "last_price": None,
                         "text": None},
        "next_earnings_date": None, "earnings_text": None, "dividend_text": None,
        "eps_forecasts": None, "dividend_forecasts": None,
    }


def _parse_forecasts(soup) -> dict:
    """Strict-pattern parse of FT's forecasts view.  NOT verified against live
    content (the view is paywalled); every pattern either matches in full or
    leaves the field None."""
    d = _empty_forecasts()
    text = " ".join(soup.get_text(" ", strip=True).split())
    for table in soup.find_all("table"):
        rows = {}
        for tr in table.find_all("tr"):
            cells = [_txt(c) for c in tr.find_all(["th", "td"])]
            if cells and cells[0]:
                rows[cells[0].lower()] = cells[1:]
        if not set(RATING_WEIGHTS) <= set(rows):
            continue
        head = [(_txt(th) or "").lower() for th in table.select("thead th")][1:]
        ncol = len(rows["buy"])
        if not head and ncol == 1:
            head = ["latest"]
        if len(head) != ncol:
            continue
        for i, h in enumerate(head):
            period = _REC_PERIODS.get(h)
            if not period:
                continue
            counts = {k: parse_number(rows[k][i]) if i < len(rows[k]) else None for k in RATING_WEIGHTS}
            if all(v is not None and float(v).is_integer() for v in counts.values()):
                counts = {k: int(v) for k, v in counts.items()}
                d["recommendation_counts"][period] = counts
                d["derived_mean_rating"][period] = mean_rating_from_counts(counts)
        break
    m = re.search(r"As of [^.]*?, the consensus forecast amongst (\d+) polled investment analysts "
                  r"covering [^.]*? advises [^.]*?\.", text)
    if m:
        d["consensus_text"], d["analysts_polled"] = m.group(0), int(m.group(1))
        low = m.group(0).lower()
        for label, pat in (("Underperform", "underperform"), ("Outperform", "outperform"),
                           ("Buy", "purchase equity"), ("Sell", "sell their"), ("Hold", "hold their")):
            if pat in low:
                d["consensus_recommendation"] = label
                break
    m = re.search(r"The (\d+) analysts? offering 12 month price targets? for [^.]*? have a median "
                  r"target of ([\d,]+\.\d+), with a high estimate of ([\d,]+\.\d+) and a low "
                  r"estimate of ([\d,]+\.\d+)\. The median estimate represents an? "
                  r"(-?[\d,]+\.\d+)% (increase|decrease) from the last price of ([\d,]+\.\d+)", text)
    if m:
        pct = parse_number(m.group(5))
        if pct is not None and m.group(6) == "decrease" and pct > 0:
            pct = -pct
        d["price_target"].update(analysts=int(m.group(1)), median=parse_number(m.group(2)),
                                 high=parse_number(m.group(3)), low=parse_number(m.group(4)),
                                 median_vs_last_price_pct=pct, last_price=parse_number(m.group(7)),
                                 text=m.group(0))
    m = re.search(r"The next earnings announcement is expected on ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    if m:
        d["next_earnings_date"] = parse_date(m.group(1))
    m = re.search(r"On [A-Z][a-z]{2} \d{1,2}, \d{4}, .{0,120}? reported .{0,60}? earnings of "
                  r".{0,40}? per share\.", text)
    d["earnings_text"] = m.group(0) if m else None
    m = re.search(r"In \d{4}, .{0,120}? reported a dividend of .{0,200}? fiscal year, .{0,60}?%\.", text)
    d["dividend_text"] = m.group(0) if m else None
    return d


def forecasts(symbol: str, exchange_hint: str | None = None, *, force: bool = False,
              max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Analyst consensus, recommendation counts (+ derived 1-5 mean), 12-month
    price target, EPS / dividend forecasts from FT's forecasts tearsheet.

    As of 2026-11-14 FT serves this view to subscribers only, so this returns
    available=False, unavailable_reason='paywall: ...' and None for every field.
    """
    res = _resolved(symbol, exchange_hint, max_age)
    url = _sym_url("forecasts", res["ft_symbol"]) if res["ft_symbol"] else None
    rec = _base_record(symbol, res, "forecasts", url)
    rec.update(_empty_forecasts())
    rec["layout_verified"] = False
    if not res["ft_symbol"]:
        rec["unavailable_reason"] = res["unavailable_reason"]
        rec["retrieved_at"] = res["retrieved_at"]
        return rec
    page = _paywalled_get("forecasts", url, max_age, force)
    rec["retrieved_at"] = page["retrieved_at"]
    if page["status"] != "ok":
        rec["unavailable_reason"] = f"{page['status']}: {page['detail']}"
        if page["status"] == "paywall":
            rec["paywall_note"] = PAYWALLED_VIEWS_NOTE
            if page.get("not_fetched"):
                rec["not_fetched"], rec["paywall_evidence"] = True, page["evidence"]
                rec["source"] += " [not requested; retrieved_at is that of the last paywalled " \
                                 "response observed, see paywall_evidence]"
        return rec
    rec.update(available=True, source=_source(page["final_url"]),
               attribution=_attribution(page["soup"]))
    rec.update(_parse_forecasts(page["soup"]))
    rec["parser_note"] = ("FT forecasts layout could not be verified when this adapter was built "
                          "(view was paywalled); strict patterns only - cross-check before use")
    return rec


STATEMENTS = {
    "incomestatement": "IncomeStatement", "income": "IncomeStatement", "is": "IncomeStatement",
    "balancesheet": "BalanceSheet", "balance": "BalanceSheet", "bs": "BalanceSheet",
    "cashflow": "CashFlow", "cash": "CashFlow", "cf": "CashFlow",
}
# canonical field -> provider row labels accepted (exact, case-insensitive)
_FIN_ROWS = {
    "IncomeStatement": {
        "revenue": ("total revenue", "revenue"),
        "operating_income": ("operating income",),
        "net_income": ("net income", "net income after taxes"),
        "diluted_eps": ("diluted eps excluding extraordinary items", "diluted normalised eps",
                        "diluted normalized eps"),
    },
    "BalanceSheet": {
        "total_debt": ("total debt",),
        "cash": ("cash and short term investments", "cash & short term investments",
                 "total cash and short term investments"),
        "total_assets": ("total assets",),
        "total_liabilities": ("total liabilities",),
    },
    "CashFlow": {
        "cash_from_operations": ("total cash from operations", "cash from operating activities",
                                 "total cash from operating activities"),
        "capex": ("capital expenditures", "capital expenditure"),
        "free_cash_flow": ("free cash flow",),
    },
}


def _parse_financials(soup, statement: str) -> dict:
    """Generic fiscal-year table parse (label -> value per fiscal year).  NOT
    verified against live content (the view is paywalled)."""
    out = {"fiscal_years": None, "units_note": None, "rows": None,
           **{k: None for k in _FIN_ROWS[statement]}}
    for table in soup.find_all("table"):
        head = [_txt(th) for th in table.select("thead th")]
        years = [h for h in head[1:] if h and re.fullmatch(r"(?:FY ?)?(?:19|20)\d{2}", h)]
        if len(head) < 2 or len(years) != len(head) - 1:
            continue
        years = [re.search(r"\d{4}", y).group(0) for y in years]
        rows = {}
        for tr in table.select("tbody tr"):
            cells = [_txt(c) for c in tr.find_all(["th", "td"])]
            if len(cells) != len(head) or not cells[0]:
                continue
            rows[cells[0]] = {"raw": dict(zip(years, cells[1:])),
                              "values": {y: parse_number(v) for y, v in zip(years, cells[1:])}}
        if not rows:
            continue
        out["fiscal_years"], out["rows"] = years, rows
        out["units_note"] = head[0]
        lower = {k.lower(): v for k, v in rows.items()}
        for key, labels in _FIN_ROWS[statement].items():
            for lab in labels:
                if lab in lower:
                    out[key] = {"label": lab, "by_fiscal_year": lower[lab]["values"]}
                    break
        break
    return out


def financials(symbol: str, statement: str = "IncomeStatement", exchange_hint: str | None = None, *,
               force: bool = False, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """Annual statements from FT's financials tearsheet
    (statement = IncomeStatement | BalanceSheet | CashFlow).

    As of 2026-11-14 FT serves this view to subscribers only, so this returns
    available=False, unavailable_reason='paywall: ...' and None for every field.
    The only public FT financial-statement data is profile()['income_statement_yoy']
    (last two fiscal years of revenue / net income) and profile()['key_ratios'].
    """
    stmt = STATEMENTS.get(re.sub(r"[^a-z]", "", str(statement).lower()))
    if stmt is None:
        raise ValueError(f"statement must be one of IncomeStatement, BalanceSheet, CashFlow; got {statement!r}")
    res = _resolved(symbol, exchange_hint, max_age)
    url = _sym_url("financials", res["ft_symbol"], f"&subview={stmt}") if res["ft_symbol"] else None
    rec = _base_record(symbol, res, "financials", url)
    rec["statement"] = stmt
    rec.update({"fiscal_years": None, "units_note": None, "rows": None,
                **{k: None for k in _FIN_ROWS[stmt]}})
    rec["layout_verified"] = False
    if not res["ft_symbol"]:
        rec["unavailable_reason"] = res["unavailable_reason"]
        rec["retrieved_at"] = res["retrieved_at"]
        return rec
    page = _paywalled_get("financials", url, max_age, force)
    rec["retrieved_at"] = page["retrieved_at"]
    if page["status"] != "ok":
        rec["unavailable_reason"] = f"{page['status']}: {page['detail']}"
        if page["status"] == "paywall":
            rec["paywall_note"] = PAYWALLED_VIEWS_NOTE
            if page.get("not_fetched"):
                rec["not_fetched"], rec["paywall_evidence"] = True, page["evidence"]
                rec["source"] += " [not requested; retrieved_at is that of the last paywalled " \
                                 "response observed, see paywall_evidence]"
        return rec
    rec.update(available=True, source=_source(page["final_url"]),
               attribution=_attribution(page["soup"]))
    rec.update(_parse_financials(page["soup"], stmt))
    rec["parser_note"] = ("FT financials layout could not be verified when this adapter was built "
                          "(view was paywalled); cross-check before use")
    return rec


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------
def snapshot(symbol: str, exchange_hint: str | None = None, *, include_peers: bool = False,
             paywalled: bool = True, max_age: float | None = DEFAULT_MAX_AGE) -> dict:
    """resolve + summary + profile (+ the paywalled forecasts / financials stubs)."""
    out = {"symbol": symbol,
           "resolve": resolve(symbol, exchange_hint, max_age=max_age),
           "summary": summary(symbol, exchange_hint, max_age=max_age),
           "profile": profile(symbol, exchange_hint, include_peers=include_peers, max_age=max_age)}
    if paywalled:
        out["forecasts"] = forecasts(symbol, exchange_hint, max_age=max_age)
        out["financials"] = {s: financials(symbol, s, exchange_hint, max_age=max_age)
                             for s in ("IncomeStatement", "BalanceSheet", "CashFlow")}
    return out


def fetch_many(symbols: list[str], hints: dict[str, str] | None = None, *, workers: int = 3,
               include_peers: bool = False, paywalled: bool = False,
               max_age: float | None = DEFAULT_MAX_AGE) -> dict[str, dict]:
    """snapshot() for many tickers ({ticker: snapshot}); at most 4 threads, and the
    shared fetch() throttle keeps markets.ft.com at <= 2 requests/second."""
    hints = hints or {}

    def one(sym):
        try:
            return sym, snapshot(sym, hints.get(sym), include_peers=include_peers,
                                 paywalled=paywalled, max_age=max_age)
        except Exception as e:  # never let one ticker sink the batch
            return sym, {"symbol": sym, "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as ex:
        return dict(ex.map(one, symbols))


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python3 -m screener.sources.ft",
        description="FT Markets tearsheet adapter. Tickers may be plain (NVDA), carry an exchange "
                    "hint (VOD@LSE, DT@NYSE) or be FT symbols (NVDA:NSQ, 'GEF B:NYQ').")
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--hint", help="exchange hint applied to tickers without their own @hint")
    ap.add_argument("--only", help="comma list of: resolve,summary,profile,forecasts,financials")
    ap.add_argument("--peers", action="store_true", help="include peer rows in profile")
    ap.add_argument("--brief", action="store_true",
                    help="drop raw/definitions/news/description blocks from the output")
    ap.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE,
                    help="cache validity in seconds (0 = always re-fetch)")
    a = ap.parse_args(argv)
    want = set((a.only or "resolve,summary,profile,forecasts,financials").split(","))
    out = {}
    for tok in a.tickers:
        sym, _, hint = tok.partition("@")
        hint = hint or a.hint
        rec: dict = {}
        if "resolve" in want:
            rec["resolve"] = resolve(sym, hint, max_age=a.max_age)
        if "summary" in want:
            rec["summary"] = summary(sym, hint, max_age=a.max_age)
        if "profile" in want:
            rec["profile"] = profile(sym, hint, include_peers=a.peers, max_age=a.max_age)
        if "forecasts" in want:
            rec["forecasts"] = forecasts(sym, hint, max_age=a.max_age)
        if "financials" in want:
            rec["financials"] = {s: financials(sym, s, hint, max_age=a.max_age)
                                 for s in ("IncomeStatement", "BalanceSheet", "CashFlow")}
        if a.brief:
            drop = {"raw", "definitions", "ft_news", "ic_news", "press_releases", "description",
                    "institutional_holders", "location", "tried", "other_key_stats",
                    "paywall_evidence"}

            def prune(o):
                if isinstance(o, dict):
                    return {k: prune(v) for k, v in o.items() if k not in drop}
                if isinstance(o, list):
                    return [prune(v) for v in o]
                return o
            rec = prune(rec)
        out[tok] = rec
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
