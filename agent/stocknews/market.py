"""Market snapshot (quotes, earnings, status) and number formatting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MarketSnapshot:
    quotes: dict[str, dict] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    earnings: list[dict] = field(default_factory=list)
    status: dict | None = None
    errors: list[str] = field(default_factory=list)
    # S&P 500 top 20 for this run: [{rank, symbol, name, sector, marketCap}], ranked by market cap
    universe: list[dict] = field(default_factory=list)

    def quote(self, symbol: str) -> dict | None:
        return self.quotes.get(symbol.upper())

    @property
    def top20(self) -> list[str]:
        return [row["symbol"] for row in self.universe]

    def to_dict(self) -> dict:
        return {"quotes": self.quotes, "names": self.names, "earnings": self.earnings,
                "status": self.status, "errors": self.errors, "universe": self.universe}

    @classmethod
    def from_dict(cls, data: dict) -> "MarketSnapshot":
        return cls(quotes=dict(data.get("quotes") or {}), names=dict(data.get("names") or {}),
                   earnings=list(data.get("earnings") or []), status=data.get("status"),
                   errors=list(data.get("errors") or []), universe=list(data.get("universe") or []))

    def as_of(self) -> datetime | None:
        stamps = []
        for q in self.quotes.values():
            if q.get("asOf"):
                try:
                    stamps.append(datetime.fromisoformat(q["asOf"]))
                except ValueError:
                    continue
        return max(stamps) if stamps else None


def coverage(cfg: dict, snapshot: MarketSnapshot) -> tuple[list[str], list[str]]:
    """(top-20 symbols in rank order, watchlist symbols outside the top 20)."""
    top20 = snapshot.top20
    extras = [w["symbol"] for w in cfg.get("watchlist", []) if w["symbol"] not in top20]
    return top20, extras


def cls_for(pct: float | None) -> str:
    if pct is None or abs(pct) < 0.005:
        return "flat"
    return "green" if pct > 0 else "red"


def fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    arrow = "▲" if pct > 0.005 else "▼" if pct < -0.005 else "■"
    return f"{arrow} {abs(pct):.2f}%"


def fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"${value:,.0f}" if value >= 100000 else f"${value:,.2f}"
    return f"${value:,.2f}"


def fmt_cap(value: float | None) -> str:
    """Market cap in USD -> '$4.94T' / '$983B'."""
    if not value:
        return "—"
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:,.0f}B"
    return f"${value / 1e6:,.0f}M"


def rank_universe(pool: list[dict], quotes: dict[str, dict], profiles: dict[str, dict],
                  size: int = 20) -> list[dict]:
    """Top `size` pool members by market cap (Finnhub profile, USD millions).

    Members without a quote are skipped (nothing to show). Members without a market cap
    keep their pool position after the ranked ones, so the config order is the fallback.
    """
    rows = []
    for order, item in enumerate(pool):
        sym = item["symbol"]
        if sym not in quotes:
            continue
        cap_m = (profiles.get(sym) or {}).get("marketCapitalization")
        try:
            cap = float(cap_m) * 1e6 if cap_m else None
        except (TypeError, ValueError):
            cap = None
        rows.append({"symbol": sym, "name": item.get("name") or (profiles.get(sym) or {}).get("name") or sym,
                     "sector": item.get("sector"), "marketCap": cap, "_order": order})
    rows.sort(key=lambda r: (r["marketCap"] is None, -(r["marketCap"] or 0), r["_order"]))
    top = rows[:size]
    for rank, row in enumerate(top, 1):
        row["rank"] = rank
        del row["_order"]
    return top


def fmt_range(low: float | None, high: float | None) -> str:
    if low is None or high is None or not low or not high:
        return "—"
    return f"{fmt_price(low)} – {fmt_price(high)}"


def market_table(snapshot: MarketSnapshot, symbols: list[str]) -> str:
    rows = ["SYMBOL | NAME | PRICE | CHG% | DAY RANGE"]
    for sym in symbols:
        q = snapshot.quote(sym)
        if not q:
            rows.append(f"{sym} | {snapshot.names.get(sym, sym)} | n/a | n/a | n/a")
            continue
        rows.append(
            f"{sym} | {snapshot.names.get(sym, sym)} | {q['price']:.2f} | {q['changePct']:+.2f}% | "
            f"{(q.get('low') or 0):.2f}-{(q.get('high') or 0):.2f}"
        )
    return "\n".join(rows)
