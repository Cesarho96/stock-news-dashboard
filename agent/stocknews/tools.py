"""Research tools the model can call during STEP 1."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from .clock import EditionContext
from .evidence import EvidencePool
from .finnhub import Finnhub, news_item_time
from .httpclient import HttpError
from .market import MarketSnapshot

log = logging.getLogger(__name__)
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class ResearchTools:
    def __init__(self, fh: Finnhub | None, pool: EvidencePool, snapshot: MarketSnapshot,
                 ctx: EditionContext, budget: int = 6):
        self.fh = fh
        self.pool = pool
        self.snapshot = snapshot
        self.ctx = ctx
        self.budget = budget
        self.calls = 0
        self.log: list[str] = []

    def execute(self, name: str, args: dict) -> tuple[dict, bool]:
        """Run a tool; returns (result, is_error)."""
        if self.calls >= self.budget:
            return {"error": "Tool budget exhausted. Continue with the evidence you have."}, True
        self.calls += 1
        if not self.fh:
            return {"error": "Market data provider not configured."}, True
        try:
            if name == "get_company_news":
                return self._company_news(args), False
            if name == "get_quotes":
                return self._quotes(args), False
        except HttpError as exc:
            log.warning("tool %s failed: %s", name, exc)
            return {"error": f"Data provider error: {exc}"}, True
        except (TypeError, ValueError) as exc:
            return {"error": f"Invalid arguments: {exc}"}, True
        return {"error": f"Unknown tool {name!r}."}, True

    def _company_news(self, args: dict) -> dict:
        symbol = str(args.get("symbol", "")).strip().upper()
        if not _TICKER.match(symbol):
            raise ValueError("symbol must be a ticker like SMCI")
        days = max(1, min(7, int(args.get("days") or 3)))
        end = self.ctx.now_local.date()
        start = end - timedelta(days=days)
        added = []
        for item in self.fh.company_news(symbol, start, end)[:30]:
            ev = self.pool.add(
                title=item.get("headline", ""), url=item.get("url", ""),
                source=item.get("source") or "Finnhub", published=news_item_time(item),
                summary=item.get("summary", ""), tickers=[symbol], origin=f"tool:{symbol}",
            )
            if ev and ev not in added:
                added.append(ev)
            if len(added) >= 8:
                break
        self.log.append(f"get_company_news({symbol}, {days}d) -> {len(added)} items")
        return {
            "symbol": symbol,
            "items": [
                {"id": e.id, "time": e.published.astimezone(self.ctx.tz).strftime("%m-%d %H:%M"),
                 "source": e.source, "new": e.in_window, "title": e.title, "summary": e.summary}
                for e in added
            ],
            "note": "Cite these IDs like any other evidence." if added else "No news found for this window.",
        }

    def ensure_quote(self, symbol: str) -> dict | None:
        """Return a quote for `symbol`, fetching it (outside the tool budget) if needed."""
        symbol = symbol.upper()
        q = self.snapshot.quote(symbol)
        if q or not self.fh or not _TICKER.match(symbol):
            return q
        try:
            q = self.fh.quote(symbol)
        except HttpError as exc:
            log.warning("quote for %s failed: %s", symbol, exc)
            return None
        if q:
            self.snapshot.quotes[symbol] = q
            if symbol not in self.snapshot.names:
                try:
                    profile = self.fh.profile(symbol)
                except HttpError:
                    profile = None
                self.snapshot.names[symbol] = (profile or {}).get("name") or symbol
        return q

    def _quotes(self, args: dict) -> dict:
        symbols = [str(s).strip().upper() for s in (args.get("symbols") or [])][:10]
        out = []
        for sym in symbols:
            if not _TICKER.match(sym):
                continue
            q = self.ensure_quote(sym)
            if q:
                out.append({"symbol": sym, "name": self.snapshot.names.get(sym, sym),
                            "price": round(q["price"], 2), "changePct": round(q["changePct"], 2)})
            else:
                out.append({"symbol": sym, "error": "no quote"})
        self.log.append(f"get_quotes({','.join(symbols)})")
        return {"quotes": out}
