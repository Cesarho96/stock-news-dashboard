"""Turn market data + evidence + the model's analysis into the dashboard's edition JSON.

The browser renderer (web/assets/dashboard.js) only displays what is here: every URL,
source name, timestamp and price is filled in by this module from collected data.

Edition schema v2: page 01 "Top of the day" holds three cards about the S&P 500 top 20
and a 20-row board (rank, price, change, market cap, one-line AI note with its source).
The watchlist page only lists watchlist tickers outside the top 20 and is left out when
there are none. Pages are numbered in the order they appear.
"""

from __future__ import annotations

import re
from datetime import date

from .clock import EditionContext
from .evidence import Evidence, EvidencePool, trim
from .i18n import t
from .market import MarketSnapshot, cls_for, coverage, fmt_cap, fmt_pct, fmt_price, fmt_range
from .schema import THEMES
from .settings import Settings
from .validate import cited_ids

EDITION_SCHEMA_VERSION = 2
BADGES = {
    "alert": "ncat-r",
    "risk": "ncat-v",
    "opportunity": "ncat-o",
    "macro": "ncat-i",
    "earnings": "ncat-b",
    "data": "ncat-n",
}
MAX_CHIPS = 5


def model_label(model_id: str) -> str:
    """'global.anthropic.claude-sonnet-5-5' -> 'Claude Sonnet 5.5'."""
    m = re.search(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?![0-9])", model_id or "")
    if not m:
        return model_id or "unknown model"
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"Claude {m.group(1).capitalize()} {version}"


# ---- small pieces --------------------------------------------------------------------
def _source(ctx: EditionContext, ev: Evidence | None) -> dict | None:
    if ev is None:
        return None
    return {"name": ev.source, "url": ev.url, "time": ctx.stamp_label(ev.published), "id": ev.id}


def _sources(ctx: EditionContext, pool: EvidencePool, ids: list[str], limit: int = 3) -> list[dict]:
    out = []
    for item_id in ids[:limit]:
        src = _source(ctx, pool.get(item_id))
        if src:
            out.append(src)
    return out


def _fields(lang: str, what: str, why: str) -> list[dict]:
    out = []
    if what:
        out.append({"label": t(lang, "field.what"), "text": what})
    if why:
        out.append({"label": t(lang, "field.why"), "text": why})
    return out


def _price_fields(snapshot: MarketSnapshot, symbol: str) -> dict:
    q = snapshot.quote(symbol)
    return {
        "price": fmt_price(q["price"]) if q else "—",
        "change": fmt_pct(q["changePct"]) if q else "",
        "cls": cls_for(q["changePct"]) if q else "flat",
    }


def _ticker_chip(snapshot: MarketSnapshot, symbol: str) -> dict | None:
    if not snapshot.quote(symbol):
        return None
    return {"symbol": symbol, "name": snapshot.names.get(symbol, symbol), **_price_fields(snapshot, symbol)}


def _chips(snapshot: MarketSnapshot, tickers: list[str], top20: list[str]) -> list[dict]:
    """Small 'NVDA ▲ 1.2%' chips for the top-20 companies a card is about."""
    out = []
    for sym in tickers:
        if sym in top20 and snapshot.quote(sym) and len(out) < MAX_CHIPS:
            out.append({"symbol": sym, **_price_fields(snapshot, sym)})
    return out


def _story(ctx: EditionContext, pool: EvidencePool, item: dict) -> dict:
    ids = item.get("evidence") or []
    return {
        "title": item.get("title", ""),
        "fields": _fields(ctx.language, item.get("whatHappened", ""), item.get("whyItMatters", "")),
        "tickers": item.get("tickers") or [],
        "sources": _sources(ctx, pool, ids),
        "inWindow": any(pool.get(i) and pool.get(i).in_window for i in ids),
    }


def _evidence_story(ctx: EditionContext, ev: Evidence) -> dict:
    return {
        "title": ev.title,
        "fields": [{"label": t(ctx.language, "field.summary"), "text": ev.summary}] if ev.summary else [],
        "tickers": ev.tickers,
        "sources": [_source(ctx, ev)],
        "inWindow": ev.in_window,
    }


def _money(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.0f}M"
    return f"${v:,.2f}"


def _earnings_rows(ctx: EditionContext, snapshot: MarketSnapshot) -> list[dict]:
    rows = []
    for r in snapshot.earnings:
        try:
            day = date.fromisoformat(str(r.get("date")))
        except ValueError:
            continue
        hour = str(r.get("hour") or "").lower()
        rows.append({"symbol": str(r.get("symbol", "")).upper(), "date": day,
                     "hour": t(ctx.language, f"earnings.{hour}") if hour in ("bmo", "amc", "dmh") else "",
                     "eps": r.get("epsEstimate"), "revenue": r.get("revenueEstimate")})
    return rows


def _earnings_label(ctx: EditionContext, rows: list[dict], symbol: str) -> str | None:
    for r in rows:
        if r["symbol"] == symbol:
            when = ctx.date_label(r["date"]) + (f" · {r['hour']}" if r["hour"] else "")
            return f"{t(ctx.language, 'earnings.label')}: {when}"
    return None


def _top20_headlines(pool: EvidencePool, top20: list[str]) -> list[Evidence]:
    """In-window items about top-20 companies; broad market/RSS stories before company feeds."""
    wanted = set(top20)
    items = [e for e in pool.items.values() if e.in_window and wanted.intersection(e.tickers)]
    broad = [e for e in items if e.origin == "market" or e.origin.startswith("rss:")]
    rest = [e for e in items if e not in broad]
    return (sorted(broad, key=lambda e: e.published, reverse=True)
            + sorted(rest, key=lambda e: e.published, reverse=True))


# ---- shared sections ----------------------------------------------------------------
def _base(ctx: EditionContext, cfg: dict, demo: bool = False) -> dict:
    lang = ctx.language
    return {
        "schemaVersion": EDITION_SCHEMA_VERSION,
        "id": ctx.edition_id,
        "date": ctx.date,
        "slot": ctx.slot,
        "language": lang,
        "timezone": cfg.get("timezone", "America/New_York"),
        "tzLabel": ctx.tz_label,
        "generatedAt": ctx.now_utc.isoformat().replace("+00:00", "Z"),
        "demo": demo,
        "title": (cfg.get("title") or {}).get(lang) or "AI Stock News Dashboard",
        "subtitle": f"{t(lang, 'market')} · {ctx.long_date()} · {ctx.slot_label().upper()} · {ctx.time_label()}",
        "timelineLabel": f"{ctx.short_date()} · {ctx.slot_label()}",
    }


def _strip(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot) -> dict:
    tiles = []
    for s in cfg.get("strip", []):
        px = _price_fields(snapshot, s["symbol"])
        tiles.append({"name": s["name"], "mini": s["symbol"], "value": px["price"], "change": px["change"],
                      "cls": px["cls"]})
    as_of = snapshot.as_of()
    meta = (t(ctx.language, "strip.meta", asof=ctx.stamp_label(as_of)) if as_of
            else t(ctx.language, "strip.metaNoTime"))
    return {"meta": meta, "tiles": tiles}


def _top_cards(ctx: EditionContext, snapshot: MarketSnapshot, pool: EvidencePool, top: dict,
               top20: list[str]) -> list[dict]:
    """The three 'Top of the day' cards. Any card the model did not deliver (or that the
    quality gate withheld) is replaced by a headline card about the top 20."""
    lang = ctx.language
    lead, alert, mover = top.get("whatHappened") or {}, top.get("keyAlert") or {}, top.get("marketMover") or {}
    fallback = _fallback_top(ctx, snapshot, pool, top20)
    cards = []
    if lead.get("evidence") and lead.get("paragraph"):
        cards.append({"slot": "lead", "kicker": t(lang, "kicker.lead"), "badge": None, "title": lead["title"],
                      "paragraph": lead["paragraph"], "fields": [], "ticker": None,
                      "chips": _chips(snapshot, lead.get("tickers") or [], top20),
                      "sources": _sources(ctx, pool, lead["evidence"])})
    else:
        cards.append(fallback[0])
    if alert.get("evidence") and alert.get("whatHappened"):
        cat = alert.get("category") or "alert"
        cards.append({"slot": "alert", "kicker": t(lang, "kicker.alert"),
                      "badge": {"text": t(lang, f"badge.{cat}"), "cls": BADGES.get(cat, "ncat-r")},
                      "title": alert["title"], "paragraph": None,
                      "fields": _fields(lang, alert["whatHappened"], alert.get("whyItMatters", "")),
                      "ticker": None, "chips": _chips(snapshot, alert.get("tickers") or [], top20),
                      "sources": _sources(ctx, pool, alert["evidence"])})
    else:
        cards.append(fallback[1])
    sym = mover.get("ticker") or ""
    chip = _ticker_chip(snapshot, sym) if sym in top20 else None
    if chip and mover.get("evidence") and mover.get("whatHappened"):
        cards.append({"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None,
                      "title": mover["title"], "paragraph": None,
                      "fields": _fields(lang, mover["whatHappened"], mover.get("whyItMatters", "")),
                      "ticker": chip, "chips": [], "sources": _sources(ctx, pool, mover["evidence"])})
    else:
        cards.append(fallback[2])
    return cards


def _fallback_top(ctx: EditionContext, snapshot: MarketSnapshot, pool: EvidencePool,
                  top20: list[str]) -> list[dict]:
    """Headline-only cards, still limited to top-20 companies."""
    lang = ctx.language
    best = None
    for sym in top20:
        q = snapshot.quote(sym)
        if q and (best is None or abs(q["changePct"]) > abs(best[1]["changePct"])):
            best = (sym, q)
    mover_news = pool.by_ticker(best[0], in_window_only=True) if best else []
    mover_ev = mover_news[0] if mover_news else None
    headlines = [e for e in _top20_headlines(pool, top20) if e is not mover_ev]
    lead_ev = headlines[0] if headlines else None
    alert_ev = headlines[1] if len(headlines) > 1 else None
    empty = t(lang, "fallback.noTop")
    lead = {"slot": "lead", "kicker": t(lang, "kicker.lead"), "badge": None,
            "title": lead_ev.title if lead_ev else empty,
            "paragraph": (lead_ev.summary or None) if lead_ev else None, "fields": [], "ticker": None,
            "chips": _chips(snapshot, lead_ev.tickers, top20) if lead_ev else [],
            "sources": [_source(ctx, lead_ev)] if lead_ev else []}
    alert = {"slot": "alert", "kicker": t(lang, "kicker.alert"), "badge": None,
             "title": alert_ev.title if alert_ev else empty, "paragraph": None,
             "fields": ([{"label": t(lang, "field.summary"), "text": alert_ev.summary}]
                        if alert_ev and alert_ev.summary else []),
             "ticker": None, "chips": _chips(snapshot, alert_ev.tickers, top20) if alert_ev else [],
             "sources": [_source(ctx, alert_ev)] if alert_ev else []}
    if best:
        sym = best[0]
        mover = {"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None,
                 "title": mover_ev.title if mover_ev else f"{snapshot.names.get(sym, sym)} ({sym})",
                 "paragraph": None,
                 "fields": [{"label": t(lang, "field.summary"),
                             "text": (mover_ev.summary if mover_ev and mover_ev.summary
                                      else t(lang, "fallback.mover"))}],
                 "ticker": _ticker_chip(snapshot, sym), "chips": [],
                 "sources": [_source(ctx, mover_ev)] if mover_ev else []}
    else:
        mover = {"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None, "title": empty,
                 "paragraph": None, "fields": [], "ticker": None, "chips": [], "sources": []}
    return [lead, alert, mover]


def _board(ctx: EditionContext, snapshot: MarketSnapshot, pool: EvidencePool, notes: list | None,
           earnings: list[dict], used: list[str]) -> dict:
    """The top-20 table. Rows without a validated AI note show the latest source headline."""
    lang = ctx.language
    by_symbol = {n["ticker"]: n for n in notes or [] if isinstance(n, dict) and n.get("ticker")}
    rows = []
    for row in snapshot.universe:
        sym = row["symbol"]
        note = by_symbol.get(sym)
        sentiment, label = None, None
        if note and note.get("note"):
            kind, text = "ai", note["note"]
            key = note.get("sentiment") or "neutral"
            sentiment = {"key": key, "label": t(lang, f"sent.{key}")}
            ev = next((pool.get(i) for i in note.get("evidence") or [] if pool.get(i)), None)
        else:
            news = pool.by_ticker(sym, in_window_only=True)
            ev = news[0] if news else None
            if ev:
                kind, text, label = "headline", ev.title, t(lang, "board.headline")
            else:
                kind, text = "none", t(lang, "board.noNews")
        if ev:
            used.append(ev.id)
        rows.append({
            "rank": row["rank"],
            "symbol": sym,
            "name": row.get("name") or snapshot.names.get(sym, sym),
            **_price_fields(snapshot, sym),
            "marketCap": fmt_cap(row.get("marketCap")),
            "sentiment": sentiment,
            "note": text,
            "noteKind": kind,
            "noteLabel": label,
            "source": _source(ctx, ev),
            "earnings": _earnings_label(ctx, earnings, sym),
        })
    return {
        "title": t(lang, "board.title"),
        "note": t(lang, "board.note"),
        "columns": {k: t(lang, f"board.col.{k}") for k in ("rank", "company", "price", "change", "cap", "note")},
        "rows": rows,
    }


def _watchlist_cards(ctx: EditionContext, snapshot: MarketSnapshot, pool: EvidencePool, extras: list[str],
                     notes: list | None, earnings: list[dict], fallback: bool, used: list[str]) -> list[dict]:
    lang = ctx.language
    by_symbol = {n["ticker"]: n for n in notes or [] if isinstance(n, dict) and n.get("ticker")}
    cards = []
    for sym in extras:
        note = by_symbol.get(sym)
        cited = [pool.get(i) for i in (note or {}).get("evidence") or [] if pool.get(i)]
        headlines: list[Evidence] = []
        for ev in cited + pool.by_ticker(sym, in_window_only=True):
            if ev not in headlines:
                headlines.append(ev)
        headlines = headlines[:3]
        used.extend(e.id for e in headlines)
        cards.append({
            "symbol": sym,
            "name": snapshot.names.get(sym, sym),
            **_price_fields(snapshot, sym),
            "sentiment": ({"key": note["sentiment"], "label": t(lang, f"sent.{note['sentiment']}")}
                          if note else None),
            "summary": note["summary"] if note else t(lang, "watch.noAi" if fallback else "watch.noNote"),
            "watch": ({"label": t(lang, "field.watch"), "text": note["whatToWatch"]}
                      if note and note.get("whatToWatch") else None),
            "earnings": _earnings_label(ctx, earnings, sym),
            "headlines": [{"title": e.title, "name": e.source, "url": e.url,
                           "time": ctx.stamp_label(e.published)} for e in headlines],
        })
    return cards


def _markets_page(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot, symbols: list[str],
                  earnings: list[dict]) -> dict:
    lang = ctx.language
    moves = [(snapshot.quote(s)["changePct"], s) for s in symbols if snapshot.quote(s)]
    up = sorted((m for m in moves if m[0] > 0), reverse=True)[:3]
    down = sorted(m for m in moves if m[0] < 0)[:3]
    movers = []
    for _, sym in up + sorted(down, reverse=True):  # best to worst
        q = snapshot.quote(sym)
        movers.append({
            "symbol": sym,
            "name": snapshot.names.get(sym, sym),
            **_price_fields(snapshot, sym),
            "metrics": [
                {"label": t(lang, "metric.change"),
                 "value": f"{'+' if q['change'] >= 0 else '−'}{fmt_price(abs(q['change']))}",
                 "cls": cls_for(q["changePct"])},
                {"label": t(lang, "metric.range"), "value": fmt_range(q.get("low"), q.get("high"))},
                {"label": t(lang, "metric.prev"), "value": fmt_price(q.get("prevClose"))},
            ],
        })
    tiles = []
    for s in cfg.get("sectors", []):
        px = _price_fields(snapshot, s["etf"])
        tiles.append({"name": (s.get("label") or {}).get(lang) or s["id"], "mini": s["etf"],
                      "value": px["price"], "change": px["change"], "cls": px["cls"]})
    agenda = []
    for r in earnings:
        when = ctx.date_label(r["date"]) + (f" · {r['hour']}" if r["hour"] else "")
        parts = [f"{r['symbol']} ({snapshot.names.get(r['symbol'], r['symbol'])})"]
        if r["eps"] is not None:
            parts.append(f"EPS est. {_money(r['eps'])}")
        if r["revenue"]:
            parts.append(f"revenue est. {_money(r['revenue'])}")
        agenda.append({"when": when, "text": " · ".join(parts)})
    return {
        "type": "markets", "title": t(lang, "page.markets"),
        "movers": {"title": t(lang, "movers.title"), "items": movers},
        "secondary": {"summary": t(lang, "secondary.summary"), "tiles": tiles, "note": t(lang, "secondary.note")},
        "agenda": {"title": t(lang, "agenda.title"), "items": agenda, "empty": t(lang, "agenda.empty")},
    }


def _sector_tabs(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot, stories_by_sector: dict) -> list[dict]:
    tabs = []
    for s in cfg.get("sectors", []):
        q = snapshot.quote(s["etf"])
        tabs.append({
            "id": s["id"],
            "label": (s.get("label") or {}).get(ctx.language) or s["id"],
            "perf": {"symbol": s["etf"], "change": fmt_pct(q["changePct"]), "cls": cls_for(q["changePct"])} if q else None,
            "stories": stories_by_sector.get(s["id"], []),
            "empty": t(ctx.language, "sector.empty"),
        })
    tabs.sort(key=lambda tab: 0 if tab["stories"] else 1)  # stable: sectors with news first
    return tabs


def _footer(ctx: EditionContext, pool: EvidencePool, ids: list[str]) -> dict:
    lang = ctx.language
    seen: dict[str, str] = {}
    for item_id in ids:
        ev = pool.get(item_id)
        if ev and ev.source not in seen:
            seen[ev.source] = ev.url
    return {
        "sourcesLabel": t(lang, "footer.sources"),
        "sources": [{"name": n, "url": u} for n, u in sorted(seen.items(), key=lambda kv: kv[0].lower())],
        "methodology": t(lang, "footer.methodology"),
        "disclaimer": t(lang, "footer.disclaimer"),
    }


def _method_lines(ctx: EditionContext, cfg: dict, pool: EvidencePool, settings: Settings, qa: dict) -> list[str]:
    lang = ctx.language
    since = ctx.stamp_label(ctx.window_start_utc)
    lines = [t(lang, "method.collect", n=len(pool), s=len(pool.sources()), since=since,
               days=(cfg.get("limits") or {}).get("contextDays", 7)),
             t(lang, "method.top20", n=len((cfg.get("universe") or {}).get("pool") or []))]
    if qa.get("status") == "fallback" and (qa.get("quality") or {}).get("gate") == "withhold":
        reason = str(qa.get("reason", "")).removeprefix("quality gate: ")
        lines.append(t(lang, "method.withheldAll", reason=reason))
    elif qa.get("status") == "fallback":
        lines.append(t(lang, "method.fallback", reason=qa.get("reason", "error")))
    elif qa.get("status") != "demo":
        tools = t(lang, "method.tools", n=qa["toolCalls"]) if qa.get("toolCalls") else ""
        repairs = t(lang, "method.repairs", n=qa["repairs"]) if qa.get("repairs") else ""
        lines.append(t(lang, "method.agent", model=model_label(settings.model_id), tools=tools, repairs=repairs))
        quality = qa.get("quality") or {}
        if quality.get("groundedness") is not None:
            lines.append(t(lang, "method.quality", score=f"{quality['groundedness']:.2f}",
                           judge=model_label(quality.get("judgeModel", "")),
                           unverified=quality.get("unverifiedNumbers", 0), fixes=qa.get("fixes", 0)))
        elif quality:
            lines.append(t(lang, "method.qualityNoJudge", unverified=quality.get("unverifiedNumbers", 0),
                           fixes=qa.get("fixes", 0)))
        if quality.get("withheld"):
            lines.append(t(lang, "method.withheld", n=len(quality["withheld"])))
    lines.append(t(lang, "method.data"))
    return lines


# ---- editions --------------------------------------------------------------------------
def _compose(ctx: EditionContext, cfg: dict, settings: Settings, snapshot: MarketSnapshot, pool: EvidencePool,
             analysis: dict, qa: dict, demo: bool, fallback: bool) -> dict:
    lang = ctx.language
    edition = _base(ctx, cfg, demo=demo)
    top20, extras = coverage(cfg, snapshot)
    earnings = _earnings_rows(ctx, snapshot)
    used: list[str] = []

    cards = _top_cards(ctx, snapshot, pool, {} if fallback else (analysis.get("top") or {}), top20)
    board = _board(ctx, snapshot, pool, None if fallback else analysis.get("top20"), earnings, used)

    if fallback:
        shown = {s["id"] for c in cards for s in c["sources"]}
        headlines = [e for e in pool.market_wide() if e.in_window and e.id not in shown][:10]
        stories = [_evidence_story(ctx, e) for e in headlines]
        used += [e.id for e in headlines]
        blocks = [{"id": "headlines", "title": t(lang, "theme.headlines"), "stories": stories}] if stories else []
        n_latest = len(stories)
        by_sector: dict[str, list] = {}
        signals = [{"kind": "data", "label": t(lang, "signal.data"),
                    "text": f"{r['symbol']} · {t(lang, 'earnings.label')}: {ctx.date_label(r['date'])}"
                            + (f" · {r['hour']}" if r["hour"] else ""), "sources": []} for r in earnings[:6]]
    else:
        # latest, grouped by theme in order of first appearance
        grouped: dict[str, dict] = {}
        for item in analysis.get("latest") or []:
            theme = item.get("theme") if item.get("theme") in THEMES else "company"
            block = grouped.setdefault(theme, {"id": theme, "title": t(lang, f"theme.{theme}"), "stories": []})
            block["stories"].append(_story(ctx, pool, item))
        blocks = list(grouped.values())
        n_latest = len(analysis.get("latest") or [])
        by_sector = {}
        for item in analysis.get("sectors") or []:
            by_sector.setdefault(item["sector"], []).append(_story(ctx, pool, item))
        signals = [{"kind": r["kind"], "label": t(lang, f"signal.{r['kind']}"), "text": r["text"],
                    "sources": _sources(ctx, pool, r.get("evidence") or [], 2)}
                   for r in analysis.get("radar") or []]
        used += cited_ids(analysis)

    if fallback:
        edition["intro"] = {
            "greeting": t(lang, f"greeting.{ctx.slot}"),
            "headline": t(lang, "fallback.headline"),
            "body": t(lang, "fallback.body"),
            "stamp": t(lang, "stamp.fallback", since=ctx.stamp_label(ctx.window_start_utc).upper(), n=len(pool),
                       s=len(pool.sources())),
        }
    else:
        edition["intro"] = {
            "greeting": t(lang, f"greeting.{ctx.slot}"),
            "headline": analysis.get("headline", ""),
            "body": analysis.get("summary", ""),
            "stamp": t(lang, "stamp", since=ctx.stamp_label(ctx.window_start_utc).upper(), n=len(pool),
                       s=len(pool.sources()), model=model_label(settings.model_id).upper()),
        }
    edition["strip"] = _strip(ctx, cfg, snapshot)
    pages = [
        {"type": "top", "title": t(lang, "page.top"), "cards": cards, "board": board},
        {"type": "latest", "title": t(lang, "page.latest"),
         "countLabel": t(lang, "story") if n_latest == 1 else t(lang, "stories", n=n_latest),
         "blocks": blocks, "empty": t(lang, "latest.empty")},
    ]
    if extras:
        pages.append({"type": "watchlist", "title": t(lang, "page.watchlist"),
                      "cards": _watchlist_cards(ctx, snapshot, pool, extras,
                                                None if fallback else analysis.get("watchlist"),
                                                earnings, fallback, used)})
    pages += [
        _markets_page(ctx, cfg, snapshot, top20 + extras, earnings),
        {"type": "sectors", "title": t(lang, "page.sectors"), "tabs": _sector_tabs(ctx, cfg, snapshot, by_sector)},
        {"type": "radar", "title": t(lang, "page.radar"), "signals": signals,
         "method": {"title": t(lang, "method.title"), "lines": _method_lines(ctx, cfg, pool, settings, qa)}},
    ]
    for number, page in enumerate(pages, 1):
        page["number"] = f"{number:02d}"
    edition["pages"] = pages
    used += [s["id"] for c in cards for s in c["sources"]]
    edition["footer"] = _footer(ctx, pool, used)
    edition["qa"] = qa
    return edition


def compose_edition(ctx: EditionContext, cfg: dict, settings: Settings, snapshot: MarketSnapshot,
                    pool: EvidencePool, analysis: dict, qa: dict, demo: bool = False) -> dict:
    """Full edition from the model's validated analysis."""
    return _compose(ctx, cfg, settings, snapshot, pool, analysis, qa, demo=demo, fallback=False)


def compose_fallback(ctx: EditionContext, cfg: dict, settings: Settings, snapshot: MarketSnapshot,
                     pool: EvidencePool, qa: dict) -> dict:
    """Market data + source headlines, no AI text. Used when the AI step fails or is withheld."""
    return _compose(ctx, cfg, settings, snapshot, pool, {}, qa, demo=False, fallback=True)


def short_reason(exc: BaseException) -> str:
    return trim(re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}"), 160)
