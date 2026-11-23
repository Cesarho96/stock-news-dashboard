"""Shared helpers for scripts/deploy.py and scripts/release.py (stack names, outputs, sessions)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

APP = os.environ.get("STACK_PREFIX", "stock-news-dashboard")
ENVIRONMENTS = ("dev", "prod")


def app_stack(env: str) -> str:
    return f"{APP}-{env}"


def bootstrap_stack() -> str:
    return f"{APP}-bootstrap"


def registry_prefix(env: str) -> str:
    return f"/{APP}/{env}"


def session(region: str | None = None, profile: str | None = None):
    import boto3

    return boto3.Session(region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                         or "us-east-1", profile_name=profile or os.environ.get("AWS_PROFILE") or None)


READY = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE",
         "IMPORT_ROLLBACK_COMPLETE"}
DEAD = {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED", "DELETE_FAILED"}


class Outputs(dict):
    """Stack outputs; a missing key explains itself instead of raising KeyError."""

    def __init__(self, stack: str, values: dict):
        super().__init__(values)
        self.stack = stack

    def __missing__(self, key):
        raise SystemExit(f"stack {self.stack} has no output '{key}': it was not deployed from this version of "
                         "template.yaml. Deploy the current release first (python scripts/deploy.py app --env ...).")


def stack_info(sess, stack: str) -> dict | None:
    try:
        return sess.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:
        if "does not exist" in str(exc):
            return None
        raise


def failure_events(sess, stack: str, limit: int = 8) -> list[str]:
    """FAILED events of the latest attempt, newest first (the last one listed is usually the cause)."""
    rows: list[str] = []
    try:
        page = next(iter(sess.client("cloudformation").get_paginator("describe_stack_events")
                         .paginate(StackName=stack)))
    except Exception:
        return rows
    for ev in page.get("StackEvents", []):
        if str(ev.get("ResourceStatus", "")).endswith("_FAILED") and ev.get("ResourceStatusReason"):
            rows.append(f"{ev['LogicalResourceId']}: {ev['ResourceStatusReason']}")
        if len(rows) >= limit:
            break
    return rows


EMPTY_CHANGE_SET = ("didn't contain changes", "No updates are to be performed")


def rejected_change_set(sess, stack: str) -> str | None:
    """Reason of the newest change set if CloudFormation rejected it. A rejected change set (template,
    transform or permission problem) leaves no stack events: its reason is only on the change set."""
    try:
        summaries = sess.client("cloudformation").list_change_sets(StackName=stack).get("Summaries", [])
    except Exception:
        return None
    if not summaries:
        return None
    newest = max(summaries, key=lambda s: str(s.get("CreationTime", "")))
    reason = newest.get("StatusReason") or ""
    if newest.get("Status") != "FAILED" or not reason or any(text in reason for text in EMPTY_CHANGE_SET):
        return None
    return f"{newest.get('ChangeSetName', '?')}: {reason}"


def change_set_hint(reason: str) -> str | None:
    if "transform/Serverless" in reason and "not authorized" in reason:
        return ("the CloudFormation execution role may not run the SAM transform. Update the bootstrap stack "
                "(python scripts/deploy.py bootstrap --github-repo OWNER/REPO); this version grants it.")
    return None


def explain_stack(sess, stack: str, info: dict) -> str:
    status = info["StackStatus"]
    reason = info.get("StackStatusReason")
    lines = [f"stack {stack} ({sess.region_name}) is {status}" + (f": {reason}" if reason else "")]
    if status.endswith("_IN_PROGRESS") and status != "REVIEW_IN_PROGRESS":
        lines.append("CloudFormation is still working on it (the CloudFront distribution alone takes 5-15 "
                     "minutes). Wait until it finishes, then run the command again.")
        return "\n".join(lines)
    rejected = rejected_change_set(sess, stack)
    if rejected:
        lines.append(f"CloudFormation rejected the last change set: {rejected}")
        hint = change_set_hint(rejected)
        if hint:
            lines.append(f"what to do: {hint}")
    events = failure_events(sess, stack)
    if events:
        lines.append("failures of the last attempt (newest first; the last one listed is usually the cause):")
        lines += [f"  - {e}" for e in events]
    if status == "REVIEW_IN_PROGRESS":
        lines.append(("Its first change set was rejected" if rejected else "Its first change set was never executed")
                     + ", so the stack has no resources yet and there is nothing to delete. Fix the cause and "
                     "deploy again: SAM creates a new change set.")
    elif status == "UPDATE_ROLLBACK_COMPLETE":
        lines.append("CloudFormation rolled the stack back; the previous release is still live. Fix the cause and "
                     "deploy again.")
    elif status in DEAD:
        profile = f" --profile {sess.profile_name}" if getattr(sess, "profile_name", None) not in (None, "default") else ""
        lines.append("The stack's first deployment did not complete, so CloudFormation cannot update it. Fix the "
                     "cause, then delete it and deploy again:\n"
                     f"  aws cloudformation delete-stack --stack-name {stack} --region {sess.region_name}{profile}")
    return "\n".join(lines)


def sam_deploy(sess, stack: str, cmd: list[str]) -> None:
    """Run `sam deploy`; on failure, say why from the stack and its last change set instead of a traceback."""
    try:
        run(cmd)
    except subprocess.CalledProcessError:
        info = stack_info(sess, stack)
        detail = explain_stack(sess, stack, info) if info else f"stack {stack} was not created"
        raise SystemExit(f"sam deploy failed (its output is above).\n{detail}") from None


def previous_parameters(sess, stack: str) -> dict[str, str]:
    """Parameter values the stack was last deployed with ({} if it does not exist)."""
    info = stack_info(sess, stack) or {}
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in info.get("Parameters", [])}


def outputs(sess, stack: str) -> dict[str, str]:
    info = stack_info(sess, stack)
    if info is None:
        raise SystemExit(f"stack {stack} not found in {sess.region_name} for this AWS profile (wrong account or "
                         "region? check with: aws sts get-caller-identity)")
    if info["StackStatus"] not in READY:
        raise SystemExit(explain_stack(sess, stack, info))
    return Outputs(stack, {o["OutputKey"]: o["OutputValue"] for o in info.get("Outputs", [])})


def params(env: str, model_profile: str | None = None) -> dict[str, str]:
    """Stack parameters of an environment, with its model profile (infra/models.json) expanded."""
    from stocknews.stackparams import load_stack_params

    return load_stack_params(ROOT / "infra" / "params" / f"{env}.json", ROOT / "infra" / "models.json",
                             model_profile=model_profile)


def tool(name: str) -> str:
    found = shutil.which(name)  # resolves sam.cmd / sam.exe on Windows
    if not found:
        raise SystemExit(f"'{name}' was not found in PATH (install the AWS SAM CLI)")
    return found


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True,
                              text=True).stdout.strip()
    except Exception:
        raise SystemExit("could not read the git commit; pass --release explicitly") from None
