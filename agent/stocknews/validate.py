"""Validate the model's edition JSON against the evidence pool and fix what is safe to fix.

Safe fixes are applied in place (unknown evidence IDs dropped, duplicate or unsupported
stories removed, over-long text trimmed). Problems only the model can fix are returned
as "hard" issues and sent back to it in a repair round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .evidence import EvidencePool, title_key, trim
from .schema import ALERT_CATEGORIES, SENTIMENTS, SIGNAL_KINDS, THEMES

_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
LIMITS = {"headline": 160, "summary": 800, "title": 140, "paragraph": 900, "field": 650, "signal": 320}


@dataclass
class Issue:
    path: str
    message: str
    hard: bool = True

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class ValidationContext:
    pool: EvidencePool
    watchlist: list[str]  # extra watchlist tickers (outside the top 20)
    sector_ids: list[str]
    quote_lookup: Callable[[str], dict | None]
    top20: list[str] = field(default_factory=list)
    min_latest: int = 3
    max_latest: int = 12
    max_per_sector: int = 2
    max_radar: int = 6


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def hard(self) -> list[Issue]:
        return [i for i in self.issues if i.hard]

    @property
    def ok(self) -> bool:
        return not self.hard


def _text(obj: dict, key: str, limit: int, path: str, report: Report, required: bool = True) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        if required:
            report.issues.append(Issue(f"{path}.{key}", "missing or empty text"))
        obj[key] = ""
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        report.fixes.append(f"{path}.{key}: trimmed to {limit} characters")
        value = trim(value, limit)
    obj[key] = value
    return value


def _evidence(obj: dict, path: str, ctx: ValidationContext, report: Report) -> list[str]:
    raw = obj.get("evidence")
    ids: list[str] = []
    for x in raw if isinstance(raw, list) else []:
        item = str(x).strip().upper()
        if item in ctx.pool:
            if item not in ids:
                ids.append(item)
        else:
            report.fixes.append(f"{path}: removed unknown evidence id {item}")
    obj["evidence"] = ids
    return ids


def _tickers(obj: dict) -> list[str]:
    raw = obj.get("tickers")
    out = []
    for x in raw if isinstance(raw, list) else []:
        sym = str(x).strip().upper()
        if _TICKER.match(sym) and sym not in out:
            out.append(sym)
    obj["tickers"] = out
    return out


def _in_window(ids: list[str], ctx: ValidationContext) -> bool:
    return any(ctx.pool.get(i) and ctx.pool.get(i).in_window for i in ids)


def _about_top20(ids: list[str], ctx: ValidationContext) -> bool:
    top = set(ctx.top20)
    return any(ctx.pool.get(i) and top.intersection(ctx.pool.get(i).tickers) for i in ids)


def _notes(a: dict, key: str, symbols: list[str], text_key: str, report: Report, ctx: ValidationContext,
           extra_text: str | None = None) -> None:
    """One note per symbol: drop unknown/duplicate entries, flag missing ones (hard)."""
    notes = a.get(key)
    if not isinstance(notes, list):
        if symbols:
            report.issues.append(Issue(key, "missing list of notes"))
        notes = []
    by_symbol: dict[str, dict] = {}
    for i, note in enumerate(notes):
        path = f"{key}[{i}]"
        if not isinstance(note, dict):
            continue
        sym = str(note.get("ticker", "")).strip().upper()
        if sym not in symbols:
            report.fixes.append(f"{path}: removed note for {sym or '?'} (not in {key})")
            continue
        if sym in by_symbol:
            report.fixes.append(f"{path}: removed duplicate note for {sym}")
            continue
        note["ticker"] = sym
        if note.get("sentiment") not in SENTIMENTS:
            note["sentiment"] = "neutral"
        _text(note, text_key, LIMITS["field"], path, report)
        if extra_text:
            _text(note, extra_text, LIMITS["field"], path, report, required=False)
        _evidence(note, path, ctx, report)
        by_symbol[sym] = note
    missing = [s for s in symbols if s not in by_symbol]
    if missing:
        report.issues.append(Issue(key, "missing notes for: " + ", ".join(missing)))
    if symbols or key in a:
        a[key] = [by_symbol[s] for s in symbols if s in by_symbol]


def validate_analysis(a: Any, ctx: ValidationContext) -> Report:
    report = Report()
    if not isinstance(a, dict):
        report.issues.append(Issue("$", "the response must be a JSON object"))
        return report

    _text(a, "headline", LIMITS["headline"], "headline", report)
    _text(a, "summary", LIMITS["summary"], "summary", report)

    # ---- top ------------------------------------------------------------------------
    top = a.get("top")
    leads: dict[str, str] = {}
    if not isinstance(top, dict):
        report.issues.append(Issue("top", "missing object with whatHappened, keyAlert and marketMover"))
        a["top"] = top = {}
    for key in ("whatHappened", "keyAlert", "marketMover"):
        block = top.get(key)
        path = f"top.{key}"
        if not isinstance(block, dict):
            report.issues.append(Issue(path, "missing"))
            continue
        _text(block, "title", LIMITS["title"], path, report)
        if key == "whatHappened":
            _text(block, "paragraph", LIMITS["paragraph"], path, report)
        else:
            _text(block, "whatHappened", LIMITS["field"], path, report)
            _text(block, "whyItMatters", LIMITS["field"], path, report)
        if key == "keyAlert" and block.get("category") not in ALERT_CATEGORIES:
            block["category"] = "alert"
        if key != "marketMover":
            tickers = _tickers(block)
            if ctx.top20 and not set(tickers).intersection(ctx.top20):
                report.issues.append(Issue(f"{path}.tickers",
                                           "Top of the day must be about TOP 20 companies; list at least one TOP 20 ticker"))
        ids = _evidence(block, path, ctx, report)
        if not ids:
            report.issues.append(Issue(path, "cite at least one evidence ID that exists in the EVIDENCE list"))
        else:
            if key != "marketMover" and not _in_window(ids, ctx):
                report.issues.append(Issue(path, "must cite at least one NEW item from this news window"))
            if ctx.top20 and not _about_top20(ids, ctx):
                report.issues.append(Issue(path, "cite at least one evidence item about a TOP 20 company"))
        if ids:
            leads.setdefault(ids[0], path)
        if key == "marketMover":
            sym = str(block.get("ticker", "")).strip().upper()
            block["ticker"] = sym
            if ctx.top20 and sym not in ctx.top20:
                report.issues.append(Issue(f"{path}.ticker", f"'{sym}' is not in the TOP 20; pick a TOP 20 ticker"))
            elif not _TICKER.match(sym) or not ctx.quote_lookup(sym):
                report.issues.append(Issue(f"{path}.ticker",
                                           f"no market data for '{sym}'; pick a ticker from MARKET DATA"))

    # ---- latest ---------------------------------------------------------------------
    latest = a.get("latest")
    if not isinstance(latest, list):
        report.issues.append(Issue("latest", "missing list of stories"))
        latest = []
    kept: list[dict] = []
    seen_titles = {title_key(b.get("title", "")) for b in top.values() if isinstance(b, dict)}
    for i, item in enumerate(latest):
        path = f"latest[{i}]"
        if not isinstance(item, dict):
            report.fixes.append(f"{path}: removed (not an object)")
            continue
        if item.get("theme") not in THEMES:
            item["theme"] = "company"
        title = _text(item, "title", LIMITS["title"], path, report, required=False)
        _text(item, "whatHappened", LIMITS["field"], path, report, required=False)
        _text(item, "whyItMatters", LIMITS["field"], path, report, required=False)
        _tickers(item)
        ids = _evidence(item, path, ctx, Report())
        reason = None
        if not title or not item["whatHappened"]:
            reason = "empty story"
        elif not ids:
            reason = "no valid evidence"
        elif not _in_window(ids, ctx):
            reason = "no evidence from this news window"
        elif ids[0] in leads:
            reason = f"repeats the story already used in {leads[ids[0]]}"
        elif title_key(title) in seen_titles:
            reason = "duplicate title"
        if reason:
            report.fixes.append(f"{path}: removed ({reason})")
            continue
        leads[ids[0]] = path
        seen_titles.add(title_key(title))
        kept.append(item)
    if len(kept) > ctx.max_latest:
        report.fixes.append(f"latest: kept the first {ctx.max_latest} stories")
        kept = kept[: ctx.max_latest]
    a["latest"] = kept
    if len(kept) < ctx.min_latest and ctx.pool.in_window_count >= 10:
        report.issues.append(Issue("latest", f"only {len(kept)} valid stories; write at least "
                                             f"{ctx.min_latest} distinct stories citing NEW evidence"))

    # ---- top-20 notes and extra watchlist notes ----------------------------------------
    _notes(a, "top20", ctx.top20, "note", report, ctx)
    if ctx.watchlist:
        _notes(a, "watchlist", ctx.watchlist, "summary", report, ctx, extra_text="whatToWatch")
    elif "watchlist" in a:
        a.pop("watchlist")

    # ---- sectors --------------------------------------------------------------------
    sectors = a.get("sectors") if isinstance(a.get("sectors"), list) else []
    kept_sectors: list[dict] = []
    per_sector: dict[str, int] = {}
    for i, item in enumerate(sectors):
        path = f"sectors[{i}]"
        if not isinstance(item, dict) or item.get("sector") not in ctx.sector_ids:
            report.fixes.append(f"{path}: removed (unknown sector)")
            continue
        title = _text(item, "title", LIMITS["title"], path, report, required=False)
        _text(item, "whatHappened", LIMITS["field"], path, report, required=False)
        _text(item, "whyItMatters", LIMITS["field"], path, report, required=False)
        _tickers(item)
        ids = _evidence(item, path, ctx, Report())
        reason = None
        if not title or not ids:
            reason = "no title or no valid evidence"
        elif ids[0] in leads:
            reason = f"repeats {leads[ids[0]]}"
        elif per_sector.get(item["sector"], 0) >= ctx.max_per_sector:
            reason = "too many stories for this sector"
        if reason:
            report.fixes.append(f"{path}: removed ({reason})")
            continue
        leads[ids[0]] = path
        per_sector[item["sector"]] = per_sector.get(item["sector"], 0) + 1
        kept_sectors.append(item)
    a["sectors"] = kept_sectors

    # ---- radar ----------------------------------------------------------------------
    radar = a.get("radar") if isinstance(a.get("radar"), list) else []
    kept_radar = []
    for i, item in enumerate(radar):
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in SIGNAL_KINDS:
            item["kind"] = "data"
        if not _text(item, "text", LIMITS["signal"], f"radar[{i}]", report, required=False):
            continue
        _evidence(item, f"radar[{i}]", ctx, Report())
        kept_radar.append(item)
    a["radar"] = kept_radar[: ctx.max_radar]
    return report


def cited_ids(a: dict) -> list[str]:
    """All evidence IDs cited anywhere in a (validated) analysis, in reading order."""
    out: list[str] = []

    def take(obj):
        for x in (obj or {}).get("evidence") or []:
            if x not in out:
                out.append(x)

    for block in (a.get("top") or {}).values():
        take(block)
    for key in ("top20", "latest", "watchlist", "sectors", "radar"):
        for item in a.get(key) or []:
            take(item)
    return out
