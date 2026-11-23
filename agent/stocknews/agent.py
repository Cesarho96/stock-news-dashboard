"""The news agent: research with tools, write the edition JSON, validate, repair.

Conversation (Anthropic Messages format on Bedrock):
  user:      briefing (market data + evidence) + STEP 1 research instruction
  assistant: tool calls ... -> research note                (phase 1, tools: auto)
  user:      STEP 2 write instruction
  assistant: edition JSON                                   (phase 2, tools: none, JSON schema)
  user:      repair message, up to N rounds: validation problems, or (once, on a valid
             draft) figures that match neither MARKET DATA nor the evidence cited with them
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
from .quality import unverified_numbers
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


def _where(unit_id: str) -> str:
    """'top20:XOM' -> 'top20 note for XOM'; 'latest:1' -> 'latest[1]'; 'top.keyAlert' unchanged."""
    section, _, key = unit_id.partition(":")
    if not key:
        return unit_id
    return f"{section} note for {key}" if section in ("top20", "watchlist") else f"{section}[{key}]"


def figure_problems(figures: list[dict]) -> list[str]:
    return [f'{_where(f["unit"])}: the figure "{f["value"]}" matches neither MARKET DATA for the companies it names '
            "nor the evidence cited in this entry. Take it from MARKET DATA (if it is another symbol's move, name "
            "that symbol next to it, e.g. \"oil (USO)\"), cite the evidence item that states it, or remove it. No "
            "figures from memory, such as year-to-date moves that no cited item states." for f in figures]


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
        last_valid: tuple[dict, Report] | None = None
        figures_sent = False
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
            if not problems and analysis is not None:
                last_valid = (analysis, report)
                if not figures_sent:
                    # Corrective step before the quality gate: figures the gate would withhold go back
                    # to the model once, with the instruction to correct, source or remove them.
                    figures = unverified_numbers(analysis, snapshot, pool, cfg)
                    if figures and attempt < self.settings.max_repairs and self._time_left() >= 240:
                        figures_sent = True
                        problems = figure_problems(figures)
                        notes.append(f"Figure check: {len(figures)} unverified figure(s) sent back to the model")
            if not problems:
                break
            log.warning("repair round %d: %d problems: %s", attempt, len(problems), problems[:5])
            if attempt >= self.settings.max_repairs:
                notes.append(f"Validation issues left after {repairs} repair round(s): {len(problems)}")
                break
            if self._time_left() < 240:
                notes.append("Skipped repair rounds: not enough time left in this run")
                break
            repairs += 1
            repair = prompts.repair_message(problems, truncated)
            messages.append({"role": "user", "content": [{"type": "text", "text": repair}]})

        if not report.ok and last_valid is not None:
            # a later round broke a draft that was valid: publish the valid one (the gate handles its figures)
            analysis, report = last_valid
            notes = [n for n in notes if not n.startswith("Validation issues left")]
            notes.append("Kept the last valid draft: a later repair round introduced validation issues")
        if analysis is None:
            raise AgentError("the model never returned valid JSON")
        return AgentResult(
            analysis=analysis, report=report, repairs=repairs,
            tool_calls=self.tools.calls if self.tools else 0,
            research_note=research_note, notes=notes, first_report=first_report,
            prompt_fingerprint=fingerprint, llm_seconds=round(self.clock() - started, 1),
        )


__all__ = ["NewsAgent", "AgentResult", "AgentError", "LLMError", "parse_json_text"]
