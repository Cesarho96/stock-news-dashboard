"""Model profiles (infra/models.json), the Bedrock access preflight and per-model settings."""

import json
import sys
from pathlib import Path

import pytest
from conftest import FakeClientError, ScriptedBedrock, text_response

from stocknews.evals import runner
from stocknews.evals.judge import judge_client
from stocknews.llm import BedrockClaude
from stocknews.settings import Settings
from stocknews.stackparams import load_profiles, load_stack_params

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import deploy  # noqa: E402


def test_repo_profiles_and_params():
    profiles = load_profiles(ROOT / "infra" / "models.json")
    assert {"target", "haiku-4-5"} <= set(profiles)
    assert profiles["target"]["ModelId"] == "global.anthropic.claude-sonnet-5-5"
    assert profiles["target"]["JudgeModelId"] == "global.anthropic.claude-haiku-5-5"
    for env in ("dev", "prod"):
        values = load_stack_params(ROOT / "infra" / "params" / f"{env}.json")
        assert "ModelProfile" not in values and not any(k.startswith("_") for k in values)
        assert values["ModelId"] == values["JudgeModelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert values["Effort"] == "none" and values["Thinking"] == "off" and values["PriceInputMtok"] == "1"


def test_profile_expansion_and_overrides(tmp_path):
    (tmp_path / "models.json").write_text(json.dumps({
        "_comment": "x", "a": {"ModelId": "m-a", "Effort": "low"}, "b": {"ModelId": "m-b", "Effort": "high"}}))
    params = tmp_path / "params"
    params.mkdir()
    (params / "prod.json").write_text(json.dumps({"_comment": "y", "ModelProfile": "a", "Effort": "medium",
                                                  "Language": "es"}))
    assert load_stack_params(params / "prod.json") == {"ModelId": "m-a", "Effort": "medium", "Language": "es"}
    # choosing a profile explicitly (check-models / evals --model-profile) also replaces model keys
    assert load_stack_params(params / "prod.json", model_profile="b") == {"ModelId": "m-b", "Effort": "high",
                                                                          "Language": "es"}
    with pytest.raises(ValueError, match="unknown model profile 'zzz'"):
        load_stack_params(params / "prod.json", model_profile="zzz")


def test_eval_runner_applies_the_interim_profile():
    s = Settings()
    lang = runner.apply_params(s, load_stack_params(ROOT / "infra" / "params" / "prod.json"))
    assert lang == "en" and s.model_id.startswith("global.anthropic.claude-haiku-4-5")
    assert s.effort == "" and s.thinking == "off" and s.judge_effort == "" and s.judge_thinking == "off"
    assert s.price_input_mtok == 1.0 and s.price_output_mtok == 5.0
    llm = BedrockClaude(s.model_id, "us-east-1", s.effort, s.thinking, client=ScriptedBedrock([]))
    body = llm._body("sys", [], None, None, {"type": "object"})
    assert "thinking" not in body and "effort" not in body.get("output_config", {})
    assert body["output_config"]["format"]["type"] == "json_schema"  # structured outputs stay on


def test_judge_client_follows_judge_settings(monkeypatch):
    monkeypatch.setenv("JUDGE_THINKING", "off")
    monkeypatch.setenv("JUDGE_EFFORT", "none")
    s = Settings.from_env()
    j = judge_client(s)
    assert j.features["thinking"] is False and j.features["effort"] is False and j.max_tokens == 12000


def test_check_model_reports_dropped_features():
    fake = ScriptedBedrock([
        FakeClientError("ValidationException", "output_config.effort: Extra inputs are not permitted"),
        text_response({"ok": True}),
    ])
    llm = BedrockClaude("global.anthropic.claude-haiku-4-5-20251001-v1:0", "us-east-1", "medium", "off", client=fake)
    result = deploy.check_model(llm)
    assert result["ok"] and result["dropped"] == ["effort"] and "Effort / JudgeEffort to none" in result["hints"][0]
    sent = fake.requests[-1]
    assert sent["tool_choice"] == {"type": "none"} and sent["tools"][0]["strict"] is True


@pytest.mark.parametrize("code,message,expected", [
    ("AccessDeniedException", "You don't have access to the model with the specified model ID.", "Open the model"),
    ("AccessDeniedException", "not authorized to perform: aws-marketplace:Subscribe", "aws-marketplace:Subscribe"),
    ("ResourceNotFoundException", "Model use case details have not been submitted for this account.", "use-case form"),
    ("ThrottlingException", "Too many tokens, please wait before trying again.", "Service Quotas"),
    ("ValidationException", "The provided model identifier is invalid.", "inference profile id"),
    ("ValidationException", "Invocation of model ID anthropic.claude-sonnet-5-5 with on-demand throughput isn't "
                            "supported. Retry your request with the ID or ARN of an inference profile.",
     "inference profile id"),
])
def test_check_model_explains_access_problems(code, message, expected):
    llm = BedrockClaude("global.anthropic.claude-sonnet-5-5", "us-east-1", "medium", "adaptive",
                        client=ScriptedBedrock([FakeClientError(code, message)]))
    result = deploy.check_model(llm)
    assert not result["ok"] and code in result["error"] and expected in result["advice"]
