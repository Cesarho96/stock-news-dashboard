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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        s = cls()
        s.model_id = env.get("MODEL_ID") or s.model_id
        s.bedrock_region = env.get("BEDROCK_REGION") or env.get("AWS_REGION") or s.bedrock_region
        effort = (env.get("EFFORT") or s.effort).strip().lower()
        s.effort = "" if effort in ("", "none", "off", "default") else effort
        s.thinking = (env.get("THINKING") or s.thinking).strip().lower()
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
    return cfg


def limits(cfg: dict) -> dict:
    defaults = {
        "companyNewsInWindow": 6,
        "companyNewsContext": 2,
        "marketNews": 25,
        "rssPerFeed": 8,
        "filingsPerTicker": 3,
        "maxEvidence": 200,
        "summaryChars": 280,
        "contextDays": 7,
        "earningsDaysAhead": 14,
        "manifestEditions": 120,
    }
    defaults.update(cfg.get("limits") or {})
    return defaults
