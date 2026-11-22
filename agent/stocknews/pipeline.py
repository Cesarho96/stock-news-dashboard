"""End-to-end run: collect -> agent -> validate -> compose -> publish."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from .agent import AgentError, NewsAgent
from .clock import build_context
from .collect import collect_all
from .compose import compose_edition, compose_fallback, model_label, short_reason
from .finnhub import Finnhub, FinnhubError
from .httpclient import HttpClient
from .llm import BedrockClaude, LLMError
from .sec import SecEdgar
from .settings import Settings, limits, load_config
from .tools import ResearchTools

log = logging.getLogger(__name__)


def run_edition(settings: Settings, publisher, *, llm: BedrockClaude | None = None,
                http: HttpClient | None = None, fh: Finnhub | None = None, sec: SecEdgar | None = None,
                now: datetime | None = None, slot: str | None = None, use_llm: bool = True,
                deadline: float | None = None, cfg: dict | None = None) -> dict:
    started = time.monotonic()
    cfg = cfg or load_config(settings)
    lim = limits(cfg)
    ctx = build_context(now, tz_name=cfg.get("timezone", "America/New_York"),
                        tz_label=cfg.get("timezoneLabel", "ET"), slot=slot, language=settings.language,
                        context_days=lim["contextDays"])
    http = http or HttpClient()
    if fh is None and settings.finnhub_api_key:
        try:
            fh = Finnhub(settings.finnhub_api_key, http, settings.finnhub_min_interval)
        except FinnhubError as exc:
            log.warning("%s", exc)
    if fh is None:
        log.warning("No Finnhub key: prices and company news will be missing")
    if sec is None and settings.sec_user_agent:
        try:
            sec = SecEdgar(settings.sec_user_agent, http)
        except ValueError as exc:
            log.warning("SEC EDGAR disabled: %s", exc)

    snapshot, pool, stats = collect_all(ctx, cfg, settings, fh, sec, http)
    qa: dict = {
        "status": "ok",
        "model": settings.model_id,
        "modelLabel": model_label(settings.model_id),
        "evidenceCount": len(pool),
        "inWindow": pool.in_window_count,
        "sourceErrors": stats["errors"][:12],
        "toolCalls": 0,
        "repairs": 0,
        "notes": [],
    }

    edition = None
    if use_llm and len(pool) > 0:
        llm = llm or BedrockClaude(settings.model_id, settings.bedrock_region, settings.effort,
                                   settings.thinking, settings.max_tokens)
        tools = ResearchTools(fh, pool, snapshot, ctx, budget=settings.max_tool_calls) if fh else None
        agent = NewsAgent(llm, settings, tools=tools, deadline=deadline)
        try:
            result = agent.run(ctx, cfg, snapshot, pool)
            qa.update({
                "toolCalls": result.tool_calls,
                "repairs": result.repairs,
                "notes": result.notes + [str(i) for i in result.report.hard][:10],
                "fixes": len(result.report.fixes),
                "researchLog": tools.log if tools else [],
            })
            if result.report.hard:
                qa["status"] = "degraded"
            edition = compose_edition(ctx, cfg, settings, snapshot, pool, result.analysis, qa)
        except (AgentError, LLMError) as exc:
            log.error("AI step failed: %s", exc)
            qa.update({"status": "fallback", "reason": short_reason(exc)})
        finally:
            qa["usage"] = dict(llm.usage)
            qa["estimatedCostUsd"] = llm.estimated_cost(settings.price_input_mtok, settings.price_output_mtok)
            if llm.downgrades:
                qa["notes"].append("Bedrock features disabled for this run: " + ", ".join(llm.downgrades))
    else:
        qa.update({"status": "fallback", "reason": "AI step skipped" if not use_llm else "no evidence collected"})

    if edition is None:
        edition = compose_fallback(ctx, cfg, settings, snapshot, pool, qa)
    qa["seconds"] = round(time.monotonic() - started, 1)
    publisher.publish(edition)
    log.info("edition %s published: status=%s evidence=%d tools=%d repairs=%d cost~$%s in %.0fs",
             edition["id"], qa["status"], len(pool), qa.get("toolCalls", 0), qa.get("repairs", 0),
             qa.get("estimatedCostUsd", 0), qa["seconds"])
    return edition
