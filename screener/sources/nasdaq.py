"""Nasdaq.com public JSON API adapter (host api.nasdaq.com).

These are the endpoints the public nasdaq.com stock pages load for an ordinary
anonymous visitor.  No key, cookie or login is involved; anything that answers
with a rate limit / block / non-JSON body is reported as unavailable.

Public functions (all return plain JSON-serialisable dicts):

    historical(symbol, from_date, to_date)  daily OHLCV, ascending
    institutional(symbol)                   13F ownership summary + position changes
    earnings(symbol)                        next earnings date + EPS surprise history
    analyst(symbol)                         price target, ratings, PEG, forecasts, revisions
    summary(symbol)                         market cap, sector, 52-week range, last close
    financials(symbol)                      annual + quarterly statements and ratios
    short_interest(symbol)                  bi-monthly short interest (Nasdaq-listed only)
    insider_trades(symbol)                  3/12-month insider activity + recent trades
    snapshot(symbol, parts)                 several of the above for one symbol
    fetch_many(symbols, parts)              snapshot() for many symbols (<= 4 threads)

Conventions
-----------
* Every record carries "source" (provider + exact URL(s)), "retrieved_at",
  "available", "error" and "warnings".  Sub-records fetched from their own
  endpoint carry their own "source"/"retrieved_at".
* A value the provider does not give, or that cannot be parsed, is None.
  Nothing is estimated or filled in.  Fields whose name starts with
  "derived" / lives in a "derived" block are simple arithmetic on provider
  numbers and are None unless every input is present.
* Provider facts worth knowing (verified 2026-11-14):
  - Errors mostly come back as HTTP 200 with status.rCode != 200 or
    data == null (+ "message"/bCodeMessage); both are treated as unavailable.
  - Historical prices AND volumes are split-adjusted but NOT dividend-adjusted
    (AAPL 2016-11-14 close 26.4275 = 105.71 / 4; STWD closes all have two
    decimals with no dividend scaling).  Maximum depth is a rolling 10 years.
  - The historical endpoint returns nothing / a truncated start when `todate`
    lies well in the past, so the request always ends at today and the window
    is cut client-side.
  - Analyst data (earnings date, surprises, forecasts, PEG) is supplied to
    Nasdaq by Zacks Investment Research.
  - PEG "0.0" with no explanatory text means "not available", not zero.
  - Financial statement values are in thousands; '--' is used both for zero
    and for "not reported" and is returned as None.
  - Class shares use a dot (BRK.B, GEF.B); coverage for secondary share
    classes is thin (quote only).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ..http import FetchError, fetch

PROVIDER = "Nasdaq.com public JSON API"
BASE = "https://api.nasdaq.com/api"
HEADERS = {"Accept": "application/json, text/plain, */*"}
DEFAULT_MAX_AGE = 20 * 3600

# Politeness on top of screener.http's own per-host throttle: at most ~2
# network requests per second to api.nasdaq.com, whatever the thread count.
MIN_INTERVAL = 0.5
MAX_WORKERS = 4

CORE_PARTS = ("summary", "institutional", "earnings", "analyst", "financials", "historical")
EXTRA_PARTS = ("short_interest", "insider_trades")
ALL_PARTS = CORE_PARTS + EXTRA_PARTS

PRICE_ADJUSTMENT = (
    "Prices and volumes are split-adjusted; NOT adjusted for dividends "
    "(close = actual closing price restated for later splits only)."
)
MAX_HISTORY_NOTE = "Provider keeps a rolling 10-year daily history (about 2,514 bars)."

_gate = threading.Lock()
_next_slot = 0.0


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------
def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def provider_symbol(symbol: str) -> str:
    """Pipeline symbol -> Nasdaq symbol.  Class shares use a dot: BRK.B, GEF.B."""
    s = str(symbol).strip().upper()
    return s.replace("/", ".").replace("-", ".").replace(" ", ".")


def _q(sym: str) -> str:
    return quote(sym, safe=".")


_NULLS = {
    "", "n/a", "na", "n.a.", "-", "--", "---", "none", "null", "nan", "n/a%",
    "--%", "tbd", "tba", "not available", "inf", "-inf", "infinity", "-infinity",
    "∞", "-∞", "—", "–",
}
_SUFFIX = {"K": 1e3, "M": 1e6, "MM": 1e6, "B": 1e9, "BN": 1e9, "T": 1e12}
_NUM_RE = re.compile(r"^([+-]?)(\d+(?:\.\d*)?|\.\d+)\s*(K|MM|M|BN|B|T)?$", re.I)
_GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:K|MM|M|BN|B|T)?$", re.I)


def parse_number(x):
    """'$1,234.5' '(1,234)' '-$187,000' '12.3%' '3.9T' '1.2B' 'N/A' '--' -> float | None.

    Percent signs are dropped (the value stays in percent units).  Anything
    that is not unambiguously a number returns None.
    """
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(x) else None
    s = str(x).strip()
    if s.lower() in _NULLS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    s = s.replace("−", "-").replace("$", "").replace(" ", "")
    if s.endswith("%"):
        s = s[:-1]
    if s.lower() in _NULLS:
        return None
    if "," in s:  # thousands separators must be well formed: 1,234,567.89
        if not _GROUPED_RE.match(s):
            return None
        s = s.replace(",", "")
    m = _NUM_RE.match(s)
    if not m:
        return None
    val = float(m.group(2))
    if m.group(3):
        val *= _SUFFIX[m.group(3).upper()]
    if m.group(1) == "-":
        val = -val
    if neg:
        val = -val
    return val


def parse_int(x):
    """Like parse_number but only for whole numbers (share counts, holders)."""
    v = parse_number(x)
    if v is None or abs(v - round(v)) > 1e-6:
        return None
    return int(round(v))


_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b. %d, %Y")
_TEXT_DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})")
_SLASH_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def parse_date(s):
    """'11/13/2026' | '9/30/2026' | 'Nov 13, 2026' | '2026-11-13' -> 'YYYY-MM-DD' | None."""
    if s is None:
        return None
    if isinstance(s, dt.datetime):
        return s.date().isoformat()
    if isinstance(s, dt.date):
        return s.isoformat()
    s = str(s).strip()
    if s.lower() in _NULLS:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _find_date(text):
    """First date embedded in free text ('... : Nov 17, 2026' / 'on 11/17/2026')."""
    if not text:
        return None
    for rx in (_TEXT_DATE_RE, _SLASH_DATE_RE):
        m = rx.search(str(text))
        if m:
            d = parse_date(m.group(1))
            if d:
                return d
    return None


def _as_date(x) -> dt.date | None:
    iso = parse_date(x)
    return dt.date.fromisoformat(iso) if iso else None


def _clean(s):
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s or None


def _label_value(node):
    """Nasdaq {'label': .., 'value': ..} node -> (label, value)."""
    if isinstance(node, dict):
        return node.get("label"), node.get("value")
    return None, node


# --------------------------------------------------------------------------
# HTTP (always through screener.http.fetch)
# --------------------------------------------------------------------------
def _polite_fetch(url: str, *, max_age):
    """fetch() with an extra <= 2 req/s spacing for uncached requests."""
    global _next_slot
    with _gate:
        wait = _next_slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        my_slot = time.monotonic() + MIN_INTERVAL
        _next_slot = my_slot
    t0 = time.time()
    try:
        res = fetch(url, headers=HEADERS, max_age=max_age, retries=3, timeout=30)
    except FetchError:
        raise
    if res.get("ts", t0) < t0 - 0.001:  # served from the on-disk cache
        with _gate:
            if _next_slot == my_slot:
                _next_slot = 0.0
        res = dict(res, _from_cache=True)
    return res


def _provider_error(payload) -> str | None:
    status = payload.get("status") or {}
    rcode = status.get("rCode")
    msgs = [
        _clean(b.get("errorMessage"))
        for b in (status.get("bCodeMessage") or [])
        if isinstance(b, dict) and b.get("errorMessage")
    ]
    message = payload.get("message")
    if isinstance(message, str) and _clean(message):
        msgs.insert(0, _clean(message))
    if rcode != 200:
        return f"provider rCode {rcode}: " + ("; ".join(msgs) or "no message")
    if payload.get("data") is None:
        return "no data: " + ("; ".join(msgs) or "provider returned data=null")
    return None


def _get(path: str, *, max_age=DEFAULT_MAX_AGE):
    """GET {BASE}/{path}.  Returns (data | None, meta).

    meta = {"source", "url", "retrieved_at", "error", "provider_message"}.
    """
    url = f"{BASE}/{path}"
    meta = {
        "source": f"{PROVIDER}: {url}",
        "url": url,
        "retrieved_at": None,
        "error": None,
        "provider_message": None,
    }
    payload = None
    for attempt in (0, 1):
        try:
            res = _polite_fetch(url, max_age=max_age if attempt == 0 else 0)
        except FetchError as e:
            meta["retrieved_at"] = _utcnow()
            if e.status == 429:
                meta["error"] = "unavailable: rate limited (HTTP 429)"
            elif e.status in (401, 403):
                meta["error"] = f"unavailable: access denied (HTTP {e.status})"
            elif e.status is None:
                meta["error"] = f"unavailable: network error {e.detail}".strip()
            else:
                meta["error"] = f"unavailable: HTTP {e.status}"
            return None, meta
        meta["retrieved_at"] = res["retrieved_at"]
        try:
            payload = json.loads(res["text"])
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = None
            meta["error"] = "unavailable: non-JSON response (block page / challenge?)"
        else:
            meta["error"] = _provider_error(payload)
            msg = payload.get("message")
            meta["provider_message"] = _clean(msg) if isinstance(msg, str) else None
        # A cached provider-side error may have been transient: re-ask once.
        transient = meta["error"] and (payload is None or (payload.get("status") or {}).get("rCode") != 200)
        if transient and res.get("_from_cache") and attempt == 0:
            continue
        break
    if meta["error"]:
        return None, meta
    return payload.get("data"), meta


def _base(symbol: str, sym: str, metas: list[dict]) -> dict:
    stamps = [m["retrieved_at"] for m in metas if m.get("retrieved_at")]
    return {
        "symbol": symbol,
        "provider_symbol": sym,
        "source": f"{PROVIDER}: " + " | ".join(m["url"] for m in metas),
        "retrieved_at": max(stamps) if stamps else _utcnow(),
        "available": False,
        "error": None,
        "warnings": [],
    }


def _sub(meta: dict) -> dict:
    return {
        "source": meta["source"],
        "retrieved_at": meta["retrieved_at"],
        "available": meta["error"] is None,
        "error": meta["error"],
    }


# --------------------------------------------------------------------------
# 1. historical
# --------------------------------------------------------------------------
def historical(symbol: str, from_date, to_date=None, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Daily OHLCV between from_date and to_date (inclusive), ascending by date.

    Split-adjusted, not dividend-adjusted.  Provider depth: rolling 10 years.
    """
    sym = provider_symbol(symbol)
    today = dt.date.today()
    f = _as_date(from_date)
    t = _as_date(to_date) if to_date is not None else today
    if f is None or t is None:
        raise ValueError(f"bad date(s): {from_date!r} {to_date!r}")
    # Windows that end in the past come back empty or with a truncated start,
    # so always ask up to today and cut the window here.
    req_to = max(t, today)
    path = (
        f"quote/{_q(sym)}/historical?assetclass=stocks"
        f"&fromdate={f.isoformat()}&todate={req_to.isoformat()}&limit=9999"
    )
    data, meta = _get(path, max_age=max_age)
    out = _base(symbol, sym, [meta])
    out.update(
        {
            "requested_from": f.isoformat(),
            "requested_to": t.isoformat(),
            "interval": "1d",
            "price_adjustment": PRICE_ADJUSTMENT,
            "max_history": MAX_HISTORY_NOTE,
            "units": {"open/high/low/close": "USD per share", "volume": "shares"},
            "provider_headers": None,
            "provider_total_records": None,
            "n_rows": 0,
            "first_date": None,
            "last_date": None,
            "rows": [],
        }
    )
    if meta["error"]:
        out["error"] = meta["error"]
        return out
    table = data.get("tradesTable") or {}
    raw_rows = table.get("rows") or []
    out["provider_headers"] = table.get("headers")
    out["provider_total_records"] = parse_int(data.get("totalRecords"))
    if not raw_rows:
        out["error"] = "no daily history returned by provider for this symbol/window"
        return out

    by_date: dict[str, dict] = {}
    bad_dates = dupes = 0
    for r in raw_rows:
        d = parse_date(r.get("date"))
        if d is None:
            bad_dates += 1
            continue
        if d in by_date:
            dupes += 1
        by_date[d] = {
            "date": d,
            "open": parse_number(r.get("open")),
            "high": parse_number(r.get("high")),
            "low": parse_number(r.get("low")),
            "close": parse_number(r.get("close")),
            "volume": parse_int(r.get("volume")),
        }
    all_dates = sorted(by_date)
    rows = [by_date[d] for d in all_dates if f.isoformat() <= d <= t.isoformat()]

    w = out["warnings"]
    if bad_dates:
        w.append(f"{bad_dates} row(s) dropped: unparseable date")
    if dupes:
        w.append(f"{dupes} duplicate date(s) collapsed")
    total = out["provider_total_records"]
    if total is not None and total > len(raw_rows):
        w.append(f"provider reports {total} records but returned {len(raw_rows)}")
    if all_dates and (dt.date.fromisoformat(all_dates[0]) - f).days > 7:
        w.append(
            f"history starts {all_dates[0]}, later than requested {f.isoformat()} "
            "(10-year provider limit or listing date)"
        )
    missing = sum(1 for r in rows if r["close"] is None)
    if missing:
        w.append(f"{missing} row(s) with missing close")
    no_vol = sum(1 for r in rows if r["volume"] is None)
    if no_vol:
        w.append(f"{no_vol} row(s) with missing volume")
    odd = sum(
        1
        for r in rows
        if None not in (r["high"], r["low"], r["close"])
        and not (r["low"] - 1e-9 <= r["close"] <= r["high"] + 1e-9)
    )
    if odd:
        w.append(f"{odd} row(s) where close lies outside the day's high/low")
    gap = 0
    for a, b in zip(rows, rows[1:]):
        gap = max(gap, (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days)
    if gap > 5:
        w.append(f"largest gap between consecutive bars is {gap} calendar days")

    out["rows"] = rows
    out["n_rows"] = len(rows)
    if rows:
        out["first_date"], out["last_date"] = rows[0]["date"], rows[-1]["date"]
        out["available"] = True
    else:
        out["error"] = "no rows inside the requested window"
    return out


# --------------------------------------------------------------------------
# 2. institutional holdings (13F)
# --------------------------------------------------------------------------
_POSITION_KEYS = {
    "increased positions": "increased",
    "decreased positions": "decreased",
    "held positions": "held",
    "total institutional shares": "total",
    "new positions": "new",
    "sold out positions": "sold_out",
}


def _positions(table) -> dict:
    out = {}
    for r in (table or {}).get("rows") or []:
        key = _POSITION_KEYS.get(str(r.get("positions", "")).strip().lower())
        if key:
            out[key] = {
                "label": _clean(r.get("positions")),
                "holders": parse_int(r.get("holders")),
                "shares": parse_int(r.get("shares")),
            }
    return out


def institutional(symbol: str, *, limit: int = 15, max_age=DEFAULT_MAX_AGE) -> dict:
    """Institutional (13F) ownership summary and position changes.

    net_share_change = "Increased Positions" shares - "Decreased Positions"
    shares.  (In the provider table Increased + Decreased + Held shares add up
    exactly to Total Institutional Shares, i.e. the Increased/Decreased share
    figures are the amounts added/removed, not the size of those holdings.)
    """
    sym = provider_symbol(symbol)
    path = f"company/{_q(sym)}/institutional-holdings?limit={int(limit)}&type=TOTAL"
    data, meta = _get(path, max_age=max_age)
    out = _base(symbol, sym, [meta])
    out.update(
        {
            "institutional_ownership_pct": None,
            "institutional_ownership_label": None,
            "shares_outstanding_millions": None,
            "shares_outstanding_label": None,
            "total_holdings_value_millions": None,
            "total_holdings_value_label": None,
            "total_institutional_holders": None,
            "total_institutional_shares": None,
            "increased": None,
            "decreased": None,
            "held": None,
            "new_positions": None,
            "sold_out_positions": None,
            "net_share_change": None,
            "net_share_change_definition": "increased.shares - decreased.shares (provider 'Active Positions' table)",
            "net_holders_change": None,
            "net_share_change_pct_of_institutional_shares": None,
            "report_date": None,
            "report_date_latest": None,
            "report_date_basis": (
                "Provider gives no as-of date for the aggregate tables (asOf=null). report_date = "
                "most common 13F period-end in the 'Date' column of the largest holders returned; "
                "report_date_latest = newest such date. Aggregates combine each holder's latest filing, "
                "so quarters can be mixed (see holder_report_dates)."
            ),
            "holder_report_dates": None,
            "top_holders": [],
        }
    )
    if meta["error"]:
        out["error"] = meta["error"]
        return out

    own = data.get("ownershipSummary") or {}
    lab, val = _label_value(own.get("SharesOutstandingPCT"))
    out["institutional_ownership_label"], out["institutional_ownership_pct"] = lab, parse_number(val)
    lab, val = _label_value(own.get("ShareoutstandingTotal"))
    out["shares_outstanding_label"], out["shares_outstanding_millions"] = lab, parse_number(val)
    lab, val = _label_value(own.get("TotalHoldingsValue"))
    out["total_holdings_value_label"], out["total_holdings_value_millions"] = lab, parse_number(val)

    act = _positions(data.get("activePositions"))
    nso = _positions(data.get("newSoldOutPositions"))
    out["increased"], out["decreased"], out["held"] = act.get("increased"), act.get("decreased"), act.get("held")
    out["new_positions"], out["sold_out_positions"] = nso.get("new"), nso.get("sold_out")
    tot = act.get("total") or {}
    out["total_institutional_holders"], out["total_institutional_shares"] = tot.get("holders"), tot.get("shares")

    inc, dec, held = act.get("increased") or {}, act.get("decreased") or {}, act.get("held") or {}
    if inc.get("shares") is not None and dec.get("shares") is not None:
        out["net_share_change"] = inc["shares"] - dec["shares"]
        if tot.get("shares"):
            out["net_share_change_pct_of_institutional_shares"] = round(
                100.0 * out["net_share_change"] / tot["shares"], 4
            )
    if inc.get("holders") is not None and dec.get("holders") is not None:
        out["net_holders_change"] = inc["holders"] - dec["holders"]
    parts = [inc.get("shares"), dec.get("shares"), held.get("shares")]
    if None not in parts and tot.get("shares") is not None and sum(parts) != tot["shares"]:
        out["warnings"].append("increased+decreased+held shares do not add up to total institutional shares")
    pct = out["institutional_ownership_pct"]
    if pct is not None and pct > 100:
        out["warnings"].append(f"provider shows institutional ownership above 100% ({pct}%)")

    table = ((data.get("holdingsTransactions") or {}).get("table") or {})
    holders = []
    for r in table.get("rows") or []:
        holders.append(
            {
                "owner": _clean(r.get("ownerName")),
                "report_date": parse_date(r.get("date")),
                "shares_held": parse_int(r.get("sharesHeld")),
                "shares_change": parse_int(r.get("sharesChange")),
                "shares_change_pct": parse_number(r.get("sharesChangePCT")),
                "market_value_thousands": parse_number(r.get("marketValue")),
            }
        )
    out["top_holders"] = holders
    dates = Counter(h["report_date"] for h in holders if h["report_date"])
    if dates:
        out["holder_report_dates"] = dict(sorted(dates.items(), reverse=True))
        out["report_date"] = sorted(dates.items(), key=lambda kv: (kv[1], kv[0]))[-1][0]
        out["report_date_latest"] = max(dates)
        if len(dates) > 1:
            out["warnings"].append(
                "largest holders report different 13F period-ends: "
                + ", ".join(f"{d} x{n}" for d, n in sorted(dates.items(), reverse=True))
            )
    out["available"] = bool(act) or pct is not None
    if not out["available"]:
        out["error"] = "institutional tables empty"
    return out


# --------------------------------------------------------------------------
# 3. earnings date + surprises
# --------------------------------------------------------------------------
def _next_earnings(data: dict | None, meta: dict) -> dict:
    rec = _sub(meta)
    rec.update(
        {
            "next_earnings_date": None,
            "date_status": None,
            "time_of_day": None,
            "fiscal_quarter_ending": None,
            "consensus_eps_forecast": None,
            "consensus_n_analysts": None,
            "eps_same_quarter_last_year": None,
            "is_past": None,
            "announcement": None,
            "report_text": None,
            "data_provider": "Zacks Investment Research (as stated by Nasdaq)",
        }
    )
    if meta["error"] or not isinstance(data, dict):
        return rec
    text = _clean(data.get("reportText"))
    ann = _clean(data.get("announcement"))
    rec["report_text"], rec["announcement"] = text, ann

    d_ann = _find_date(ann.split(":", 1)[1]) if ann and ":" in ann else None
    d_txt = None
    if text:
        m = re.search(r"report earnings on\s+(\d{1,2}/\d{1,2}/\d{4})\s*([^.]*)\.", text)
        if m:
            d_txt = parse_date(m.group(1))
            tod = _clean(m.group(2))
            rec["time_of_day"] = tod.lower() if tod else None
        low = text.lower()
        if "hasn't provided" in low or "has not provided" in low:
            rec["date_status"] = "not provided by vendor"
        elif "derived from an algorithm" in low or "is estimated to report" in low:
            rec["date_status"] = "estimated (algorithm based on historical reporting dates; may be revised)"
        elif "expected" in low and "to report earnings" in low:
            rec["date_status"] = "expected (vendor-supplied date)"
        m = re.search(r"fiscal quarter ending\s+([A-Za-z]{3,9}\.? \d{4})", text, re.I)
        if m:
            rec["fiscal_quarter_ending"] = m.group(1)
        m = re.search(r"based on\s+(\d+)\s+analysts?", text, re.I)
        if m:
            rec["consensus_n_analysts"] = int(m.group(1))
        m = re.search(r"consensus EPS forecast for the quarter is\s+(\(?-?\$?-?[\d.,]+\)?)", text, re.I)
        if m:
            rec["consensus_eps_forecast"] = parse_number(m.group(1).rstrip(".,"))
        m = re.search(r"same quarter last year was\s+(\(?-?\$?-?[\d.,]+\)?)", text, re.I)
        if m:
            rec["eps_same_quarter_last_year"] = parse_number(m.group(1).rstrip(".,"))

    if d_ann and d_txt and d_ann != d_txt:
        rec["error"] = f"conflicting dates in provider payload: announcement {d_ann} vs text {d_txt}"
    else:
        rec["next_earnings_date"] = d_ann or d_txt
    if rec["next_earnings_date"]:
        rec["is_past"] = rec["next_earnings_date"] < dt.date.today().isoformat()
        if rec["date_status"] is None:
            rec["date_status"] = "stated by provider (confirmation status not given)"
    return rec


def _surprises(data: dict | None, meta: dict) -> dict:
    rec = _sub(meta)
    rec.update(
        {
            "order": "most recent quarter first",
            "provider_headers": None,
            "eps_definition": (
                "Provider column 'Earnings Per Share*' vs 'Consensus EPS* Forecast' "
                "(Zacks Investment Research; Zacks reported EPS is normally before non-recurring items)"
            ),
            "quarters": [],
            "n_quarters": 0,
            "n_beats": None,
            "n_misses": None,
            "n_in_line": None,
        }
    )
    if meta["error"] or not isinstance(data, dict):
        return rec
    table = data.get("earningsSurpriseTable") or {}
    rec["provider_headers"] = table.get("headers")
    rows = []
    for r in table.get("rows") or []:
        eps, cons = parse_number(r.get("eps")), parse_number(r.get("consensusForecast"))
        pct = parse_number(r.get("percentageSurprise"))
        row = {
            "fiscal_quarter_end": _clean(r.get("fiscalQtrEnd")),
            "date_reported": parse_date(r.get("dateReported")),
            "eps_reported": eps,
            "eps_consensus": cons,
            "surprise_pct": pct,
        }
        if None not in (eps, cons, pct) and abs(cons) > 1e-9:
            calc = 100.0 * (eps - cons) / abs(cons)
            if abs(calc - pct) > max(0.6, 0.02 * abs(calc)):
                row["note"] = f"provider surprise % differs from (eps-consensus)/|consensus| = {calc:.2f}%"
        rows.append(row)
    rec["quarters"], rec["n_quarters"] = rows, len(rows)
    if rows:
        known = [r["surprise_pct"] for r in rows if r["surprise_pct"] is not None]
        rec["n_beats"] = sum(1 for p in known if p > 0)
        rec["n_misses"] = sum(1 for p in known if p < 0)
        rec["n_in_line"] = sum(1 for p in known if p == 0)
    else:
        rec["available"] = False
        rec["error"] = "no surprise rows"
    return rec


def earnings(symbol: str, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Next earnings date and the last four quarterly EPS surprises."""
    sym = provider_symbol(symbol)
    d1, m1 = _get(f"analyst/{_q(sym)}/earnings-date", max_age=max_age)
    d2, m2 = _get(f"company/{_q(sym)}/earnings-surprise", max_age=max_age)
    out = _base(symbol, sym, [m1, m2])
    nxt, sur = _next_earnings(d1, m1), _surprises(d2, m2)
    out["next_earnings_date"] = nxt["next_earnings_date"]
    out["next_earnings_date_status"] = nxt["date_status"]
    out["next_earnings"] = nxt
    out["surprise_history"] = sur
    out["available"] = bool(nxt["available"] or sur["available"])
    errs = [f"{k}: {v['error']}" for k, v in (("earnings-date", nxt), ("earnings-surprise", sur)) if v["error"]]
    if out["available"]:
        out["warnings"].extend(errs)
    else:
        out["error"] = "; ".join(errs) or "no earnings data"
    return out


# --------------------------------------------------------------------------
# 4. analyst data
# --------------------------------------------------------------------------
def _target_price(data, meta) -> dict:
    rec = _sub(meta)
    rec.update(
        {
            "mean": None, "high": None, "low": None,
            "definition": "consensusOverview.priceTarget / highPriceTarget / lowPriceTarget "
                          "(analyst 12-month price targets; USD)",
            "overview_buy": None, "overview_hold": None, "overview_sell": None,
            "latest_history_point": None,
            "history": [],
        }
    )
    if meta["error"] or not isinstance(data, dict):
        return rec
    ov = data.get("consensusOverview") or {}
    rec["mean"] = parse_number(ov.get("priceTarget"))
    rec["high"] = parse_number(ov.get("highPriceTarget"))
    rec["low"] = parse_number(ov.get("lowPriceTarget"))
    rec["overview_buy"], rec["overview_hold"], rec["overview_sell"] = (
        parse_int(ov.get("buy")), parse_int(ov.get("hold")), parse_int(ov.get("sell")))
    for k in ("mean", "high", "low"):  # 0 is the provider's "no target" filler
        if rec[k] is not None and rec[k] <= 0:
            rec[k] = None
    hist = []
    for p in data.get("historicalConsensus") or []:
        z = p.get("z") or {}
        tgt = parse_number(p.get("y"))
        hist.append(
            {
                "date": parse_date(z.get("date")),
                "mean_target": tgt if tgt and tgt > 0 else None,
                "buy": parse_int(z.get("buy")),
                "hold": parse_int(z.get("hold")),
                "sell": parse_int(z.get("sell")),
                "consensus": _clean(z.get("consensus")),
            }
        )
    rec["history"] = hist
    rec["latest_history_point"] = hist[-1] if hist else None
    if rec["mean"] is None and not hist:
        rec["available"], rec["error"] = False, "no price target data"
    return rec


def _ratings(data, meta) -> dict:
    rec = _sub(meta)
    rec.update({"mean_rating": None, "n_analysts": None, "summary_text": None, "brokers": [],
                "upgrades_downgrades": []})
    if meta["error"] or not isinstance(data, dict):
        return rec
    rec["mean_rating"] = _clean(data.get("meanRatingType"))
    rec["summary_text"] = _clean(data.get("ratingsSummary"))
    m = re.search(r"Based on\s+(\d+)\s+analysts?", rec["summary_text"] or "", re.I)
    if m:
        rec["n_analysts"] = int(m.group(1))
    rec["brokers"] = [b for b in (data.get("brokerNames") or []) if b]
    rec["upgrades_downgrades"] = data.get("upgradesDowngrades") or []
    if rec["mean_rating"] is None and rec["n_analysts"] is None:
        rec["available"], rec["error"] = False, "no ratings data"
    return rec


def _peg(data, meta) -> dict:
    rec = _sub(meta)
    rec.update(
        {
            "peg": None,
            "peg_raw": None,
            "peg_label": None,
            "peg_definition": None,
            "peg_note": (
                "Forward PEG: P/E on forecast earnings for the next 12 months divided by the analyst-consensus "
                "forecast growth rate. The provider does not publish the growth rate or horizon it divides by; "
                "it is not the fiscal-year growth shown in eps_growth_forecast."
            ),
            "pe_label": None,
            "pe_definition": None,
            "data_provider": None,
            "pe_ratios": [],
            "eps_growth_forecast": [],
            "eps_growth_title": None,
        }
    )
    if meta["error"] or not isinstance(data, dict):
        return rec
    pegr, per, gr = data.get("pegr") or {}, data.get("per") or {}, data.get("gr") or {}
    raw = parse_number(pegr.get("pegValue"))
    rec["peg_raw"] = raw
    rec["peg_label"] = _clean(pegr.get("label"))
    rec["peg_definition"] = _clean(pegr.get("text"))
    # 0.0 (and a missing explanation) is the provider's "not available" filler.
    rec["peg"] = raw if raw is not None and raw != 0 else None
    rec["pe_label"], rec["pe_definition"] = _clean(per.get("label")), _clean(per.get("text"))
    rec["data_provider"] = _clean(per.get("dataProvider"))
    for p in per.get("peRatioChart") or []:
        label = _clean(p.get("x"))
        m = re.match(r"(\d{4})\s+(\w+)", label or "")
        rec["pe_ratios"].append(
            {
                "period": label,
                "fiscal_year": int(m.group(1)) if m else None,
                "basis": ("actual" if m and m.group(2).lower().startswith("actual") else
                          "estimate" if m and m.group(2).lower().startswith("estimate") else None),
                "pe": parse_number(p.get("y")),
            }
        )
    rec["eps_growth_title"] = _clean(gr.get("title"))
    pe_by_fy = {}
    for p in gr.get("peGrowthChart") or []:
        kind = str(p.get("z") or "").strip().lower()
        fy = _clean(p.get("x"))
        if kind == "growth":
            rec["eps_growth_forecast"].append({"fiscal_year": fy, "growth_pct": parse_number(p.get("y"))})
        elif kind.startswith("p/e"):
            pe_by_fy[fy] = parse_number(p.get("y"))
    for g in rec["eps_growth_forecast"]:
        g["pe_same_year"] = pe_by_fy.get(g["fiscal_year"])
    if rec["peg"] is None and not rec["pe_ratios"] and not rec["eps_growth_forecast"]:
        rec["available"], rec["error"] = False, "no PEG / P-E data"
    return rec


def _forecast_rows(table) -> list[dict]:
    rows = []
    for r in (table or {}).get("rows") or []:
        rows.append(
            {
                "fiscal_end": _clean(r.get("fiscalEnd")),
                "consensus_eps": parse_number(r.get("consensusEPSForecast")),
                "high_eps": parse_number(r.get("highEPSForecast")),
                "low_eps": parse_number(r.get("lowEPSForecast")),
                "n_estimates": parse_int(r.get("noOfEstimates")),
                "revisions_up_4w": parse_int(r.get("up")),
                "revisions_down_4w": parse_int(r.get("down")),
            }
        )
    return rows


def _forecast(data, meta) -> dict:
    rec = _sub(meta)
    rec.update({"provider_headers": None, "quarterly": [], "yearly": [],
                "revisions_window": "Over the Last 4 Weeks Number of Revisions (provider column header)"})
    if meta["error"] or not isinstance(data, dict):
        return rec
    rec["provider_headers"] = ((data.get("yearlyForecast") or {}).get("headers")
                               or (data.get("quarterlyForecast") or {}).get("headers"))
    rec["quarterly"] = _forecast_rows(data.get("quarterlyForecast"))
    rec["yearly"] = _forecast_rows(data.get("yearlyForecast"))
    if not rec["quarterly"] and not rec["yearly"]:
        rec["available"], rec["error"] = False, "no forecast rows"
    return rec


def _pct_change(new, old):
    if new is None or old is None or abs(old) < 1e-9:
        return None
    return round(100.0 * (new - old) / abs(old), 2)


def _momentum(data, meta) -> dict:
    rec = _sub(meta)
    rec.update(
        {
            "fiscal_quarter": None, "fiscal_year": None,
            "quarter_eps": {"current": None, "one_week_ago": None, "one_month_ago": None},
            "year_eps": {"current": None, "one_week_ago": None, "one_month_ago": None},
            "derived_quarter_eps_change_1m_pct": None,
            "derived_year_eps_change_1m_pct": None,
            "revisions": {"quarter_up": None, "quarter_down": None, "year_up": None, "year_down": None,
                          "quarter_label": None, "year_label": None,
                          "window": "not stated on this endpoint; the counts are the same ones the "
                                    "earnings-forecast table labels 'Over the Last 4 Weeks'"},
            "derived_net_revisions_quarter": None,
            "derived_net_revisions_year": None,
        }
    )
    if meta["error"] or not isinstance(data, dict):
        return rec
    cc, ch = data.get("changeInConsensus") or {}, data.get("estimatesChanged") or {}
    for key, name in (("currentData", "current"), ("weekData", "one_week_ago"), ("monthData", "one_month_ago")):
        node = cc.get(key) or {}
        rec["quarter_eps"][name] = parse_number(node.get("qtrMean"))
        rec["year_eps"][name] = parse_number(node.get("yrMean"))
    m = re.search(r"([A-Za-z]{3,9} \d{4})", cc.get("fiscalQtrEndText") or "")
    rec["fiscal_quarter"] = m.group(1) if m else None
    m = re.search(r"([A-Za-z]{3,9} \d{4})", cc.get("fiscalYearEndText") or "")
    rec["fiscal_year"] = m.group(1) if m else None
    rv = rec["revisions"]
    rv["quarter_up"] = parse_int((ch.get("quarterDataUp") or {}).get("changeValue"))
    rv["quarter_down"] = parse_int((ch.get("quarterDataDown") or {}).get("changeValue"))
    rv["year_up"] = parse_int((ch.get("yearDataUp") or {}).get("changeValue"))
    rv["year_down"] = parse_int((ch.get("yearDataDown") or {}).get("changeValue"))
    rv["quarter_label"], rv["year_label"] = _clean(ch.get("qtrFiscalQtrEndText")), _clean(ch.get("yrFiscalYearEndText"))
    rec["derived_quarter_eps_change_1m_pct"] = _pct_change(rec["quarter_eps"]["current"], rec["quarter_eps"]["one_month_ago"])
    rec["derived_year_eps_change_1m_pct"] = _pct_change(rec["year_eps"]["current"], rec["year_eps"]["one_month_ago"])
    if rv["quarter_up"] is not None and rv["quarter_down"] is not None:
        rec["derived_net_revisions_quarter"] = rv["quarter_up"] - rv["quarter_down"]
    if rv["year_up"] is not None and rv["year_down"] is not None:
        rec["derived_net_revisions_year"] = rv["year_up"] - rv["year_down"]
    empty = all(v is None for v in rec["quarter_eps"].values()) and all(v is None for v in rec["year_eps"].values())
    if empty and rv["year_up"] is None and rv["quarter_up"] is None:
        rec["available"], rec["error"] = False, "no estimate momentum data"
    return rec


def analyst(symbol: str, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Price target, ratings, PEG / forward P/E, EPS forecasts and estimate revisions."""
    sym = provider_symbol(symbol)
    q = _q(sym)
    endpoints = (
        ("price_target", f"analyst/{q}/targetprice", _target_price),
        ("ratings", f"analyst/{q}/ratings", _ratings),
        ("peg_ratio", f"analyst/{q}/peg-ratio", _peg),
        ("earnings_forecast", f"analyst/{q}/earnings-forecast", _forecast),
        ("estimate_momentum", f"analyst/{q}/estimate-momentum", _momentum),
    )
    metas, parts = [], {}
    for name, path, parser in endpoints:
        data, meta = _get(path, max_age=max_age)
        metas.append(meta)
        parts[name] = parser(data, meta)
    out = _base(symbol, sym, metas)
    out.update(parts)
    out["data_provider"] = "Zacks Investment Research (as credited by Nasdaq)"

    # Cross-check: momentum revision counts vs the first forecast rows.
    fc, mo = parts["earnings_forecast"], parts["estimate_momentum"]
    if fc["available"] and mo["available"]:
        rv = mo["revisions"]
        y0 = fc["yearly"][0] if fc["yearly"] else None
        q0 = fc["quarterly"][0] if fc["quarterly"] else None
        same = True
        if y0 and None not in (rv["year_up"], rv["year_down"]):
            same &= (y0["revisions_up_4w"], y0["revisions_down_4w"]) == (rv["year_up"], rv["year_down"])
        if q0 and None not in (rv["quarter_up"], rv["quarter_down"]):
            same &= (q0["revisions_up_4w"], q0["revisions_down_4w"]) == (rv["quarter_up"], rv["quarter_down"])
        mo["revisions"]["matches_earnings_forecast_4w_counts"] = bool(same)

    failed = [f"{n}: {parts[n]['error']}" for n, _, _ in endpoints if parts[n]["error"]]
    out["available"] = any(parts[n]["available"] for n, _, _ in endpoints)
    if not out["available"]:
        out["error"] = "; ".join(failed) or "no analyst data"
    else:
        out["warnings"].extend(failed)
    return out


# --------------------------------------------------------------------------
# 5. summary
# --------------------------------------------------------------------------
def _range(value, sep, order):
    """'148.80 - 243.37' / '$243.37/$148.8' -> (low, high) or (None, None)."""
    if not isinstance(value, str) or sep not in value:
        return None, None
    a, _, b = value.partition(sep)
    x, y = parse_number(a), parse_number(b)
    if x is None or y is None:
        return None, None
    low, high = (x, y) if order == "low-high" else (y, x)
    if low > high:
        return None, None
    return low, high


def summary(symbol: str, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Quote header + key data: last close, market cap, sector/industry, 52-week range."""
    sym = provider_symbol(symbol)
    info, m1 = _get(f"quote/{_q(sym)}/info?assetclass=stocks", max_age=max_age)
    summ, m2 = _get(f"quote/{_q(sym)}/summary?assetclass=stocks", max_age=max_age)
    out = _base(symbol, sym, [m1, m2])
    out.update(
        {
            "company_name": None, "stock_type": None, "exchange": None,
            "is_nasdaq_listed": None, "is_nasdaq100": None, "market_status": None,
            "last_sale_price": None, "last_trade_date": None, "last_trade_timestamp_raw": None,
            "is_real_time": None, "net_change": None, "pct_change": None, "volume": None,
            "previous_close": None,
            "previous_close_note": "close of the session before last_trade_date (not the latest close)",
            "market_cap": None, "market_cap_label": None,
            "sector": None, "industry": None,
            "classification_note": "Nasdaq's own sector/industry scheme (not GICS)",
            "week52_high": None, "week52_low": None, "week52_label": None,
            "average_volume": None,
            "average_volume_label": None,
            "one_year_target": None, "one_year_target_label": None,
            "annualized_dividend": None, "ex_dividend_date": None, "dividend_pay_date": None,
            "current_yield_pct": None,
            "pe_ratio": None, "forward_pe_1yr": None, "eps": None, "beta": None,
            "upcoming_events": [],
            "raw_summary": None,
            "info": _sub(m1), "summary_data": _sub(m2),
        }
    )
    w = out["warnings"]
    lo_i = hi_i = None
    if not m1["error"] and isinstance(info, dict):
        pd_ = info.get("primaryData") or {}
        out["company_name"] = _clean(info.get("companyName"))
        out["stock_type"] = _clean(info.get("stockType"))
        out["exchange"] = _clean(info.get("exchange"))
        out["is_nasdaq_listed"] = info.get("isNasdaqListed")
        out["is_nasdaq100"] = info.get("isNasdaq100")
        out["market_status"] = _clean(info.get("marketStatus"))
        out["last_sale_price"] = parse_number(pd_.get("lastSalePrice"))
        out["last_trade_timestamp_raw"] = _clean(pd_.get("lastTradeTimestamp"))
        out["last_trade_date"] = _find_date(pd_.get("lastTradeTimestamp"))
        out["is_real_time"] = pd_.get("isRealTime")
        nc, pc = pd_.get("netChange"), pd_.get("percentageChange")
        out["net_change"] = 0.0 if str(nc).strip().upper() == "UNCH" else parse_number(nc)
        out["pct_change"] = 0.0 if str(pc).strip().upper() == "UNCH" else parse_number(pc)
        out["volume"] = parse_int(pd_.get("volume"))
        lab, val = _label_value((info.get("keyStats") or {}).get("fiftyTwoWeekHighLow"))
        lo_i, hi_i = _range(val, " - ", "low-high")
        for note in info.get("notifications") or []:
            for ev in note.get("eventTypes") or []:
                out["upcoming_events"].append(
                    {"event": _clean(ev.get("eventName")), "message": _clean(ev.get("message")),
                     "date": _find_date(ev.get("message"))}
                )
    if not m2["error"] and isinstance(summ, dict):
        sd = summ.get("summaryData") or {}
        out["raw_summary"] = {k: v for k, v in sd.items() if isinstance(v, dict)}

        def get(key):
            return _label_value(sd.get(key))

        out["exchange"] = _clean(get("Exchange")[1]) or out["exchange"]
        sec, ind = _clean(get("Sector")[1]), _clean(get("Industry")[1])
        out["sector"] = None if (sec or "").lower() in _NULLS else sec
        out["industry"] = None if (ind or "").lower() in _NULLS else ind
        out["market_cap_label"], mc = get("MarketCap")
        out["market_cap"] = parse_number(mc)
        if out["market_cap"] is not None and out["market_cap"] <= 0:
            out["market_cap"] = None
        out["one_year_target_label"], tgt = get("OneYrTarget")
        out["one_year_target"] = parse_number(tgt)
        out["previous_close"] = parse_number(get("PreviousClose")[1])
        if out["volume"] is None:
            out["volume"] = parse_int(get("ShareVolume")[1])
        out["average_volume_label"], av = get("AverageVolume")
        out["average_volume"] = parse_int(av)
        out["annualized_dividend"] = parse_number(get("AnnualizedDividend")[1])
        out["ex_dividend_date"] = parse_date(get("ExDividendDate")[1])
        out["dividend_pay_date"] = parse_date(get("DividendPaymentDate")[1])
        out["current_yield_pct"] = parse_number(get("Yield")[1])
        out["pe_ratio"] = parse_number(get("PERatio")[1])
        out["forward_pe_1yr"] = parse_number(get("ForwardPE1Yr")[1])
        out["eps"] = parse_number(get("EarningsPerShare")[1])
        out["beta"] = parse_number(get("Beta")[1])
        out["week52_label"], rng = get("FiftTwoWeekHighLow")
        lo_s, hi_s = _range(rng, "/", "high-low")
        if lo_s is not None:
            out["week52_low"], out["week52_high"] = lo_s, hi_s
            if lo_i is not None and (abs(lo_i - lo_s) > 0.011 or abs(hi_i - hi_s) > 0.011):
                w.append(f"52-week range differs between endpoints: summary {lo_s}-{hi_s} vs info {lo_i}-{hi_i}")
        if "AverageVolume" not in sd:
            w.append("average volume is not in the provider payload (derive it from historical())")
    if out["week52_low"] is None and lo_i is not None:
        out["week52_low"], out["week52_high"] = lo_i, hi_i
        out["week52_label"] = "52 Week Range: (quote info)"
    for name, m in (("info", m1), ("summary", m2)):
        if m["error"]:
            w.append(f"{name}: {m['error']}")
    out["available"] = not (m1["error"] and m2["error"])
    if not out["available"]:
        out["error"] = "; ".join(w)
        out["warnings"] = []
    return out


# --------------------------------------------------------------------------
# 6. financials
# --------------------------------------------------------------------------
_FIN_TABLES = (
    ("income_statement", "incomeStatementTable"),
    ("balance_sheet", "balanceSheetTable"),
    ("cash_flow", "cashFlowTable"),
    ("ratios", "financialRatiosTable"),
)
# ratio label -> statement rows it is computed from (a ratio of 0% / NaN / inf
# with a missing input is an artefact of the provider's arithmetic, not a fact)
_RATIO_INPUTS = {
    "gross margin": [("income_statement", "Gross Profit")],
    "operating margin": [("income_statement", "Operating Income")],
    "pre-tax margin": [("income_statement", "Earnings Before Tax")],
    "profit margin": [("income_statement", "Net Income")],
    "current ratio": [("balance_sheet", "Total Current Assets"), ("balance_sheet", "Total Current Liabilities")],
    "quick ratio": [("balance_sheet", "Total Current Assets"), ("balance_sheet", "Total Current Liabilities")],
    "cash ratio": [("balance_sheet", "Cash and Cash Equivalents"), ("balance_sheet", "Total Current Liabilities")],
    "pre-tax roe": [("income_statement", "Earnings Before Tax"), ("balance_sheet", "Total Equity")],
    "after tax roe": [("income_statement", "Net Income"), ("balance_sheet", "Total Equity")],
}


def _fin_table(table) -> tuple[str | None, list, list]:
    headers = (table or {}).get("headers") or {}
    keys = sorted((k for k in headers if k != "value1"), key=lambda k: int(re.sub(r"\D", "", k) or 0))
    periods = [parse_date(headers[k]) for k in keys]
    rows, section = [], None
    for r in (table or {}).get("rows") or []:
        label = _clean(r.get("value1"))
        raw = [r.get(k) for k in keys]
        if all((v is None or str(v).strip() == "") for v in raw):
            section = label  # heading row such as "Operating Expenses"
            continue
        rows.append({"label": label, "section": section, "values": [parse_number(v) for v in raw]})
    return _clean(headers.get("value1")), periods, rows


def _find(rows, label):
    hits = [r for r in rows or [] if (r["label"] or "").lower() == label.lower()]
    return hits[0]["values"] if hits else None


def _parse_fin(data, meta, frequency: str) -> dict:
    rec = _sub(meta)
    rec.update({"frequency": frequency, "period_header": None, "periods": [],
                "income_statement": [], "balance_sheet": [], "cash_flow": [], "ratios": [],
                "notes": []})
    if meta["error"] or not isinstance(data, dict):
        return rec
    for name, key in _FIN_TABLES:
        header, periods, rows = _fin_table(data.get(key))
        if name == "income_statement":
            rec["period_header"], rec["periods"] = header, periods
        elif periods and periods != rec["periods"]:
            rec["notes"].append(f"{name} periods {periods} differ from income statement periods")
        rec[name] = rows
    if not rec["periods"] or all(p is None for p in rec["periods"]):
        rec["available"], rec["error"] = False, "no statement periods"
        return rec
    # Null out ratio cells that the provider computed from missing inputs.
    for row in rec["ratios"]:
        deps = _RATIO_INPUTS.get((row["label"] or "").lower())
        if not deps:
            continue
        for i in range(len(row["values"])):
            for table, label in deps:
                vals = _find(rec[table], label)
                if vals is None or i >= len(vals) or vals[i] is None:
                    if row["values"][i] is not None:
                        row["values"][i] = None
                        note = f"ratio '{row['label']}' blanked where input '{label}' is not reported"
                        if note not in rec["notes"]:
                            rec["notes"].append(note)
                    break
    return rec


def _sum4(vals):
    if vals is None or len(vals) < 4 or any(v is None for v in vals[:4]):
        return None
    return float(sum(vals[:4]))


def _derive_financials(annual: dict, quarterly: dict) -> dict:
    d = {
        "note": "Arithmetic on provider rows only; None unless every input is reported. Thousands.",
        "latest_quarter_end": None,
        "ttm_quarters": None,
        "ttm_valid": False,
        "ttm_invalid_reason": None,
        "revenue_ttm": None, "gross_profit_ttm": None, "operating_income_ttm": None,
        "ebit_ttm": None, "interest_expense_ttm": None, "pretax_income_ttm": None,
        "net_income_ttm": None, "depreciation_ttm": None,
        "operating_cash_flow_ttm": None, "capex_ttm": None,
        "free_cash_flow_ttm": None,
        "free_cash_flow_definition": "'Net Cash Flow-Operating' - |'Capital Expenditures'|",
        "ebitda_ttm": None,
        "ebitda_definition": "'Operating Income' + 'Depreciation' (cash-flow statement row)",
        "interest_coverage_ttm": None,
        "interest_coverage_definition": "'Earnings Before Interest and Tax' TTM / 'Interest Expense' TTM "
                                        "(only when every quarter's interest expense is positive)",
        "latest_balance_sheet": None,
        "annual": [],
    }
    q, a = quarterly, annual
    if q.get("available") and q["periods"]:
        periods = q["periods"]
        d["latest_quarter_end"] = periods[0]
        ok, reason = True, None
        if len(periods) < 4 or any(p is None for p in periods[:4]):
            ok, reason = False, "fewer than four dated quarters"
        else:
            ds = [dt.date.fromisoformat(p) for p in periods[:4]]
            gaps = [(x - y).days for x, y in zip(ds, ds[1:])]
            if any(g < 75 or g > 110 for g in gaps):
                ok, reason = False, f"quarter ends are not consecutive (gaps {gaps} days)"
        # Guard against year-to-date (cumulative) quarterly figures.
        if ok and a.get("available") and a["periods"] and a["periods"][0] in periods[:4]:
            i = periods.index(a["periods"][0])
            for table, label in (("cash_flow", "Net Cash Flow-Operating"), ("income_statement", "Total Revenue")):
                qv, av = _find(q[table], label), _find(a[table], label)
                if qv and av and qv[i] is not None and av[0] is not None and av[0] != 0 and qv[i] == av[0]:
                    ok, reason = False, f"quarterly '{label}' equals the annual figure (cumulative data suspected)"
        d["ttm_valid"], d["ttm_invalid_reason"] = ok, reason
        if ok:
            d["ttm_quarters"] = periods[:4]
            inc, cf = q["income_statement"], q["cash_flow"]
            d["revenue_ttm"] = _sum4(_find(inc, "Total Revenue"))
            d["gross_profit_ttm"] = _sum4(_find(inc, "Gross Profit"))
            d["operating_income_ttm"] = _sum4(_find(inc, "Operating Income"))
            d["ebit_ttm"] = _sum4(_find(inc, "Earnings Before Interest and Tax"))
            d["pretax_income_ttm"] = _sum4(_find(inc, "Earnings Before Tax"))
            d["net_income_ttm"] = _sum4(_find(inc, "Net Income"))
            d["depreciation_ttm"] = _sum4(_find(cf, "Depreciation"))
            d["operating_cash_flow_ttm"] = _sum4(_find(cf, "Net Cash Flow-Operating"))
            capex = _find(cf, "Capital Expenditures")
            d["capex_ttm"] = _sum4(capex)
            cfo = _find(cf, "Net Cash Flow-Operating")
            if d["operating_cash_flow_ttm"] is not None and d["capex_ttm"] is not None:
                d["free_cash_flow_ttm"] = float(sum(c - abs(x) for c, x in zip(cfo[:4], capex[:4])))
            if d["operating_income_ttm"] is not None and d["depreciation_ttm"] is not None:
                d["ebitda_ttm"] = d["operating_income_ttm"] + d["depreciation_ttm"]
            ie = _find(inc, "Interest Expense")
            if ie and len(ie) >= 4 and all(v is not None for v in ie[:4]):
                d["interest_expense_ttm"] = float(sum(ie[:4]))
                if all(v > 0 for v in ie[:4]) and d["ebit_ttm"] is not None:
                    d["interest_coverage_ttm"] = round(d["ebit_ttm"] / d["interest_expense_ttm"], 3)
        bs = q["balance_sheet"]

        def first(label):
            v = _find(bs, label)
            return v[0] if v else None

        cash, sti = first("Cash and Cash Equivalents"), first("Short-Term Investments")
        std = first("Short-Term Debt / Current Portion of Long-Term Debt")
        ltd = first("Long-Term Debt")
        total_debt = std + ltd if std is not None and ltd is not None else None
        d["latest_balance_sheet"] = {
            "period": periods[0],
            "cash_and_equivalents": cash,
            "short_term_investments": sti,
            "short_term_debt": std,
            "long_term_debt": ltd,
            "total_debt": total_debt,
            "total_debt_definition": "short_term_debt + long_term_debt, only when both are reported "
                                     "(the provider prints '--' for zero as well as for not-reported)",
            "net_debt": (total_debt - cash - sti) if None not in (total_debt, cash, sti) else None,
            "net_debt_definition": "total_debt - cash_and_equivalents - short_term_investments",
            "total_current_assets": first("Total Current Assets"),
            "total_current_liabilities": first("Total Current Liabilities"),
            "total_assets": first("Total Assets"),
            "total_liabilities": first("Total Liabilities"),
            "total_equity": first("Total Equity"),
        }
    if a.get("available") and a["periods"]:
        inc, cf = a["income_statement"], a["cash_flow"]
        rev, ni = _find(inc, "Total Revenue"), _find(inc, "Net Income")
        oi = _find(inc, "Operating Income")
        cfo, capex = _find(cf, "Net Cash Flow-Operating"), _find(cf, "Capital Expenditures")
        for i, p in enumerate(a["periods"]):
            def at(v):
                return v[i] if v and i < len(v) else None
            c, x = at(cfo), at(capex)
            d["annual"].append(
                {
                    "fiscal_year_end": p,
                    "revenue": at(rev),
                    "operating_income": at(oi),
                    "net_income": at(ni),
                    "operating_cash_flow": c,
                    "capex": x,
                    "free_cash_flow": (c - abs(x)) if c is not None and x is not None else None,
                }
            )
    return d


def financials(symbol: str, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Annual (frequency=1) and quarterly (frequency=2) statements, four periods each.

    Statement values are in thousands as displayed ('$' shown for every filer;
    ADR figures appear converted to USD).  Ratio rows are percentages
    (Current Ratio 390.5 means 3.905x).  Newest period first.
    """
    sym = provider_symbol(symbol)
    a, m1 = _get(f"company/{_q(sym)}/financials?frequency=1", max_age=max_age)
    qd, m2 = _get(f"company/{_q(sym)}/financials?frequency=2", max_age=max_age)
    out = _base(symbol, sym, [m1, m2])
    annual, quarterly = _parse_fin(a, m1, "annual"), _parse_fin(qd, m2, "quarterly")
    out.update(
        {
            "units": "statement values in thousands of the displayed currency ('$'); ratios in percent",
            "period_order": "newest first",
            "null_marker_note": "provider '--' (zero or not reported) is returned as None",
            "annual": annual,
            "quarterly": quarterly,
            "derived": _derive_financials(annual, quarterly),
        }
    )
    for name, rec in (("annual", annual), ("quarterly", quarterly)):
        if rec["error"]:
            out["warnings"].append(f"{name}: {rec['error']}")
        out["warnings"].extend(f"{name}: {n}" for n in rec["notes"])
    ie = _find(quarterly["income_statement"], "Interest Expense") or []
    if any(v is not None and v < 0 for v in ie) and any(v is not None and v > 0 for v in ie):
        out["warnings"].append("quarterly 'Interest Expense' changes sign between periods (provider sign convention is inconsistent)")
    out["available"] = bool(annual["available"] or quarterly["available"])
    if not out["available"]:
        out["error"] = "; ".join(out["warnings"]) or "no financial statements"
        out["warnings"] = []
    return out


# --------------------------------------------------------------------------
# extras: short interest, insider trades
# --------------------------------------------------------------------------
def short_interest(symbol: str, *, max_age=DEFAULT_MAX_AGE) -> dict:
    """Bi-monthly short interest history.  Provider covers Nasdaq-listed stocks only."""
    sym = provider_symbol(symbol)
    data, meta = _get(f"quote/{_q(sym)}/short-interest?assetClass=stocks", max_age=max_age)
    out = _base(symbol, sym, [meta])
    out.update({"order": "most recent settlement date first", "provider_headers": None,
                "latest": None, "derived_change_vs_prior_pct": None, "rows": []})
    if meta["error"]:
        out["error"] = meta["error"]
        return out
    table = data.get("shortInterestTable") or {}
    out["provider_headers"] = table.get("headers")
    for r in table.get("rows") or []:
        out["rows"].append(
            {
                "settlement_date": parse_date(r.get("settlementDate")),
                "short_interest_shares": parse_int(r.get("interest")),
                "avg_daily_share_volume": parse_int(r.get("avgDailyShareVolume")),
                "days_to_cover": parse_number(r.get("daysToCover")),
            }
        )
    if not out["rows"]:
        out["error"] = "no short interest rows"
        return out
    out["latest"] = out["rows"][0]
    if len(out["rows"]) > 1:
        out["derived_change_vs_prior_pct"] = _pct_change(
            out["rows"][0]["short_interest_shares"], out["rows"][1]["short_interest_shares"])
    out["available"] = True
    return out


_INSIDER_ROWS = {
    "number of open market buys": "open_market_buys",
    "number of sells": "sells",
    "total insider trades": "total_trades",
    "number of shares bought": "shares_bought",
    "number of shares sold": "shares_sold",
    "total shares traded": "shares_traded",
    "net activity": "net_shares",
}


def insider_trades(symbol: str, *, limit: int = 15, max_age=DEFAULT_MAX_AGE) -> dict:
    """Insider activity counts for the last 3 and 12 months plus the latest trades."""
    sym = provider_symbol(symbol)
    path = f"company/{_q(sym)}/insider-trades?limit={int(limit)}&type=ALL&sortColumn=lastDate&sortOrder=DESC"
    data, meta = _get(path, max_age=max_age)
    out = _base(symbol, sym, [meta])
    out.update({"last_3_months": {}, "last_12_months": {}, "total_transactions": None, "recent_transactions": []})
    if meta["error"]:
        out["error"] = meta["error"]
        return out
    for key in ("numberOfTrades", "numberOfSharesTraded"):
        for r in (data.get(key) or {}).get("rows") or []:
            name = _INSIDER_ROWS.get(str(r.get("insiderTrade", "")).strip().lower())
            if name:
                out["last_3_months"][name] = parse_int(r.get("months3"))
                out["last_12_months"][name] = parse_int(r.get("months12"))
    tt = data.get("transactionTable") or {}
    out["total_transactions"] = parse_int(tt.get("totalRecords"))
    for r in (tt.get("table") or {}).get("rows") or []:
        out["recent_transactions"].append(
            {
                "insider": _clean(r.get("insider")),
                "relation": _clean(r.get("relation")),
                "date": parse_date(r.get("lastDate")),
                "transaction_type": _clean(r.get("transactionType")),
                "owner_type": _clean(r.get("ownType")),
                "shares_traded": parse_int(r.get("sharesTraded")),
                "price": parse_number(r.get("lastPrice")) or None,  # provider prints $0.00 when unknown
                "shares_held": parse_int(r.get("sharesHeld")),
            }
        )
    out["available"] = bool(out["last_3_months"] or out["recent_transactions"])
    if not out["available"]:
        out["error"] = "no insider data"
    return out


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------
def snapshot(symbol: str, parts=CORE_PARTS, *, from_date=None, to_date=None,
             max_age=DEFAULT_MAX_AGE) -> dict:
    """Run several adapter functions for one symbol."""
    funcs = {
        "summary": summary, "institutional": institutional, "earnings": earnings,
        "analyst": analyst, "financials": financials,
        "short_interest": short_interest, "insider_trades": insider_trades,
    }
    out = {"symbol": symbol, "provider_symbol": provider_symbol(symbol)}
    for part in parts:
        if part == "historical":
            t = _as_date(to_date) if to_date else dt.date.today()
            f = _as_date(from_date) if from_date else t - dt.timedelta(days=3653)
            out[part] = historical(symbol, f, t, max_age=max_age)
        elif part in funcs:
            out[part] = funcs[part](symbol, max_age=max_age)
        else:
            raise ValueError(f"unknown part {part!r}; choose from {ALL_PARTS}")
    return out


def fetch_many(symbols, parts=CORE_PARTS, *, workers: int = MAX_WORKERS, **kw) -> dict:
    """snapshot() for many symbols with at most 4 threads (requests stay <= ~2/s)."""
    symbols = list(symbols)
    workers = max(1, min(int(workers), MAX_WORKERS, len(symbols) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: snapshot(s, parts, **kw), symbols))
    return dict(zip(symbols, results))


def _trim_history(rec: dict, keep: int = 3) -> dict:
    rows = rec.get("rows") or []
    if len(rows) > 2 * keep:
        rec = dict(rec)
        rec["rows_omitted"] = len(rows) - 2 * keep
        rec["rows"] = rows[:keep] + rows[-keep:]
        rec["rows_note"] = f"first {keep} and last {keep} bars shown; use --full-history for all"
    return rec


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python3 -m screener.sources.nasdaq",
        description="Nasdaq.com public API adapter; prints JSON.",
    )
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--parts", default=",".join(ALL_PARTS),
                    help="comma list from: " + ",".join(ALL_PARTS))
    ap.add_argument("--from", dest="from_date", default=None, help="history start YYYY-MM-DD (default: 10y back)")
    ap.add_argument("--to", dest="to_date", default=None, help="history end YYYY-MM-DD (default: today)")
    ap.add_argument("--full-history", action="store_true", help="print every daily bar")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    args = ap.parse_args(argv)

    parts = tuple(p.strip() for p in args.parts.split(",") if p.strip())
    res = fetch_many(args.tickers, parts, workers=args.workers, from_date=args.from_date,
                     to_date=args.to_date, max_age=0 if args.refresh else DEFAULT_MAX_AGE)
    if not args.full_history:
        for rec in res.values():
            if "historical" in rec:
                rec["historical"] = _trim_history(rec["historical"])
    json.dump(res, sys.stdout, indent=2, allow_nan=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
