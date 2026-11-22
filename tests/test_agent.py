"""Validator, Bedrock client and the agent loop (with a scripted fake Bedrock)."""

import copy
import json

import pytest
from conftest import FakeClientError, FakeHttp, ScriptedBedrock, text_response, tool_response, valid_analysis

from stocknews.agent import NewsAgent, parse_json_text
from stocknews.collect import collect_all
from stocknews.llm import BedrockClaude, LLMError
from stocknews.market import coverage
from stocknews.schema import analysis_schema
from stocknews.tools import ResearchTools
from stocknews.validate import ValidationContext, validate_analysis


@pytest.fixture
def world(ctx, cfg, settings, fake_fh):
    snapshot, pool, _ = collect_all(ctx, cfg, settings, fake_fh, None, FakeHttp())
    return snapshot, pool


def ids_for(pool, symbol=None, market=False):
    if market:
        return [e.id for e in pool.market_wide() if e.in_window]
    return [e.id for e in pool.by_ticker(symbol, in_window_only=True)]


def good_analysis(cfg, pool, snapshot, mover=None):
    return valid_analysis(snapshot, pool, cfg, mover=mover)


def vctx(cfg, pool, snapshot):
    top20, extras = coverage(cfg, snapshot)
    return ValidationContext(pool=pool, watchlist=extras, top20=top20,
                             sector_ids=[s["id"] for s in cfg["sectors"]], quote_lookup=snapshot.quote)


# ---- validator ---------------------------------------------------------------------------
def test_validator_accepts_good_analysis(cfg, world):
    snapshot, pool = world
    a = good_analysis(cfg, pool, snapshot)
    report = validate_analysis(a, vctx(cfg, pool, snapshot))
    assert report.ok, report.hard
    assert len(a["latest"]) == 4 and [n["ticker"] for n in a["top20"]] == snapshot.top20
    assert [n["ticker"] for n in a["watchlist"]] == ["NFLX"]


def test_validator_fixes_and_flags(cfg, world):
    snapshot, pool = world
    a = good_analysis(cfg, pool, snapshot)
    a["top"]["whatHappened"]["evidence"] = ["E999"]  # hallucinated id -> hard
    a["top"]["marketMover"]["ticker"] = "NOPE"  # not a top-20 company -> hard
    a["latest"].append(copy.deepcopy(a["latest"][0]))  # duplicate -> removed
    a["latest"][1]["evidence"] = ["E998"]  # unknown only -> removed
    a["top20"] = a["top20"][1:] + [{"ticker": "TSLA", "sentiment": "bullish", "note": "x", "evidence": []},
                                   {"ticker": "SMCI", "sentiment": "bullish", "note": "x", "evidence": []}]
    a["watchlist"].append({"ticker": "AAPL", "sentiment": "mixed", "summary": "in the top 20", "whatToWatch": "",
                           "evidence": []})
    a["sectors"].append({"sector": "crypto", "title": "x", "whatHappened": "y", "whyItMatters": "z",
                         "tickers": [], "evidence": []})
    a["headline"] = "word " * 80
    report = validate_analysis(a, vctx(cfg, pool, snapshot))
    hard = " | ".join(str(i) for i in report.hard)
    assert "top.whatHappened: cite at least one evidence ID" in hard
    assert "top.marketMover.ticker: 'NOPE' is not in the TOP 20" in hard
    assert f"top20: missing notes for: {snapshot.top20[0]}" in hard
    assert [n["ticker"] for n in a["top20"]] == snapshot.top20[1:]  # SMCI dropped, rank order restored
    assert [n["ticker"] for n in a["watchlist"]] == ["NFLX"]  # AAPL is not an extra
    assert len(a["latest"]) == 3 and len(a["sectors"]) == 0
    assert len(a["headline"]) <= 160
    assert any("removed unknown evidence id E999" in f for f in report.fixes)


def test_validator_top_cards_must_be_about_top20(cfg, world):
    snapshot, pool = world
    a = good_analysis(cfg, pool, snapshot)
    macro = [e.id for e in pool.market_wide() if e.in_window and not e.tickers]
    a["top"]["keyAlert"].update(tickers=[], evidence=macro[:1])  # Fed story: no top-20 company
    a["top"]["marketMover"]["ticker"] = "NFLX"  # extra watchlist ticker, not top 20
    report = validate_analysis(a, vctx(cfg, pool, snapshot))
    hard = " | ".join(str(i) for i in report.hard)
    assert "top.keyAlert.tickers: Top of the day must be about TOP 20 companies" in hard
    assert "top.keyAlert: cite at least one evidence item about a TOP 20 company" in hard
    assert "top.marketMover.ticker: 'NFLX' is not in the TOP 20" in hard


def test_validator_drops_story_repeating_top(cfg, world):
    snapshot, pool = world
    a = good_analysis(cfg, pool, snapshot)
    a["latest"][0]["evidence"] = list(a["top"]["keyAlert"]["evidence"])
    report = validate_analysis(a, vctx(cfg, pool, snapshot))
    assert any("repeats the story already used in top.keyAlert" in f for f in report.fixes)


def test_parse_json_text_tolerates_fences():
    assert parse_json_text('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('prefix {"b": [1,2]} suffix') == {"b": [1, 2]}


def test_schema_is_stable_across_rankings():
    a = analysis_schema(["NVDA", "AAPL"], ["NFLX"], ["technology"])
    b = analysis_schema(["AAPL", "NVDA"], ["NFLX"], ["technology"])
    assert a == b  # enums are sorted: a re-ranking alone does not invalidate Bedrock's schema cache
    assert "watchlist" not in analysis_schema(["AAPL"], [], ["technology"])["properties"]
    empty = analysis_schema([], [], ["technology"])  # no market data this run: no empty enums
    assert "enum" not in empty["properties"]["top"]["properties"]["marketMover"]["properties"]["ticker"]


# ---- Bedrock client ----------------------------------------------------------------------
def test_llm_request_shape_and_feature_downgrades():
    fake = ScriptedBedrock([
        FakeClientError("ValidationException", "output_config.effort: Extra inputs are not permitted"),
        text_response({"ok": True}),
    ])
    llm = BedrockClaude("global.anthropic.claude-sonnet-5-5", "us-east-1", effort="medium", client=fake)
    resp = llm.create(system="sys", messages=[{"role": "user", "content": "hi"}],
                      tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
                      tool_choice={"type": "none"}, output_schema={"type": "object"})
    first, second = fake.requests
    assert first["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert first["thinking"] == {"type": "adaptive"}
    assert first["tools"][0]["strict"] is True and first["tool_choice"] == {"type": "none"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "effort" not in second["output_config"]  # dropped, schema kept
    assert llm.downgrades == ["effort"] and resp["stop_reason"] == "end_turn"
    assert llm.usage["calls"] == 1 and llm.estimated_cost(2.0, 10.0) > 0


def test_llm_raises_on_access_denied():
    fake = ScriptedBedrock([FakeClientError("AccessDeniedException", "not authorized")])
    llm = BedrockClaude("m", "us-east-1", client=fake)
    with pytest.raises(LLMError, match="AccessDeniedException"):
        llm.create(system="s", messages=[])


# ---- agent loop --------------------------------------------------------------------------
def test_agent_researches_writes_and_repairs(ctx, cfg, settings, world, fake_fh):
    snapshot, pool = world
    tools = ResearchTools(fake_fh, pool, snapshot, ctx, budget=settings.max_tool_calls)

    def research_note(body):
        # Phase 1 continues the same conversation: tool results came back as a user turn.
        results = body["messages"][-1]["content"]
        assert results[0]["type"] == "tool_result" and not results[0]["is_error"]
        payload = json.loads(results[0]["content"])
        assert payload["items"][0]["title"].startswith("Super Micro")
        briefing = body["messages"][0]["content"][0]["text"]
        assert "TOP 20 (largest S&P 500 companies by market cap" in briefing and "| NVDA | NVIDIA |" in briefing
        return text_response("- Lead: NVDA\n- Mover: TSLA")

    def first_draft(body):
        assert body["tool_choice"] == {"type": "none"}
        assert body["output_config"]["format"]["type"] == "json_schema"
        schema = body["output_config"]["format"]["schema"]
        assert schema["properties"]["top"]["properties"]["marketMover"]["properties"]["ticker"]["enum"] == sorted(
            snapshot.top20)
        a = good_analysis(cfg, pool, snapshot, mover="TSLA")
        a["top20"] = a["top20"][:-1]  # forget the 20th company -> repair round
        return text_response(a)

    def repaired(body):
        msg = body["messages"][-1]["content"][0]["text"]
        assert f"missing notes for: {snapshot.top20[-1]}" in msg
        return text_response(good_analysis(cfg, pool, snapshot, mover="TSLA"))

    fake = ScriptedBedrock([
        tool_response(("get_company_news", {"symbol": "SMCI", "days": 2}), ("get_quotes", {"symbols": ["SMCI"]})),
        research_note, first_draft, repaired,
    ])
    llm = BedrockClaude(settings.model_id, "us-east-1", effort="medium", client=fake)
    result = NewsAgent(llm, settings, tools=tools).run(ctx, cfg, snapshot, pool)
    assert result.report.ok and result.repairs == 1 and result.tool_calls == 2
    assert result.first_report is not None and not result.first_report.ok
    assert result.analysis["top"]["marketMover"]["ticker"] == "TSLA"
    assert len(result.prompt_fingerprint) == 12
    assert "SMCI" in snapshot.quotes and snapshot.names["SMCI"] == "Super Micro Computer"
    assert any(e.origin == "tool:SMCI" for e in pool.items.values())
    # thinking/tool blocks were passed back unchanged in later turns
    assert fake.requests[1]["messages"][1]["content"][0]["type"] == "tool_use"


def test_agent_without_tools_falls_back_to_prompt_schema(ctx, cfg, settings, world):
    snapshot, pool = world
    settings.research_tools = False
    fake = ScriptedBedrock([
        FakeClientError("ValidationException", "output_config.format: json_schema is not supported for this model"),
        FakeClientError("ValidationException", "output_config.format: json_schema is not supported for this model"),
        text_response("```json\n" + json.dumps(good_analysis(cfg, pool, snapshot)) + "\n```"),
    ])
    llm = BedrockClaude(settings.model_id, "us-east-1", effort="medium", client=fake)
    result = NewsAgent(llm, settings, tools=None).run(ctx, cfg, snapshot, pool)
    assert result.report.ok
    assert llm.downgrades == ["effort", "format"]
    assert "output_config" not in fake.requests[-1]
    assert len(fake.requests[0]["messages"]) == 1  # no research phase


def test_agent_stops_when_lambda_time_is_short(ctx, cfg, settings, world, fake_fh):
    from stocknews.agent import AgentError

    snapshot, pool = world
    tools = ResearchTools(fake_fh, pool, snapshot, ctx, budget=3)
    llm = BedrockClaude(settings.model_id, "us-east-1", client=ScriptedBedrock([]))  # no call allowed
    agent = NewsAgent(llm, settings, tools=tools, deadline=100.0, clock=lambda: 0.0)
    with pytest.raises(AgentError, match="not enough time"):
        agent.run(ctx, cfg, snapshot, pool)
