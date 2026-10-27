"""One HTTP client for every source: retries, a polite per-host rate limit and a
small on-disk JSON cache so a re-run does not hammer the public APIs."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from varbot.config import SETTINGS

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 varbot/0.1"
)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class OfflineError(RuntimeError):
    """Raised when VARBOT_OFFLINE is set and the request has no cache entry."""


class _RateLimiter:
    """Minimum spacing between requests to the same host."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, min_interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = last + min_interval - now
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()


class Http:
    def __init__(self, cache_dir: Path | None = None, offline: bool | None = None) -> None:
        self.cache_dir = Path(cache_dir or SETTINGS.cache_dir)
        self.offline = SETTINGS.offline if offline is None else offline
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._limiter = _RateLimiter()
        # Per-host minimum spacing in seconds. CoinGecko's public tier allows
        # roughly 30 calls a minute; the others are more generous.
        self.min_interval = {
            "api.coingecko.com": 2.2,
            "pro-api.coingecko.com": 0.3,
            "api.binance.com": 0.15,
            "api.x.com": 1.0,
            "t.me": 1.0,
            "omni-client-api.prod.ap-northeast-1.variational.io": 1.0,
        }

    # -- cache -------------------------------------------------------------
    def _cache_path(self, url: str, params: dict | None, headers_key: str) -> Path:
        key = json.dumps([url, params or {}, headers_key], sort_keys=True)
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        host = urlsplit(url).netloc.replace(":", "_")
        return self.cache_dir / host / f"{digest}.json"

    def _read_cache(self, path: Path, ttl: float | None) -> Any | None:
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        age = time.time() - entry.get("fetched_at", 0)
        if ttl is not None and age > ttl and not self.offline:
            return None
        return entry

    def _write_cache(self, path: Path, body: Any, kind: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched_at": time.time(), "kind": kind, "body": body}))
        except OSError as exc:  # cache is best-effort
            log.debug("cache write failed for %s: %s", path, exc)

    # -- requests ----------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        ttl: float | None = 600,
        kind: str = "json",
        retries: int = 3,
        timeout: float = 30,
    ) -> Any:
        """GET with cache. `kind` is "json" or "text". `ttl` in seconds; None caches forever."""
        headers = headers or {}
        # Authorisation headers must not leak into the cache key; a hash is enough.
        headers_key = hashlib.sha256(json.dumps(sorted(headers.items())).encode()).hexdigest()[:8]
        path = self._cache_path(url, params, headers_key)
        cached = self._read_cache(path, ttl)
        if cached is not None:
            return cached["body"]
        if self.offline:
            raise OfflineError(f"offline and no cached response for {url}")

        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self._limiter.wait(host, self.min_interval.get(host, 0.2))
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
                time.sleep(min(2**attempt, 10))
                continue
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt * 2, 30)
                log.warning("GET %s -> %d, waiting %.0fs", url, resp.status_code, wait)
                last_exc = HttpError(resp.status_code, url, resp.text)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, url, resp.text)
            body = resp.json() if kind == "json" else resp.text
            self._write_cache(path, body, kind)
            return body
        assert last_exc is not None
        raise last_exc

    def post_json(self, url: str, payload: dict, *, timeout: float = 30) -> Any:
        """Uncached POST (used for Telegram delivery)."""
        if self.offline:
            raise OfflineError(f"offline: not posting to {url}")
        resp = self.session.post(url, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, url, resp.text)
        return resp.json()


_default: Http | None = None


def http() -> Http:
    global _default
    if _default is None:
        _default = Http()
    return _default
