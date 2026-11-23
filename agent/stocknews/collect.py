"""Collect market data and evidence from every configured source.

Each source fails independently: an outage in one feed is logged and noted in the
edition's QA notes instead of stopping the run.

Companies covered: the S&P 500 top 20 (ranked by market cap from the configured pool)
plus any watchlist tickers outside that top 20.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .clock import EditionContext
from .evidence import EvidencePool
from .finnhub import Finnhub, news_item_time
from .httpclient import HttpClient, HttpError
from .market import MarketSnapshot, rank_universe
from .rss import FeedError, parse_feed
from .sec import SecEdgar
from .settings import Settings, company_info, limits
from .storage import JsonDoc

log = logging.getLogger(__name__)


def _unique(seq):
    return list(dict.fromkeys(seq))


def ticker_matcher(companies: list[dict]):
    """Return f(text) -> tickers mentioned (by symbol, company name or alias).

    Names and aliases are matched case-sensitively (proper nouns). Symbols of one or two
    letters (V, MA, GE) only match in explicit forms like "$V" or "(V)" to avoid noise.
    """
    patterns = []
    for c in companies:
        sym = c["symbol"]
        names = [c.get("name") or ""] + list(c.get("aliases") or [])
        alts = sorted({re.escape(n) for n in names if n and len(n) >= 3}, key=len, reverse=True)
        rx_names = re.compile(r"(?<![\w&])(" + "|".join(alts) + r")(?![\w&])") if alts else None
        if len(sym) <= 2:
            rx_sym = re.compile(r"(\$" + re.escape(sym) + r"(?![A-Za-z0-9])|\(\s*(?:NYSE:|NASDAQ:)?\s*"
                                + re.escape(sym) + r"\s*\))")
        else:
            rx_sym = re.compile(r"(?<![A-Za-z0-9$.])\$?" + re.escape(sym) + r"(?![A-Za-z0-9])")
        patterns.append((sym, rx_sym, rx_names))

    def match(text: str) -> list[str]:
        found = []
        for sym, rx_sym, rx_names in patterns:
            if rx_sym.search(text) or (rx_names and rx_names.search(text)):
                found.append(sym)
        return found

    return match


def load_profiles(fh: Finnhub | None, symbols: list[str], cache: JsonDoc, now: datetime,
                  max_age_hours: float, stats: dict) -> dict[str, dict]:
    """Company profiles (market cap, name), cached so the pool costs ~40 calls a day, not a run."""
    doc = cache.load() or {}
    profiles: dict[str, dict] = dict(doc.get("profiles") or {})
    fetched_at = None
    try:
        fetched_at = datetime.fromisoformat(doc["fetchedAt"]) if doc.get("fetchedAt") else None
    except (TypeError, ValueError):
        fetched_at = None
    stale = fetched_at is None or now - fetched_at > timedelta(hours=max_age_hours)
    todo = symbols if stale else [s for s in symbols if s not in profiles]
    if fh and todo:
        refreshed = 0
        for sym in todo:
            try:
                p = fh.profile(sym)
            except HttpError as exc:
                stats["errors"].append(f"finnhub profile {sym}: {exc}"[:200])
                continue
            if p:
                profiles[sym] = {k: p.get(k) for k in ("name", "marketCapitalization", "shareOutstanding",
                                                        "finnhubIndustry")}
                refreshed += 1
        if refreshed:
            cache.save({"fetchedAt": (now if stale else (fetched_at or now)).isoformat(),
                        "profiles": profiles})
        stats["sources"]["finnhub:profiles"] = refreshed
    return profiles


def collect_all(ctx: EditionContext, cfg: dict, settings: Settings, fh: Finnhub | None,
                sec: SecEdgar | None, http: HttpClient,
                profile_cache: JsonDoc | None = None) -> tuple[MarketSnapshot, EvidencePool, dict]:
    lim = limits(cfg)
    snapshot = MarketSnapshot()
    pool = EvidencePool(ctx.now_utc, ctx.window_start_utc, ctx.context_start_utc, lim["summaryChars"])
    stats: dict = {"errors": [], "sources": {}}
    info = company_info(cfg)
    universe = cfg.get("universe") or {}
    pool_cfg = universe.get("pool") or []
    pool_syms = [p["symbol"] for p in pool_cfg]
    watch = [w["symbol"] for w in cfg.get("watchlist", [])]
    match = ticker_matcher(list(info.values()))

    def note_error(where: str, exc: Exception) -> None:
        msg = f"{where}: {exc}"
        log.warning(msg)
        stats["errors"].append(msg[:200])

    for item in cfg.get("strip", []):
        snapshot.names[item["symbol"]] = item["name"]
    for sector in cfg.get("sectors", []):
        label = (sector.get("label") or {}).get(ctx.language) or sector["id"]
        snapshot.names.setdefault(sector["etf"], f"{label} ETF")
    for sym, c in info.items():
        snapshot.names[sym] = c.get("name") or sym

    # ---- quotes, profiles and the top-20 ranking ----------------------------------
    if fh:
        symbols = _unique([s["symbol"] for s in cfg.get("strip", [])]
                          + [s["etf"] for s in cfg.get("sectors", [])] + pool_syms + watch)
        for sym in symbols:
            try:
                q = fh.quote(sym)
                if q:
                    snapshot.quotes[sym] = q
                else:
                    stats["errors"].append(f"finnhub quote: no data for {sym}")
            except HttpError as exc:
                note_error(f"finnhub quote {sym}", exc)
    profiles = load_profiles(fh, pool_syms, profile_cache or JsonDoc(None), ctx.now_utc,
                             lim["profileMaxAgeHours"], stats)
    snapshot.universe = rank_universe(pool_cfg, snapshot.quotes, profiles, int(universe.get("size", 20)))
    top20 = snapshot.top20
    extras = [s for s in watch if s not in top20]
    covered = top20 + extras
    log.info("top %d: %s | extra watchlist: %s", len(top20), ",".join(top20), ",".join(extras) or "-")

    if fh:
        snapshot.status = fh.market_status()
        today = ctx.now_local.date()
        try:
            rows = fh.earnings_calendar(today, today + timedelta(days=lim["earningsDaysAhead"]))
            snapshot.earnings = sorted(
                [r for r in rows if str(r.get("symbol", "")).upper() in covered],
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
        for sym in covered:
            try:
                items = fh.company_news(sym, start, today)
            except HttpError as exc:
                note_error(f"finnhub company news {sym}", exc)
                continue
            for item in items:
                related = [r.strip().upper() for r in str(item.get("related") or "").split(",") if r.strip()]
                tickers = _unique([sym] + [r for r in related if r in covered])
                pool.add(title=item.get("headline", ""), url=item.get("url", ""),
                         source=item.get("source") or "Finnhub", published=news_item_time(item),
                         summary=item.get("summary", ""), tickers=tickers, origin=f"company:{sym}")
            stats["sources"][f"finnhub:{sym}"] = len(items)

    # ---- SEC EDGAR filings --------------------------------------------------------
    if sec:
        for sym in covered:
            try:
                filings = sec.recent_filings(sym, (info.get(sym) or {}).get("cik"), ctx.context_start_utc,
                                             limit=lim["filingsPerTicker"])
            except (HttpError, ValueError) as exc:
                note_error(f"sec {sym}", exc)
                continue
            for f in filings:
                pool.add(title=f["title"], url=f["url"], source="SEC EDGAR", published=f["published"],
                         summary=f["summary"], tickers=[sym], origin=f"sec:{sym}", kind="filing")
            stats["sources"][f"sec:{sym}"] = len(filings)

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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
