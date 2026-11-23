"""The news agent: research with tools, write the edition JSON, validate, repair.

Conversation (Anthropic Messages format on Bedrock):
  user:      briefing (market data + evidence) + STEP 1 research instruction
  assistant: tool calls ... -> research note                (phase 1, tools: auto)
  user:      STEP 2 write instruction
  assistant: edition JSON                                   (phase 2, tools: none, JSON schema)
  user:      repair message (only if validation finds hard problems), up to N rounds
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from . import prompts
from .clock import EditionContext
from .evidence import EvidencePool
from .llm import BedrockClaude, LLMError
from .market import MarketSnapshot, coverage
from .schema import analysis_schema, tool_definitions
from .settings import Settings
from .tools import ResearchTools
from .validate import Issue, Report, ValidationContext, validate_analysis

log = logging.getLogger(__name__)


class AgentError(Exception):
    pass


@dataclass
class AgentResult:
    analysis: dict
    report: Report
    repairs: int = 0
    tool_calls: int = 0
    research_note: str = ""
    notes: list[str] = field(default_factory=list)
    first_report: Report | None = None  # validation of the first draft (before any repair)
    prompt_fingerprint: str = ""
    llm_seconds: float = 0.0


def _text_of(content: list) -> str:
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def parse_json_text(text: str) -> dict:
    """Parse the model's JSON (tolerates code fences or prose around the object)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


class NewsAgent:
    def __init__(self, llm: BedrockClaude, settings: Settings, tools: ResearchTools | None = None,
                 deadline: float | None = None, clock=time.monotonic):
        self.llm = llm
        self.settings = settings
        self.tools = tools
        self.deadline = deadline
        self.clock = clock

    def _time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - self.clock()

    def run(self, ctx: EditionContext, cfg: dict, snapshot: MarketSnapshot, pool: EvidencePool) -> AgentResult:
        started = self.clock()
        top20, extras = coverage(cfg, snapshot)
        sector_ids = [s["id"] for s in cfg.get("sectors", [])]
        schema = analysis_schema(top20, extras, sector_ids)
        fingerprint = prompts.prompt_fingerprint(ctx.language, analysis_schema(["TOP"], ["EXTRA"], sector_ids))
        system = prompts.system_prompt(ctx.language)
        use_tools = bool(self.tools and self.settings.research_tools and self.settings.max_tool_calls > 0)
        tool_defs = tool_definitions() if use_tools else None

        briefing = {"type": "text", "text": prompts.briefing(ctx, cfg, snapshot, pool),
                    "cache_control": {"type": "ephemeral"}}
        research_note = ""
        if use_tools:
            # ---- phase 1: research with tools ------------------------------------------
            messages: list[dict] = [{"role": "user", "content": [
                briefing, {"type": "text", "text": prompts.research_instruction(self.settings.max_tool_calls)}]}]
            for _ in range(self.settings.max_tool_calls + 2):
                if self._time_left() < 420:
                    research_note = "(research stopped early to leave time for writing)"
                    break
                resp = self.llm.create(system=system, messages=messages, tools=tool_defs,
                                       tool_choice={"type": "auto"})
                content = resp.get("content") or []
                messages.append({"role": "assistant", "content": content})
                if resp.get("stop_reason") == "refusal":
                    raise AgentError("the model declined the research step")
                uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if uses:  # every tool_use must get a tool_result before the next turn
                    results = []
                    for use in uses:
                        output, is_error = self.tools.execute(use.get("name", ""), use.get("input") or {})
                        results.append({"type": "tool_result", "tool_use_id": use.get("id"),
                                        "content": json.dumps(output, ensure_ascii=False),
                                        "is_error": is_error})
                    messages.append({"role": "user", "content": results})
                    continue
                research_note = _text_of(content)
                break
            else:
                research_note = "(research stopped: too many tool rounds)"
            log.info("research done: %d tool calls; note: %s", self.tools.calls,
                     research_note[:500].replace("\n", " "))
            messages.append({"role": "user", "content": [{"type": "text", "text": prompts.write_instruction()}]})
        else:
            messages = [{"role": "user", "content": [briefing, {"type": "text", "text": prompts.write_instruction()}]}]
        # Without structured output (not offered, or the schema is too complex to compile) the Bedrock
        # client appends the schema to the last user turn, and validation + repair rounds enforce it.

        # ---- phase 2: write + validate + repair ---------------------------------------
        vctx = ValidationContext(
            pool=pool, watchlist=extras, sector_ids=sector_ids, top20=top20,
            quote_lookup=(self.tools.ensure_quote if self.tools else snapshot.quote),
        )
        analysis: dict | None = None
        report = Report()
        first_report: Report | None = None
        repairs = 0
        notes: list[str] = []
        for attempt in range(self.settings.max_repairs + 1):
            if attempt == 0 and self._time_left() < 150:
                raise AgentError("not enough time left in this run to write the edition")
            resp = self.llm.create(system=system, messages=messages, tools=tool_defs,
                                   tool_choice={"type": "none"} if tool_defs else None,
                                   output_schema=schema)
            content = resp.get("content") or []
            messages.append({"role": "assistant", "content": content})
            stop = resp.get("stop_reason")
            if stop == "refusal":
                raise AgentError("the model declined to write the edition")
            truncated = stop == "max_tokens"
            text = _text_of(content)
            problems: list[str] = []
            try:
                candidate = parse_json_text(text)
                report = validate_analysis(candidate, vctx)
                analysis = candidate
                problems = [str(i) for i in report.hard]
            except (json.JSONDecodeError, ValueError) as exc:
                problems = [f"$: the response was not valid JSON ({exc})"]
                report = Report(issues=[Issue("$", f"not valid JSON ({exc})")])
            if first_report is None:
                first_report = report
            if not problems:
                break
            log.warning("validation round %d: %d problems: %s", attempt, len(problems), problems[:5])
            if attempt >= self.settings.max_repairs:
                notes.append(f"Validation issues left after {repairs} repair round(s): {len(problems)}")
                break
            if self._time_left() < 240:
                notes.append("Skipped repair rounds: not enough time left in this run")
                break
            repairs += 1
            repair = prompts.repair_message(problems, truncated)
            messages.append({"role": "user", "content": [{"type": "text", "text": repair}]})

        if analysis is None:
            raise AgentError("the model never returned valid JSON")
        return AgentResult(
            analysis=analysis, report=report, repairs=repairs,
            tool_calls=self.tools.calls if self.tools else 0,
            research_note=research_note, notes=notes, first_report=first_report,
            prompt_fingerprint=fingerprint, llm_seconds=round(self.clock() - started, 1),
        )


__all__ = ["NewsAgent", "AgentResult", "AgentError", "LLMError", "parse_json_text"]
