"""RSS parsing, evidence pool, clock and collection."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FIXTURES, NOW, FakeHttp

from stocknews.clock import _USEastern, build_context, pick_slot
from stocknews.collect import collect_all, ticker_matcher
from stocknews.evidence import EvidencePool, url_key
from stocknews.rss import FeedError, parse_feed
from stocknews.settings import limits, parse_tickers


# ---- RSS ---------------------------------------------------------------------------------
def test_parse_rss_items_and_cleanup():
    items = parse_feed((FIXTURES / "rss_sample.xml").read_bytes())
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "Microsoft & OpenAI expand cloud agreement, shares edge up"
    assert first["summary"] == "The companies extended their multi-year deal."
    assert first["published"] == datetime(2026, 11, 20, 19, 5, tzinfo=timezone.utc)
    assert items[1]["url"] == "https://feeds.example/markets/yields"  # from permalink guid
    assert items[1]["published"] == datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)
    assert items[2]["published"] is None


def test_parse_atom():
    items = parse_feed((FIXTURES / "atom_sample.xml").read_bytes())
    assert items == [{
        "title": "Federal Reserve issues FOMC statement on policy rate",
        "url": "https://atom.example/press/fomc",
        "summary": "The Committee decided to maintain the target range.",
        "published": datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc),
    }]


def test_rss_rejects_dtd_entities():
    with pytest.raises(FeedError):
        parse_feed((FIXTURES / "evil_dtd.xml").read_bytes())


# ---- evidence pool -----------------------------------------------------------------------
def _pool():
    return EvidencePool(NOW, NOW - timedelta(hours=24), NOW - timedelta(days=7), summary_chars=60)


def test_pool_dedupes_merges_tickers_and_orders_ids():
    pool = _pool()
    pool.add(title="Apple unveils new product line at event", url="https://x.example/a?utm_source=feed",
             source="Reuters", published=NOW - timedelta(hours=2), tickers=["AAPL"], origin="company:AAPL")
    pool.add(title="Apple unveils new product line at event", url="https://www.x.example/a/",
             source="Yahoo", published=NOW - timedelta(hours=1), tickers=["MSFT"], origin="company:MSFT")
    pool.add(title="Market wrap: stocks rise into the close", url="https://x.example/wrap", source="CNBC",
             published=NOW - timedelta(hours=3), origin="market")
    pool.add(title="Too old to keep around at all", url="https://x.example/old", source="CNBC",
             published=NOW - timedelta(days=9), origin="market")
    pool.add(title="short", url="https://x.example/s", source="CNBC", published=NOW, origin="market")
    pool.finalize(limits({}))
    assert list(pool.items) == ["E001", "E002"]
    assert pool.get("E001").title.startswith("Market wrap")  # market-wide first
    merged = pool.get("E002")
    assert set(merged.tickers) == {"AAPL", "MSFT"}
    assert merged.in_window


def test_pool_caps_and_context_flags():
    pool = _pool()
    lim = limits({"limits": {"companyNewsInWindow": 2, "companyNewsContext": 1}})
    for i in range(5):
        pool.add(title=f"AAPL in-window story number {i}", url=f"https://x.example/n{i}", source="S",
                 published=NOW - timedelta(hours=i + 1), tickers=["AAPL"], origin="company:AAPL")
    for i in range(3):
        pool.add(title=f"AAPL older context story {i}", url=f"https://x.example/c{i}", source="S",
                 published=NOW - timedelta(days=2 + i), tickers=["AAPL"], origin="company:AAPL")
    pool.finalize(lim)
    assert len(pool) == 3
    assert pool.in_window_count == 2
    line = pool.get("E003").prompt_line(timezone.utc)
    assert "| ctx |" in line and "AAPL" in line


def test_pool_live_additions_after_finalize():
    pool = _pool()
    pool.finalize(limits({}))
    ev = pool.add(title="Super Micro shares jump on orders", url="https://x.example/smci", source="Reuters",
                  published=NOW - timedelta(hours=1), tickers=["SMCI"], origin="tool:SMCI")
    assert ev.id == "E001" and "E001" in pool
    again = pool.add(title="Super Micro shares jump on orders", url="https://x.example/smci?utm_medium=x",
                     source="Yahoo", published=NOW, tickers=["SMCI"], origin="tool:SMCI")
    assert again is ev


def test_url_key_keeps_meaningful_query():
    assert url_key("https://finnhub.io/api/news?id=abc&utm_source=t") == "finnhub.io/api/news?id=abc"


def test_ticker_matcher():
    match = ticker_matcher([{"symbol": "META", "name": "Meta Platforms", "aliases": ["Facebook"]},
                            {"symbol": "AAPL", "name": "Apple", "aliases": []}])
    assert match("Facebook parent posts record ad sales") == ["META"]
    assert match("$AAPL and Apple suppliers") == ["AAPL"]
    assert match("Pineapple prices climb") == []


def test_parse_tickers():
    assert parse_tickers(" aapl, msft;nvda,BAD TICKER, aapl ") == ["AAPL", "MSFT", "NVDA"]


# ---- clock -------------------------------------------------------------------------------
def test_slots_and_windows():
    assert build_context(NOW).slot == "close"
    monday_morning = datetime(2026, 11, 23, 13, 35, tzinfo=timezone.utc)  # 8:35 AM ET
    ctx = build_context(monday_morning)
    assert ctx.slot == "premarket"
    assert ctx.now_utc - ctx.window_start_utc == timedelta(hours=72)
    assert build_context(datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)).slot == "weekend"
    assert ctx.edition_id == "2026-11-23-premarket"


def test_us_eastern_fallback_matches_zoneinfo():
    from zoneinfo import ZoneInfo

    ny, fallback = ZoneInfo("America/New_York"), _USEastern()
    for dt in [datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
               datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc),
               datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), datetime(2026, 12, 24, 23, 0, tzinfo=timezone.utc)]:
        assert dt.astimezone(ny).replace(tzinfo=None) == dt.astimezone(fallback).replace(tzinfo=None), dt


def test_labels_spanish():
    ctx = build_context(NOW, language="es")
    assert ctx.long_date() == "VIERNES 20 DE NOVIEMBRE DE 2026"
    assert ctx.time_label() == "16:35 ET"
    assert pick_slot(ctx.now_local) == "close"


# ---- collection --------------------------------------------------------------------------
def test_collect_all(ctx, cfg, settings, fake_fh, tmp_path):
    from stocknews.storage import JsonDoc

    http = FakeHttp({"cnbc": (FIXTURES / "rss_sample.xml").read_bytes()})
    cache = JsonDoc(str(tmp_path / "profiles.json"))
    snapshot, pool, stats = collect_all(ctx, cfg, settings, fake_fh, None, http, profile_cache=cache)
    assert "SPY" in snapshot.quotes and "XLK" in snapshot.quotes and "NVDA" in snapshot.quotes
    # top 20 by market cap from the configured pool; NFLX (watchlist) ranks 22nd
    assert len(snapshot.universe) == 20 and snapshot.top20[:4] == ["NVDA", "AAPL", "MSFT", "GOOGL"]
    assert [r["rank"] for r in snapshot.universe] == list(range(1, 21)) and "NFLX" not in snapshot.top20
    assert snapshot.universe[0]["marketCap"] == 4_450_000 * 1e6
    news_calls = [c[1] for c in fake_fh.calls if c[0] == "company_news"]
    assert news_calls == snapshot.top20 + ["NFLX"]  # top 20 + watchlist extras only
    assert [r["symbol"] for r in snapshot.earnings] == ["NVDA"]  # rows for uncovered tickers dropped
    titles = [e.title for e in pool.items.values()]
    assert "Old story outside the window about markets" not in titles  # market news: window only
    assert any("Microsoft & OpenAI" in t for t in titles)  # RSS item kept and tagged
    msft_rss = next(e for e in pool.items.values() if "OpenAI" in e.title)
    assert msft_rss.tickers == ["MSFT"] and msft_rss.source == "CNBC"
    assert any("rss marketwatch-top" in e for e in stats["errors"])  # unreachable feed noted, not fatal
    assert pool.by_ticker("AAPL")[0].origin == "company:AAPL"
    # profiles are cached: a second run within the max age does not call the profile endpoint again
    assert cache.load()["profiles"]["NVDA"]["marketCapitalization"] == 4_450_000
    fake_fh.calls.clear()
    collect_all(ctx, cfg, settings, fake_fh, None, http, profile_cache=cache)
    assert not [c for c in fake_fh.calls if c[0] == "profile"]


def test_rank_universe_orders_by_market_cap():
    from stocknews.market import rank_universe

    pool = [{"symbol": "AAA", "name": "A"}, {"symbol": "BBB", "name": "B"}, {"symbol": "CCC", "name": "C"},
            {"symbol": "DDD", "name": "D"}]
    quotes = {s: {"price": 1.0} for s in ("AAA", "BBB", "CCC")}  # DDD has no quote: skipped
    profiles = {"AAA": {"marketCapitalization": 100}, "BBB": {"marketCapitalization": 300}, "CCC": {}}
    ranked = rank_universe(pool, quotes, profiles, size=3)
    assert [(r["rank"], r["symbol"], r["marketCap"]) for r in ranked] == [(1, "BBB", 3e8), (2, "AAA", 1e8),
                                                                          (3, "CCC", None)]


def test_fmt_cap():
    from stocknews.market import fmt_cap

    assert fmt_cap(4.94e12) == "$4.94T" and fmt_cap(9.83e11) == "$983B" and fmt_cap(None) == "—"
