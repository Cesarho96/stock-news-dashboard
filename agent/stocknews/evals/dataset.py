"""Eval cases: the frozen inputs of one edition (market snapshot + evidence pool + timing).

A case is captured from every production run (inside its run record), so any edition can be
replayed later against a new prompt, model or code version. Specs accepted by load_cases():

  seed:                         the built-in synthetic case (evals/seed.py)
  path/to/case.json[.gz]        one case, a run record, or {"cases": [...]}
  path/to/folder/               every *.json / *.json.gz inside
  s3://bucket/prefix/           every object under the prefix (cases or run records)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..clock import EditionContext, build_context
from ..evidence import EvidencePool
from ..market import MarketSnapshot
from ..storage import read_json, split_s3

log = logging.getLogger(__name__)
CASE_VERSION = 1


def case_from_run(ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot, pool: EvidencePool, meta: dict,
                  case_id: str | None = None) -> dict:
    return {
        "caseVersion": CASE_VERSION,
        "caseId": case_id or f"{ctx.edition_id}-{ctx.now_utc.strftime('%H%M%S')}",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "edition": {"now": ctx.now_utc.isoformat(), "slot": ctx.slot, "language": ctx.language,
                    "timezone": cfg.get("timezone", "America/New_York"), "tzLabel": ctx.tz_label,
                    "contextDays": (ctx.now_utc - ctx.context_start_utc).days or 7,
                    "windowHours": round((ctx.now_utc - ctx.window_start_utc).total_seconds() / 3600)},
        "watchlist": [w["symbol"] for w in cfg.get("watchlist", [])],
        "snapshot": snapshot.to_dict(),
        "pool": pool.to_dict(),
        "meta": meta,
    }


def materialize(case: dict, cfg: dict, language: str | None = None):
    """Rebuild (cfg, ctx, snapshot, pool, tool_items) from a case.

    Evidence the research tools added during the original run is held back from the pool
    and served again by ReplayFinnhub if the model asks for it.
    """
    ed = case["edition"]
    cfg = dict(cfg)
    known = {w["symbol"]: w for w in cfg.get("watchlist", [])}
    if case.get("watchlist"):
        cfg["watchlist"] = [known.get(s, {"symbol": s, "name": s, "sector": None}) for s in case["watchlist"]]
    now = datetime.fromisoformat(ed["now"])
    ctx = build_context(now, tz_name=ed.get("timezone", "America/New_York"), tz_label=ed.get("tzLabel", "ET"),
                        slot=ed.get("slot"), language=language or ed.get("language", "en"),
                        context_days=int(ed.get("contextDays") or 7), window_hours=ed.get("windowHours"))
    snapshot = MarketSnapshot.from_dict(case["snapshot"])
    data = dict(case["pool"])
    items = list(data.get("items") or [])
    data["items"] = [it for it in items if not str(it.get("origin", "")).startswith("tool:")]
    tool_items = [it for it in items if str(it.get("origin", "")).startswith("tool:")]
    pool = EvidencePool.from_dict(data)
    return cfg, ctx, snapshot, pool, tool_items


class ReplayFinnhub:
    """Answers the research tools from a recorded case instead of the live API."""

    def __init__(self, snapshot: MarketSnapshot, tool_items: list[dict]):
        self.quotes = {k: dict(v) for k, v in snapshot.quotes.items()}
        self.names = dict(snapshot.names)
        self.tool_items = tool_items
        self.calls = 0

    def company_news(self, symbol, start, end):
        self.calls += 1
        out = []
        for it in self.tool_items:
            if symbol in (it.get("tickers") or []):
                published = datetime.fromisoformat(it["published"])
                out.append({"headline": it["title"], "url": it["url"], "source": it.get("source"),
                            "datetime": int(published.timestamp()), "summary": it.get("summary", ""),
                            "related": symbol})
        return out

    def quote(self, symbol):
        self.calls += 1
        q = self.quotes.get(symbol)
        return dict(q) if q else None

    def profile(self, symbol):
        return {"name": self.names.get(symbol, symbol)}


def _as_cases(doc, origin: str) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    if "cases" in doc:
        return [c for c in doc["cases"] if isinstance(c, dict) and "snapshot" in c]
    if "case" in doc and isinstance(doc["case"], dict):  # a run record
        return [doc["case"]]
    if "snapshot" in doc and "pool" in doc:
        return [doc]
    log.warning("not an eval case: %s", origin)
    return []


def load_cases(specs: list[str], cfg: dict, s3=None, max_cases: int | None = None) -> list[dict]:
    cases: list[dict] = []
    for spec in specs:
        if spec in ("seed", "seed:"):
            from .seed import seed_case

            cases.append(seed_case(cfg))
        elif spec.startswith("s3://"):
            if s3 is None:
                import boto3

                s3 = boto3.client("s3")
            bucket, prefix = split_s3(spec)
            keys = []
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith((".json", ".json.gz"))]
            for key in sorted(keys, reverse=True):  # newest first (keys carry dates)
                cases += _as_cases(read_json(f"s3://{bucket}/{key}", s3), f"s3://{bucket}/{key}")
        else:
            path = Path(spec)
            files = sorted(path.glob("*.json*")) if path.is_dir() else [path]
            for f in files:
                cases += _as_cases(read_json(str(f)), str(f))
        if max_cases and len(cases) >= max_cases:
            break
    return cases[:max_cases] if max_cases else cases


__all__ = ["case_from_run", "materialize", "load_cases", "ReplayFinnhub"]
