#!/usr/bin/env python3
"""Build once, deploy many: package a release, deploy it to dev/prod, publish its site version.

  python scripts/deploy.py bootstrap --github-repo OWNER/REPO --email you@example.com  (once per account)
  python scripts/deploy.py package [--release SHA]      # Lambda zip + template + site -> artifacts bucket
  python scripts/deploy.py app  --env dev [--release SHA]  # CloudFormation/SAM (CodeDeploy smoke gate inside)
  python scripts/deploy.py site --env dev [--release SHA]  # releases/<SHA>/ + index.html switch
  python scripts/deploy.py release --env prod           # package (if needed) + app + site, for local use
  python scripts/deploy.py run --env dev [--slot close] # publish an edition now
  python scripts/deploy.py validate                     # cfn-lint + SAM transform check (no AWS calls)

Needs: Python 3.10+, boto3, the AWS SAM CLI and AWS credentials (or the GitHub OIDC role in CI).
Release id = the git commit SHA. Parameters per environment live in infra/params/<env>.json;
ALARM_EMAIL and SEC_USER_AGENT environment variables override the matching parameters.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

from awsenv import (ROOT, app_stack, bootstrap_stack, git_sha, outputs, params, registry_prefix, run, session,
                    tool)

BUILD = ROOT / ".build"
ASSET_CACHE = "public, max-age=31536000, immutable"
CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".html": "text/html; charset=utf-8", ".json": "application/json"}


# ---- helpers ---------------------------------------------------------------------------------------
def _bootstrap(sess) -> dict:
    return outputs(sess, bootstrap_stack())


def _s3_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def _overrides(values: dict[str, str]) -> list[str]:
    """Key=Value tokens for SAM; values with spaces or commas are double-quoted."""
    out = []
    for k, v in values.items():
        v = str(v)
        out.append(f'{k}="{v.replace(chr(34), "")}"' if re.search(r"[\s,]", v) else f"{k}={v}")
    return out


def build_site(release: str) -> Path:
    """web/ -> .build/<release>/site/ with index.html pointing at releases/<release>/assets/."""
    out = BUILD / release / "site"
    assets = out / "releases" / release / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "web" / "assets").iterdir():
        if f.is_file():
            (assets / f.name).write_bytes(f.read_bytes())
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    html, n = re.subn(r'(href|src)="assets/', rf'\1="releases/{release}/assets/', html)
    if n < 3:
        raise SystemExit("web/index.html: expected at least 3 asset references to rewrite")
    html = html.replace('<meta charset="UTF-8">', f'<meta charset="UTF-8">\n<meta name="release" content="{release}">', 1)
    (out / "releases" / release / "index.html").write_text(html, encoding="utf-8")
    return out


# ---- commands ---------------------------------------------------------------------------------------
def cmd_validate(args) -> None:
    """cfn-lint both templates and run the SAM transform for each environment (offline)."""
    import copy
    import shutil

    os.environ.setdefault("AWS_DEFAULT_REGION", args.region or "us-east-1")
    try:
        from samtranslator.translator.transform import transform
        from samtranslator.yaml_helper import yaml_parse
    except ModuleNotFoundError:
        raise SystemExit("aws-sam-translator is not installed: pip install -r requirements-dev.txt") from None

    if shutil.which("cfn-lint"):
        run([tool("cfn-lint"), str(ROOT / "template.yaml"), str(ROOT / "infra" / "bootstrap.yaml")])
    else:
        print("cfn-lint is not installed; skipping the lint step")

    class NoManagedPolicies:
        def load(self):
            return {}

    sam = yaml_parse((ROOT / "template.yaml").read_text(encoding="utf-8"))
    for res in sam["Resources"].values():  # what `sam package` does: local code -> S3 object
        if res["Type"] == "AWS::Serverless::Function":
            res["Properties"]["CodeUri"] = "s3://artifacts-bucket-example/releases/example/agent.zip"
    for env in ("dev", "prod"):
        values = dict(params(env), Environment=env, ReleaseId="0" * 40, OpsBucketName="ops-bucket-example")
        out = transform(copy.deepcopy(sam), values, NoManagedPolicies())
        kinds = {r["Type"] for r in out["Resources"].values()}
        for needed in ("AWS::Lambda::Alias", "AWS::CodeDeploy::DeploymentGroup", "AWS::Scheduler::Schedule"):
            if needed not in kinds:
                raise SystemExit(f"SAM transform for {env}: missing {needed}")
        print(f"SAM transform OK for {env}: {len(out['Resources'])} resources")


def cmd_bootstrap(args) -> None:
    sess = session(args.region, args.profile)
    overrides = {"GitHubRepository": args.github_repo, "CreateOidcProvider": "true" if args.create_oidc_provider
                 else "false", "MonthlyBudgetUsd": str(args.budget), "BudgetEmail": args.email or "",
                 "AppName": os.environ.get("STACK_PREFIX", "stock-news-dashboard")}
    run([tool("sam"), "deploy", "--template-file", str(ROOT / "infra" / "bootstrap.yaml"), "--stack-name",
         bootstrap_stack(), "--region", sess.region_name, "--capabilities", "CAPABILITY_NAMED_IAM",
         "--no-confirm-changeset", "--no-fail-on-empty-changeset", "--parameter-overrides", *_overrides(overrides)]
        + (["--profile", args.profile] if args.profile else []))
    out = _bootstrap(sess)
    print("\nBootstrap outputs (set these as GitHub repository variables):")
    for key in ("DeployRoleArn", "EvalsRoleArn", "ArtifactsBucketName", "OpsBucketName", "CloudFormationExecRoleArn"):
        print(f"  {key}: {out.get(key)}")


def cmd_package(args) -> None:
    sess = session(args.region, args.profile)
    release = args.release or git_sha()
    boot = _bootstrap(sess)
    bucket = boot["ArtifactsBucketName"]
    s3 = sess.client("s3")
    target = BUILD / release
    target.mkdir(parents=True, exist_ok=True)
    packaged = target / "packaged.yaml"
    run([tool("sam"), "package", "--template-file", str(ROOT / "template.yaml"), "--s3-bucket", bucket,
         "--s3-prefix", f"releases/{release}", "--output-template-file", str(packaged), "--region", sess.region_name]
        + (["--profile", args.profile] if args.profile else []))
    s3.upload_file(str(packaged), bucket, f"releases/{release}/packaged.yaml")
    site = build_site(release)
    for f in sorted(p for p in site.rglob("*") if p.is_file()):
        key = f"releases/{release}/site/{f.relative_to(site).as_posix()}"
        s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": CONTENT_TYPES.get(f.suffix) or (
            mimetypes.guess_type(f.name)[0] or "application/octet-stream")})
    (target / "release.json").write_text(json.dumps({"release": release, "bucket": bucket}), encoding="utf-8")
    print(f"packaged release {release} -> s3://{bucket}/releases/{release}/")


def _check_deployable(sess, env: str, release: str, force: bool) -> None:
    from stocknews.registry import Registry

    try:
        blocked = Registry(registry_prefix(env), ssm=sess.client("ssm")).blocked()
    except Exception as exc:
        print(f"warning: could not read the blocked-release list: {exc}")
        return
    if release in blocked and not force:
        raise SystemExit(f"release {release} was rolled back in {env} and is blocked. Fix forward with a new "
                         f"commit, or run: python scripts/release.py unblock --env {env} --release {release}")


def cmd_app(args) -> None:
    sess = session(args.region, args.profile)
    release = args.release or git_sha()
    _check_deployable(sess, args.env, release, args.force)
    boot = _bootstrap(sess)
    packaged = BUILD / release / "packaged.yaml"
    if not packaged.exists():
        packaged.parent.mkdir(parents=True, exist_ok=True)
        key = f"releases/{release}/packaged.yaml"
        if not _s3_exists(sess.client("s3"), boot["ArtifactsBucketName"], key):
            raise SystemExit(f"release {release} is not packaged; run: python scripts/deploy.py package --release {release}")
        sess.client("s3").download_file(boot["ArtifactsBucketName"], key, str(packaged))
    values = params(args.env)
    values.update(Environment=args.env, ReleaseId=release, OpsBucketName=boot["OpsBucketName"])
    for env_name, param in (("ALARM_EMAIL", "AlarmEmail"), ("SEC_USER_AGENT", "SecUserAgent")):
        if os.environ.get(env_name):
            values[param] = os.environ[env_name]
    cmd = [tool("sam"), "deploy", "--template-file", str(packaged), "--stack-name", app_stack(args.env),
           "--region", sess.region_name, "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND",
           "--no-confirm-changeset", "--no-fail-on-empty-changeset", "--s3-bucket", boot["ArtifactsBucketName"],
           "--s3-prefix", f"releases/{release}", "--tags", f"app={os.environ.get('STACK_PREFIX', 'stock-news-dashboard')}",
           f"environment={args.env}", "--parameter-overrides", *_overrides(values)]
    if boot.get("CloudFormationExecRoleArn") and not args.no_exec_role:
        cmd += ["--role-arn", boot["CloudFormationExecRoleArn"]]
    if args.profile:
        cmd += ["--profile", args.profile]
    run(cmd)
    _register(sess, args.env, release)


def _register(sess, env: str, release: str) -> None:
    """Record the live version in the registry (CodeDeploy's hook already did on updates)."""
    from stocknews.registry import Registry

    out = outputs(sess, app_stack(env))
    alias = sess.client("lambda").get_alias(FunctionName=out["NewsAgentFunctionName"], Name="live")
    doc = Registry(registry_prefix(env), ssm=sess.client("ssm")).register(release, alias["FunctionVersion"], "deploy")
    print(f"{env}: live = release {doc['releaseId']} (Lambda version {doc['functionVersion']}, {doc['state']})")


def cmd_site(args) -> None:
    from stocknews.ops import restore_site
    from stocknews.registry import Registry

    sess = session(args.region, args.profile)
    release = args.release or git_sha()
    boot, out = _bootstrap(sess), outputs(sess, app_stack(args.env))
    s3, bucket, site = sess.client("s3"), boot["ArtifactsBucketName"], out["SiteBucketName"]
    prefix = f"releases/{release}/site/"
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])]
    if not keys:
        raise SystemExit(f"no site bundle for {release}; run: python scripts/deploy.py package --release {release}")
    for key in keys:
        dest = key[len(prefix):]
        ext = Path(dest).suffix
        s3.copy_object(Bucket=site, Key=dest, CopySource={"Bucket": bucket, "Key": key}, MetadataDirective="REPLACE",
                       ContentType=CONTENT_TYPES.get(ext, "application/octet-stream"),
                       CacheControl="no-cache" if dest.endswith(".html") else ASSET_CACHE)
    restore_site(site, release, out.get("DistributionId"), s3=s3, cf=sess.client("cloudfront"))
    Registry(registry_prefix(args.env), ssm=sess.client("ssm")).set_site(release)
    print(f"{args.env}: site release {release} is live at {out.get('DashboardUrl')}")


def cmd_release(args) -> None:
    sess = session(args.region, args.profile)
    args.release = args.release or git_sha()
    boot = _bootstrap(sess)
    if not _s3_exists(sess.client("s3"), boot["ArtifactsBucketName"], f"releases/{args.release}/packaged.yaml"):
        cmd_package(args)
    cmd_app(args)
    cmd_site(args)


def cmd_run(args) -> None:
    sess = session(args.region, args.profile)
    out = outputs(sess, app_stack(args.env))
    payload = {"source": "manual"}
    if args.slot:
        payload["slot"] = args.slot
    if args.no_llm:
        payload["noLlm"] = True
    sess.client("lambda").invoke(FunctionName=f"{out['NewsAgentFunctionName']}:live", InvocationType="Event",
                                 Payload=json.dumps(payload).encode())
    print(f"started an edition in {args.env}; it appears at {out.get('DashboardUrl')} in about 3-6 minutes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--region")
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="cfn-lint + SAM transform check")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("bootstrap", help="one-time account setup (OIDC roles, buckets, budget)")
    p.add_argument("--github-repo", required=True, help="OWNER/REPO allowed to deploy through GitHub OIDC")
    p.add_argument("--create-oidc-provider", action="store_true",
                   help="create the GitHub OIDC provider (skip if the account already has one)")
    p.add_argument("--budget", type=int, default=30, help="monthly AWS budget alert in USD")
    p.add_argument("--email", help="email for budget alerts")
    p.set_defaults(fn=cmd_bootstrap)
    for name, fn, text in (("package", cmd_package, "build and upload a release"),
                           ("app", cmd_app, "deploy the stack for a release"),
                           ("site", cmd_site, "publish the site files of a release"),
                           ("release", cmd_release, "package + app + site")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--release", help="release id (default: current git commit SHA)")
        if name != "package":
            p.add_argument("--env", required=True, choices=["dev", "prod"])
        if name in ("app", "release"):
            p.add_argument("--force", action="store_true", help="deploy even if the release is blocked")
            p.add_argument("--no-exec-role", action="store_true",
                           help="deploy with your own credentials instead of the CloudFormation execution role")
        p.set_defaults(fn=fn)
    p = sub.add_parser("run", help="publish an edition now")
    p.add_argument("--env", required=True, choices=["dev", "prod"])
    p.add_argument("--slot", choices=["premarket", "midday", "close", "weekend"])
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
