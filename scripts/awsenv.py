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


def outputs(sess, stack: str) -> dict[str, str]:
    cf = sess.client("cloudformation")
    try:
        stacks = cf.describe_stacks(StackName=stack)["Stacks"]
    except Exception as exc:
        raise SystemExit(f"stack {stack} not found in {sess.region_name}: {exc}") from None
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


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
