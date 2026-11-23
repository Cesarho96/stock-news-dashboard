"""Evidence pool: every news item / filing the agent may cite, each with a stable ID.

The LLM only ever cites IDs (E001, E002, ...). URLs, sources and timestamps are filled in
by code from this pool, so the dashboard cannot show a link the collectors did not see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse

_TRACKING = ("utm_", "mod", "cmpid", "ref", "src", "taid", "yptr", "ncid", ".tsrc", "guccounter",
             "guce_", "fbclid", "gclid", "mc_cid", "mc_eid", "cid", "smid", "sh")
_WORDS = re.compile(r"[a-z0-9]+")


def url_key(url: str) -> str:
    p = urlparse(url.strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
             if not k.lower().startswith(_TRACKING)]
    return f"{host}{p.path.rstrip('/')}?{urlencode(sorted(query))}"


def title_key(title: str) -> str:
    return " ".join(_WORDS.findall(title.lower()))


def trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


@dataclass
class Evidence:
    id: str
    kind: str  # news | filing
    title: str
    summary: str
    source: str
    url: str
    published: datetime
    tickers: list[str] = field(default_factory=list)
    origin: str = "market"  # market | rss:<id> | company:<T> | sec:<T> | tool:<T>
    in_window: bool = True

    def prompt_line(self, tz) -> str:
        when = self.published.astimezone(tz).strftime("%m-%d %H:%M")
        tickers = ",".join(self.tickers) or "-"
        flag = "NEW" if self.in_window else "ctx"
        text = self.title if not self.summary else f"{self.title} — {self.summary}"
        return f"{self.id} | {when} | {self.source} | {tickers} | {flag} | {text}"


@dataclass
class _Candidate:
    kind: str
    title: str
    summary: str
    source: str
    url: str
    published: datetime
    tickers: list[str]
    origin: str


class EvidencePool:
    def __init__(self, now: datetime, window_start: datetime, context_start: datetime,
                 summary_chars: int = 280):
        self.now = now
        self.window_start = window_start
        self.context_start = context_start
        self.summary_chars = summary_chars
        self._candidates: list[_Candidate] = []
        self.items: dict[str, Evidence] = {}
        self._keys: dict[str, str] = {}
        self._next = 1
        self.finalized = False
        self.errors: list[str] = []

    # ---- intake -------------------------------------------------------------------
    def _clean(self, *, title: str, url: str, published: datetime | None) -> tuple[str, str, datetime] | None:
        title = re.sub(r"\s+", " ", (title or "")).strip()
        url = (url or "").strip()
        if len(title) < 12 or not url.lower().startswith(("http://", "https://")):
            return None
        if published is None:
            return None
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published > self.now + timedelta(hours=2) or published < self.context_start:
            return None
        return title, url, min(published, self.now)

    def add(self, *, title: str, url: str, source: str, published: datetime | None,
            summary: str = "", tickers=(), origin: str = "market", kind: str = "news") -> Evidence | None:
        cleaned = self._clean(title=title, url=url, published=published)
        if not cleaned:
            return None
        title, url, published = cleaned
        tickers = [t.upper() for t in tickers if t]
        summary = trim(re.sub(r"\s+", " ", summary or ""), self.summary_chars)
        if summary and title_key(summary).startswith(title_key(title)):
            summary = trim(summary[len(title):].lstrip(" .:-—"), self.summary_chars)
        cand = _Candidate(kind, title, summary, (source or "Unknown").strip(), url, published, tickers, origin)
        if not self.finalized:
            self._candidates.append(cand)
            return None
        return self._admit(cand)

    def _admit(self, cand: _Candidate) -> Evidence:
        keys = (f"u:{url_key(cand.url)}", f"t:{title_key(cand.title)}")
        for key in keys:
            if key in self._keys:
                existing = self.items[self._keys[key]]
                for tkr in cand.tickers:
                    if tkr not in existing.tickers:
                        existing.tickers.append(tkr)
                return existing
        ev = Evidence(
            id=f"E{self._next:03d}",
            kind=cand.kind,
            title=cand.title,
            summary=cand.summary,
            source=cand.source,
            url=cand.url,
            published=cand.published,
            tickers=list(dict.fromkeys(cand.tickers)),
            origin=cand.origin,
            in_window=cand.published >= self.window_start,
        )
        self._next += 1
        self.items[ev.id] = ev
        for key in keys:
            self._keys[key] = ev.id
        return ev

    def finalize(self, limits: dict) -> None:
        """Dedupe, cap per origin, and assign IDs in reading order."""
        cands = sorted(self._candidates, key=lambda c: c.published, reverse=True)
        seen: dict[str, _Candidate] = {}
        unique: list[_Candidate] = []
        for c in cands:
            keys = (f"u:{url_key(c.url)}", f"t:{title_key(c.title)}")
            hit = next((seen[k] for k in keys if k in seen), None)
            if hit:
                for tkr in c.tickers:
                    if tkr not in hit.tickers:
                        hit.tickers.append(tkr)
                continue
            for k in keys:
                seen[k] = c
            unique.append(c)

        counts: dict[tuple[str, bool], int] = {}
        kept: list[_Candidate] = []
        for c in unique:
            in_window = c.published >= self.window_start
            if c.origin.startswith("company:"):
                cap = limits["companyNewsInWindow"] if in_window else limits["companyNewsContext"]
            elif c.origin.startswith("sec:"):
                cap = limits["filingsPerTicker"]
            elif c.origin.startswith("rss:"):
                cap = limits["rssPerFeed"] if in_window else 0
            else:  # market-wide news: window only
                cap = limits["marketNews"] if in_window else 0
            bucket = (c.origin, in_window if not c.origin.startswith("sec:") else True)
            if counts.get(bucket, 0) >= cap:
                continue
            counts[bucket] = counts.get(bucket, 0) + 1
            kept.append(c)

        kept.sort(key=lambda c: (c.published >= self.window_start, c.published), reverse=True)
        kept = kept[: limits["maxEvidence"]]

        def order(c: _Candidate):
            if c.origin == "market" or c.origin.startswith("rss:"):
                group = 0
            elif c.origin.startswith("company:"):
                group = 1
            else:
                group = 2
            return (group, c.origin if group == 1 else "", -c.published.timestamp())

        self.finalized = True
        for c in sorted(kept, key=order):
            self._admit(c)
        self._candidates.clear()

    # ---- serialization (run records, eval datasets) -----------------------------------
    def to_dict(self) -> dict:
        return {
            "now": self.now.isoformat(),
            "windowStart": self.window_start.isoformat(),
            "contextStart": self.context_start.isoformat(),
            "summaryChars": self.summary_chars,
            "items": [
                {"id": e.id, "kind": e.kind, "title": e.title, "summary": e.summary, "source": e.source,
                 "url": e.url, "published": e.published.isoformat(), "tickers": e.tickers,
                 "origin": e.origin, "inWindow": e.in_window}
                for e in self.items.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidencePool":
        def ts(value: str) -> datetime:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        pool = cls(ts(data["now"]), ts(data["windowStart"]), ts(data["contextStart"]),
                   int(data.get("summaryChars") or 280))
        pool.finalized = True
        top = 0
        for it in data.get("items") or []:
            ev = Evidence(id=it["id"], kind=it.get("kind", "news"), title=it["title"],
                          summary=it.get("summary", ""), source=it.get("source", ""), url=it["url"],
                          published=ts(it["published"]), tickers=list(it.get("tickers") or []),
                          origin=it.get("origin", "market"), in_window=bool(it.get("inWindow", True)))
            pool.items[ev.id] = ev
            pool._keys[f"u:{url_key(ev.url)}"] = ev.id
            pool._keys[f"t:{title_key(ev.title)}"] = ev.id
            try:
                top = max(top, int(ev.id[1:]))
            except ValueError:
                pass
        pool._next = top + 1
        return pool

    # ---- queries -------------------------------------------------------------------
    def __contains__(self, item_id: str) -> bool:
        return item_id in self.items

    def get(self, item_id: str) -> Evidence | None:
        return self.items.get(item_id)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def in_window_count(self) -> int:
        return sum(1 for e in self.items.values() if e.in_window)

    def sources(self) -> list[str]:
        return sorted({e.source for e in self.items.values()})

    def by_ticker(self, symbol: str, in_window_only: bool = False) -> list[Evidence]:
        out = [e for e in self.items.values() if symbol in e.tickers and (e.in_window or not in_window_only)]
        return sorted(out, key=lambda e: e.published, reverse=True)

    def market_wide(self) -> list[Evidence]:
        out = [e for e in self.items.values() if e.origin == "market" or e.origin.startswith("rss:")]
        return sorted(out, key=lambda e: e.published, reverse=True)

    def prompt_sections(self, tz, watchlist: list[str]) -> str:
        lines: list[str] = []
        market = [e for e in self.items.values() if e.origin == "market" or e.origin.startswith("rss:")]
        if market:
            lines.append("## MARKET-WIDE NEWS")
            lines += [e.prompt_line(tz) for e in market]
        for sym in watchlist:
            group = [e for e in self.items.values() if e.origin == f"company:{sym}"]
            if group:
                lines.append(f"## COMPANY NEWS: {sym}")
                lines += [e.prompt_line(tz) for e in group]
        filings = [e for e in self.items.values() if e.origin.startswith("sec:")]
        if filings:
            lines.append("## SEC FILINGS (official)")
            lines += [e.prompt_line(tz) for e in filings]
        research = [e for e in self.items.values() if e.origin.startswith("tool:")]
        if research:
            lines.append("## RESEARCH TOOL RESULTS")
            lines += [e.prompt_line(tz) for e in research]
        return "\n".join(lines) if lines else "(no evidence collected)"
