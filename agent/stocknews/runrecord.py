"""Run records: everything needed to audit, debug or replay one edition.

One gzipped JSON per run, written to s3://<ops bucket>/runs/<environment>/<date>/ (or a
local folder in local runs). It holds:

* case     - the frozen inputs (market snapshot, evidence pool, timing): a ready-made eval case
* release  - release id (git SHA), model ids, prompt version and fingerprint, schema version
* outputs  - the validated analysis, the first-draft validation report, the research log
* quality  - deterministic checks, judge verdicts and the quality-gate decision
* qa       - status, tokens, cost and timings (also published with the edition)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import prompts
from .settings import Settings
from .storage import write_json

log = logging.getLogger(__name__)
RECORD_VERSION = 1


def record_location(settings: Settings, edition: dict, kind: str = "runs") -> str | None:
    stamp = edition["generatedAt"].replace("-", "").replace(":", "").replace("Z", "")[-6:]
    name = f"{edition['date']}/{edition['id']}-{stamp}.json.gz"
    if settings.ops_bucket:
        return f"s3://{settings.ops_bucket}/{kind}/{settings.environment}/{name}"
    if settings.records_dir:
        return f"{settings.records_dir.rstrip('/')}/{kind}/{name}"
    return None


def build_run_record(settings: Settings, cfg: dict, out) -> dict:
    from .evals.dataset import case_from_run

    result = out.result
    first = getattr(result, "first_report", None)
    final = getattr(result, "report", None)
    return {
        "recordVersion": RECORD_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "edition": {"id": out.edition["id"], "file": out.edition.get("file"), "status": out.qa.get("status")},
        "release": {
            "releaseId": settings.release_id,
            "environment": settings.environment,
            "model": settings.model_id,
            "effort": settings.effort,
            "judgeModel": settings.judge_model_id if settings.judge_enabled else None,
            "promptVersion": prompts.PROMPT_VERSION,
            "promptFingerprint": getattr(result, "prompt_fingerprint", None),
            "qualityFloor": settings.quality_floor,
            "qualityTarget": settings.quality_target,
        },
        "case": case_from_run(out.ctx, cfg, out.snapshot, out.pool,
                              {"kind": "captured", "environment": settings.environment,
                               "release": settings.release_id, "status": out.qa.get("status")}),
        "outputs": {
            "analysis": out.analysis,
            "researchNote": getattr(result, "research_note", ""),
            "researchLog": out.qa.get("researchLog", []),
            "firstDraft": {"hard": [str(i) for i in first.hard], "fixes": first.fixes} if first else None,
            "final": {"hard": [str(i) for i in final.hard], "fixes": final.fixes} if final else None,
        },
        "quality": {
            "checks": out.checks.__dict__ if out.checks else None,
            "judge": out.judge.to_dict() if out.judge is not None else None,
            "decision": out.decision.__dict__ if out.decision else None,
        },
        "qa": out.qa,
    }


def save_run_record(settings: Settings, cfg: dict, out) -> str | None:
    location = record_location(settings, out.edition)
    if not location:
        return None
    write_json(location, build_run_record(settings, cfg, out))
    return location
