"""A clearly labeled demo edition with synthetic data, for previewing the layout offline.

Every headline starts with "[Demo]", every link points to example.com, and the page shows
a demo banner. Nothing here is real market data or news (prices and market caps are
random numbers; the top-20 ranking is built from them).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .clock import build_context
from .compose import compose_edition
from .evidence import EvidencePool
from .i18n import t
from .market import MarketSnapshot, coverage, rank_universe
from .settings import Settings, company_info, limits, load_config

DEMO_SOURCES = ["Demo Wire", "Sample Markets Daily", "Example Business News", "Placeholder Finance"]
SENTIMENTS = ["bullish", "bearish", "neutral", "mixed"]


def build_demo_edition(settings: Settings | None = None, now: datetime | None = None,
                       slot: str | None = None, cfg: dict | None = None) -> dict:
    settings = settings or Settings()
    cfg = cfg or load_config(settings)
    lim = limits(cfg)
    ctx = build_context(now, tz_name=cfg.get("timezone", "America/New_York"),
                        tz_label=cfg.get("timezoneLabel", "ET"), slot=slot, language=settings.language)
    rng = random.Random(ctx.date + ctx.slot)
    lang = ctx.language
    info = company_info(cfg)
    universe = cfg.get("universe") or {}
    pool_cfg = universe.get("pool") or []

    snapshot = MarketSnapshot()
    for item in cfg["strip"]:
        snapshot.names[item["symbol"]] = item["name"]
    for sym, c in info.items():
        snapshot.names[sym] = c.get("name") or sym
    symbols = list(dict.fromkeys([s["symbol"] for s in cfg["strip"]] + [s["etf"] for s in cfg["sectors"]]
                                 + list(info)))
    as_of = (ctx.now_utc - timedelta(minutes=5)).isoformat()
    for sym in symbols:
        price = rng.uniform(40, 650)
        pct = rng.uniform(-3.2, 3.6)
        prev = price / (1 + pct / 100)
        snapshot.quotes[sym] = {"symbol": sym, "price": price, "change": price - prev, "changePct": pct,
                                "high": price * 1.012, "low": price * 0.985, "open": prev * 1.002,
                                "prevClose": prev, "asOf": as_of}
    profiles = {p["symbol"]: {"marketCapitalization": 4.8e6 * 0.88 ** i * rng.uniform(0.95, 1.05)}
                for i, p in enumerate(pool_cfg)}  # USD millions, roughly descending
    snapshot.universe = rank_universe(pool_cfg, snapshot.quotes, profiles, int(universe.get("size", 20)))
    top20, extras = coverage(cfg, snapshot)
    covered = top20 + extras

    today = ctx.now_local.date()
    picks = rng.sample(covered, min(3, len(covered)))
    snapshot.earnings = [
        {"symbol": sym, "date": (today + timedelta(days=3 + i * 2)).isoformat(),
         "hour": ["amc", "bmo", "amc"][i], "epsEstimate": round(rng.uniform(0.5, 4.0), 2),
         "revenueEstimate": rng.uniform(5e9, 9e10)}
        for i, sym in enumerate(picks)
    ]

    pool = EvidencePool(ctx.now_utc, ctx.window_start_utc, ctx.context_start_utc, lim["summaryChars"])
    n = 0

    def add(title: str, tickers: list[str], origin: str, hours_ago: float, summary: str) -> None:
        nonlocal n
        n += 1
        pool.add(title=f"[Demo] {title}", url=f"https://example.com/demo/story-{n}",
                 source=DEMO_SOURCES[n % len(DEMO_SOURCES)],
                 published=ctx.now_utc - timedelta(hours=hours_ago),
                 summary=f"Synthetic placeholder text. {summary}", tickers=tickers, origin=origin)

    lead_pair = top20[:2]
    add(f"Sample market wrap: {' and '.join(lead_pair)} lead the largest caps in demo session", lead_pair,
        "market", 2, "Layout test for a market wrap item about the top 20.")
    add(f"Sample alert: regulator opens a review involving {top20[2]}", [top20[2]], "market", 3,
        "Layout test for an alert item.")
    add("Sample macro story: central bank minutes preview", [], "market", 5, "Layout test for a macro item.")
    add("Sample deals story: two demo companies agree to merge", [], "market", 7, "Layout test for a deal item.")
    for i, sym in enumerate(covered):
        add(f"Sample headline about {sym}: product and demand update", [sym], f"company:{sym}",
            1 + i * 0.5, f"Layout test for a {sym} company item.")
        add(f"Sample analyst note on {sym}", [sym], f"company:{sym}", 8 + i * 0.5,
            "Layout test for an analyst item.")
    pool.finalize(lim)

    market_ids = [e.id for e in pool.market_wide()]
    by = {sym: [e.id for e in pool.by_ticker(sym)] for sym in covered}
    mover = max(top20, key=lambda s: abs(snapshot.quotes[s]["changePct"]))
    sector_of = {sym: (info.get(sym) or {}).get("sector") for sym in covered}
    sectors_present = [s for s in cfg["sectors"] if s["id"] in sector_of.values()]
    placeholder = "Synthetic placeholder sentence used to preview the layout."
    analysis = {
        "headline": t(lang, "demo.headline"),
        "summary": t(lang, "demo.body"),
        "top": {
            "whatHappened": {"title": "[Demo] Sample lead story about the top 20",
                             "paragraph": " ".join([placeholder] * 3), "tickers": lead_pair,
                             "evidence": market_ids[:1] + by[lead_pair[0]][:1]},
            "keyAlert": {"category": "risk", "title": "[Demo] Sample key alert for a top-20 company",
                         "whatHappened": placeholder, "whyItMatters": placeholder, "tickers": [top20[2]],
                         "evidence": market_ids[1:2]},
            "marketMover": {"ticker": mover, "title": f"[Demo] Sample explanation of the move in {mover}",
                            "whatHappened": placeholder, "whyItMatters": placeholder, "evidence": by[mover][:1]},
        },
        "top20": [
            {"ticker": sym, "sentiment": rng.choice(SENTIMENTS),
             "note": f"[Demo] Placeholder one-line note on why {sym} moved.",
             "evidence": by[sym][:1] if i % 4 else []}
            for i, sym in enumerate(top20)
        ],
        "latest": [
            {"theme": "macro", "title": "[Demo] Sample macro story", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [], "evidence": market_ids[2:3]},
            {"theme": "deals", "title": "[Demo] Sample deals story", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [], "evidence": market_ids[3:4]},
        ] + [
            {"theme": ["ai-tech", "earnings", "company"][i % 3],
             "title": f"[Demo] Sample story about {sym}", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [sym], "evidence": by[sym][1:2]}
            for i, sym in enumerate(covered[3:8]) if sym != mover
        ],
        "watchlist": [
            {"ticker": sym, "sentiment": rng.choice(SENTIMENTS), "summary": placeholder,
             "whatToWatch": "Placeholder: next scheduled company event.", "evidence": by[sym][:2]}
            for sym in extras
        ],
        "sectors": [
            {"sector": s["id"], "title": f"[Demo] Sample {s['id']} sector story", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [],
             "evidence": [by[sym][1] for sym in covered[8:] if sector_of.get(sym) == s["id"]
                          and len(by[sym]) > 1][:1]}
            for s in sectors_present
        ],
        "radar": [
            {"kind": "catalyst", "text": "[Demo] Placeholder upcoming catalyst.", "evidence": []},
            {"kind": "risk", "text": "[Demo] Placeholder risk to monitor.", "evidence": []},
            {"kind": "data", "text": "[Demo] Placeholder economic data release.", "evidence": []},
        ],
    }
    analysis["sectors"] = [s for s in analysis["sectors"] if s["evidence"]]
    qa = {"status": "demo", "model": "demo", "modelLabel": "demo", "evidenceCount": len(pool),
          "inWindow": pool.in_window_count, "toolCalls": 0, "repairs": 0, "notes": [], "sourceErrors": [],
          "release": settings.release_id}
    edition = compose_edition(ctx, cfg, settings, snapshot, pool, analysis, qa, demo=True)
    edition["intro"]["stamp"] = t(lang, "demo.stamp")
    return edition


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(build_demo_edition(now=datetime.now(timezone.utc)), indent=1)[:2000])
