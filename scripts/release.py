#!/usr/bin/env python3
"""Operate releases: status, fast rollback, kill switch, edition withdrawal, eval datasets.

  python scripts/release.py status   --env prod
  python scripts/release.py rollback --env prod [--to-release SHA | --to-version N] --reason "..." [--yes]
  python scripts/release.py kill-switch --env prod --off --reason "..."     (--on to re-enable the AI)
  python scripts/release.py withdraw --env prod --edition 2026-11-20-close
  python scripts/release.py mark-good --env prod                            (end the bake by hand)
  python scripts/release.py block|unblock --env prod --release SHA
  python scripts/release.py check-deployable --env prod --release SHA      (exit 1 when blocked)
  python scripts/release.py capture --env prod --latest 3 --dataset golden (run records -> eval cases)
  python scripts/release.py history --env prod                              (registry audit trail)
  python scripts/release.py baseline --env prod                             (eval report of the last good release)

The release registry lives in SSM Parameter Store under /stock-news-dashboard/<env>/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from awsenv import app_stack, bootstrap_stack, outputs, registry_prefix, session


def _ctx(args):
    from stocknews.registry import Registry

    sess = session(args.region, args.profile)
    out = outputs(sess, app_stack(args.env))
    return sess, out, Registry(registry_prefix(args.env), ssm=sess.client("ssm"))


def _confirm(args, text: str) -> None:
    if args.yes:
        return
    if input(f"{text} Type 'yes' to continue: ").strip().lower() != "yes":
        raise SystemExit("cancelled")


def cmd_status(args) -> None:
    sess, out, reg = _ctx(args)
    lam = sess.client("lambda")
    live = lam.get_alias(FunctionName=out["NewsAgentFunctionName"], Name="live")
    status = reg.status()
    status["liveAlias"] = {"functionVersion": live["FunctionVersion"], "description": live.get("Description", "")}
    try:
        manifest = json.loads(sess.client("s3").get_object(Bucket=out["SiteBucketName"],
                                                           Key="data/manifest.json")["Body"].read())
        status["recentEditions"] = [{k: e.get(k) for k in ("id", "status", "release", "generatedAt")}
                                    for e in (manifest.get("editions") or [])[:5]]
    except Exception as exc:
        status["recentEditions"] = f"manifest not readable: {exc}"
    alarms = sess.client("cloudwatch").describe_alarms(AlarmNamePrefix=app_stack(args.env), StateValue="ALARM")
    status["alarmsFiring"] = [a["AlarmName"] for a in alarms.get("MetricAlarms", [])]
    status["dashboardUrl"] = out.get("DashboardUrl")
    status["operationsDashboard"] = out.get("OperationsDashboardUrl")
    print(json.dumps(status, indent=2, default=str))


def _version_for_release(lam, function: str, release: str) -> str:
    """Find the Lambda version published for a release (SAM sets VersionDescription)."""
    for page in lam.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        for v in page["Versions"]:
            if v["Version"] != "$LATEST" and release in (v.get("Description") or ""):
                return v["Version"]
    raise SystemExit(f"no Lambda version found for release {release}")


def cmd_rollback(args) -> None:
    from stocknews.ops import rollback

    sess, out, reg = _ctx(args)
    lam = sess.client("lambda")
    target = None
    if args.to_version or args.to_release:
        version = args.to_version or _version_for_release(lam, out["NewsAgentFunctionName"], args.to_release)
        cfg = lam.get_function_configuration(FunctionName=out["NewsAgentFunctionName"], Qualifier=version)
        release = (cfg.get("Environment") or {}).get("Variables", {}).get("RELEASE_ID") or args.to_release
        has_site = True
        try:
            sess.client("s3").head_object(Bucket=out["SiteBucketName"], Key=f"releases/{release}/index.html")
        except Exception:
            has_site = False
        target = {"releaseId": release, "functionVersion": str(version), "siteRelease": release if has_site else None}
    target = target or reg.last_good()
    if not target:
        raise SystemExit("no last-good release recorded; pass --to-release or --to-version")
    _confirm(args, f"Roll {args.env} back to release {target['releaseId']} (Lambda version "
                   f"{target['functionVersion']}, site {target.get('siteRelease')})?")
    result = rollback(function_name=out["NewsAgentFunctionName"], alias="live", registry=reg,
                      reason=args.reason or "manual rollback", target=target, site_bucket=out["SiteBucketName"],
                      distribution_id=out.get("DistributionId"), lam=lam, s3=sess.client("s3"),
                      cf=sess.client("cloudfront"))
    print(json.dumps(result, indent=2))
    if result["changed"] and not args.no_regenerate:
        lam.invoke(FunctionName=f"{out['NewsAgentFunctionName']}:live", InvocationType="Event",
                   Payload=json.dumps({"source": "rollback"}).encode())
        print("started a fresh edition with the restored release")


def cmd_kill_switch(args) -> None:
    _, _, reg = _ctx(args)
    enabled = bool(args.on)
    reg.set_ai_enabled(enabled, args.reason or "")
    print(f"{args.env}: AI step {'ENABLED' if enabled else 'DISABLED (editions publish market data and headlines only)'}")


def cmd_withdraw(args) -> None:
    from stocknews.ops import withdraw_edition

    sess, out, _ = _ctx(args)
    _confirm(args, f"Withdraw edition {args.edition} from {args.env}?")
    print(json.dumps(withdraw_edition(out["SiteBucketName"], args.edition, s3=sess.client("s3"),
                                      distribution_id=out.get("DistributionId"), cf=sess.client("cloudfront"))))


def cmd_mark_good(args) -> None:
    _, _, reg = _ctx(args)
    cur = reg.current()
    if not cur:
        raise SystemExit("no current release in the registry")
    print(json.dumps(reg.mark_good(cur, args.reason or "marked good by hand"), indent=2))


def cmd_block(args) -> None:
    _, _, reg = _ctx(args)
    (reg.block(args.release, args.reason or "manual") if args.cmd == "block" else reg.unblock(args.release))
    print(json.dumps({"blocked": reg.blocked()}))


def cmd_check(args) -> None:
    from stocknews.registry import Registry

    sess = session(args.region, args.profile)
    try:
        blocked = Registry(registry_prefix(args.env), ssm=sess.client("ssm")).blocked()
    except Exception as exc:
        print(f"warning: registry not readable ({exc}); assuming deployable")
        return
    if args.release in blocked:
        raise SystemExit(f"release {args.release} is blocked in {args.env} (it was rolled back)")
    print(f"release {args.release} is deployable to {args.env}")


def cmd_capture(args) -> None:
    """Copy production run records into an eval dataset (they embed the replayable case)."""
    sess = session(args.region, args.profile)
    ops_bucket = outputs(sess, bootstrap_stack())["OpsBucketName"]
    s3 = sess.client("s3")
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=ops_bucket,
                                                                              Prefix=f"runs/{args.env}/")
            for o in page.get("Contents", [])]
    if args.edition:
        keys = [k for k in keys if f"/{args.edition}-" in k]
    keys = sorted(keys, reverse=True)[: args.latest]
    for key in keys:
        dest = f"evals/datasets/{args.dataset}/{key.rsplit('/', 1)[-1]}"
        s3.copy_object(Bucket=ops_bucket, Key=dest, CopySource={"Bucket": ops_bucket, "Key": key})
        print(f"s3://{ops_bucket}/{dest}")
    if not keys:
        raise SystemExit("no matching run records")


def cmd_history(args) -> None:
    sess = session(args.region, args.profile)
    ssm = sess.client("ssm")
    name = f"{registry_prefix(args.env)}/release/current"
    rows = []
    for page in ssm.get_paginator("get_parameter_history").paginate(Name=name):
        rows += page["Parameters"]
    for p in rows[-args.limit:]:
        print(f"{p['LastModifiedDate']:%Y-%m-%d %H:%M} v{p['Version']:<3} {p.get('Description', '')}")


def cmd_baseline(args) -> None:
    """Print the S3 URI of the last good release's eval report (regression baseline), or nothing."""
    from stocknews.registry import Registry

    sess = session(args.region, args.profile)
    try:
        good = Registry(registry_prefix(args.env), ssm=sess.client("ssm")).last_good()
        bucket = os.environ.get("OPS_BUCKET") or outputs(sess, bootstrap_stack())["OpsBucketName"]
        if not good:
            return
        key = f"evals/reports/{good['releaseId']}.json"
        sess.client("s3").head_object(Bucket=bucket, Key=key)
    except (Exception, SystemExit) as exc:
        if not args.quiet:
            print(f"no baseline: {exc}", file=sys.stderr)
        return
    print(f"s3://{bucket}/{key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--region")
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--env", required=True, choices=["dev", "prod"])
        p.set_defaults(fn=fn)
        return p

    add("status", cmd_status, "registry, live version, recent editions and firing alarms")
    p = add("rollback", cmd_rollback, "move the live alias and the site back to a validated release")
    p.add_argument("--to-release", help="git SHA of the release to restore (default: last good)")
    p.add_argument("--to-version", help="Lambda version to restore")
    p.add_argument("--reason", required=True)
    p.add_argument("--no-regenerate", action="store_true", help="do not publish a fresh edition afterwards")
    p.add_argument("--yes", action="store_true")
    p = add("kill-switch", cmd_kill_switch, "turn the AI step off/on without a deployment")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--off", action="store_true")
    group.add_argument("--on", action="store_true")
    p.add_argument("--reason")
    p = add("withdraw", cmd_withdraw, "remove a published edition from the manifest")
    p.add_argument("--edition", required=True)
    p.add_argument("--yes", action="store_true")
    p = add("mark-good", cmd_mark_good, "make the current release the rollback target now")
    p.add_argument("--reason")
    for name in ("block", "unblock"):
        p = add(name, cmd_block, f"{name} a release id")
        p.add_argument("--release", required=True)
        p.add_argument("--reason")
    p = add("check-deployable", cmd_check, "fail when a release was rolled back (blocked)")
    p.add_argument("--release", required=True)
    p = add("capture", cmd_capture, "copy run records into an eval dataset")
    p.add_argument("--dataset", default="golden")
    p.add_argument("--latest", type=int, default=3)
    p.add_argument("--edition", help="only records of this edition id")
    p = add("history", cmd_history, "audit trail of the live release")
    p.add_argument("--limit", type=int, default=20)
    p = add("baseline", cmd_baseline, "eval report URI of the last good release")
    p.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
