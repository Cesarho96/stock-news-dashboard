"""Turn market data + evidence + the model's analysis into the dashboard's edition JSON.

The browser renderer (web/assets/dashboard.js) only displays what is here: every URL,
source name, timestamp and price is filled in by this module from collected data.
"""

from __future__ import annotations

import re
from datetime import date

from .clock import EditionContext
from .evidence import Evidence, EvidencePool, trim
from .i18n import t
from .market import MarketSnapshot, cls_for, fmt_pct, fmt_price, fmt_range
from .schema import THEMES
from .settings import Settings
from .validate import cited_ids

SCHEMA_VERSION = 1
BADGES = {
    "alert": "ncat-r",
    "risk": "ncat-v",
    "opportunity": "ncat-o",
    "macro": "ncat-i",
    "earnings": "ncat-b",
    "data": "ncat-n",
}


def model_label(model_id: str) -> str:
    """'global.anthropic.claude-sonnet-5-5' -> 'Claude Sonnet 5.5'."""
    m = re.search(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?![0-9])", model_id or "")
    if not m:
        return model_id or "unknown model"
    version = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"Claude {m.group(1).capitalize()} {version}"


def _sources(ctx: EditionContext, pool: EvidencePool, ids: list[str], limit: int = 3) -> list[dict]:
    out = []
    for item_id in ids[:limit]:
        ev = pool.get(item_id)
        if ev:
            out.append({"name": ev.source, "url": ev.url, "time": ctx.stamp_label(ev.published), "id": ev.id})
    return out


def _fields(lang: str, what: str, why: str) -> list[dict]:
    out = []
    if what:
        out.append({"label": t(lang, "field.what"), "text": what})
    if why:
        out.append({"label": t(lang, "field.why"), "text": why})
    return out


def _ticker_chip(snapshot: MarketSnapshot, symbol: str) -> dict | None:
    q = snapshot.quote(symbol)
    if not q:
        return None
    return {
        "symbol": symbol,
        "name": snapshot.names.get(symbol, symbol),
        "price": fmt_price(q["price"]),
        "change": fmt_pct(q["changePct"]),
        "cls": cls_for(q["changePct"]),
    }


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
        "sources": [{"name": ev.source, "url": ev.url, "time": ctx.stamp_label(ev.published), "id": ev.id}],
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


# ---- shared sections ----------------------------------------------------------------
def _base(ctx: EditionContext, cfg: dict, demo: bool = False) -> dict:
    lang = ctx.language
    return {
        "schemaVersion": SCHEMA_VERSION,
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
        q = snapshot.quote(s["symbol"])
        tiles.append({
            "name": s["name"],
            "mini": s["symbol"],
            "value": fmt_price(q["price"]) if q else "—",
            "change": fmt_pct(q["changePct"]) if q else "",
            "cls": cls_for(q["changePct"]) if q else "flat",
        })
    as_of = snapshot.as_of()
    meta = (t(ctx.language, "strip.meta", asof=ctx.stamp_label(as_of)) if as_of
            else t(ctx.language, "strip.metaNoTime"))
    return {"meta": meta, "tiles": tiles}


def _markets_page(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot) -> dict:
    lang = ctx.language
    movers = []
    for w in cfg["watchlist"]:
        q = snapshot.quote(w["symbol"])
        if not q:
            continue
        movers.append((q["changePct"], {
            "symbol": w["symbol"],
            "name": snapshot.names.get(w["symbol"], w["symbol"]),
            "price": fmt_price(q["price"]),
            "change": fmt_pct(q["changePct"]),
            "cls": cls_for(q["changePct"]),
            "metrics": [
                {"label": t(lang, "metric.change"),
                 "value": f"{'+' if q['change'] >= 0 else '−'}{fmt_price(abs(q['change']))}",
                 "cls": cls_for(q["changePct"])},
                {"label": t(lang, "metric.range"), "value": fmt_range(q.get("low"), q.get("high"))},
                {"label": t(lang, "metric.prev"), "value": fmt_price(q.get("prevClose"))},
            ],
        }))
    movers.sort(key=lambda m: m[0], reverse=True)
    tiles = []
    for s in cfg.get("sectors", []):
        q = snapshot.quote(s["etf"])
        tiles.append({
            "name": (s.get("label") or {}).get(lang) or s["id"],
            "mini": s["etf"],
            "value": fmt_price(q["price"]) if q else "—",
            "change": fmt_pct(q["changePct"]) if q else "",
            "cls": cls_for(q["changePct"]) if q else "flat",
        })
    agenda = []
    for r in _earnings_rows(ctx, snapshot):
        when = ctx.date_label(r["date"]) + (f" · {r['hour']}" if r["hour"] else "")
        parts = [f"{r['symbol']} ({snapshot.names.get(r['symbol'], r['symbol'])})"]
        if r["eps"] is not None:
            parts.append(f"EPS est. {_money(r['eps'])}")
        if r["revenue"]:
            parts.append(f"revenue est. {_money(r['revenue'])}")
        agenda.append({"when": when, "text": " · ".join(parts)})
    return {
        "type": "markets", "number": "04", "title": t(lang, "page.markets"),
        "movers": {"title": t(lang, "movers.title"), "items": [m[1] for m in movers]},
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
               days=(cfg.get("limits") or {}).get("contextDays", 7))]
    if qa.get("status") == "fallback":
        lines.append(t(lang, "method.fallback", reason=qa.get("reason", "error")))
    elif qa.get("status") != "demo":
        tools = t(lang, "method.tools", n=qa["toolCalls"]) if qa.get("toolCalls") else ""
        repairs = t(lang, "method.repairs", n=qa["repairs"]) if qa.get("repairs") else ""
        lines.append(t(lang, "method.agent", model=model_label(settings.model_id), tools=tools, repairs=repairs))
    lines.append(t(lang, "method.data"))
    return lines


# ---- full edition (AI analysis) ----------------------------------------------------------
def compose_edition(ctx: EditionContext, cfg: dict, settings: Settings, snapshot: MarketSnapshot,
                    pool: EvidencePool, analysis: dict, qa: dict, demo: bool = False) -> dict:
    lang = ctx.language
    edition = _base(ctx, cfg, demo=demo)
    top = analysis.get("top") or {}
    lead, alert, mover = top.get("whatHappened") or {}, top.get("keyAlert") or {}, top.get("marketMover") or {}
    fallback_top = _fallback_top(ctx, cfg, snapshot, pool)

    cards = []
    if lead.get("evidence") and lead.get("paragraph"):
        cards.append({"slot": "lead", "kicker": t(lang, "kicker.lead"), "badge": None, "title": lead["title"],
                      "paragraph": lead["paragraph"], "fields": [], "ticker": None,
                      "sources": _sources(ctx, pool, lead["evidence"])})
    else:
        cards.append(fallback_top[0])
    if alert.get("evidence") and alert.get("whatHappened"):
        cat = alert.get("category") or "alert"
        cards.append({"slot": "alert", "kicker": t(lang, "kicker.alert"),
                      "badge": {"text": t(lang, f"badge.{cat}"), "cls": BADGES.get(cat, "ncat-r")},
                      "title": alert["title"], "paragraph": None,
                      "fields": _fields(lang, alert["whatHappened"], alert.get("whyItMatters", "")),
                      "ticker": None, "sources": _sources(ctx, pool, alert["evidence"])})
    else:
        cards.append(fallback_top[1])
    chip = _ticker_chip(snapshot, mover.get("ticker", "")) if mover.get("ticker") else None
    if chip and mover.get("evidence") and mover.get("whatHappened"):
        cards.append({"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None,
                      "title": mover["title"], "paragraph": None,
                      "fields": _fields(lang, mover["whatHappened"], mover.get("whyItMatters", "")),
                      "ticker": chip, "sources": _sources(ctx, pool, mover["evidence"])})
    else:
        cards.append(fallback_top[2])

    # latest, grouped by theme in order of first appearance
    blocks: dict[str, dict] = {}
    for item in analysis.get("latest") or []:
        theme = item.get("theme") if item.get("theme") in THEMES else "company"
        block = blocks.setdefault(theme, {"id": theme, "title": t(lang, f"theme.{theme}"), "stories": []})
        block["stories"].append(_story(ctx, pool, item))
    n_latest = len(analysis.get("latest") or [])

    # watchlist
    notes = {n["ticker"]: n for n in analysis.get("watchlist") or [] if isinstance(n, dict) and n.get("ticker")}
    earnings = _earnings_rows(ctx, snapshot)
    extra_ids: list[str] = []
    wl_cards = []
    for w in cfg["watchlist"]:
        sym = w["symbol"]
        note = notes.get(sym)
        cited = [pool.get(i) for i in (note or {}).get("evidence") or [] if pool.get(i)]
        recent = pool.by_ticker(sym, in_window_only=True)
        headlines: list[Evidence] = []
        for ev in cited + recent:
            if ev not in headlines:
                headlines.append(ev)
        headlines = headlines[:3]
        extra_ids += [e.id for e in headlines]
        q = snapshot.quote(sym)
        wl_cards.append({
            "symbol": sym,
            "name": snapshot.names.get(sym, w.get("name") or sym),
            "price": fmt_price(q["price"]) if q else "—",
            "change": fmt_pct(q["changePct"]) if q else "",
            "cls": cls_for(q["changePct"]) if q else "flat",
            "sentiment": ({"key": note["sentiment"], "label": t(lang, f"sent.{note['sentiment']}")}
                          if note else None),
            "summary": note["summary"] if note else (t(lang, "watch.noAi") if qa.get("status") == "fallback"
                                                     else t(lang, "watch.noNote")),
            "watch": ({"label": t(lang, "field.watch"), "text": note["whatToWatch"]}
                      if note and note.get("whatToWatch") else None),
            "earnings": _earnings_label(ctx, earnings, sym),
            "headlines": [{"title": e.title, "name": e.source, "url": e.url,
                           "time": ctx.stamp_label(e.published)} for e in headlines],
        })

    # sectors
    by_sector: dict[str, list] = {}
    for item in analysis.get("sectors") or []:
        by_sector.setdefault(item["sector"], []).append(_story(ctx, pool, item))

    # radar
    signals = [{"kind": r["kind"], "label": t(lang, f"signal.{r['kind']}"), "text": r["text"],
                "sources": _sources(ctx, pool, r.get("evidence") or [], 2)}
               for r in analysis.get("radar") or []]

    edition["intro"] = {
        "greeting": t(lang, f"greeting.{ctx.slot}"),
        "headline": analysis.get("headline", ""),
        "body": analysis.get("summary", ""),
        "stamp": t(lang, "stamp", since=ctx.stamp_label(ctx.window_start_utc).upper(), n=len(pool),
                   s=len(pool.sources()), model=model_label(settings.model_id).upper()),
    }
    edition["strip"] = _strip(ctx, cfg, snapshot)
    edition["pages"] = [
        {"type": "top", "number": "01", "title": t(lang, "page.top"), "cards": cards},
        {"type": "latest", "number": "02", "title": t(lang, "page.latest"),
         "countLabel": t(lang, "story") if n_latest == 1 else t(lang, "stories", n=n_latest),
         "blocks": list(blocks.values()), "empty": t(lang, "latest.empty")},
        {"type": "watchlist", "number": "03", "title": t(lang, "page.watchlist"), "cards": wl_cards},
        _markets_page(ctx, cfg, snapshot),
        {"type": "sectors", "number": "05", "title": t(lang, "page.sectors"),
         "tabs": _sector_tabs(ctx, cfg, snapshot, by_sector)},
        {"type": "radar", "number": "06", "title": t(lang, "page.radar"), "signals": signals,
         "method": {"title": t(lang, "method.title"), "lines": _method_lines(ctx, cfg, pool, settings, qa)}},
    ]
    used = cited_ids(analysis) + extra_ids + [s["id"] for c in cards for s in c["sources"]]
    edition["footer"] = _footer(ctx, pool, used)
    edition["qa"] = qa
    return edition


# ---- fallback edition (no AI) -----------------------------------------------------------
def _fallback_top(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot, pool: EvidencePool) -> list[dict]:
    lang = ctx.language
    market = [e for e in pool.market_wide() if e.in_window] or pool.market_wide()
    lead_ev = market[0] if market else None
    alert_ev = market[1] if len(market) > 1 else None
    lead = {"slot": "lead", "kicker": t(lang, "kicker.lead"), "badge": None,
            "title": lead_ev.title if lead_ev else t(lang, "latest.empty"),
            "paragraph": (lead_ev.summary or None) if lead_ev else None, "fields": [], "ticker": None,
            "sources": _sources(ctx, pool, [lead_ev.id]) if lead_ev else []}
    alert = {"slot": "alert", "kicker": t(lang, "kicker.alert"), "badge": None,
             "title": alert_ev.title if alert_ev else t(lang, "latest.empty"), "paragraph": None,
             "fields": ([{"label": t(lang, "field.summary"), "text": alert_ev.summary}]
                        if alert_ev and alert_ev.summary else []),
             "ticker": None, "sources": _sources(ctx, pool, [alert_ev.id]) if alert_ev else []}
    best = None
    for w in cfg["watchlist"]:
        q = snapshot.quote(w["symbol"])
        if q and (best is None or abs(q["changePct"]) > abs(best[1]["changePct"])):
            best = (w["symbol"], q)
    if best:
        sym = best[0]
        news = pool.by_ticker(sym, in_window_only=True)
        mover = {"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None,
                 "title": news[0].title if news else f"{snapshot.names.get(sym, sym)} ({sym})",
                 "paragraph": None,
                 "fields": [{"label": t(lang, "field.summary"),
                             "text": (news[0].summary if news and news[0].summary else t(lang, "fallback.mover"))}],
                 "ticker": _ticker_chip(snapshot, sym),
                 "sources": _sources(ctx, pool, [news[0].id]) if news else []}
    else:
        mover = {"slot": "mover", "kicker": t(lang, "kicker.mover"), "badge": None,
                 "title": t(lang, "latest.empty"), "paragraph": None, "fields": [], "ticker": None, "sources": []}
    return [lead, alert, mover]


def compose_fallback(ctx: EditionContext, cfg: dict, settings: Settings, snapshot: MarketSnapshot,
                     pool: EvidencePool, qa: dict) -> dict:
    """Market data + source headlines, no AI text. Used when the AI step fails."""
    lang = ctx.language
    headlines = [e for e in pool.market_wide() if e.in_window][:10]
    cards = _fallback_top(ctx, cfg, snapshot, pool)
    used_top = {s["id"] for c in cards for s in c["sources"]}
    stories = [_evidence_story(ctx, e) for e in headlines if e.id not in used_top]
    earnings = _earnings_rows(ctx, snapshot)
    signals = [{"kind": "data", "label": t(lang, "signal.data"),
                "text": f"{r['symbol']} · {t(lang, 'earnings.label')}: {ctx.date_label(r['date'])}"
                        + (f" · {r['hour']}" if r["hour"] else ""), "sources": []} for r in earnings[:6]]
    analysis = {"headline": t(lang, "fallback.headline"), "summary": t(lang, "fallback.body"),
                "latest": [], "watchlist": [], "sectors": [], "radar": []}
    edition = compose_edition(ctx, cfg, settings, snapshot, pool, analysis, qa)
    edition["intro"]["stamp"] = t(lang, "stamp.fallback", since=ctx.stamp_label(ctx.window_start_utc).upper(),
                                  n=len(pool), s=len(pool.sources()))
    for page in edition["pages"]:
        if page["type"] == "latest":
            page["blocks"] = ([{"id": "headlines", "title": t(lang, "theme.headlines"), "stories": stories}]
                              if stories else [])
            page["countLabel"] = t(lang, "story") if len(stories) == 1 else t(lang, "stories", n=len(stories))
        elif page["type"] == "radar":
            page["signals"] = signals
    used = [s["id"] for c in cards for s in c["sources"]] + [e.id for e in headlines]
    for card in edition["pages"][2]["cards"]:
        for h in card["headlines"]:
            match = next((e for e in pool.items.values() if e.url == h["url"]), None)
            if match:
                used.append(match.id)
    edition["footer"] = _footer(ctx, pool, used)
    return edition


def short_reason(exc: BaseException) -> str:
    return trim(re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}"), 160)
