"""Per-edition metrics, suite aggregates and threshold checks (shared by CI evals and the smoke test)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..validate import cited_ids


def case_metrics(out) -> dict:
    """Flat metrics for one produced edition (pipeline.Outcome)."""
    qa = out.qa
    q = qa.get("quality") or {}
    usage = qa.get("usage") or {}
    return {
        "status": qa.get("status"),
        "gate": q.get("gate"),
        "schemaValid": out.result is not None,
        "firstPassValid": bool(q.get("firstPassValid")),
        "finalValid": bool(q.get("finalValid")) and qa.get("status") != "fallback",
        "repairs": qa.get("repairs", 0),
        "toolCalls": qa.get("toolCalls", 0),
        "citationErrors": q.get("citationErrors", 0),
        "citedIds": len(cited_ids(out.analysis)) if out.analysis else 0,
        "unverifiedNumbers": q.get("unverifiedNumbers", 0),
        "adviceFlags": q.get("adviceFlags", 0),
        "top20Coverage": q.get("top20Coverage"),
        "groundedness": q.get("groundedness"),
        "unsupported": q.get("unsupported", 0),
        "partial": q.get("partial", 0),
        "withheld": len(q.get("withheld") or []),
        "judgeError": q.get("judgeError"),
        "costUsd": round((qa.get("estimatedCostUsd") or 0) + (qa.get("judgeCostUsd") or 0), 4),
        "seconds": qa.get("seconds") or 0,
        "llmSeconds": qa.get("llmSeconds") or 0,
        "inputTokens": usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0),
        "outputTokens": usage.get("output", 0),
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def aggregate(metrics: list[dict]) -> dict:
    n = len(metrics)
    grounded = [m["groundedness"] for m in metrics if m.get("groundedness") is not None]
    coverage = [m["top20Coverage"] for m in metrics if m.get("top20Coverage") is not None]
    cited = sum(m.get("citedIds", 0) for m in metrics)
    errors = sum(m.get("citationErrors", 0) for m in metrics)
    return {
        "cases": n,
        "schema_valid_rate": _mean([1.0 if m["schemaValid"] else 0.0 for m in metrics]),
        "first_pass_valid_rate": _mean([1.0 if m["firstPassValid"] else 0.0 for m in metrics]),
        "final_valid_rate": _mean([1.0 if m["finalValid"] else 0.0 for m in metrics]),
        "judged_rate": round(len(grounded) / n, 4) if n else None,
        "groundedness_mean": _mean(grounded),
        "groundedness_min": min(grounded) if grounded else None,
        "top20_coverage_mean": _mean(coverage),
        "unverified_numbers_mean": _mean([float(m["unverifiedNumbers"]) for m in metrics]),
        "unverified_numbers_total": sum(m["unverifiedNumbers"] for m in metrics),
        "advice_flags_total": sum(m["adviceFlags"] for m in metrics),
        "citation_error_rate": round(errors / (cited + errors), 4) if (cited + errors) else 0.0,
        "withhold_rate": _mean([1.0 if m.get("gate") == "withhold" else 0.0 for m in metrics]),
        "degrade_rate": _mean([1.0 if m.get("status") == "degraded" else 0.0 for m in metrics]),
        "fallback_rate": _mean([1.0 if m.get("status") == "fallback" else 0.0 for m in metrics]),
        "repairs_mean": _mean([float(m["repairs"]) for m in metrics]),
        "cost_usd_mean": _mean([m["costUsd"] for m in metrics]),
        "cost_usd_total": round(sum(m["costUsd"] for m in metrics), 4),
        "seconds_mean": _mean([float(m["seconds"]) for m in metrics]),
        "seconds_p95": _p95([float(m["seconds"]) for m in metrics]),
    }


@dataclass
class Verdict:
    ok: bool
    rows: list[dict] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        return [r["text"] for r in self.rows if not r["ok"]]


def check(agg: dict, spec: dict, baseline: dict | None = None) -> Verdict:
    """Compare aggregates with {"checks": {metric: {min|max: x}}, "regression": {metric: max_drop}}."""
    rows = []
    for metric, rule in (spec.get("checks") or {}).items():
        value = agg.get(metric)
        for op, threshold in rule.items():
            if op not in ("min", "max"):
                continue
            ok = value is not None and (value >= threshold if op == "min" else value <= threshold)
            sign = "≥" if op == "min" else "≤"
            rows.append({"metric": metric, "op": op, "threshold": threshold, "value": value, "ok": ok,
                         "text": f"{metric} = {value} (needs {sign} {threshold})"})
    if baseline:
        for metric, drop in (spec.get("regression") or {}).items():
            if metric == "description" or not isinstance(drop, (int, float)):
                continue
            base, value = baseline.get(metric), agg.get(metric)
            if base is None:
                continue
            ok = value is not None and value >= base - drop
            rows.append({"metric": f"{metric} vs baseline", "op": "regression", "threshold": round(base - drop, 4),
                         "value": value, "ok": ok,
                         "text": f"{metric} = {value} vs baseline {base} (max drop {drop})"})
    return Verdict(ok=all(r["ok"] for r in rows), rows=rows)
