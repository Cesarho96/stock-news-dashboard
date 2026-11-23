"""Run the eval suite: replay dataset cases through the real agent + judge and gate on thresholds.

    python -m stocknews.evals --dataset seed: --dataset s3://OPS/evals/datasets/golden/ \\
        --gate gate --out .evals/report.json --summary .evals/summary.md [--baseline report.json]

Exit code: 0 = gate passed, 1 = gate failed, 2 = the suite could not run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import prompts
from ..compose import model_label
from ..llm import BedrockClaude
from ..pipeline import produce
from ..settings import Settings, load_config
from ..stackparams import load_stack_params
from ..storage import read_json, write_json
from .dataset import ReplayFinnhub, load_cases, materialize
from .judge import judge_client
from .metrics import Verdict, aggregate, case_metrics, check

log = logging.getLogger(__name__)
THRESHOLDS = Path(__file__).with_name("thresholds.json")


def load_thresholds(path: str | None = None) -> dict:
    return json.loads(Path(path or THRESHOLDS).read_text(encoding="utf-8"))


def evaluate_case(case: dict, settings: Settings, cfg: dict, llm: BedrockClaude | None = None,
                  judge_llm: BedrockClaude | None = None, language: str | None = None) -> dict:
    cfg_case, ctx, snapshot, pool, tool_items = materialize(case, cfg, language=language)
    out = produce(settings, cfg_case, ctx, snapshot, pool, {"errors": [], "sources": {}}, llm=llm,
                  judge_llm=judge_llm, fh=ReplayFinnhub(snapshot, tool_items))
    q = out.qa.get("quality") or {}
    return {
        "caseId": case.get("caseId"),
        "kind": (case.get("meta") or {}).get("kind"),
        "edition": ctx.edition_id,
        "status": out.qa.get("status"),
        "reason": out.qa.get("reason"),
        "metrics": case_metrics(out),
        "issues": (out.qa.get("notes") or [])[:10],
        "problems": q.get("problems") or {},
        "unverified": q.get("unverified") or [],
        "withheld": q.get("withheld") or [],
        "promptFingerprint": out.qa.get("promptFingerprint"),
    }


def run_suite(specs: list[str], settings: Settings, cfg: dict | None = None, gate: str = "gate",
              thresholds: dict | None = None, baseline: dict | None = None, llm_factory=None, judge_factory=None,
              max_cases: int | None = None, language: str | None = None) -> dict:
    cfg = cfg or load_config(settings)
    thresholds = thresholds or load_thresholds()
    cases = load_cases(specs, cfg, max_cases=max_cases)
    if not cases:
        raise RuntimeError(f"no eval cases found in {specs}")
    started = time.monotonic()
    llm_factory = llm_factory or (lambda: BedrockClaude(settings.model_id, settings.bedrock_region, settings.effort,
                                                        settings.thinking, settings.max_tokens))
    judge_factory = judge_factory or (lambda: judge_client(settings))
    reports = []
    for case in cases:
        log.info("eval case %s", case.get("caseId"))
        reports.append(evaluate_case(case, settings, cfg, llm=llm_factory(),
                                     judge_llm=judge_factory() if settings.judge_enabled else None,
                                     language=language))
    agg = aggregate([r["metrics"] for r in reports])
    spec = thresholds.get(gate) if gate and gate != "none" else None
    if spec and not settings.judge_enabled:  # judge switched off: its checks cannot apply
        spec = dict(spec, checks={k: v for k, v in (spec.get("checks") or {}).items()
                                  if not k.startswith(("groundedness", "judged"))})
    verdict = check(agg, spec, (baseline or {}).get("aggregate")) if spec else Verdict(ok=True)
    return {
        "reportVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "release": settings.release_id,
        "environment": settings.environment,
        "model": settings.model_id,
        "judgeModel": settings.judge_model_id if settings.judge_enabled else None,
        "promptVersion": prompts.PROMPT_VERSION,
        "promptFingerprint": next((r["promptFingerprint"] for r in reports if r.get("promptFingerprint")), None),
        "datasets": specs,
        "seconds": round(time.monotonic() - started, 1),
        "aggregate": agg,
        "gate": {"name": gate, "ok": verdict.ok, "checks": verdict.rows, "failures": verdict.failures,
                 "baselineRelease": (baseline or {}).get("release")},
        "cases": reports,
    }


def apply_params(settings: Settings, params: dict) -> str | None:
    """Use a stack's parameters (infra/params/<env>.json, model profile expanded) so CI evaluates
    exactly what will be deployed. Returns the stack language."""
    def effort(value, current):
        v = str(value if value is not None else current or "").strip().lower()
        return "" if v in ("", "none", "off") else v

    settings.model_id = params.get("ModelId") or settings.model_id
    settings.effort = effort(params.get("Effort"), settings.effort)
    settings.thinking = str(params.get("Thinking") or settings.thinking).strip().lower()
    settings.judge_model_id = params.get("JudgeModelId") or settings.judge_model_id
    settings.judge_effort = effort(params.get("JudgeEffort"), settings.judge_effort)
    settings.judge_thinking = str(params.get("JudgeThinking") or settings.judge_thinking).strip().lower()
    if "JudgeEnabled" in params:
        settings.judge_enabled = str(params["JudgeEnabled"]).lower() == "true"
    for key, attr in (("QualityFloor", "quality_floor"), ("QualityTarget", "quality_target"),
                      ("PriceInputMtok", "price_input_mtok"), ("PriceOutputMtok", "price_output_mtok"),
                      ("JudgePriceInputMtok", "judge_price_input_mtok"),
                      ("JudgePriceOutputMtok", "judge_price_output_mtok")):
        if params.get(key):
            setattr(settings, attr, float(params[key]))
    return params.get("Language")


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if value else "0"
    return str(value)


def summary_markdown(report: dict) -> str:
    gate = report["gate"]
    head = "✅ passed" if gate["ok"] else "❌ FAILED"
    lines = [
        f"## LLM evals: {head} (gate `{gate['name']}`)",
        "",
        f"Release `{report['release']}` · prompt `{report['promptVersion']}` (fingerprint "
        f"`{report.get('promptFingerprint') or '—'}`) · model {model_label(report['model'])} · judge "
        f"{model_label(report['judgeModel']) if report.get('judgeModel') else 'off'} · {report['aggregate']['cases']} "
        f"case(s) · {report['seconds']}s · ${report['aggregate']['cost_usd_total']}",
        "",
    ]
    if gate["checks"]:
        lines += ["| Check | Needs | Value | |", "|---|---|---|---|"]
        for row in gate["checks"]:
            needs = {"min": "≥ ", "max": "≤ ", "regression": "≥ "}[row["op"]] + _fmt(row["threshold"])
            lines.append(f"| {row['metric']} | {needs} | {_fmt(row['value'])} | {'✅' if row['ok'] else '❌'} |")
        lines.append("")
    lines += ["| Case | Status | Groundedness | Unverified | Withheld | Repairs | Cost $ | Seconds |",
              "|---|---|---|---|---|---|---|---|"]
    for r in report["cases"]:
        m = r["metrics"]
        lines.append(f"| {r['caseId']} | {r['status']} | {_fmt(m['groundedness'])} | {m['unverifiedNumbers']} | "
                     f"{m['withheld']} | {m['repairs']} | {_fmt(m['costUsd'])} | {_fmt(m['seconds'])} |")
    problems = [(r["caseId"], uid, p) for r in report["cases"] for uid, ps in r["problems"].items() for p in ps]
    unverified = [(r["caseId"], u["unit"], u["value"]) for r in report["cases"] for u in r["unverified"]]
    if problems or unverified:
        lines += ["", "<details><summary>Judge problems and unverified figures</summary>", ""]
        lines += [f"- `{c}` {uid}: {p}" for c, uid, p in problems[:40]]
        lines += [f"- `{c}` {uid}: unverified figure {v}" for c, uid, v in unverified[:40]]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocknews.evals", description=__doc__.split("\n")[0])
    parser.add_argument("--dataset", action="append", help="seed:, a file, a folder or an s3:// prefix (repeatable)")
    parser.add_argument("--gate", default="gate", help="threshold set in thresholds.json (gate | smoke | none)")
    parser.add_argument("--thresholds", help="thresholds file (default: the packaged thresholds.json)")
    parser.add_argument("--baseline", help="previous report (file or s3://) for regression checks")
    parser.add_argument("--out", help="write the JSON report here (file or s3://)")
    parser.add_argument("--summary", help="append a Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--language", choices=["en", "es"], help="override the language of every case")
    parser.add_argument("--params", help="evaluate the configuration of a stack: infra/params/<env>.json "
                                         "(ModelId, Effort, JudgeModelId, JudgeEnabled, Language, quality thresholds)")
    parser.add_argument("--model-profile", help="with --params: evaluate another profile of infra/models.json "
                                                "(e.g. target) instead of the one the params file selects")
    parser.add_argument("--model", help="override MODEL_ID")
    parser.add_argument("--judge-model", help="override JUDGE_MODEL_ID")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    settings = Settings.from_env()
    settings.environment = os.environ.get("ENVIRONMENT", "ci")
    language = args.language
    if args.params:
        language = apply_params(settings, load_stack_params(args.params, model_profile=args.model_profile)) or language
    if args.model:
        settings.model_id = args.model
    if args.judge_model:
        settings.judge_model_id = args.judge_model
    baseline = None
    if args.baseline:
        try:
            baseline = read_json(args.baseline)
        except Exception as exc:  # first release: no baseline yet
            log.warning("baseline %s not loaded (%s); skipping regression checks", args.baseline, exc)
    try:
        report = run_suite(args.dataset or ["seed:"], settings, gate=args.gate,
                           thresholds=load_thresholds(args.thresholds), baseline=baseline,
                           max_cases=args.max_cases, language=args.language or language)
    except Exception as exc:
        log.exception("eval suite failed to run: %s", exc)
        return 2
    if args.out:
        write_json(args.out, report)
    text = summary_markdown(report)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text, file=sys.stderr)
    return 0 if report["gate"]["ok"] else 1
