"""Operations: the deployment smoke gate, automatic and manual rollback, and edition withdrawal.

* pre_traffic_handler   - CodeDeploy PreTraffic hook. Invokes the NEW Lambda version with
                          {"mode": "smoke"} (seed edition, real models, nothing published) and
                          only lets the `live` alias move to it when the smoke test passes.
* auto_rollback_handler - EventBridge rule on the rollback alarms. While a release is still
                          baking, moves `live` back to the last validated version, restores the
                          matching site release, blocks the bad release and emails the operator.
* rollback()            - the same rollback, used by scripts/release.py and the rollback workflow.
* withdraw_edition()    - removes a published edition from the manifest (the file is kept).
"""

from __future__ import annotations

import json
import logging
import os
import time

from .publish import MANIFEST_CACHE
from .registry import Registry

log = logging.getLogger(__name__)
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class RollbackError(Exception):
    pass


def _client(name: str, **config):
    import boto3

    if config:
        from botocore.config import Config

        return boto3.client(name, config=Config(**config))
    return boto3.client(name)


def notify(topic_arn: str | None, subject: str, message: str, sns=None) -> None:
    log.info("%s\n%s", subject, message)
    if not topic_arn:
        return
    try:
        (sns or _client("sns")).publish(TopicArn=topic_arn, Subject=subject[:100], Message=message[:60000])
    except Exception as exc:
        log.error("notification failed: %s", exc)


def version_of(arn_or_version: str) -> str:
    """'arn:aws:lambda:us-east-1:123:function:name:12' -> '12'."""
    return str(arn_or_version).rsplit(":", 1)[-1]


# ---- site releases ---------------------------------------------------------------------------
def restore_site(bucket: str, site_release: str, distribution_id: str | None = None, s3=None, cf=None) -> str:
    """Point the site's index.html back to releases/<site_release>/ (assets are immutable)."""
    s3 = s3 or _client("s3")
    source = f"releases/{site_release}/index.html"
    s3.copy_object(Bucket=bucket, Key="index.html", CopySource={"Bucket": bucket, "Key": source},
                   MetadataDirective="REPLACE", ContentType="text/html; charset=utf-8", CacheControl="no-cache")
    if distribution_id:
        (cf or _client("cloudfront")).create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={"Paths": {"Quantity": 2, "Items": ["/", "/index.html"]},
                               "CallerReference": f"restore-{site_release}-{int(time.time())}"})
    return source


# ---- rollback ----------------------------------------------------------------------------------
def rollback(*, function_name: str, alias: str, registry: Registry, reason: str, target: dict | None = None,
             site_bucket: str | None = None, distribution_id: str | None = None, lam=None, s3=None, cf=None) -> dict:
    lam = lam or _client("lambda")
    target = target or registry.last_good()
    if not target or not target.get("functionVersion"):
        raise RollbackError("there is no validated release to roll back to")
    live = str(lam.get_alias(FunctionName=function_name, Name=alias)["FunctionVersion"])
    current = registry.current() or {}
    bad = current if str(current.get("functionVersion")) == live else {"releaseId": None, "functionVersion": live}
    result = {"changed": False, "from": {"releaseId": bad.get("releaseId"), "functionVersion": live},
              "to": {"releaseId": target.get("releaseId"), "functionVersion": str(target["functionVersion"])},
              "site": None, "reason": reason}
    if live != str(target["functionVersion"]):
        lam.update_alias(FunctionName=function_name, Name=alias, FunctionVersion=str(target["functionVersion"]),
                         RoutingConfig={"AdditionalVersionWeights": {}},
                         Description=f"rollback: {reason}"[:256])
        result["changed"] = True
    site = target.get("siteRelease")
    if site_bucket and site and site != (registry.site_current() or {}).get("siteRelease"):
        result["site"] = restore_site(site_bucket, site, distribution_id, s3, cf)
        registry.set_site(site)
        result["changed"] = True
    if result["changed"]:
        registry.mark_rolled_back(bad, target, reason)
    return result


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _rollback_message(result: dict, alarm: str, why: str) -> str:
    return "\n".join([
        f"Alarm: {alarm}",
        f"Why: {why}",
        "",
        f"Rolled back from release {result['from']['releaseId']} (Lambda version {result['from']['functionVersion']})",
        f"to the last validated release {result['to']['releaseId']} (Lambda version {result['to']['functionVersion']}).",
        f"Site restored: {result['site'] or 'not needed'}",
        "The bad release was added to the blocked list; CI will refuse to deploy it again.",
        "",
        "Next steps (docs/llmops.md, 'Runbook'):",
        "1. Check the CloudWatch dashboard and the run records of the failing editions.",
        "2. Fix forward with a revert or a fix PR; the pipeline re-runs the eval gate.",
        "3. If a bad edition is still visible: python scripts/release.py withdraw --env <env> --edition <id>.",
    ])


def auto_rollback_handler(event, context):
    """EventBridge: CloudWatch Alarm State Change -> ALARM, for the stack's rollback alarms."""
    detail = (event or {}).get("detail") or {}
    alarm = detail.get("alarmName", "unknown alarm")
    state = (detail.get("state") or {}).get("value")
    why = (detail.get("state") or {}).get("reason", "")[:500]
    if state != "ALARM":
        return {"action": "ignored", "state": state}
    topic, stack = _env("TOPIC_ARN"), _env("STACK_NAME", "stock-news-dashboard")
    registry = Registry(_env("REGISTRY_PREFIX"))
    current, good = registry.current(), registry.last_good()

    def no_action(text: str) -> dict:
        notify(topic, f"[{stack}] {alarm}: no automatic rollback",
               f"Alarm: {alarm}\nWhy: {why}\n\n{text}\n\nRegistry: {json.dumps(registry.status(), indent=1)}")
        return {"action": "notified", "detail": text}

    if _env("AUTO_ROLLBACK", "ENABLED").upper() != "ENABLED":
        return no_action("Automatic rollback is disabled (AutoRollback parameter).")
    if not current or current.get("state") != "baking":
        return no_action("The live release is already validated (outside its bake window), so the cause is "
                         "probably data, a provider or the model, not the release. Follow the runbook; use "
                         "scripts/release.py rollback if you still want to go back.")
    if not good or str(good.get("functionVersion")) == str(current.get("functionVersion")):
        return no_action("There is no earlier validated release to roll back to.")
    try:
        result = rollback(function_name=_env("FUNCTION_NAME"), alias=_env("ALIAS", "live"), registry=registry,
                          reason=f"alarm {alarm}", target=good, site_bucket=_env("SITE_BUCKET") or None,
                          distribution_id=_env("DISTRIBUTION_ID") or None)
    except Exception as exc:
        notify(topic, f"[{stack}] {alarm}: AUTOMATIC ROLLBACK FAILED",
               f"Alarm: {alarm}\nWhy: {why}\nError: {exc}\n\nRoll back by hand: python scripts/release.py "
               f"rollback --env <env> (or the 'rollback' GitHub workflow).")
        raise
    notify(topic, f"[{stack}] rolled back to {result['to']['releaseId']} after {alarm}",
           _rollback_message(result, alarm, why))
    if result["changed"] and _env("REGENERATE_AFTER_ROLLBACK", "true").lower() == "true":
        _client("lambda").invoke(FunctionName=f"{_env('FUNCTION_NAME')}:{_env('ALIAS', 'live')}",
                                 InvocationType="Event", Payload=json.dumps({"source": "rollback"}).encode())
    return {"action": "rolled-back", **result}


# ---- deployment smoke gate -----------------------------------------------------------------------
def pre_traffic_handler(event, context):
    """CodeDeploy PreTraffic hook: smoke-test the new version, then report Succeeded/Failed."""
    deployment_id = event.get("DeploymentId")
    hook_id = event.get("LifecycleEventHookExecutionId")
    new_version = _env("NEW_VERSION")
    topic, stack = _env("TOPIC_ARN"), _env("STACK_NAME", "stock-news-dashboard")
    status, detail = "Failed", {}
    try:
        if _env("SMOKE_ENABLED", "true").lower() == "false":
            status, detail = "Succeeded", {"skipped": "SMOKE_ENABLED=false"}
        else:
            lam = _client("lambda", read_timeout=900, connect_timeout=10, retries={"total_max_attempts": 1})
            resp = lam.invoke(FunctionName=new_version, InvocationType="RequestResponse",
                              Payload=json.dumps({"mode": "smoke"}).encode())
            payload = json.loads(resp["Payload"].read() or b"{}")
            detail = payload if isinstance(payload, dict) else {"payload": payload}
            if not resp.get("FunctionError") and detail.get("ok"):
                status = "Succeeded"
        if status == "Succeeded" and _env("REGISTRY_PREFIX"):
            Registry(_env("REGISTRY_PREFIX")).register(detail.get("release") or _env("RELEASE_ID"),
                                                       version_of(new_version), "codedeploy")
    except Exception as exc:
        log.exception("smoke test errored")
        detail = {"error": str(exc)[:1000]}
        status = "Failed"
    finally:
        log.info("pre-traffic smoke for %s: %s %s", new_version, status, json.dumps(detail)[:3000])
        if deployment_id and hook_id:
            _client("codedeploy").put_lifecycle_event_hook_execution_status(
                deploymentId=deployment_id, lifecycleEventHookExecutionId=hook_id, status=status)
    if status != "Succeeded":
        notify(topic, f"[{stack}] deployment blocked: smoke test failed",
               "The new Lambda version did not pass the smoke test, so CodeDeploy kept the current version "
               f"live and CloudFormation will roll the stack back.\n\nVersion: {new_version}\n"
               f"Details: {json.dumps(detail, indent=1)[:5000]}")
    return {"status": status, "detail": detail}


# ---- editions ---------------------------------------------------------------------------------
def withdraw_edition(bucket: str, edition_id: str, s3=None, distribution_id: str | None = None, cf=None,
                     prefix: str = "data/") -> dict:
    """Remove an edition from the manifest so readers fall back to the previous one."""
    s3 = s3 or _client("s3")
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=prefix + "manifest.json")["Body"].read())
    before = len(manifest.get("editions") or [])
    entries = [e for e in manifest.get("editions") or [] if e.get("id") != edition_id]
    if len(entries) == before:
        raise ValueError(f"edition {edition_id} is not in the manifest")
    manifest["editions"] = entries
    manifest["latest"] = entries[0]["id"] if entries else None
    manifest["withdrawn"] = list(dict.fromkeys((manifest.get("withdrawn") or []) + [edition_id]))[-50:]
    s3.put_object(Bucket=bucket, Key=prefix + "manifest.json",
                  Body=json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                  ContentType="application/json; charset=utf-8", CacheControl=MANIFEST_CACHE)
    if distribution_id:
        (cf or _client("cloudfront")).create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={"Paths": {"Quantity": 1, "Items": [f"/{prefix}manifest.json"]},
                               "CallerReference": f"withdraw-{edition_id}-{int(time.time())}"})
    return {"withdrawn": edition_id, "latest": manifest["latest"]}
