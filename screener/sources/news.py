"""Google News RSS search -> dated headlines (evidence for "why did it fall?").

Public feed (no key, no login), one request per query:

    https://news.google.com/rss/search?q={query}+when:{days}d&hl=en-US&gl=US&ceid=US:en

What the feed gives per item: headline (with " - Publisher" appended),
publisher name + site, pubDate, and a news.google.com redirect link.  It does
NOT give article text, and this module does not fetch articles.

This module only COLLECTS headlines.  It never classifies a company, scores
severity or concludes why a stock fell -- that is the reviewer's job.  The one
derived attribute, ``title_match``, is a literal string test (does the headline
contain the company name / ticker), reported so a reviewer can separate
headlines about the company from search noise.

Provider behaviour worth knowing
  * At most ~100 items per query, ordered by Google's relevance, not by date.
  * Matching is fuzzy and full-text: a hit for '"Acme" fraud' can be an article
    about somebody else that merely mentions Acme.  Read the headline.
  * ``when:Nd`` is applied by Google; items whose pubDate is still older than
    the window are dropped here and counted in ``n_outside_window``.
  * pubDate is often date-only precision (stamped 07:00/08:00 GMT = midnight
    US Pacific); do not read intraday order into it.
  * Headlines can be truncated by the provider ("...").
  * The feed's own notice says it is provided for personal, non-commercial
    feed-reader use.

CLI:
  python3 -m screener.sources.news TICKER [TICKER ...] [--days 75] [--max-per-query 40] [--flat]
                                   [--name "Company name"] [--alias "Other name"]
  python3 -m screener.sources.news --query "free text query" [--days 60]
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

from ..http import FetchError, fetch

PROVIDER = "Google News RSS"
RSS_URL = "https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-US&gl=US&ceid=US:en"

_MAX_AGE = 6 * 3600
_RETRIES = 3
_TIMEOUT = 30
_MAX_THREADS = 4
_gate = threading.BoundedSemaphore(_MAX_THREADS)  # never >4 concurrent calls to the host

# (key, topic as worded in the screen's brief, query template).  In Google's
# query syntax OR binds tighter than the implicit AND, so
#   "X" SEC investigation OR subpoena   ==   "X" AND SEC AND (investigation OR subpoena)
# {name} is the quoted company name (plus quoted aliases joined with OR).
SCAN_QUERIES: list[tuple[str, str, str]] = [
    ("fraud", "fraud", "{name} fraud"),
    ("accounting_restatement", "accounting restatement",
     '{name} restatement OR restate OR restates OR "accounting irregularities" OR "material weakness"'),
    ("short_seller_report", "short seller report",
     '{name} "short seller" OR "short-seller" OR "short report"'),
    ("going_concern", "going concern", '{name} "going concern"'),
    ("withdraws_guidance", "withdraws / suspends guidance",
     "{name} withdraws OR suspends OR pulls guidance OR outlook OR forecast"),
    ("sec_investigation", "SEC investigation / subpoena",
     "{name} SEC investigation OR subpoena OR probe"),
    ("doj_probe", "DOJ probe",
     '{name} DOJ OR "Justice Department" OR "Department of Justice" probe OR investigation'),
    ("class_action", "class action", '{name} "class action"'),
    ("fda_rejection", "FDA rejection / clinical hold",
     '{name} FDA rejection OR rejects OR "complete response letter" OR "clinical hold"'),
    ("delisting", "delisting", "{name} delisting OR delisted OR delist"),
    ("bankruptcy", "bankruptcy", '{name} bankruptcy OR "Chapter 11" OR insolvency'),
    ("ceo_cfo_resigns", "CEO/CFO resigns",
     '{name} CEO OR CFO resigns OR "steps down" OR departure OR resignation OR ousted'),
    ("earnings_plunge", "earnings plunge",
     "{name} earnings stock OR shares plunges OR plunge OR tumbles OR sinks OR slumps OR drops"),
    ("guidance_cut", "guidance cut",
     "{name} cuts OR lowers OR slashes OR trims guidance OR outlook OR forecast"),
    ("downgrade", "downgrade", "{name} downgrade OR downgrades OR downgraded"),
    ("stock_falls_ticker", "general: '{ticker} stock falls'", "{ticker} stock falls"),
    ("stock_falls_name", "general: company name + shares falling",
     "{name} stock OR shares falls OR falling OR drops OR plunges OR tumbles OR sinks OR slides"),
    ("why_stock_down", "general: why the stock is down",
     "{name} why stock OR shares down OR dropped OR fell OR falling OR plunged OR crushed"),
    ("company_stock_news", "general: company name + stock (broadest, highest recall)",
     "{name} stock"),
]

# Names headlines use when they differ from the registered company name
# (search terms only -- never data values).  Callers can pass more via `aliases`.
# A short alias that is a leading part of the registered name ("NetScout" for
# "NetScout Systems") replaces the longer form in the query, which widens recall.
NEWS_ALIASES: dict[str, tuple[str, ...]] = {
    "AGM": ("Farmer Mac",),
    "BZ": ("BOSS Zhipin",),
    "HALO": ("Halozyme",),
    "NTCT": ("NetScout",),
    "IONS": ("Ionis",),
    "CORT": ("Corcept",),
    "NRIX": ("Nurix",),
    "URGN": ("UroGen",),
    "AXON": ("Axon",),
    "SOUN": ("SoundHound",),
    "OPEN": ("Opendoor",),
    "XIFR": ("XPLR",),
    "VCYT": ("Veracyte",),
}

_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|llc|lp|l\.p|plc|"
    r"n\.v|nv|s\.a|sa|ag|se|holdings|holding)\.?$", re.I)
_SHARE_DESCRIPTOR = re.compile(
    r"\s+(?:-\s+.*|class [a-z]\b.*|common (?:stock|shares).*|ordinary shares.*|"
    r"american deposit[ao]ry shares.*|sponsored adr.*|\badr\b.*|\bads\b.*)$", re.I)
_GENERIC_TAIL = {
    "therapeutics", "pharmaceuticals", "pharma", "biosciences", "sciences", "systems",
    "technologies", "technology", "education", "enterprise", "enterprises", "international",
    "infrastructure", "interactive", "energy", "group", "financial", "bancorp", "software",
    "networks", "industries", "solutions", "resources", "partners", "trust", "property",
    "power", "ai", "labs", "global", "brands", "communications",
}
_EXCH = r"(?:NASDAQ|NYSE|NYSEAMERICAN|NYSEARCA|NYSEMKT|AMEX|CBOE|LSE|LON)"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# name handling
# --------------------------------------------------------------------------- #
def search_name(company_name: str | None) -> str | None:
    """Registered name -> the name as headlines print it.

    'Grand Canyon Education, Inc.' -> 'Grand Canyon Education'; 'F5, Inc.' -> 'F5';
    'KANZHUN LIMITED - American Depository Shares' -> 'KANZHUN'; 'Greif Inc.' -> 'Greif'.
    """
    if not company_name:
        return None
    s = re.sub(r"\s+", " ", str(company_name)).strip()
    s = _SHARE_DESCRIPTOR.sub("", s).strip(" ,.-")
    for _ in range(3):
        new = _LEGAL_SUFFIX.sub("", s).strip(" ,.-")
        if new == s or not new:
            break
        s = new
    return s or None


def _core_name(short: str | None) -> str | None:
    """Drop trailing generic words: 'Halozyme Therapeutics' -> 'Halozyme' (weak match key)."""
    if not short:
        return None
    words = short.split()
    while len(words) > 1 and words[-1].lower().strip(".,") in _GENERIC_TAIL:
        words.pop()
    core = " ".join(words)
    return core if core != short and len(core) >= 4 else None


def _word_re(term: str, flags=re.I) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", flags)


def _matcher(name: str | None, aliases: list[str], ticker: str | None):
    """Build title -> 'name' | 'alias' | 'ticker' | 'core_name' | None (literal tests only)."""
    name_re = _word_re(name) if name else None
    alias_res = [_word_re(a) for a in aliases if a]
    core = _core_name(name)
    core_re = _word_re(core) if core else None
    tick_res: list[re.Pattern] = []
    if ticker:
        t = re.escape(ticker)
        tick_res = [
            re.compile(r"\(\s*(?:" + _EXCH + r"\s*:\s*)?" + t + r"\s*\)"),          # (LOPE) (NASDAQ:LOPE)
            re.compile(_EXCH + r"\s*:\s*" + t + r"(?![A-Za-z0-9])"),                 # NYSE: DT
            re.compile(r"(?<![A-Za-z0-9])" + t + r"\s*:\s*" + _EXCH),                # DT:NYSE
            re.compile(r"\$" + t + r"(?![A-Za-z0-9])"),                              # $DT
            re.compile(r"(?<![A-Za-z0-9])" + t + r"\.US(?![A-Za-z0-9])"),            # LOPE.US
            re.compile(r"^" + t + r":\s"),                                           # "LOPE: ..."
            re.compile(r"(?<![A-Za-z0-9])" + t + r"\s+(?:[Ss]tock|[Ss]hares)(?![A-Za-z])"),  # FFIV Stock
        ]

    def match(title: str | None) -> str | None:
        if not title:
            return None
        if name_re and name_re.search(title):
            return "name"
        if any(r.search(title) for r in alias_res):
            return "alias"
        if any(r.search(title) for r in tick_res):
            return "ticker"
        if core_re and core_re.search(title):
            return "core_name"
        return None

    return match


def _news_ticker(ticker: str) -> str:
    """Pipeline symbol -> the form headlines use (BRK/B, BRK-B -> BRK.B)."""
    return str(ticker).strip().upper().replace("/", ".").replace("-", ".")


# --------------------------------------------------------------------------- #
# feed
# --------------------------------------------------------------------------- #
def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _strip_publisher(raw_title: str, publisher: str | None) -> str:
    """Google appends ' - Publisher'; some outlets also append ' | Publisher' themselves."""
    title = raw_title.strip()
    if publisher:
        for sep in (" - ", " | "):
            if title.endswith(sep + publisher):
                title = title[: -len(sep + publisher)].strip()
    return title


def _parse_pubdate(raw: str | None) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def search(query: str, days: int = 60, *, max_items: int | None = None,
           max_age: float | None = _MAX_AGE) -> dict:
    """Run one Google News RSS search restricted to the last ``days`` days.

    Returns
      {"query", "days", "available", "error", "source", "url", "retrieved_at",
       "window_start", "n_returned", "n_outside_window", "n_undated",
       "n_duplicate_titles", "truncated_to",
       "items": [{"title", "publisher", "publisher_url", "published" (ISO-8601 UTC),
                  "published_date", "link", "rank", "source", "retrieved_at"}, ...]}
    Items keep Google's relevance order (``rank`` 1 = first in the feed).
    ``source`` on every record is the provider + exact feed URL; the news outlet
    is ``publisher``.  On any failure ``available`` is False and ``items`` is [].
    """
    days = max(1, int(days))
    url = RSS_URL.format(q=quote_plus(str(query).strip()), days=days)
    source = f"{PROVIDER} {url}"
    out: dict = {
        "query": query, "days": days, "available": False, "error": None,
        "source": source, "url": url, "retrieved_at": _now(), "window_start": None,
        "n_returned": 0, "n_outside_window": 0, "n_undated": 0, "n_duplicate_titles": 0,
        "truncated_to": None, "items": [],
    }
    try:
        with _gate:
            res = fetch(url, max_age=max_age, retries=_RETRIES, timeout=_TIMEOUT)
    except FetchError as e:
        if e.status == 429:
            out["error"] = "HTTP 429 (rate limited by Google News) - treated as unavailable"
        elif e.status is None:
            out["error"] = f"unreachable (no HTTP response): {e.detail}"
        else:
            out["error"] = f"HTTP {e.status}: {(e.detail or '')[:120]!r}"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out

    out["retrieved_at"] = res["retrieved_at"]
    final_host = urlparse(res.get("url") or url).netloc
    if final_host != "news.google.com":
        out["error"] = f"redirected to {final_host} (consent / bot wall) - treated as unavailable"
        return out
    try:
        root = ET.fromstring(res["text"])
    except ET.ParseError as e:
        out["error"] = f"payload is not RSS/XML ({e}); first bytes {res['text'][:80]!r}"
        return out
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        out["error"] = f"unexpected XML root <{root.tag}>"
        return out

    try:
        fetched = _dt.datetime.strptime(res["retrieved_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc)
    except ValueError:
        fetched = _dt.datetime.now(_dt.timezone.utc)
    cutoff = fetched - _dt.timedelta(days=days)
    out["window_start"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    items, seen = [], set()
    raw_items = channel.findall("item")
    out["n_returned"] = len(raw_items)
    for rank, it in enumerate(raw_items, 1):
        raw_title = (it.findtext("title") or "").strip()
        if not raw_title:
            continue
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None else None
        publisher_url = src.get("url") if src is not None else None
        title = _strip_publisher(raw_title, publisher)
        published = _parse_pubdate(it.findtext("pubDate"))
        if published is None:
            out["n_undated"] += 1
        elif published < cutoff:
            out["n_outside_window"] += 1
            continue
        key = (_norm_title(title), publisher)
        if key in seen:
            out["n_duplicate_titles"] += 1
            continue
        seen.add(key)
        items.append({
            "title": title,
            "publisher": publisher or None,
            "publisher_url": publisher_url or None,
            "published": published.strftime("%Y-%m-%dT%H:%M:%SZ") if published else None,
            "published_date": published.strftime("%Y-%m-%d") if published else None,
            "link": (it.findtext("link") or "").strip() or None,
            "rank": rank,
            "source": source,
            "retrieved_at": res["retrieved_at"],
        })
    if max_items is not None and len(items) > max_items:
        out["truncated_to"] = max_items
        items = items[:max_items]
    out["items"] = items
    out["available"] = True
    return out


# --------------------------------------------------------------------------- #
# existential scan
# --------------------------------------------------------------------------- #
def _query_names(short: str | None, aliases, tick: str) -> list[str]:
    """Names to OR together in a query; drops a name that merely extends another one
    ('NetScout Systems' when 'NetScout' is present) because the shorter phrase covers it."""
    names = [str(n).strip() for n in [short, *(aliases or ()), *NEWS_ALIASES.get(tick, ())] if n]
    names = list(dict.fromkeys(n for n in names if n))
    keep = []
    for n in names:
        covered = any(o.lower() != n.lower() and _word_re(o).search(n) for o in names)
        if not covered:
            keep.append(n)
    return keep


def scan_queries(company_name: str | None, ticker: str, aliases=None) -> list[dict]:
    """The exact query strings existential_scan() will run (no network)."""
    tick = _news_ticker(ticker)
    names = _query_names(search_name(company_name), aliases, tick)
    name_expr = " OR ".join(f'"{n}"' for n in names)
    out = []
    for key, topic, tmpl in SCAN_QUERIES:
        if "{name}" in tmpl and not name_expr:
            continue
        out.append({"key": key, "topic": topic.format(ticker=tick),
                    "query": tmpl.format(name=name_expr, ticker=tick)})
    return out


def existential_scan(company_name: str | None, ticker: str, days: int = 75, *,
                     aliases=None, max_per_query: int | None = 40,
                     max_age: float | None = _MAX_AGE) -> dict:
    """Targeted headline sweep for one company; evidence only, no verdict.

    Runs the SCAN_QUERIES set (fraud, accounting restatement, short seller report,
    going concern, withdraws/suspends guidance, SEC investigation/subpoena, DOJ probe,
    class action, FDA rejection/clinical hold, delisting, bankruptcy, CEO/CFO resigns,
    earnings plunge, guidance cut, downgrade) plus the general '{ticker} stock falls',
    company-name "shares falling", "why is the stock down" and '"name" stock' queries.

    Returns
      {"ticker", "company_name", "search_names", "days", "window_start", "available",
       "n_queries", "n_queries_failed", "errors": {key: error}, "n_unique_headlines",
       "n_unique_company_headlines", "source", "retrieved_at", "note",
       "queries": [{"key", "topic", "query", "url", "source", "retrieved_at", "available",
                    "error", "n_returned", "n_outside_window", "n_in_window", "n_kept",
                    "items": [{"title", "publisher", "publisher_url", "published",
                               "published_date", "link", "rank", "title_match",
                               "matched_queries", "duplicate_of", "source", "retrieved_at"}]}]}
    De-duplication: the same headline (normalised title) is kept in every query group
    that returned it, but only its first occurrence (query order above) has
    ``duplicate_of`` None; later ones point at that first group.  ``matched_queries``
    lists every query that returned the headline.  Use unique_headlines() for the flat
    de-duplicated list.
    ``title_match``: 'name' | 'alias' | 'ticker' | 'core_name' | None -- literal test of
    the headline text only ('core_name' = name without generic tail words; weak).
    A query that fails (429, wall, network) has available False and is listed in
    ``errors``; an empty ``items`` with available True means Google returned nothing.
    """
    tick = _news_ticker(ticker)
    short = search_name(company_name)
    alias_list = list(dict.fromkeys([*(aliases or ()), *NEWS_ALIASES.get(tick, ())]))
    plan = scan_queries(company_name, ticker, aliases)
    match = _matcher(short, alias_list, tick)

    with ThreadPoolExecutor(max_workers=_MAX_THREADS) as pool:
        results = list(pool.map(lambda p: search(p["query"], days, max_age=max_age), plan))

    first_seen: dict[str, str] = {}
    matched: dict[str, list[str]] = {}
    groups, errors = [], {}
    for p, res in zip(plan, results):
        items = res["items"]
        if max_per_query is not None:
            items = items[:max_per_query]
        local_seen, kept = set(), []
        for it in items:
            norm = _norm_title(it["title"])
            if norm in local_seen:  # same headline syndicated by another outlet
                continue
            local_seen.add(norm)
            matched.setdefault(norm, []).append(p["key"])
            dup_of = first_seen.get(norm)
            if dup_of is None:
                first_seen[norm] = p["key"]
            rec = dict(it)
            rec["title_match"] = match(it["title"])
            rec["matched_queries"] = matched[norm]  # shared list, complete after the loop
            rec["duplicate_of"] = dup_of
            kept.append(rec)
        if not res["available"]:
            errors[p["key"]] = res["error"]
        groups.append({
            "key": p["key"], "topic": p["topic"], "query": p["query"],
            "url": res["url"], "source": res["source"], "retrieved_at": res["retrieved_at"],
            "available": res["available"], "error": res["error"],
            "n_returned": res["n_returned"], "n_outside_window": res["n_outside_window"],
            "n_in_window": len(res["items"]), "n_kept": len(kept), "items": kept,
        })
    # freeze the shared lists so the result is plain, independent JSON data
    for g in groups:
        for rec in g["items"]:
            rec["matched_queries"] = list(rec["matched_queries"])

    uniq = [r for g in groups for r in g["items"] if r["duplicate_of"] is None]
    ok = [g for g in groups if g["available"]]
    return {
        "ticker": tick, "company_name": company_name,
        "search_names": _query_names(short, aliases, tick),
        "days": int(days),
        "window_start": min((r["window_start"] for r in results if r["window_start"]), default=None),
        "available": bool(ok),
        "n_queries": len(groups), "n_queries_failed": len(groups) - len(ok), "errors": errors,
        "n_unique_headlines": len(uniq),
        "n_unique_company_headlines": sum(1 for r in uniq if r["title_match"]),
        "source": f"{PROVIDER} {RSS_URL.format(q='{query}', days=max(1, int(days)))}",
        "retrieved_at": max((g["retrieved_at"] for g in groups), default=_now()),
        "note": ("Headlines only, exactly as returned by the feed; not verified, not classified. "
                 "title_match is a literal string test. Zero items for a query means the feed "
                 "returned nothing, not that the event did not happen."),
        "queries": groups,
    }


def unique_headlines(scan: dict, *, company_only: bool = False) -> list[dict]:
    """Flat, de-duplicated headline list from an existential_scan() result, newest first.

    company_only=True keeps only headlines whose text names the company or ticker
    (title_match is not None).  Undated items sort last.
    """
    rows = [dict(r) for g in scan.get("queries", []) for r in g["items"]
            if r["duplicate_of"] is None and (r["title_match"] or not company_only)]
    rows.sort(key=lambda r: (r["published"] is not None, r["published"] or ""), reverse=True)
    return rows


# --------------------------------------------------------------------------- #
def _lookup_name(ticker: str) -> str | None:
    try:
        from ..universe import load_universe
        df = load_universe()
        hit = df[df["symbol"].str.upper() == ticker.upper()]
        return str(hit.iloc[0]["name"]) if len(hit) else None
    except Exception:  # noqa: BLE001 - name lookup is a CLI convenience only
        return None


def _main(argv: list[str]) -> int:
    tickers, days, name, aliases, query, cap, flat = [], None, None, [], None, 40, False
    it = iter(argv)
    for a in it:
        if a == "--days":
            days = int(next(it))
        elif a == "--name":
            name = next(it)
        elif a == "--alias":
            aliases.append(next(it))
        elif a == "--query":
            query = next(it)
        elif a == "--max-per-query":
            cap = int(next(it))
        elif a == "--flat":
            flat = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            tickers.append(a.upper())
    if query:
        out: dict = search(query, days or 60)
    elif not tickers:
        print(__doc__)
        return 2
    else:
        out = {}
        for t in tickers:
            company = name if (name and len(tickers) == 1) else _lookup_name(t)
            scan = existential_scan(company, t, days or 75, aliases=aliases, max_per_query=cap)
            if flat:
                scan["headlines"] = unique_headlines(scan, company_only=True)
                scan.pop("queries")
            out[t] = scan
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
