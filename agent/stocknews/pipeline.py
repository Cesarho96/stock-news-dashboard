"""End-to-end run: collect -> agent -> validate -> quality gate -> compose -> publish -> record.

produce() is shared by the scheduled run, the deployment smoke test and the offline evals,
so what CI measures is exactly what production does.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from . import prompts
from .agent import AgentError, AgentResult, NewsAgent
from .clock import EditionContext, build_context
from .collect import collect_all
from .compose import compose_edition, compose_fallback, model_label, short_reason
from .evidence import EvidencePool
from .finnhub import Finnhub, FinnhubError
from .httpclient import HttpClient
from .llm import BedrockClaude, LLMError
from .market import MarketSnapshot
from .quality import Checks, Decision, decide, run_checks, withhold
from .schema import SCHEMA_VERSION
from .sec import SecEdgar
from .settings import Settings, limits, load_config
from .storage import JsonDoc
from .tools import ResearchTools

log = logging.getLogger(__name__)
JUDGE_MIN_SECONDS = 120  # skip the online judge when less time than this is left in the Lambda


@dataclass
class Outcome:
    edition: dict
    ctx: EditionContext
    snapshot: MarketSnapshot
    pool: EvidencePool
    analysis: dict | None = None
    result: AgentResult | None = None
    checks: Checks | None = None
    judge: object | None = None  # evals.judge.JudgeResult
    decision: Decision | None = None

    @property
    def qa(self) -> dict:
        return self.edition.get("qa") or {}


def cache_location(settings: Settings, name: str) -> str | None:
    if settings.ops_bucket:
        return f"s3://{settings.ops_bucket}/cache/{settings.environment}/{name}"
    if settings.cache_dir:
        return f"{settings.cache_dir.rstrip('/')}/{name}"
    return None


def base_qa(settings: Settings, pool: EvidencePool, stats: dict) -> dict:
    return {
        "status": "ok",
        "model": settings.model_id,
        "modelLabel": model_label(settings.model_id),
        "release": settings.release_id,
        "environment": settings.environment,
        "promptVersion": prompts.PROMPT_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "evidenceCount": len(pool),
        "inWindow": pool.in_window_count,
        "sourceErrors": (stats.get("errors") or [])[:12],
        "toolCalls": 0,
        "repairs": 0,
        "notes": [],
    }


def produce(settings: Settings, cfg: dict, ctx: EditionContext, snapshot: MarketSnapshot, pool: EvidencePool,
            stats: dict, *, llm: BedrockClaude | None = None, judge_llm: BedrockClaude | None = None,
            fh=None, use_llm: bool = True, skip_reason: str | None = None, deadline: float | None = None,
            clock=time.monotonic) -> Outcome:
    """Agent + validation + quality gate + composition (no collection, no publishing)."""
    started = clock()
    qa = base_qa(settings, pool, stats)
    out = Outcome(edition={}, ctx=ctx, snapshot=snapshot, pool=pool)
    edition = None
    if use_llm and len(pool) > 0:
        llm = llm or BedrockClaude(settings.model_id, settings.bedrock_region, settings.effort,
                                   settings.thinking, settings.max_tokens)
        tools = ResearchTools(fh, pool, snapshot, ctx, budget=settings.max_tool_calls) if fh else None
        agent = NewsAgent(llm, settings, tools=tools, deadline=deadline, clock=clock)
        try:
            result = agent.run(ctx, cfg, snapshot, pool)
            out.result, out.analysis = result, result.analysis
            qa.update({
                "toolCalls": result.tool_calls,
                "repairs": result.repairs,
                "notes": result.notes + [str(i) for i in result.report.hard][:10],
                "fixes": len(result.report.fixes),
                "researchLog": tools.log if tools else [],
                "promptFingerprint": result.prompt_fingerprint,
                "llmSeconds": result.llm_seconds,
            })
            out.checks = run_checks(result.analysis, snapshot, pool, cfg, result.report, result.first_report)
            time_left = float("inf") if deadline is None else deadline - clock()
            if settings.judge_enabled and time_left > JUDGE_MIN_SECONDS:
                from .evals.judge import judge_client, judge_edition

                out.judge = judge_edition(judge_llm or judge_client(settings), result.analysis, snapshot, pool, cfg,
                                          ctx, settings.judge_price_input_mtok, settings.judge_price_output_mtok)
            out.decision = decide(out.checks, out.judge if out.judge and not out.judge.error else None,
                                  settings.quality_floor, settings.quality_target)
            qa["quality"] = quality_summary(settings, out)
            if out.decision.action == "withhold":
                qa.update({"status": "fallback", "reason": out.decision.reason})
            else:
                if out.decision.withheld:
                    qa["quality"]["withheld"] = withhold(result.analysis, out.decision.withheld)
                if out.decision.action == "degrade":
                    qa.update({"status": "degraded", "reason": out.decision.reason})
                edition = compose_edition(ctx, cfg, settings, snapshot, pool, result.analysis, qa)
        except (AgentError, LLMError) as exc:
            log.error("AI step failed: %s", exc)
            qa.update({"status": "fallback", "reason": short_reason(exc)})
        finally:
            qa["usage"] = dict(llm.usage)
            qa["estimatedCostUsd"] = llm.estimated_cost(settings.price_input_mtok, settings.price_output_mtok)
            if out.judge is not None:
                qa["judgeCostUsd"] = out.judge.cost
            if llm.downgrades:
                qa["notes"].append("Bedrock features disabled for this run: " + ", ".join(llm.downgrades))
    else:
        reason = skip_reason or ("AI step skipped" if not use_llm else "no evidence collected")
        qa.update({"status": "fallback", "reason": reason, "aiSkipped": not use_llm})

    if edition is None:
        edition = compose_fallback(ctx, cfg, settings, snapshot, pool, qa)
    qa["seconds"] = round(clock() - started, 1)
    out.edition = edition
    return out


def quality_summary(settings: Settings, out: Outcome) -> dict:
    c, j, d = out.checks, out.judge, out.decision
    summary = {
        "gate": d.action if d else None,
        "gateReason": d.reason if d else "",
        "firstPassValid": c.first_pass_valid if c else None,
        "finalValid": c.final_valid if c else None,
        "citationErrors": c.citation_errors if c else 0,
        "unverifiedNumbers": len(c.unverified) if c else 0,
        "unverified": (c.unverified if c else [])[:10],
        "adviceFlags": sum(len(v) for v in c.advice.values()) if c else 0,
        "top20Coverage": c.top20_coverage if c else None,
        "withheld": [],
        "groundedness": None,
    }
    if j is not None:
        summary.update({
            "judgeModel": j.model,
            "groundedness": j.groundedness if not j.error else None,
            "unsupported": len(j.unsupported),
            "partial": len(j.partial),
            "judgeError": j.error,
            "problems": {k: v for k, v in list(j.problems.items())[:8]},
        })
    elif settings.judge_enabled:
        summary["judgeError"] = "skipped: not enough time left in this run"
    return summary


def run_edition(settings: Settings, publisher, *, llm: BedrockClaude | None = None,
                judge_llm: BedrockClaude | None = None, http: HttpClient | None = None, fh: Finnhub | None = None,
                sec: SecEdgar | None = None, now: datetime | None = None, slot: str | None = None,
                use_llm: bool = True, skip_reason: str | None = None, deadline: float | None = None,
                cfg: dict | None = None, profile_cache: JsonDoc | None = None, recorder=None) -> dict:
    """Scheduled run: collect, produce, publish, then write the run record (best effort)."""
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
    if profile_cache is None:
        profile_cache = JsonDoc(cache_location(settings, "profiles.json"))

    snapshot, pool, stats = collect_all(ctx, cfg, settings, fh, sec, http, profile_cache=profile_cache)
    out = produce(settings, cfg, ctx, snapshot, pool, stats, llm=llm, judge_llm=judge_llm, fh=fh, use_llm=use_llm,
                  skip_reason=skip_reason, deadline=deadline)
    edition = out.edition
    publisher.publish(edition)
    qa = edition["qa"]
    quality = qa.get("quality") or {}
    log.info(json.dumps({  # one structured line per edition (CloudWatch Logs Insights)
        "event": "edition_published", "edition": edition["id"], "file": edition.get("file"),
        "status": qa["status"], "reason": qa.get("reason"), "gate": quality.get("gate"),
        "groundedness": quality.get("groundedness"), "unverifiedNumbers": quality.get("unverifiedNumbers"),
        "withheld": len(quality.get("withheld") or []), "release": settings.release_id,
        "environment": settings.environment, "model": settings.model_id, "prompt": prompts.PROMPT_VERSION,
        "promptFingerprint": qa.get("promptFingerprint"), "evidence": len(pool), "toolCalls": qa.get("toolCalls"),
        "repairs": qa.get("repairs"), "costUsd": qa.get("estimatedCostUsd"), "judgeCostUsd": qa.get("judgeCostUsd"),
        "seconds": qa.get("seconds"),
    }, ensure_ascii=False))
    try:
        from .runrecord import save_run_record

        location = (recorder or save_run_record)(settings, cfg, out)
        if location:
            log.info("run record: %s", location)
    except Exception as exc:  # never fail a published edition because of the record
        log.warning("run record not saved: %s", exc)
    return edition
