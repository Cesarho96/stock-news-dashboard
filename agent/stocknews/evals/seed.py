"""The built-in synthetic eval case ("seed:"): a fixed, realistic after-the-close session.

Fictional events written like wire copy, with quotes that agree with the stories, so the
agent, the validator, the numeric checks and the judge can all be exercised offline and in
the deployment smoke test. Sources are "Synthetic Wire" and links point to example.com:
nothing here is real news, and this case is never published.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 11, 20, 21, 35, tzinfo=timezone.utc)  # Friday, 4:35 PM ET (after the close)
SOURCES = ["Synthetic Wire", "Synthetic Markets Desk", "Synthetic Business Daily"]

# market caps in USD millions (synthetic); the top 20 is ranked from these
CAPS = {
    "NVDA": 4_450_000, "AAPL": 4_010_000, "MSFT": 3_720_000, "GOOGL": 3_350_000, "AMZN": 2_480_000,
    "META": 1_620_000, "AVGO": 1_610_000, "TSLA": 1_330_000, "BRK.B": 1_070_000, "LLY": 950_000,
    "WMT": 860_000, "JPM": 840_000, "ORCL": 700_000, "V": 650_000, "MA": 520_000, "MU": 505_000,
    "XOM": 480_000, "AMD": 470_000, "JNJ": 455_000, "COST": 430_000, "PLTR": 420_000, "NFLX": 400_000,
    "ABBV": 390_000, "CVX": 330_000, "CSCO": 300_000, "KO": 295_000, "HD": 360_000, "PG": 350_000,
    "BAC": 340_000, "UNH": 310_000, "IBM": 260_000, "GE": 280_000, "PM": 270_000, "WFC": 265_000,
    "CRM": 250_000, "MS": 240_000, "GS": 235_000, "CAT": 230_000, "TMUS": 225_000, "LIN": 215_000,
}
# (price, % change) for the names the stories talk about; everything else gets a small random move
MOVES = {
    "NVDA": (182.40, 3.1), "AVGO": (348.90, 2.4), "TSLA": (391.20, -4.2), "AAPL": (271.30, 1.1),
    "MSFT": (497.80, 0.9), "JPM": (306.10, 1.6), "LLY": (1052.30, 5.3), "AMZN": (231.70, 0.4),
    "GOOGL": (279.60, 0.7), "META": (612.40, -1.8), "BRK.B": (492.10, 0.2), "WMT": (107.20, 2.9),
    "V": (338.50, 0.5), "MA": (566.30, 0.6), "XOM": (114.90, -1.9), "CVX": (151.20, -2.1),
    "NFLX": (112.40, 3.4), "ORCL": (226.80, -3.0), "SPY": (668.20, 0.8), "QQQ": (601.50, 1.2),
    "DIA": (463.70, 0.5), "IWM": (241.90, 1.0), "TLT": (90.40, 0.6), "GLD": (377.10, -0.3),
    "USO": (71.80, -2.0), "UUP": (27.30, -0.4), "SMH": (342.60, 2.2), "XLK": (284.10, 1.3),
    "XLE": (88.30, -1.7), "XLF": (52.90, 0.9), "XLV": (148.70, 1.5), "XLY": (237.40, -0.6),
    "XLC": (113.20, -0.2), "XLI": (152.80, 0.4),
}

# (hours ago, source idx, origin, tickers, title, summary)
STORIES = [
    (1.2, 0, "market", [], "Stocks end higher as Treasury yields ease after softer inflation reading",
     "The S&P 500 ETF (SPY) gained 0.8% and the Nasdaq-100 ETF (QQQ) rose 1.2% after core consumer prices "
     "rose less than economists expected, pulling the 10-year Treasury yield to a two-month low."),
    (2.0, 1, "market", [], "Fed governor says policy is well positioned as inflation cools",
     "A Federal Reserve governor said the central bank can be patient on further rate cuts and will watch "
     "December's jobs data closely."),
    (2.4, 2, "market", ["NVDA", "AVGO"], "Nvidia leads chipmakers higher on data-center demand outlook",
     "Nvidia rose 3.1% and Broadcom gained 2.4% after a cloud provider said it would double AI server "
     "purchases next year; the VanEck semiconductor ETF (SMH) added 2.2%."),
    (3.1, 0, "market", ["XOM", "CVX"], "Oil falls as U.S. crude inventories rise more than expected",
     "U.S. crude futures fell about 2% after government data showed a larger-than-forecast inventory build; "
     "Exxon Mobil fell 1.9% and Chevron lost 2.1%."),
    (4.0, 1, "market", [], "Retail sales beat forecasts as holiday shopping season starts early",
     "October retail sales rose 0.6% from September, above the 0.3% economists expected."),
    (1.5, 0, "company:TSLA", ["TSLA"], "Tesla recalls 120,000 vehicles over seat-belt warning software",
     "Tesla is recalling about 120,000 Model Y vehicles in the U.S. to update software for the seat-belt "
     "warning chime, according to a regulatory filing. Shares fell 4.2%."),
    (6.5, 2, "company:TSLA", ["TSLA"], "Analyst trims Tesla delivery forecast for the fourth quarter",
     "A brokerage cut its fourth-quarter delivery estimate to 470,000 vehicles, citing softer demand in Europe."),
    (2.8, 1, "company:AAPL", ["AAPL"], "Apple supplier signals strong holiday iPhone orders",
     "A major Apple assembler said November revenue rose 18% from a year earlier on iPhone demand."),
    (3.6, 0, "company:MSFT", ["MSFT"], "Microsoft signs multiyear AI cloud deal with European telecom group",
     "The agreement covers Azure capacity for AI services across 11 countries; terms were not disclosed."),
    (4.4, 2, "company:JPM", ["JPM"], "JPMorgan raises net interest income outlook for 2027",
     "JPMorgan said it now expects net interest income of about $95 billion next year, up from $92 billion "
     "previously. Shares rose 1.6%."),
    (1.9, 1, "company:LLY", ["LLY"], "Eli Lilly obesity pill wins FDA advisory panel backing",
     "An FDA advisory committee voted 14-2 that the benefits of Lilly's oral weight-loss drug outweigh its "
     "risks; a final decision is expected in the first quarter. Shares jumped 5.3%."),
    (5.2, 0, "company:AMZN", ["AMZN"], "Amazon expands same-day delivery to 40 more U.S. cities",
     "Amazon said the expansion brings same-day delivery to more than 180 metro areas."),
    (5.8, 2, "company:GOOGL", ["GOOGL"], "Alphabet's Waymo begins driverless rides in two new cities",
     "Waymo started fully driverless service in Denver and Nashville."),
    (3.3, 1, "company:META", ["META"], "Meta fined by EU regulator over advertising data practices",
     "The European regulator fined Meta 640 million euros and ordered changes to how it uses personal data "
     "for ads; Meta said it would appeal. Shares fell 1.8%."),
    (7.0, 0, "company:WMT", ["WMT"], "Walmart raises full-year outlook as e-commerce sales jump",
     "Walmart lifted its annual sales growth forecast to 4.5%-5% after U.S. e-commerce sales rose 27% in the "
     "quarter. Shares rose 2.9%."),
    (6.1, 2, "market", ["V", "MA"], "Visa and Mastercard reach settlement with merchants over card fees",
     "The settlement lowers some interchange rates for five years and still needs court approval."),
    (2.2, 1, "company:NFLX", ["NFLX"], "Netflix shares climb after ad-supported tier passes 100 million users",
     "Netflix said its ad-supported plan reached 100 million monthly active users. Shares rose 3.4%."),
    (3.9, 0, "company:ORCL", ["ORCL"], "Oracle slides as investors question data-center spending plans",
     "Oracle fell 3% after a report that it may raise more debt to fund data-center construction."),
    (8.5, 2, "company:BRK.B", ["BRK.B"], "Berkshire Hathaway discloses new stake in a property insurer",
     "A quarterly filing showed Berkshire bought shares of a U.S. property and casualty insurer."),
    (70.0, 0, "company:NVDA", ["NVDA"], "Nvidia reports record quarterly revenue of $57 billion",
     "Nvidia's third-quarter revenue rose 62% from a year earlier, driven by data-center sales."),
]
FILINGS = [
    (9.0, "TSLA", "TSLA 8-K: Departure of a principal officer",
     "Form 8-K, Item 5.02: departure of directors or certain officers."),
]
EARNINGS = [
    {"symbol": "AVGO", "days": 5, "hour": "amc", "epsEstimate": 1.87, "revenueEstimate": 17.4e9},
    {"symbol": "COST", "days": 6, "hour": "amc", "epsEstimate": 4.27, "revenueEstimate": 67.1e9},
]


def seed_case(cfg: dict) -> dict:
    """Build the synthetic case for the current config (pool, strip and sector ETFs)."""
    from ..clock import build_context
    from ..evidence import EvidencePool
    from ..market import MarketSnapshot, rank_universe
    from ..settings import company_info, limits
    from .dataset import case_from_run

    rng = random.Random(20261120)
    lim = limits(cfg)
    ctx = build_context(NOW, tz_name=cfg.get("timezone", "America/New_York"), tz_label=cfg.get("timezoneLabel", "ET"),
                        slot="close", language="en", context_days=lim["contextDays"])
    info = company_info(cfg)
    snapshot = MarketSnapshot()
    for item in cfg.get("strip", []):
        snapshot.names[item["symbol"]] = item["name"]
    for s in cfg.get("sectors", []):
        snapshot.names[s["etf"]] = f"{(s.get('label') or {}).get('en') or s['id']} ETF"
    for sym, c in info.items():
        snapshot.names[sym] = c.get("name") or sym
    symbols = list(dict.fromkeys([s["symbol"] for s in cfg.get("strip", [])] + [s["etf"] for s in cfg.get("sectors", [])]
                                 + list(info)))
    as_of = (NOW - timedelta(minutes=35)).isoformat()
    for sym in symbols:
        price, pct = MOVES.get(sym, (round(rng.uniform(60, 600), 2), round(rng.uniform(-1.2, 1.2), 2)))
        prev = round(price / (1 + pct / 100), 2)
        snapshot.quotes[sym] = {"symbol": sym, "price": price, "change": round(price - prev, 2), "changePct": pct,
                                "high": round(price * 1.008, 2), "low": round(min(price, prev) * 0.992, 2),
                                "open": prev, "prevClose": prev, "asOf": as_of}
    pool_cfg = (cfg.get("universe") or {}).get("pool") or []
    profiles = {p["symbol"]: {"marketCapitalization": CAPS.get(p["symbol"], 200_000)} for p in pool_cfg}
    snapshot.universe = rank_universe(pool_cfg, snapshot.quotes, profiles, int((cfg.get("universe") or {}).get("size", 20)))
    today = ctx.now_local.date()
    snapshot.earnings = [{"symbol": e["symbol"], "date": (today + timedelta(days=e["days"])).isoformat(),
                          "hour": e["hour"], "epsEstimate": e["epsEstimate"], "revenueEstimate": e["revenueEstimate"]}
                         for e in EARNINGS]

    pool = EvidencePool(ctx.now_utc, ctx.window_start_utc, ctx.context_start_utc, lim["summaryChars"])
    for n, (hours, src, origin, tickers, title, summary) in enumerate(STORIES, 1):
        pool.add(title=title, url=f"https://example.com/synthetic/story-{n}", source=SOURCES[src],
                 published=NOW - timedelta(hours=hours), summary=summary, tickers=tickers, origin=origin)
    for n, (hours, sym, title, summary) in enumerate(FILINGS, 1):
        pool.add(title=title, url=f"https://example.com/synthetic/filing-{n}", source="SEC EDGAR (synthetic)",
                 published=NOW - timedelta(hours=hours), summary=summary, tickers=[sym], origin=f"sec:{sym}",
                 kind="filing")
    pool.finalize(lim)
    return case_from_run(ctx, cfg, snapshot, pool, {"kind": "synthetic", "name": "seed"}, case_id="seed-2026-11-20-close")
