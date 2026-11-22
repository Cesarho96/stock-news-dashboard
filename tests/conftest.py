"""Shared fixtures: fake Finnhub/SEC/HTTP and a scripted fake Bedrock client."""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from stocknews.clock import build_context  # noqa: E402
from stocknews.settings import Settings, load_config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 20, 21, 35, tzinfo=timezone.utc)  # Fri 4:35 PM ET (after the close)


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def settings():
    s = Settings()
    s.finnhub_min_interval = 0
    s.max_repairs = 2
    s.max_tool_calls = 4
    return s


@pytest.fixture
def cfg(settings):
    c = load_config(settings)
    c["watchlist"] = c["watchlist"][:4]  # AAPL, MSFT, NVDA, AVGO
    return c


@pytest.fixture
def ctx():
    return build_context(NOW, language="en")


class FakeFinnhub:
    """Deterministic stand-in for stocknews.finnhub.Finnhub."""

    def __init__(self, now=NOW):
        self.now = now
        self.calls = []

    def _ts(self, hours_ago):
        return int((self.now - timedelta(hours=hours_ago)).timestamp())

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        if symbol == "NOPE":
            return None
        base = 100 + (sum(map(ord, symbol)) % 400)
        pct = ((sum(map(ord, symbol)) % 9) - 4) * 0.7
        return {"symbol": symbol, "price": float(base), "change": base * pct / 100, "changePct": pct,
                "high": base * 1.01, "low": base * 0.98, "open": base * 0.99, "prevClose": base / (1 + pct / 100),
                "asOf": (self.now - timedelta(minutes=5)).isoformat()}

    def market_status(self):
        return {"isOpen": False, "session": "post-market", "holiday": None}

    def earnings_calendar(self, start, end):
        return [
            {"symbol": "NVDA", "date": (start + timedelta(days=4)).isoformat(), "hour": "amc",
             "epsEstimate": 1.12, "revenueEstimate": 6.1e10},
            {"symbol": "ZZZZ", "date": start.isoformat(), "hour": "bmo"},
        ]

    def market_news(self, category="general"):
        self.calls.append(("market_news", category))
        return [
            {"headline": "Stocks close higher as Treasury yields ease after inflation data", "url": "https://news.example/a1",
             "source": "Reuters", "datetime": self._ts(1), "summary": "The S&P 500 rose as softer inflation data eased rate worries."},
            {"headline": "Fed officials signal patience on further rate moves", "url": "https://news.example/a2?utm_source=x",
             "source": "Bloomberg", "datetime": self._ts(3), "summary": "Two officials said policy is in a good place."},
            {"headline": "Nvidia suppliers rally on data center demand outlook", "url": "https://news.example/a3",
             "source": "CNBC", "datetime": self._ts(5), "summary": "Chip stocks gained after upbeat comments."},
            {"headline": "Oil slips as inventories build more than expected", "url": "https://news.example/a4",
             "source": "MarketWatch", "datetime": self._ts(6), "summary": "Crude fell for a second day."},
            {"headline": "Old story outside the window about markets", "url": "https://news.example/old",
             "source": "Reuters", "datetime": self._ts(60), "summary": "Too old for the window."},
        ]

    def company_news(self, symbol, start, end):
        self.calls.append(("company_news", symbol))
        if symbol == "SMCI":
            return [{"headline": "Super Micro shares jump on server orders", "url": "https://news.example/smci1",
                     "source": "Reuters", "datetime": self._ts(2), "summary": "Orders rose.", "related": "SMCI"}]
        return [
            {"headline": f"{symbol} unveils new product line at investor event", "url": f"https://news.example/{symbol}/1",
             "source": "Reuters", "datetime": self._ts(2), "summary": f"{symbol} detailed its roadmap.", "related": symbol},
            {"headline": f"Analyst raises price target on {symbol} citing demand", "url": f"https://news.example/{symbol}/2",
             "source": "Yahoo", "datetime": self._ts(8), "summary": "The broker lifted its target.", "related": symbol},
            {"headline": f"{symbol} context story from earlier in the week", "url": f"https://news.example/{symbol}/3",
             "source": "Yahoo", "datetime": self._ts(70), "summary": "Background.", "related": symbol},
        ]

    def profile(self, symbol):
        return {"name": {"SMCI": "Super Micro Computer"}.get(symbol, symbol)}


class FakeHttp:
    """Serves RSS fixtures by URL; anything else fails like a network error."""

    def __init__(self, routes=None):
        self.routes = routes or {}

    def get(self, url, params=None, headers=None, limiter=None):
        from stocknews.httpclient import HttpError

        for key, body in self.routes.items():
            if key in url:
                return body
        raise HttpError(f"no route for {url}")

    def get_json(self, url, **kw):
        return json.loads(self.get(url, **kw))


class _Body(io.BytesIO):
    pass


class FakeClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class ScriptedBedrock:
    """Fake bedrock-runtime client. `script` is a list of callables(body)->response dict or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def invoke_model(self, modelId, body, contentType, accept):
        payload = json.loads(body)
        self.requests.append(payload)
        if not self.script:
            raise AssertionError("unexpected extra Bedrock call")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        resp = step(payload) if callable(step) else step
        resp.setdefault("usage", {"input_tokens": 1000, "output_tokens": 200})
        return {"body": _Body(json.dumps(resp).encode())}


def text_response(obj, stop="end_turn"):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return {"content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
            "stop_reason": stop}


def tool_response(*uses):
    return {"content": [{"type": "tool_use", "id": f"tu{i}", "name": n, "input": a} for i, (n, a) in enumerate(uses)],
            "stop_reason": "tool_use"}


@pytest.fixture
def fake_fh():
    return FakeFinnhub()
