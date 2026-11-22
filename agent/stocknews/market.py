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

    def quote(self, symbol: str) -> dict | None:
        return self.quotes.get(symbol.upper())

    def as_of(self) -> datetime | None:
        stamps = []
        for q in self.quotes.values():
            if q.get("asOf"):
                try:
                    stamps.append(datetime.fromisoformat(q["asOf"]))
                except ValueError:
                    continue
        return max(stamps) if stamps else None


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
