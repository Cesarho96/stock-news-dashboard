"""AWS Lambda entry point (invoked through the `live` alias by EventBridge Scheduler).

Events (all fields optional):
  {"slot": "premarket|midday|close|weekend", "noLlm": true}   publish an edition
  {"mode": "smoke"}                                            deployment smoke test: write the
                                                               seed edition with the real models,
                                                               check the smoke thresholds, publish
                                                               nothing (CodeDeploy PreTraffic hook)
"""

from __future__ import annotations

import json
import logging
import os
import time

from .pipeline import run_edition
from .publish import S3Publisher
from .settings import Settings, limits, load_config

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("botocore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

_FINNHUB_KEY_CACHE: dict[str, str] = {}
NAMESPACE = "StockNewsDashboard"


def _finnhub_key(settings: Settings) -> str:
    if settings.finnhub_api_key:
        return settings.finnhub_api_key
    name = settings.finnhub_key_param
    if not name:
        return ""
    if name not in _FINNHUB_KEY_CACHE:
        import boto3

        try:
            resp = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
            _FINNHUB_KEY_CACHE[name] = resp["Parameter"]["Value"].strip()
        except Exception as exc:  # missing parameter or permissions: run without Finnhub
            log.error("could not read SSM parameter %s: %s", name, exc)
            _FINNHUB_KEY_CACHE[name] = ""
    return _FINNHUB_KEY_CACHE[name]


def metric_values(qa: dict) -> dict[str, tuple[float, str]]:
    """Operational and quality metrics for one edition: {name: (value, unit)}."""
    usage = qa.get("usage") or {}
    q = qa.get("quality") or {}
    status = qa.get("status")
    attempted = bool(usage.get("calls"))  # the AI step ran (not skipped by the kill switch)
    skipped = bool(qa.get("aiSkipped"))  # kill switch or noLlm: an operator decision, not a failure
    values = {
        "EditionPublished": (1, "Count"),
        "FallbackEdition": (1 if status == "fallback" and not skipped else 0, "Count"),
        "AiSkipped": (1 if skipped else 0, "Count"),
        "DegradedEdition": (1 if status == "degraded" else 0, "Count"),
        "WithheldByGate": (1 if q.get("gate") == "withhold" else 0, "Count"),
        "SectionsWithheld": (len(q.get("withheld") or []), "Count"),
        "EvidenceItems": (qa.get("evidenceCount") or 0, "Count"),
        "SourceErrors": (len(qa.get("sourceErrors") or []), "Count"),
        "ToolCalls": (qa.get("toolCalls") or 0, "Count"),
        "RepairRounds": (qa.get("repairs") or 0, "Count"),
        "InputTokens": (usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0), "Count"),
        "OutputTokens": (usage.get("output", 0), "Count"),
        "EstimatedCostUsd": (round((qa.get("estimatedCostUsd") or 0) + (qa.get("judgeCostUsd") or 0), 4), "None"),
        "RunSeconds": (qa.get("seconds") or 0, "Seconds"),
    }
    if attempted and q:
        values.update({
            "FirstPassValid": (1 if q.get("firstPassValid") else 0, "Count"),
            "CitationErrors": (q.get("citationErrors") or 0, "Count"),
            "UnverifiedNumbers": (q.get("unverifiedNumbers") or 0, "Count"),
            "AdviceFlags": (q.get("adviceFlags") or 0, "Count"),
            "JudgeError": (1 if q.get("judgeError") else 0, "Count"),
        })
        if q.get("top20Coverage") is not None:
            values["Top20Coverage"] = (q["top20Coverage"], "None")
        if q.get("groundedness") is not None:
            values["Groundedness"] = (q["groundedness"], "None")
            values["UnsupportedSections"] = (q.get("unsupported") or 0, "Count")
    if qa.get("llmSeconds"):
        values["LlmSeconds"] = (qa["llmSeconds"], "Seconds")
    return values


def emit_metrics(qa: dict, environment: str = "local", edition_id: str | None = None) -> None:
    """CloudWatch Embedded Metric Format: one log line becomes custom metrics (no API calls).

    The same JSON line carries searchable properties (edition, release, gate, prompt version)
    for the Logs Insights queries and the dashboard's "Latest editions" table.
    """
    values = metric_values(qa)
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["Service", "Environment"]],
                "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in values.items()],
            }],
        },
        "Service": "news-agent",
        "Environment": environment,
        "Edition": edition_id,
        "Release": qa.get("release"),
        "Status": qa.get("status"),
        "Gate": (qa.get("quality") or {}).get("gate"),
        "Reason": qa.get("reason"),
        "Model": qa.get("model"),
        "PromptVersion": qa.get("promptVersion"),
        **{name: value for name, (value, _) in values.items()},
    }
    print(json.dumps(record))


def smoke_test(settings: Settings, event: dict) -> dict:
    from .evals.runner import load_thresholds, run_suite

    report = run_suite([event.get("dataset") or "seed:"], settings, gate="smoke", thresholds=load_thresholds(),
                       language=settings.language)
    case = report["cases"][0]
    result = {"ok": report["gate"]["ok"], "release": settings.release_id, "failures": report["gate"]["failures"],
              "metrics": case["metrics"], "problems": case["problems"], "unverified": case["unverified"]}
    log.info("smoke test: %s", json.dumps(result)[:4000])
    return result


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    settings = Settings.from_env()
    if event.get("mode") == "smoke":
        return smoke_test(settings, event)

    settings.finnhub_api_key = _finnhub_key(settings)
    cfg = load_config(settings)
    deadline = None
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000.0
    registry = None
    if settings.registry_prefix:
        from .registry import Registry

        registry = Registry(settings.registry_prefix)
    skip = None
    if event.get("noLlm"):
        skip = "AI step skipped (noLlm)"
    elif registry is not None and not registry.ai_enabled():
        skip = "AI disabled by the operator (kill switch)"
    publisher = S3Publisher(settings.site_bucket, max_editions=limits(cfg)["manifestEditions"])
    edition = run_edition(settings, publisher, slot=event.get("slot"), use_llm=skip is None, skip_reason=skip,
                          deadline=deadline, cfg=cfg)
    qa = edition.get("qa") or {}
    emit_metrics(qa, settings.environment, edition["id"])
    promoted = None
    if registry is not None:
        try:  # bake accounting: N good editions make this release the rollback target
            promoted = registry.record_edition(settings.release_id, getattr(context, "function_version", None),
                                               qa.get("status"), settings.bake_editions, counted=skip is None)
        except Exception as exc:
            log.warning("release registry not updated: %s", exc)
        if promoted:
            log.info("release %s validated: it is now the last good release", settings.release_id)
    return {
        "edition": edition["id"],
        "file": edition.get("file"),
        "status": qa.get("status"),
        "gate": (qa.get("quality") or {}).get("gate"),
        "groundedness": (qa.get("quality") or {}).get("groundedness"),
        "evidence": qa.get("evidenceCount"),
        "toolCalls": qa.get("toolCalls"),
        "repairs": qa.get("repairs"),
        "estimatedCostUsd": qa.get("estimatedCostUsd"),
        "release": settings.release_id,
        "validated": bool(promoted),
    }
