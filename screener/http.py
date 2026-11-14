"""Shared HTTP helper for all scrapers: browser UA, per-host throttling,
exponential backoff on 429/5xx, and an on-disk response cache.

Every source module goes through `fetch()` so that re-runs are cheap,
rate limits are respected, and each raw response is kept on disk with its
retrieval timestamp (needed for the "cite source + retrieval date" rule).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("SCREENER_CACHE", ROOT / ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
# SEC asks automated clients to identify themselves; keep it generic.
SEC_UA = "equity-screener-research/1.0 (contact: admin@example.com)"

# Minimum seconds between requests to the same host (politeness + 429 avoidance).
HOST_MIN_INTERVAL = {
    "query1.finance.yahoo.com": 0.35,
    "query2.finance.yahoo.com": 0.35,
    "finance.yahoo.com": 0.5,
    "www.zacks.com": 0.6,
    "quote-feed.zacks.com": 0.3,
    "stockanalysis.com": 0.35,
    "api.stockanalysis.com": 0.25,
    "scanner.tradingview.com": 0.5,
    "finviz.com": 0.6,
    "api.nasdaq.com": 0.3,
    "data.sec.gov": 0.15,
    "www.sec.gov": 0.15,
    "markets.ft.com": 0.5,
    "news.google.com": 0.5,
}
DEFAULT_MIN_INTERVAL = 0.3

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_guard = threading.Lock()
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": BROWSER_UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
            }
        )
        _local.session = s
    return s


def _throttle(host: str) -> None:
    with _guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        gap = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
        wait = _host_last.get(host, 0.0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()


def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / h[:2] / f"{h}.json"


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int | None, detail: str = ""):
        super().__init__(f"{status} for {url} {detail}".strip())
        self.url, self.status, self.detail = url, status, detail


def fetch(
    url: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    json_body: dict | None = None,
    data: str | bytes | None = None,
    headers: dict | None = None,
    cookies: dict | None = None,
    max_age: float | None = 20 * 3600,
    retries: int = 5,
    timeout: float = 40,
    ok_statuses: tuple[int, ...] = (200,),
    session: requests.Session | None = None,
) -> dict:
    """Fetch a URL (cached). Returns {"text", "status", "url", "retrieved_at"}.

    max_age: seconds a cached copy stays valid; None = never re-fetch;
             0 = always re-fetch.
    Raises FetchError when the final status is not in ok_statuses.
    """
    key = json.dumps(
        [method, url, params, json_body, data if isinstance(data, str) else None],
        sort_keys=True,
        default=str,
    )
    path = _cache_path(key)
    if max_age != 0 and path.exists():
        try:
            cached = json.loads(path.read_text())
            if max_age is None or time.time() - cached["ts"] < max_age:
                return cached
        except Exception:
            pass

    host = urlparse(url).netloc
    sess = session or _session()
    last_status: int | None = None
    detail = ""
    for attempt in range(retries):
        _throttle(host)
        try:
            r = sess.request(
                method,
                url,
                params=params,
                json=json_body,
                data=data,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:  # network hiccup / proxy busy
            detail = repr(e)[:200]
            time.sleep(min(60, 2 ** attempt + random.random()))
            continue
        last_status = r.status_code
        if r.status_code in ok_statuses:
            out = {
                "text": r.text,
                "status": r.status_code,
                "url": r.url,
                "ts": time.time(),
                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out))
            return out
        if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 524):
            detail = r.text[:120]
            time.sleep(min(90, 3 * 2 ** attempt + random.random() * 2))
            continue
        detail = r.text[:200]
        break
    raise FetchError(url, last_status, detail)


def fetch_json(url: str, **kw):
    """fetch() + json.loads; returns (obj, retrieved_at)."""
    res = fetch(url, **kw)
    return json.loads(res["text"]), res["retrieved_at"]
