"""AWS Lambda entry point (invoked by EventBridge Scheduler).

Event (all optional): {"slot": "premarket|midday|close|weekend", "noLlm": true}
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


def emit_metrics(qa: dict) -> None:
    """CloudWatch Embedded Metric Format: one log line becomes custom metrics (no API calls)."""
    usage = qa.get("usage") or {}
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "StockNewsDashboard",
                "Dimensions": [["Service"]],
                "Metrics": [
                    {"Name": "FallbackEdition", "Unit": "Count"},
                    {"Name": "DegradedEdition", "Unit": "Count"},
                    {"Name": "EvidenceItems", "Unit": "Count"},
                    {"Name": "InputTokens", "Unit": "Count"},
                    {"Name": "OutputTokens", "Unit": "Count"},
                    {"Name": "EstimatedCostUsd", "Unit": "None"},
                    {"Name": "RunSeconds", "Unit": "Seconds"},
                ],
            }],
        },
        "Service": "news-agent",
        "FallbackEdition": 1 if qa.get("status") == "fallback" else 0,
        "DegradedEdition": 1 if qa.get("status") == "degraded" else 0,
        "EvidenceItems": qa.get("evidenceCount") or 0,
        "InputTokens": usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0),
        "OutputTokens": usage.get("output", 0),
        "EstimatedCostUsd": qa.get("estimatedCostUsd") or 0,
        "RunSeconds": qa.get("seconds") or 0,
    }
    print(json.dumps(record))


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    settings = Settings.from_env()
    settings.finnhub_api_key = _finnhub_key(settings)
    cfg = load_config(settings)
    deadline = None
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000.0
    publisher = S3Publisher(settings.site_bucket, max_editions=limits(cfg)["manifestEditions"])
    edition = run_edition(settings, publisher, slot=event.get("slot"), use_llm=not event.get("noLlm"),
                          deadline=deadline, cfg=cfg)
    qa = edition.get("qa") or {}
    emit_metrics(qa)
    return {
        "edition": edition["id"],
        "file": edition.get("file"),
        "status": qa.get("status"),
        "evidence": qa.get("evidenceCount"),
        "toolCalls": qa.get("toolCalls"),
        "repairs": qa.get("repairs"),
        "estimatedCostUsd": qa.get("estimatedCostUsd"),
    }
