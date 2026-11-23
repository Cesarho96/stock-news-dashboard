"""Run the pipeline on your machine.

  python run_local.py --demo                 # synthetic demo edition -> web/data (no keys needed)
  python run_local.py --live                 # real data + Claude on Bedrock -> web/data
  python run_local.py --live --no-llm        # real data, no Bedrock (headlines-only edition)
  python run_local.py --live --s3 BUCKET     # publish to the S3 bucket instead of web/data
  python run_local.py --live --no-judge      # skip the online LLM judge (quality gate uses rules only)

Live mode reads FINNHUB_API_KEY (or FINNHUB_KEY_PARAM with AWS credentials), SEC_USER_AGENT,
MODEL_ID, JUDGE_MODEL_ID, BEDROCK_REGION, LANGUAGE, WATCHLIST... from the environment (see README).
Local live runs cache company profiles in .cache/ and write run records to .runs/ (replayable
with: python -m stocknews.evals --dataset .runs/runs/<date>/).
Then preview with:  python -m http.server 8000 --directory web
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .demo import build_demo_edition
from .pipeline import run_edition
from .publish import LocalPublisher, S3Publisher
from .settings import Settings, limits, load_config

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a stock news dashboard edition.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="write a synthetic demo edition (offline)")
    mode.add_argument("--live", action="store_true", help="collect real data and run the AI agent")
    parser.add_argument("--no-llm", action="store_true", help="live mode without Bedrock")
    parser.add_argument("--slot", choices=["premarket", "midday", "close", "weekend"])
    parser.add_argument("--out", default=str(ROOT / "web" / "data"), help="output data folder")
    parser.add_argument("--s3", metavar="BUCKET", help="publish to this S3 bucket instead of --out")
    parser.add_argument("--lang", choices=["en", "es"], help="override LANGUAGE")
    parser.add_argument("--no-judge", action="store_true", help="skip the online LLM judge")
    parser.add_argument("--no-record", action="store_true", help="do not write a run record to .runs/")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    settings = Settings.from_env()
    if args.lang:
        settings.language = args.lang
    if args.no_judge:
        settings.judge_enabled = False
    settings.cache_dir = settings.cache_dir or str(ROOT / ".cache")
    if not args.no_record:
        settings.records_dir = settings.records_dir or str(ROOT / ".runs")
    cfg = load_config(settings)
    max_editions = limits(cfg)["manifestEditions"]
    publisher = (S3Publisher(args.s3, max_editions=max_editions) if args.s3
                 else LocalPublisher(Path(args.out), max_editions=max_editions))

    if args.demo:
        edition = build_demo_edition(settings, slot=args.slot, cfg=cfg)
        publisher.publish(edition)
    else:
        if not settings.finnhub_api_key and settings.finnhub_key_param:
            from .handler import _finnhub_key

            settings.finnhub_api_key = _finnhub_key(settings)
        if not settings.finnhub_api_key:
            print("warning: FINNHUB_API_KEY is not set; prices and company news will be missing", file=sys.stderr)
        edition = run_edition(settings, publisher, slot=args.slot, use_llm=not args.no_llm, cfg=cfg)

    qa = edition.get("qa") or {}
    print(json.dumps({"edition": edition["id"], "file": edition.get("file"), "status": qa.get("status"),
                      "evidence": qa.get("evidenceCount"), "estimatedCostUsd": qa.get("estimatedCostUsd")}))
    if not args.s3:
        print(f"Preview: python -m http.server 8000 --directory {os.path.relpath(ROOT / 'web')}  "
              f"-> http://localhost:8000")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
