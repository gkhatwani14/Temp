"""FRED (Federal Reserve Bank of St. Louis) public series downloads.

Used for the risk-free rate of the Sharpe-ratio gate and for rate context:

    DTB3    3-Month Treasury Bill Secondary Market Rate, *Discount Basis*
    DGS3MO  Market Yield on U.S. Treasury Securities at 3-Month Constant Maturity,
            Quoted on an Investment Basis
    DGS10   Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity

(the titles above are only a reading aid; the authoritative label of every
record is the ``title`` / ``units`` / ``notes`` scraped from the series page.)

Public forms used (no API key, no login):
    data  https://fred.stlouisfed.org/graph/fredgraph.csv?id={ID}&cosd=YYYY-MM-DD
          -> "observation_date,{ID}" CSV, one row per period
    meta  https://fred.stlouisfed.org/series/{ID}
          -> title, units, seasonal adjustment, frequency, last update, notes
             and the latest observation as printed on the page (cross-check)

Provider quirks handled here
  * The host silently drops (read timeout, no HTTP status) requests that carry
    the shared client's browser User-Agent, but answers the HTTP library's own
    truthful User-Agent ("python-requests/x.y") and curl immediately.  This
    module therefore sends the requests default User-Agent on FRED calls -- it
    identifies the client honestly instead of posing as a browser.
  * Missing observations (market holidays) come as an empty field in the CSV
    and as "." on the HTML pages; both are skipped and counted, never filled.
  * A ``cosd`` later than the last observation makes FRED return the WHOLE
    series from its first date, so rows are always re-filtered on ``start``.
  * Daily H.15 rates are published with a one-business-day lag: on a Saturday
    the newest observation is normally Thursday's.

Every number returned is exactly the provider's figure; nothing is estimated,
interpolated or forward-filled.

CLI:  python3 -m screener.sources.fred [SERIES_ID ...] [--years N] [--start YYYY-MM-DD] [--full]
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from ..http import FetchError, fetch

PROVIDER = "FRED (Federal Reserve Bank of St. Louis)"
CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
SERIES_URL = "https://fred.stlouisfed.org/series/{id}"

DEFAULT_SERIES = ("DTB3", "DGS3MO", "DGS10")
RISK_FREE_SERIES = "DTB3"

# Truthful client identification (see module docstring).
_HEADERS = {"User-Agent": requests.utils.default_user_agent()}
_TIMEOUT = 25
_RETRIES = 3
_MAX_AGE = 6 * 3600

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
_MISSING = {"", ".", "NA", "N/A", "NAN", "-", "--"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_date(d) -> str | None:
    """Accept None / 'YYYY-MM-DD' / date / datetime -> 'YYYY-MM-DD'."""
    if d is None:
        return None
    if isinstance(d, _dt.datetime):
        return d.date().isoformat()
    if isinstance(d, _dt.date):
        return d.isoformat()
    s = str(d).strip()[:10]
    if not _DATE_RE.match(s):
        raise ValueError(f"start/end must be YYYY-MM-DD, got {d!r}")
    _dt.date.fromisoformat(s)  # validates the calendar date
    return s


def _years_before(day: _dt.date, years: int) -> _dt.date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - years, day=28)


def _to_float(raw: str) -> float | None:
    s = raw.strip().replace(",", "")
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get(url: str, max_age: float | None) -> tuple[dict | None, str | None]:
    """fetch() wrapper -> (response, None) or (None, error-text). Never raises."""
    try:
        res = fetch(url, headers=_HEADERS, max_age=max_age, retries=_RETRIES,
                    timeout=_TIMEOUT)
        return res, None
    except FetchError as e:
        if e.status is None:
            return None, f"unreachable (no HTTP response after {_RETRIES} tries): {e.detail}"
        if e.status == 404:
            return None, "HTTP 404 (series id not found on FRED)"
        return None, f"HTTP {e.status}: {(e.detail or '')[:120]!r}"
    except Exception as e:  # noqa: BLE001 - adapter must not take the pipeline down
        return None, f"{type(e).__name__}: {str(e)[:200]}"


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #
def series(series_id: str, start=None, end=None, *, max_age: float | None = _MAX_AGE) -> dict:
    """Dated observations of one FRED series from the public graph CSV.

    start / end: 'YYYY-MM-DD' (inclusive); None = whole series.
    Missing values are skipped (their dates are listed in ``missing_dates``).

    Returns
      {"series_id", "available", "error", "source", "url", "retrieved_at",
       "start", "end", "n_observations", "n_missing_skipped", "missing_dates",
       "n_unparseable", "unparseable_samples", "first_date", "last_date",
       "observations": [{"date": "YYYY-MM-DD", "value": float}, ...]}   # ascending
    """
    sid = str(series_id).strip().upper()
    start_s, end_s = _iso_date(start), _iso_date(end)
    url = CSV_URL.format(id=sid)
    if start_s:
        url += f"&cosd={start_s}"
    if end_s:
        url += f"&coed={end_s}"
    out: dict = {
        "series_id": sid, "available": False, "error": None,
        "source": f"{PROVIDER} {url}", "url": url, "retrieved_at": _now(),
        "start": start_s, "end": end_s,
        "n_observations": 0, "n_missing_skipped": 0, "missing_dates": [],
        "n_unparseable": 0, "unparseable_samples": [],
        "first_date": None, "last_date": None, "observations": [],
    }
    if not _ID_RE.match(sid):
        out["error"] = f"invalid series id {series_id!r}"
        return out

    res, err = _get(url, max_age)
    if res is None:
        out["error"] = err
        return out
    out["retrieved_at"] = res["retrieved_at"]

    lines = res["text"].lstrip("﻿").splitlines()
    header = [h.strip() for h in lines[0].split(",")] if lines else []
    if (len(header) != 2 or header[0].lower() not in ("observation_date", "date")
            or header[1].upper() != sid):
        out["error"] = ("unexpected payload (not an 'observation_date,%s' CSV): %r"
                        % (sid, (lines[0] if lines else "")[:120]))
        return out

    obs, missing, bad = [], [], []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        date = parts[0].strip()
        raw = parts[1].strip() if len(parts) > 1 else ""
        if len(parts) != 2 or not _DATE_RE.match(date):
            bad.append(line[:60])
            continue
        if (start_s and date < start_s) or (end_s and date > end_s):
            continue  # FRED ignores an out-of-range cosd and sends everything
        if raw.upper() in _MISSING:
            missing.append(date)
            continue
        val = _to_float(raw)
        if val is None:
            bad.append(line[:60])
            continue
        obs.append({"date": date, "value": val})
    obs.sort(key=lambda o: o["date"])

    out.update(
        available=True,
        n_observations=len(obs),
        n_missing_skipped=len(missing),
        missing_dates=missing,
        n_unparseable=len(bad),
        unparseable_samples=bad[:5],
        first_date=obs[0]["date"] if obs else None,
        last_date=obs[-1]["date"] if obs else None,
        observations=obs,
    )
    return out


# --------------------------------------------------------------------------- #
# series page metadata (the provider's own label / definition)
# --------------------------------------------------------------------------- #
def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", _html.unescape(s)).strip(" ,|: ")
    return s or None


def meta(series_id: str, *, max_age: float | None = _MAX_AGE) -> dict:
    """Title, units, frequency, update stamp and notes scraped from the series page.

    Also returns the latest observation as printed on the page
    (``page_latest_date`` / ``page_latest_value``) for cross-checking the CSV.
    Any field that cannot be located is None.
    """
    sid = str(series_id).strip().upper()
    url = SERIES_URL.format(id=sid)
    out: dict = {
        "series_id": sid, "available": False, "error": None,
        "source": f"{PROVIDER} {url}", "url": url, "retrieved_at": _now(),
        "title": None, "units": None, "seasonal_adjustment": None, "frequency": None,
        "data_source": None, "release": None, "last_updated": None,
        "next_release_date": None, "observation_range": None, "notes": None,
        "page_latest_date": None, "page_latest_value": None,
    }
    if not _ID_RE.match(sid):
        out["error"] = f"invalid series id {series_id!r}"
        return out
    res, err = _get(url, max_age)
    if res is None:
        out["error"] = err
        return out
    out["retrieved_at"] = res["retrieved_at"]
    text = res["text"]
    soup = BeautifulSoup(text, "lxml")

    def meta_tag(name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        return _clean(tag.get("content")) if tag and tag.get("content") else None

    short = meta_tag("citation_short_title")
    if short is not None and short.upper() != sid:
        out["error"] = f"series page is for {short!r}, not {sid}"
        return out
    out["title"] = meta_tag("citation_title") or meta_tag("og:title")

    el = soup.find(class_="series-meta-value-units")
    out["units"] = _clean(el.get_text(" ")) if el else None
    el = soup.find(class_="series-meta-value-frequency")
    out["frequency"] = _clean(el.get_text(" ")) if el else None
    m = re.search(r"series-meta-value-units\">\s*[^<]*</span>\s*,(?:&nbsp;|\s|</div>|<div[^>]*>)*"
                  r"([^<]+?)\s*<", text)
    out["seasonal_adjustment"] = _clean(m.group(1)) if m else None

    el = soup.find("a", class_="series-source")
    out["data_source"] = _clean(el.get_text(" ")) if el else None
    el = soup.find("a", class_="series-release")
    out["release"] = _clean(el.get_text(" ")) if el else None

    m = re.search(r"Updated:\s*<span[^>]*title=\"([^\"]+)\"", text)
    out["last_updated"] = _clean(m.group(1)) if m else None
    m = re.search(r"title=\"([^\"]+)\">\s*Next Release Date:", text)
    out["next_release_date"] = _clean(m.group(1)) if m else None
    out["observation_range"] = meta_tag("dcterms:PeriodOfTime")

    m = re.search(r"class=\"series-notes[^\"]*\">(.*?)<div class=\"series-citation\"", text, re.S)
    if m:
        notes = re.sub(r"<br\s*/?>", " ", m.group(1))
        notes = _clean(re.sub(r"<[^>]+>", "", notes))
        if notes:
            notes = notes.split("For questions on the data")[0].strip() or notes
            out["notes"] = notes[:800]

    m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{4}-\d{2}|Q[1-4] \d{4}|[A-Z][a-z]{2} \d{4}|\d{4})\s*:\s*"
                  r"(?:</span>\s*)?<span class=\"series-meta-observation-value\">\s*([^<]*?)\s*</span>",
                  text)
    if m:
        out["page_latest_date"] = m.group(1)
        out["page_latest_value"] = _to_float(m.group(2))
    out["available"] = True
    return out


_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _page_date_iso(label: str | None) -> str | None:
    """Series-page period label -> the CSV's period-start date.

    '2026-11-12' -> same; 'Oct 2026' -> '2026-10-01'; 'Q3 2026' -> '2026-07-01';
    '2026' -> '2026-01-01'. Anything else -> None (no comparison is made).
    """
    if not label:
        return None
    s = label.strip()
    if _DATE_RE.match(s):
        return s
    m = re.match(r"^([A-Z][a-z]{2}) (\d{4})$", s)
    if m and m.group(1) in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}-01"
    m = re.match(r"^Q([1-4]) (\d{4})$", s)
    if m:
        return f"{m.group(2)}-{(int(m.group(1)) - 1) * 3 + 1:02d}-01"
    if re.match(r"^\d{4}$", s):
        return f"{s}-01-01"
    return None


# --------------------------------------------------------------------------- #
# convenience views
# --------------------------------------------------------------------------- #
def latest(series_id: str, *, with_meta: bool = True, max_age: float | None = _MAX_AGE) -> dict:
    """Last non-missing observation of a series, its date and the provider's label.

    Returns
      {"series_id", "available", "error", "date", "value", "previous_date",
       "previous_value", "missing_dates_after_latest", "recent_missing_dates",
       "title", "units", "seasonal_adjustment", "frequency", "notes",
       "last_updated", "page_latest_date", "page_latest_value", "page_matches_csv",
       "source", "meta_source", "retrieved_at"}
    ``value`` is in the provider's ``units`` (for the Treasury rate series:
    percent per annum, e.g. 4.14 means 4.14%).
    """
    sid = str(series_id).strip().upper()
    today = _dt.datetime.now(_dt.timezone.utc).date()
    data: dict = {}
    for lookback in (45, 800, None):  # daily -> monthly/quarterly -> anything
        start = (today - _dt.timedelta(days=lookback)).isoformat() if lookback else None
        data = series(sid, start, max_age=max_age)
        if not data["available"] or len(data["observations"]) >= 2:
            break
    out: dict = {
        "series_id": sid, "available": False, "error": data.get("error"),
        "date": None, "value": None, "previous_date": None, "previous_value": None,
        "missing_dates_after_latest": [], "recent_missing_dates": [],
        "title": None, "units": None, "seasonal_adjustment": None, "frequency": None,
        "notes": None, "last_updated": None,
        "page_latest_date": None, "page_latest_value": None, "page_matches_csv": None,
        "source": data.get("source"), "meta_source": None,
        "retrieved_at": data.get("retrieved_at"),
    }
    if not data["available"]:
        return out
    obs = data["observations"]
    if not obs:
        out["error"] = "series has no non-missing observations"
        return out
    out.update(available=True, date=obs[-1]["date"], value=obs[-1]["value"])
    if len(obs) > 1:
        out.update(previous_date=obs[-2]["date"], previous_value=obs[-2]["value"])
    out["missing_dates_after_latest"] = [d for d in data["missing_dates"] if d > obs[-1]["date"]]
    out["recent_missing_dates"] = data["missing_dates"][-5:]

    if with_meta:
        md = meta(sid, max_age=max_age)
        out["meta_source"] = md["source"]
        if md["available"]:
            for k in ("title", "units", "seasonal_adjustment", "frequency", "notes",
                      "last_updated", "page_latest_date", "page_latest_value"):
                out[k] = md[k]
            page_date = _page_date_iso(md["page_latest_date"])
            if page_date is not None and md["page_latest_value"] is not None:
                out["page_matches_csv"] = (page_date == out["date"]
                                           and abs(md["page_latest_value"] - out["value"]) < 1e-9)
        else:
            out["meta_error"] = md["error"]
    return out


def history(series_id: str = RISK_FREE_SERIES, years: int = 3, asof=None, *,
            include_observations: bool = True, max_age: float | None = _MAX_AGE) -> dict:
    """Observations for the ``years`` years ending at ``asof`` (default: today, UTC).

    Adds plain descriptive statistics of the returned observations
    (``mean`` = arithmetic mean of the non-missing daily values, same units as
    the series). Nothing is annualised, compounded or converted.
    """
    end_day = _dt.date.fromisoformat(_iso_date(asof)) if asof else _dt.datetime.now(_dt.timezone.utc).date()
    start_day = _years_before(end_day, int(years))
    data = series(series_id, start_day.isoformat(), end_day.isoformat() if asof else None,
                  max_age=max_age)
    vals = [o["value"] for o in data["observations"]]
    data["window_years"] = int(years)
    data["window_end"] = end_day.isoformat()
    data["stats"] = {
        "definition": "simple statistics of the non-missing observations in the window, in series units",
        "n": len(vals),
        "mean": (sum(vals) / len(vals)) if vals else None,
        "min": min(vals) if vals else None,
        "max": max(vals) if vals else None,
        "first": data["observations"][0] if vals else None,
        "last": data["observations"][-1] if vals else None,
    }
    if not include_observations:
        data["observations"] = []
        data["observations_omitted"] = True
    return data


def snapshot(series_ids=DEFAULT_SERIES, *, history_id: str = RISK_FREE_SERIES, years: int = 3,
             include_observations: bool = True, max_age: float | None = _MAX_AGE) -> dict:
    """Latest value of each rate series plus the ``years``-year history of ``history_id``."""
    return {
        "source": f"{PROVIDER} https://fred.stlouisfed.org/",
        "retrieved_at": _now(),
        "latest": {str(s).upper(): latest(s, max_age=max_age) for s in series_ids},
        "history": history(history_id, years, include_observations=include_observations,
                           max_age=max_age),
    }


# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    ids, years, start, full = [], 3, None, False
    it = iter(argv)
    for a in it:
        if a == "--years":
            years = int(next(it))
        elif a == "--start":
            start = next(it)
        elif a == "--full":
            full = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            ids.append(a.upper())
    ids = ids or list(DEFAULT_SERIES)
    if start:
        out: dict = {"series": {s: series(s, start) for s in ids}}
    else:
        out = snapshot(ids, history_id=RISK_FREE_SERIES if RISK_FREE_SERIES in ids else ids[0],
                       years=years, include_observations=full)
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
