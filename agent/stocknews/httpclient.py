"""Small dependency-free HTTP client with retries, rate limiting and size caps."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

DEFAULT_UA = "stock-news-dashboard/1.0 (+https://github.com/)"
_SECRET_PARAMS = re.compile(r"(token|apikey|api_key|key)=[^&]+", re.IGNORECASE)


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def redact(url: str) -> str:
    return _SECRET_PARAMS.sub(r"\1=***", url)


class RateLimiter:
    """Spaces calls at least `min_interval` seconds apart (single-threaded use)."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval = max(0.0, min_interval)
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._clock()
        if self._last is not None:
            delta = now - self._last
            if delta < self.min_interval:
                self._sleep(self.min_interval - delta)
        self._last = self._clock()


class HttpClient:
    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 20.0, retries: int = 3,
                 max_bytes: int = 6_000_000, sleep: Callable[[float], None] = time.sleep):
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self._sleep = sleep

    def get(self, url: str, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None, limiter: RateLimiter | None = None) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise HttpError(f"unsupported URL scheme: {parsed.scheme!r}")
        full = url
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            full = f"{url}{'&' if '?' in url else '?'}{query}"
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        hdrs.update(headers or {})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if limiter:
                limiter.wait()
            req = urllib.request.Request(full, headers=hdrs, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (scheme checked)
                    data = resp.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise HttpError(f"response too large from {redact(full)}")
                return data
            except urllib.error.HTTPError as exc:
                last_error = HttpError(f"HTTP {exc.code} for {redact(full)}", status=exc.code)
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 1.5
                    log.warning("retrying %s after HTTP %s (%.1fs)", redact(full), exc.code, delay)
                    self._sleep(min(delay, 30.0))
                    continue
                raise last_error from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = HttpError(f"network error for {redact(full)}: {exc}")
                if attempt < self.retries:
                    self._sleep(2 ** attempt * 1.0)
                    continue
                raise last_error from None
        raise last_error or HttpError(f"request failed: {redact(full)}")

    def get_json(self, url: str, **kwargs) -> Any:
        raw = self.get(url, **kwargs)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"invalid JSON from {redact(url)}: {exc}") from None
