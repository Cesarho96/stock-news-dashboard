"""Finnhub REST client (quotes, company news, market news, earnings, market status).

Free plan: about 60 calls/minute for personal, non-commercial use. The token is sent in
the X-Finnhub-Token header so it never appears in URLs or logs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from .httpclient import HttpClient, HttpError, RateLimiter

log = logging.getLogger(__name__)

BASE = "https://finnhub.io/api/v1"


class FinnhubError(Exception):
    pass


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class Finnhub:
    def __init__(self, api_key: str, http: HttpClient | None = None, min_interval: float = 1.05):
        if not api_key:
            raise FinnhubError("Finnhub API key is missing (set FINNHUB_API_KEY or FINNHUB_KEY_PARAM)")
        self.api_key = api_key
        self.http = http or HttpClient()
        self.limiter = RateLimiter(min_interval)
        self.calls = 0

    def _get(self, path: str, **params):
        self.calls += 1
        return self.http.get_json(
            BASE + path,
            params=params,
            headers={"X-Finnhub-Token": self.api_key},
            limiter=self.limiter,
        )

    def quote(self, symbol: str) -> dict | None:
        """Latest quote. Share-class tickers are retried with a dash (BRK.B -> BRK-B)."""
        q = self._quote(symbol)
        if q is None and "." in symbol:
            q = self._quote(symbol.replace(".", "-"))
            if q:
                q["symbol"] = symbol
        return q

    def _quote(self, symbol: str) -> dict | None:
        data = self._get("/quote", symbol=symbol)
        if not isinstance(data, dict):
            return None
        price = _num(data.get("c"))
        if not price:  # Finnhub returns zeros for unknown symbols
            return None
        prev = _num(data.get("pc"))
        change = _num(data.get("d"))
        pct = _num(data.get("dp"))
        if change is None and prev:
            change = price - prev
        if pct is None and prev:
            pct = (price - prev) / prev * 100
        ts = int(data.get("t") or 0)
        return {
            "symbol": symbol,
            "price": price,
            "change": change or 0.0,
            "changePct": pct or 0.0,
            "high": _num(data.get("h")),
            "low": _num(data.get("l")),
            "open": _num(data.get("o")),
            "prevClose": prev,
            "asOf": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
        }

    def company_news(self, symbol: str, start: date, end: date) -> list[dict]:
        data = self._get("/company-news", symbol=symbol, **{"from": start.isoformat(), "to": end.isoformat()})
        return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []

    def market_news(self, category: str = "general") -> list[dict]:
        data = self._get("/news", category=category)
        return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []

    def earnings_calendar(self, start: date, end: date) -> list[dict]:
        data = self._get("/calendar/earnings", **{"from": start.isoformat(), "to": end.isoformat()})
        rows = data.get("earningsCalendar") if isinstance(data, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def market_status(self) -> dict | None:
        try:
            data = self._get("/stock/market-status", exchange="US")
        except HttpError as exc:  # not available on every plan
            log.info("market status unavailable: %s", exc)
            return None
        return data if isinstance(data, dict) else None

    def profile(self, symbol: str) -> dict | None:
        data = self._get("/stock/profile2", symbol=symbol)
        if not (isinstance(data, dict) and data.get("name")) and "." in symbol:
            data = self._get("/stock/profile2", symbol=symbol.replace(".", "-"))
        return data if isinstance(data, dict) and data.get("name") else None


def news_item_time(item: dict) -> datetime | None:
    ts = item.get("datetime")
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
    except (TypeError, ValueError, OSError):
        return None
