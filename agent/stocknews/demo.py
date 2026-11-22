"""A clearly labeled demo edition with synthetic data, for previewing the layout offline.

Every headline starts with "[Demo]", every link points to example.com, and the page shows
a demo banner. Nothing here is real market data or news.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .clock import build_context
from .compose import compose_edition
from .evidence import EvidencePool
from .i18n import t
from .market import MarketSnapshot
from .settings import Settings, limits, load_config

DEMO_SOURCES = ["Demo Wire", "Sample Markets Daily", "Example Business News", "Placeholder Finance"]


def build_demo_edition(settings: Settings | None = None, now: datetime | None = None,
                       slot: str | None = None, cfg: dict | None = None) -> dict:
    settings = settings or Settings()
    cfg = cfg or load_config(settings)
    lim = limits(cfg)
    ctx = build_context(now, tz_name=cfg.get("timezone", "America/New_York"),
                        tz_label=cfg.get("timezoneLabel", "ET"), slot=slot, language=settings.language)
    rng = random.Random(ctx.date + ctx.slot)
    lang = ctx.language

    snapshot = MarketSnapshot()
    symbols = ([s["symbol"] for s in cfg["strip"]] + [s["etf"] for s in cfg["sectors"]]
               + [w["symbol"] for w in cfg["watchlist"]])
    for item in cfg["strip"]:
        snapshot.names[item["symbol"]] = item["name"]
    for w in cfg["watchlist"]:
        snapshot.names[w["symbol"]] = w["name"]
    as_of = (ctx.now_utc - timedelta(minutes=5)).isoformat()
    for sym in symbols:
        price = rng.uniform(40, 650)
        pct = rng.uniform(-3.2, 3.6)
        prev = price / (1 + pct / 100)
        snapshot.quotes[sym] = {"symbol": sym, "price": price, "change": price - prev, "changePct": pct,
                                "high": price * 1.012, "low": price * 0.985, "open": prev * 1.002,
                                "prevClose": prev, "asOf": as_of}
    today = ctx.now_local.date()
    picks = rng.sample([w["symbol"] for w in cfg["watchlist"]], 3)
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

    add("Sample macro story: central bank minutes preview", [], "market", 2, "Layout test for a macro item.")
    add("Sample market wrap: broad indexes mixed in demo session", [], "market", 3, "Layout test for a wrap item.")
    add("Sample policy story: regulator opens comment period", [], "market", 5, "Layout test for a policy item.")
    add("Sample deals story: two demo companies agree to merge", [], "market", 7, "Layout test for a deal item.")
    for i, w in enumerate(cfg["watchlist"]):
        add(f"Sample headline about {w['symbol']}: product and demand update", [w["symbol"]],
            f"company:{w['symbol']}", 1 + i * 0.7, f"Layout test for a {w['symbol']} company item.")
        add(f"Sample analyst note on {w['symbol']}", [w["symbol"]], f"company:{w['symbol']}",
            9 + i, "Layout test for an analyst item.")
    pool.finalize(lim)

    ids = list(pool.items)
    market_ids = [e.id for e in pool.market_wide()]
    by = {w["symbol"]: [e.id for e in pool.by_ticker(w["symbol"])] for w in cfg["watchlist"]}
    mover = max(cfg["watchlist"], key=lambda w: abs(snapshot.quotes[w["symbol"]]["changePct"]))["symbol"]
    sectors_present = [s for s in cfg["sectors"] if any(w.get("sector") == s["id"] for w in cfg["watchlist"])]
    placeholder = "Synthetic placeholder sentence used to preview the layout."
    analysis = {
        "headline": t(lang, "demo.headline"),
        "summary": t(lang, "demo.body"),
        "top": {
            "whatHappened": {"title": "[Demo] Sample lead story for the session",
                             "paragraph": " ".join([placeholder] * 3), "evidence": market_ids[:2]},
            "keyAlert": {"category": "macro", "title": "[Demo] Sample key alert",
                         "whatHappened": placeholder, "whyItMatters": placeholder, "tickers": [],
                         "evidence": market_ids[2:3]},
            "marketMover": {"ticker": mover, "title": f"[Demo] Sample explanation of the move in {mover}",
                            "whatHappened": placeholder, "whyItMatters": placeholder, "evidence": by[mover][:1]},
        },
        "latest": [
            {"theme": "deals", "title": "[Demo] Sample deals story", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [], "evidence": market_ids[3:4]},
        ] + [
            {"theme": ["ai-tech", "earnings", "company"][i % 3],
             "title": f"[Demo] Sample story about {w['symbol']}", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [w["symbol"]], "evidence": by[w["symbol"]][:1]}
            for i, w in enumerate(cfg["watchlist"][:6]) if w["symbol"] != mover
        ],
        "watchlist": [
            {"ticker": w["symbol"], "sentiment": rng.choice(["bullish", "bearish", "neutral", "mixed"]),
             "summary": placeholder, "whatToWatch": "Placeholder: next scheduled company event.",
             "evidence": by[w["symbol"]][:2]}
            for w in cfg["watchlist"]
        ],
        "sectors": [
            {"sector": s["id"], "title": f"[Demo] Sample {s['id']} sector story", "whatHappened": placeholder,
             "whyItMatters": placeholder, "tickers": [],
             "evidence": [by[w["symbol"]][1] for w in cfg["watchlist"] if w.get("sector") == s["id"]][:1]}
            for s in sectors_present
        ],
        "radar": [
            {"kind": "catalyst", "text": "[Demo] Placeholder upcoming catalyst.", "evidence": []},
            {"kind": "risk", "text": "[Demo] Placeholder risk to monitor.", "evidence": []},
            {"kind": "data", "text": "[Demo] Placeholder economic data release.", "evidence": []},
        ],
    }
    assert ids, "demo pool is empty"
    qa = {"status": "demo", "model": "demo", "modelLabel": "demo", "evidenceCount": len(pool),
          "inWindow": pool.in_window_count, "toolCalls": 0, "repairs": 0, "notes": [], "sourceErrors": []}
    edition = compose_edition(ctx, cfg, settings, snapshot, pool, analysis, qa, demo=True)
    edition["intro"]["stamp"] = t(lang, "demo.stamp")
    return edition


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(build_demo_edition(now=datetime.now(timezone.utc)), indent=1)[:2000])
