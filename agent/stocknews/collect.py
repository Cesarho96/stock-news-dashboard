"""Collect market data and evidence from every configured source.

Each source fails independently: an outage in one feed is logged and noted in the
edition's QA notes instead of stopping the run.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from .clock import EditionContext
from .evidence import EvidencePool
from .finnhub import Finnhub, news_item_time
from .httpclient import HttpClient, HttpError
from .market import MarketSnapshot
from .rss import FeedError, parse_feed
from .sec import SecEdgar
from .settings import Settings, limits

log = logging.getLogger(__name__)


def _unique(seq):
    return list(dict.fromkeys(seq))


def ticker_matcher(watchlist: list[dict]):
    """Return f(text) -> tickers mentioned (by symbol, company name or alias)."""
    patterns = []
    for w in watchlist:
        sym = w["symbol"]
        names = [w.get("name") or ""] + list(w.get("aliases") or [])
        alts = [re.escape(n) for n in names if n and len(n) >= 3]
        rx_names = re.compile(r"\b(" + "|".join(alts) + r")\b", re.IGNORECASE) if alts else None
        rx_sym = re.compile(r"(?<![A-Za-z0-9$])\$?" + re.escape(sym) + r"(?![A-Za-z0-9])")
        patterns.append((sym, rx_sym, rx_names))

    def match(text: str) -> list[str]:
        found = []
        for sym, rx_sym, rx_names in patterns:
            if rx_sym.search(text) or (rx_names and rx_names.search(text)):
                found.append(sym)
        return found

    return match


def collect_all(ctx: EditionContext, cfg: dict, settings: Settings, fh: Finnhub | None,
                sec: SecEdgar | None, http: HttpClient) -> tuple[MarketSnapshot, EvidencePool, dict]:
    lim = limits(cfg)
    snapshot = MarketSnapshot()
    pool = EvidencePool(ctx.now_utc, ctx.window_start_utc, ctx.context_start_utc, lim["summaryChars"])
    stats: dict = {"errors": [], "sources": {}}
    watch = [w["symbol"] for w in cfg["watchlist"]]
    match = ticker_matcher(cfg["watchlist"])

    def note_error(where: str, exc: Exception) -> None:
        msg = f"{where}: {exc}"
        log.warning(msg)
        stats["errors"].append(msg[:200])

    for item in cfg.get("strip", []):
        snapshot.names[item["symbol"]] = item["name"]
    for sector in cfg.get("sectors", []):
        label = (sector.get("label") or {}).get(ctx.language) or sector["id"]
        snapshot.names.setdefault(sector["etf"], f"{label} ETF")
    for w in cfg["watchlist"]:
        snapshot.names[w["symbol"]] = w.get("name") or w["symbol"]

    # ---- Finnhub: quotes, status, earnings, market + company news -------------------
    if fh:
        symbols = _unique([s["symbol"] for s in cfg.get("strip", [])]
                          + [s["etf"] for s in cfg.get("sectors", [])] + watch)
        for sym in symbols:
            try:
                q = fh.quote(sym)
                if q:
                    snapshot.quotes[sym] = q
                else:
                    stats["errors"].append(f"finnhub quote: no data for {sym}")
            except HttpError as exc:
                note_error(f"finnhub quote {sym}", exc)
        snapshot.status = fh.market_status()
        today = ctx.now_local.date()
        try:
            rows = fh.earnings_calendar(today, today + timedelta(days=lim["earningsDaysAhead"]))
            snapshot.earnings = sorted(
                [r for r in rows if str(r.get("symbol", "")).upper() in watch],
                key=lambda r: (r.get("date") or "", r.get("symbol") or ""),
            )
        except HttpError as exc:
            note_error("finnhub earnings calendar", exc)
        try:
            n = 0
            for item in fh.market_news("general"):
                text = f"{item.get('headline', '')} {item.get('summary', '')}"
                pool.add(title=item.get("headline", ""), url=item.get("url", ""),
                         source=item.get("source") or "Finnhub", published=news_item_time(item),
                         summary=item.get("summary", ""), tickers=match(text), origin="market")
                n += 1
            stats["sources"]["finnhub:market"] = n
        except HttpError as exc:
            note_error("finnhub market news", exc)
        start = ctx.context_start_utc.astimezone(ctx.tz).date()
        for sym in watch:
            try:
                items = fh.company_news(sym, start, today)
            except HttpError as exc:
                note_error(f"finnhub company news {sym}", exc)
                continue
            for item in items:
                related = [r.strip().upper() for r in str(item.get("related") or "").split(",") if r.strip()]
                tickers = _unique([sym] + [r for r in related if r in watch])
                pool.add(title=item.get("headline", ""), url=item.get("url", ""),
                         source=item.get("source") or "Finnhub", published=news_item_time(item),
                         summary=item.get("summary", ""), tickers=tickers, origin=f"company:{sym}")
            stats["sources"][f"finnhub:{sym}"] = len(items)

    # ---- SEC EDGAR filings --------------------------------------------------------
    if sec:
        for w in cfg["watchlist"]:
            try:
                filings = sec.recent_filings(w["symbol"], w.get("cik"), ctx.context_start_utc,
                                             limit=lim["filingsPerTicker"])
            except (HttpError, ValueError) as exc:
                note_error(f"sec {w['symbol']}", exc)
                continue
            for f in filings:
                pool.add(title=f["title"], url=f["url"], source="SEC EDGAR", published=f["published"],
                         summary=f["summary"], tickers=[w["symbol"]], origin=f"sec:{w['symbol']}",
                         kind="filing")
            stats["sources"][f"sec:{w['symbol']}"] = len(filings)

    # ---- RSS feeds ----------------------------------------------------------------
    if settings.rss_enabled:
        for feed in cfg.get("rss", []):
            try:
                items = parse_feed(http.get(feed["url"], headers={"Accept": "application/rss+xml, application/xml, */*"}))
            except (HttpError, FeedError) as exc:
                note_error(f"rss {feed.get('id')}", exc)
                continue
            for item in items:
                text = f"{item['title']} {item['summary']}"
                pool.add(title=item["title"], url=item["url"], source=feed.get("name") or feed["id"],
                         published=item["published"], summary=item["summary"], tickers=match(text),
                         origin=f"rss:{feed['id']}")
            stats["sources"][f"rss:{feed['id']}"] = len(items)

    pool.finalize(lim)
    snapshot.errors = stats["errors"]
    log.info("collected %d quotes, %d evidence items (%d in window), %d errors",
             len(snapshot.quotes), len(pool), pool.in_window_count, len(stats["errors"]))
    return snapshot, pool, stats
