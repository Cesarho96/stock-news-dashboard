"""Runtime settings (environment variables) and the watchlist configuration."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

CONFIG_PATH = Path(__file__).parent / "config" / "watchlist.json"
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _thinking(value: str | None, default: str) -> str:
    """'adaptive' or 'off' (models without adaptive thinking, e.g. Claude Haiku 4.5)."""
    v = (value or default or "").strip().lower()
    return "off" if v in ("off", "none", "false", "disabled", "0", "") else v


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def parse_tickers(raw: str | None) -> list[str]:
    """'aapl, msft,NVDA' -> ['AAPL', 'MSFT', 'NVDA'] (invalid symbols are dropped)."""
    if not raw:
        return []
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        sym = part.strip().upper()
        if sym and TICKER_RE.match(sym) and sym not in out:
            out.append(sym)
    return out


@dataclass
class Settings:
    # LLM (Claude on Amazon Bedrock, InvokeModel / Messages API format)
    model_id: str = "global.anthropic.claude-sonnet-5-5"
    bedrock_region: str = "us-east-1"
    effort: str = "medium"  # low | medium | high | xhigh | max | none
    thinking: str = "adaptive"  # adaptive | off
    max_tokens: int = 32000
    research_tools: bool = True
    max_tool_calls: int = 6
    max_repairs: int = 2
    # Rough cost estimate shown in logs/QA (USD per million tokens). Check Bedrock pricing.
    price_input_mtok: float = 2.0
    price_output_mtok: float = 10.0
    # Content
    language: str = "en"
    watchlist_override: list[str] = field(default_factory=list)
    # Data sources
    finnhub_api_key: str = ""
    finnhub_key_param: str = ""
    finnhub_min_interval: float = 1.05  # free tier: 60 calls/minute
    sec_user_agent: str = ""
    rss_enabled: bool = True
    # Publishing
    site_bucket: str = ""
    # LLMOps: environment, release identity, run records, online evals, release registry
    environment: str = "local"  # local | dev | prod
    release_id: str = "local"  # git SHA of the deployed build
    ops_bucket: str = ""  # run records, eval datasets/reports, caches (bootstrap stack)
    judge_enabled: bool = True
    judge_model_id: str = "global.anthropic.claude-haiku-5-5"
    judge_effort: str = "low"
    judge_thinking: str = "adaptive"  # adaptive | off
    quality_floor: float = 0.6  # groundedness below this -> AI text withheld (fallback edition)
    quality_target: float = 0.8  # groundedness below this -> edition flagged as degraded
    bake_editions: int = 3  # good editions needed before a release becomes "last good"
    registry_prefix: str = ""  # SSM path, e.g. /stock-news-dashboard/prod
    judge_price_input_mtok: float = 1.0
    judge_price_output_mtok: float = 5.0
    cache_dir: str = ""  # local runs: company-profile cache folder (S3 ops bucket in AWS)
    records_dir: str = ""  # local runs: run-record folder (S3 ops bucket in AWS)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls()
        s.model_id = env.get("MODEL_ID") or s.model_id
        s.bedrock_region = env.get("BEDROCK_REGION") or env.get("AWS_REGION") or s.bedrock_region
        effort = (env.get("EFFORT") or s.effort).strip().lower()
        s.effort = "" if effort in ("", "none", "off", "default") else effort
        s.thinking = _thinking(env.get("THINKING"), s.thinking)
        s.max_tokens = _int(env.get("MAX_TOKENS"), s.max_tokens)
        s.research_tools = _flag(env.get("RESEARCH_TOOLS"), s.research_tools)
        s.max_tool_calls = _int(env.get("MAX_TOOL_CALLS"), s.max_tool_calls)
        s.max_repairs = _int(env.get("MAX_REPAIRS"), s.max_repairs)
        s.price_input_mtok = _float(env.get("PRICE_INPUT_MTOK"), s.price_input_mtok)
        s.price_output_mtok = _float(env.get("PRICE_OUTPUT_MTOK"), s.price_output_mtok)
        lang = (env.get("LANGUAGE") or s.language).strip().lower()
        s.language = lang if lang in ("en", "es") else "en"
        s.watchlist_override = parse_tickers(env.get("WATCHLIST"))
        s.finnhub_api_key = env.get("FINNHUB_API_KEY", "").strip()
        s.finnhub_key_param = env.get("FINNHUB_KEY_PARAM", "").strip()
        s.finnhub_min_interval = _float(env.get("FINNHUB_MIN_INTERVAL"), s.finnhub_min_interval)
        s.sec_user_agent = env.get("SEC_USER_AGENT", "").strip()
        s.rss_enabled = _flag(env.get("RSS_ENABLED"), s.rss_enabled)
        s.site_bucket = env.get("SITE_BUCKET", "").strip()
        s.environment = (env.get("ENVIRONMENT") or s.environment).strip().lower()
        s.release_id = (env.get("RELEASE_ID") or s.release_id).strip()
        s.ops_bucket = env.get("OPS_BUCKET", "").strip()
        s.judge_enabled = _flag(env.get("JUDGE_ENABLED"), s.judge_enabled)
        s.judge_model_id = env.get("JUDGE_MODEL_ID") or s.judge_model_id
        judge_effort = (env.get("JUDGE_EFFORT") or s.judge_effort).strip().lower()
        s.judge_effort = "" if judge_effort in ("", "none", "off", "default") else judge_effort
        s.judge_thinking = _thinking(env.get("JUDGE_THINKING"), s.judge_thinking)
        s.quality_floor = _float(env.get("QUALITY_FLOOR"), s.quality_floor)
        s.quality_target = _float(env.get("QUALITY_TARGET"), s.quality_target)
        s.bake_editions = _int(env.get("BAKE_EDITIONS"), s.bake_editions)
        s.registry_prefix = env.get("REGISTRY_PREFIX", "").strip().rstrip("/")
        s.judge_price_input_mtok = _float(env.get("JUDGE_PRICE_INPUT_MTOK"), s.judge_price_input_mtok)
        s.judge_price_output_mtok = _float(env.get("JUDGE_PRICE_OUTPUT_MTOK"), s.judge_price_output_mtok)
        s.cache_dir = env.get("CACHE_DIR", "").strip()
        s.records_dir = env.get("RECORDS_DIR", "").strip()
        return s


def load_config(settings: Settings | None = None, path: Path | None = None) -> dict:
    """Load config/watchlist.json and apply the optional WATCHLIST override."""
    cfg = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    if settings and settings.watchlist_override:
        known = {w["symbol"]: w for w in cfg.get("watchlist", [])}
        cfg["watchlist"] = [
            known.get(sym, {"symbol": sym, "name": sym, "sector": None})
            for sym in settings.watchlist_override
        ]
    for w in cfg.get("watchlist", []):
        w["symbol"] = w["symbol"].upper()
    universe = cfg.setdefault("universe", {})
    universe.setdefault("size", 20)
    universe.setdefault("index", "S&P 500")
    for item in universe.setdefault("pool", []):
        item["symbol"] = item["symbol"].upper()
    return cfg


def company_info(cfg: dict) -> dict[str, dict]:
    """Every company the config knows (universe pool first, then the watchlist), by symbol."""
    info: dict[str, dict] = {}
    for item in (cfg.get("universe") or {}).get("pool", []) + cfg.get("watchlist", []):
        merged = dict(info.get(item["symbol"], {}))
        for key, value in item.items():
            if key == "aliases":
                merged["aliases"] = list(dict.fromkeys((merged.get("aliases") or []) + list(value or [])))
            elif value not in (None, "") or key not in merged:
                merged[key] = value
        info[item["symbol"]] = merged
    return info


def limits(cfg: dict) -> dict:
    defaults = {
        "companyNewsInWindow": 5,
        "companyNewsContext": 1,
        "marketNews": 25,
        "rssPerFeed": 8,
        "filingsPerTicker": 2,
        "maxEvidence": 260,
        "summaryChars": 260,
        "contextDays": 7,
        "earningsDaysAhead": 14,
        "manifestEditions": 120,
        "profileMaxAgeHours": 20,
    }
    defaults.update(cfg.get("limits") or {})
    return defaults
