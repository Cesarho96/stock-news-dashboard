#!/usr/bin/env python3
"""Build once, deploy many: package a release, deploy it to dev/prod, publish its site version.

  python scripts/deploy.py bootstrap --github-repo OWNER/REPO --email you@example.com  (once per account)
  python scripts/deploy.py package [--release SHA]      # Lambda zip + template + site -> artifacts bucket
  python scripts/deploy.py app  --env dev [--release SHA]  # CloudFormation/SAM (CodeDeploy smoke gate inside)
  python scripts/deploy.py site --env dev [--release SHA]  # releases/<SHA>/ + index.html switch
  python scripts/deploy.py release --env prod           # package (if needed) + app + site, for local use
  python scripts/deploy.py run --env dev [--slot close] # publish an edition now
  python scripts/deploy.py validate                     # cfn-lint + SAM transform + parameter checks (no AWS calls)
  python scripts/deploy.py check-models --env prod [--model-profile target]   # Bedrock access + schema preflight

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
import time
from pathlib import Path

from awsenv import (DEAD, ROOT, app_stack, bootstrap_stack, explain_stack, git_sha, outputs, params,
                    previous_parameters, registry_prefix, run, sam_deploy, session, stack_info, tool)

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
    check_parameters(sam["Parameters"])
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


def check_parameters(template_params: dict) -> None:
    """Every environment's parameters and every model profile must fit the template's
    AllowedValues / AllowedPattern, so a typo fails in CI instead of at deploy time."""
    from stocknews.stackparams import load_profiles

    problems = []

    def check(label: str, values: dict[str, str]) -> None:
        for key, value in values.items():
            spec = template_params.get(key)
            if spec is None:
                problems.append(f"{label}: '{key}' is not a parameter of template.yaml")
                continue
            allowed = [str(a) for a in spec.get("AllowedValues", [])]
            if allowed and value not in allowed:
                problems.append(f"{label}: {key}={value!r} is not one of {allowed}")
            pattern = spec.get("AllowedPattern")
            if pattern and not re.fullmatch(pattern, value):
                problems.append(f"{label}: {key}={value!r} does not match {pattern}")

    for env in ("dev", "prod"):
        check(f"infra/params/{env}.json", params(env))
    for name, values in load_profiles(ROOT / "infra" / "models.json").items():
        check(f"infra/models.json [{name}]", values)
    if problems:
        raise SystemExit("parameter problems:\n  " + "\n  ".join(problems))
    print("parameters OK: infra/params/*.json and every profile in infra/models.json fit template.yaml")


# ---- Bedrock access preflight -----------------------------------------------------------------
CHECK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}}}
CHECK_TOOL = {"name": "noop", "description": "Not needed for this request; present to test tool support.",
              "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}}
FEATURE_HINTS = {
    "effort": "the model rejected the effort parameter: set Effort / JudgeEffort to none in its model profile",
    "thinking": "the model rejected adaptive thinking: set Thinking / JudgeThinking to off in its model profile",
    "format": "no structured outputs with this model/profile: the agent describes the schema in the prompt "
              "(more repair rounds); prefer a model or inference profile with structured outputs",
    "strict": "no strict tool use: tools still work without it",
}


def access_advice(message: str) -> str:
    msg = message.lower()
    if "not available for this account" in msg:
        return ("AWS restricts this model for your account. Waiting, retrying, IAM or model access settings will not "
                "change that (Service Quotas may show 0 for it). Ask AWS to lift the restriction: Support Center > "
                "Create case > Account and billing (free on every support plan), and/or the AWS Sales form linked in "
                "the error. Meanwhile use a model profile whose models pass this check.")
    if "use case" in msg or "use-case" in msg:
        return ("Submit Anthropic's one-time use-case form: Bedrock console > Model catalog > pick an Anthropic "
                "model > Submit use case details. Access is usually immediate (allow up to 15 minutes).")
    if "accessdenied" in msg.replace(" ", "") or "access denied" in msg or "don't have access" in msg:
        if "marketplace" in msg:
            return ("The identity making the first call needs aws-marketplace:Subscribe, aws-marketplace:Unsubscribe "
                    "and aws-marketplace:ViewSubscriptions (an administrator has them). Run check-models with it.")
        return ("No access yet. Open the model in the Bedrock console (Model catalog > the model > Open in "
                "playground) and send one prompt, or run this check with an administrator identity: the first "
                "call subscribes the account through AWS Marketplace (up to 2 minutes; pay per use, no upfront "
                "cost). If it still says access denied after that, access to this model is restricted for your "
                "account; the Bedrock documentation says to contact AWS Sales (or your AWS account team).")
    if "throttl" in msg or "quota" in msg or "too many" in msg:
        return ("The account's quota for this model is too low (new accounts can start near zero). Service Quotas "
                "console > Amazon Bedrock > search the model name > request more tokens and requests per minute for "
                "the inference profile you use (global or us).")
    if "model identifier" in msg or "inference profile" in msg or (
            "model" in msg and ("invalid" in msg or "supported" in msg)):
        return ("Check the model id in infra/models.json: the newest models need an inference profile id "
                "(global.* or us.*) and must be offered in this region.")
    return "See the error above."


def check_model(llm) -> dict:
    """One tiny request with the features the agent uses: structured output, strict tools with
    tool_choice none, effort and thinking. Dropped features are reported with a fix."""
    started = time.monotonic()
    try:
        llm.create(system="You answer health checks.",
                   messages=[{"role": "user", "content": [{"type": "text", "text": 'Reply with {"ok": true}.'}]}],
                   tools=[CHECK_TOOL], tool_choice={"type": "none"}, output_schema=CHECK_SCHEMA)
    except Exception as exc:  # LLMError carries the Bedrock code and message
        return {"ok": False, "error": str(exc), "advice": access_advice(str(exc)),
                "seconds": round(time.monotonic() - started, 1)}
    return {"ok": True, "dropped": list(llm.downgrades), "hints": [FEATURE_HINTS[f] for f in llm.downgrades],
            "seconds": round(time.monotonic() - started, 1)}


def edition_schema() -> dict:
    """The writer's real output schema, for a representative top 20 and watchlist."""
    from stocknews.schema import analysis_schema

    cfg = json.loads((ROOT / "agent" / "stocknews" / "config" / "watchlist.json").read_text(encoding="utf-8"))
    top20 = [row["symbol"] for row in cfg["universe"]["pool"]][:20]
    extras = [w["symbol"] for w in cfg.get("watchlist", []) if w["symbol"] not in top20]
    return analysis_schema(top20, extras, [s["id"] for s in cfg.get("sectors", [])])


def check_edition_schema(llm) -> dict:
    """Send the write step's schema and research tools as the agent does (a few output tokens) to see
    whether Bedrock compiles them for constrained decoding. If not, the agent describes the schema in the
    prompt instead: editions still get AI text, and validation and repair rounds enforce the schema."""
    from stocknews.schema import tool_definitions

    started = time.monotonic()
    try:
        llm.create(system="You write JSON.",
                   messages=[{"role": "user", "content": [{"type": "text", "text": "Start the edition JSON."}]}],
                   tools=tool_definitions(), tool_choice={"type": "none"}, output_schema=edition_schema())
    except Exception as exc:
        return {"ok": False, "error": str(exc), "advice": access_advice(str(exc)),
                "seconds": round(time.monotonic() - started, 1)}
    return {"ok": True, "structured": llm.structured_output, "strict": llm.features["strict"],
            "seconds": round(time.monotonic() - started, 1)}


def describe_schema_check(result: dict) -> str:
    took = f" ({result['seconds']}s)"
    if not result["ok"]:
        return f"check failed{took}: {result['error']}\n        what to do: {result['advice']}"
    if result["structured"] and result["strict"]:
        return "compiles for structured output, with strict research tools" + took
    if result["structured"]:
        return ("compiles for structured output once the research tools are not strict (the agent drops strict "
                "by itself)" + took)
    return ("too complex for structured output on this model: the agent describes the schema in the prompt instead "
            "(editions still get AI text; validation and repair rounds enforce the schema)" + took)


def agreement_status(sess, model_id: str) -> str | None:
    """Marketplace agreement of the underlying model (AVAILABLE / PENDING / NOT_AVAILABLE / ERROR)."""
    prefix, _, rest = model_id.partition(".")
    base = rest if prefix in ("global", "us", "eu", "apac", "au", "jp", "ca", "in", "us-gov") else model_id
    try:
        resp = sess.client("bedrock").get_foundation_model_availability(modelId=base)
        return (resp.get("agreementAvailability") or {}).get("status")
    except Exception:  # older boto3 or no permission: not essential
        return None


def cmd_check_models(args) -> None:
    from botocore.config import Config

    from stocknews.llm import BedrockClaude

    sess = session(args.region, args.profile)
    values = params(args.env, args.model_profile)
    chosen = args.model_profile or json.loads(
        (ROOT / "infra" / "params" / f"{args.env}.json").read_text(encoding="utf-8")).get("ModelProfile", "-")
    client = sess.client("bedrock-runtime", config=Config(read_timeout=120, connect_timeout=10,
                                                          retries={"max_attempts": 2, "mode": "standard"}))
    print(f"model profile: {chosen} · environment: {args.env} · region: {sess.region_name}\n")
    roles = [("writer", values.get("ModelId", ""), values.get("Effort", ""), values.get("Thinking", "adaptive"))]
    if str(values.get("JudgeEnabled", "true")).lower() == "true":
        roles.append(("judge", values.get("JudgeModelId", ""), values.get("JudgeEffort", ""),
                      values.get("JudgeThinking", "adaptive")))
    failed, seen = False, {}
    for role, model, effort, thinking in roles:
        key = (model, effort, thinking)
        if key in seen:
            print(f"{role:7} same model and settings as the {seen[key]}\n")
            continue
        seen[key] = role
        effort = "" if str(effort).lower() in ("", "none", "off") else effort
        llm = BedrockClaude(model, sess.region_name, effort, thinking or "adaptive", 512, client=client)
        result = check_model(llm)
        status = agreement_status(sess, model)
        line = f"{role:7} {model}  " + (f"OK ({result['seconds']}s)" if result["ok"] else "FAILED")
        print(line + (f" · Marketplace agreement: {status}" if status else ""))
        if result["ok"]:
            for hint in result["hints"]:
                print(f"        note: {hint}")
            if role == "writer":  # thinking off: only the grammar is in question here
                schema = check_edition_schema(BedrockClaude(model, sess.region_name, "", "off", 64, client=client))
                failed = failed or not schema["ok"]
                print("        edition schema: " + describe_schema_check(schema))
        else:
            failed = True
            print(f"        {result['error']}")
            print(f"        what to do: {result['advice']}")
        print()
    if failed:
        raise SystemExit(1)
    print("all models of this profile answered with the features the agent uses")


def bootstrap_overrides(args, previous: dict[str, str]) -> dict[str, str]:
    """Options not given on a re-run keep the values the stack has. Above all, a GitHub OIDC provider this
    stack created stays: dropping --create-oidc-provider must not delete it (CI depends on it)."""
    oidc = "true" if args.create_oidc_provider else previous.get("CreateOidcProvider", "false")
    if oidc == "true" and not args.create_oidc_provider:
        print("note: keeping the GitHub OIDC provider this stack created (CreateOidcProvider=true)")
    return {"GitHubRepository": args.github_repo, "CreateOidcProvider": oidc,
            "MonthlyBudgetUsd": str(args.budget if args.budget is not None else previous.get("MonthlyBudgetUsd", 30)),
            "BudgetEmail": args.email if args.email is not None else previous.get("BudgetEmail", ""),
            "AppName": os.environ.get("STACK_PREFIX", "stock-news-dashboard")}


def cmd_bootstrap(args) -> None:
    sess = session(args.region, args.profile)
    overrides = bootstrap_overrides(args, previous_parameters(sess, bootstrap_stack()))
    sam_deploy(sess, bootstrap_stack(),
               [tool("sam"), "deploy", "--template-file", str(ROOT / "infra" / "bootstrap.yaml"), "--stack-name",
                bootstrap_stack(), "--region", sess.region_name, "--capabilities", "CAPABILITY_NAMED_IAM",
                "--no-confirm-changeset", "--no-fail-on-empty-changeset", "--parameter-overrides",
                *_overrides(overrides)] + (["--profile", args.profile] if args.profile else []))
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
    current = stack_info(sess, app_stack(args.env))
    if current and (current["StackStatus"] in DEAD or (current["StackStatus"].endswith("_IN_PROGRESS")
                                                      and current["StackStatus"] != "REVIEW_IN_PROGRESS")):
        raise SystemExit(explain_stack(sess, app_stack(args.env), current))
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
    sam_deploy(sess, app_stack(args.env), cmd)
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
                   help="create the GitHub OIDC provider (skip if the account already has one; a re-run keeps "
                        "a provider this stack created)")
    p.add_argument("--budget", type=int, help="monthly AWS budget alert in USD (default 30, or the current value)")
    p.add_argument("--email", help="email for budget alerts (default: the current value)")
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
    p = sub.add_parser("check-models", help="check Bedrock access to the writer and judge models")
    p.add_argument("--env", required=True, choices=["dev", "prod"])
    p.add_argument("--model-profile", help="check another profile of infra/models.json (e.g. target)")
    p.set_defaults(fn=cmd_check_models)
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
