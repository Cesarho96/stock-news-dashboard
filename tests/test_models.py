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
    assert {"target", "sonnet-4-6", "haiku-4-5"} <= set(profiles)
    assert profiles["target"]["ModelId"] == "global.anthropic.claude-sonnet-5-5"
    assert profiles["target"]["JudgeModelId"] == "global.anthropic.claude-haiku-5-5"
    for env in ("dev", "prod"):
        values = load_stack_params(ROOT / "infra" / "params" / f"{env}.json")
        assert "ModelProfile" not in values and not any(k.startswith("_") for k in values)
        assert values["ModelId"] == "global.anthropic.claude-sonnet-4-6"  # writer and judge differ
        assert values["JudgeModelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert values["Effort"] == "medium" and values["Thinking"] == "adaptive" and values["PriceInputMtok"] == "3"
        assert values["JudgeEffort"] == "none" and values["JudgeThinking"] == "off"


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
    assert lang == "en" and s.model_id == "global.anthropic.claude-sonnet-4-6"
    assert s.effort == "medium" and s.thinking == "adaptive" and s.judge_effort == "" and s.judge_thinking == "off"
    assert s.price_input_mtok == 3.0 and s.price_output_mtok == 15.0
    assert s.judge_model_id.startswith("global.anthropic.claude-haiku-4-5")
    writer = BedrockClaude(s.model_id, "us-east-1", s.effort, s.thinking, client=ScriptedBedrock([]))
    body = writer._body("sys", [], None, None, {"type": "object"})
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"  # structured outputs stay on
    judge = BedrockClaude(s.judge_model_id, "us-east-1", s.judge_effort, s.judge_thinking, client=ScriptedBedrock([]))
    body = judge._body("sys", [], None, None, {"type": "object"})
    assert "thinking" not in body and "effort" not in body.get("output_config", {})  # Haiku 4.5 has neither


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
    ("AccessDeniedException", "anthropic.claude-sonnet-5-5 is not available for this account. You can explore other "
                              "available models on Amazon Bedrock. For additional access options, contact AWS Sales.",
     "Account and billing"),
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


# ---- stack state messages (scripts/awsenv.py) --------------------------------------------------
class FakeCF:
    def __init__(self, stack=None, events=(), change_sets=()):
        self.stack, self.events, self.change_sets = stack, list(events), list(change_sets)

    def list_change_sets(self, StackName):
        return {"Summaries": self.change_sets}

    def describe_stacks(self, StackName):
        if self.stack is None:
            raise Exception(f"An error occurred (ValidationError): Stack with id {StackName} does not exist")
        return {"Stacks": [self.stack]}

    def get_paginator(self, name):
        events = self.events

        class P:
            def paginate(self, StackName):
                yield {"StackEvents": events}

        return P()


class FakeSession:
    region_name, profile_name = "us-east-1", "personal"

    def __init__(self, cf):
        self.cf = cf

    def client(self, name):
        return self.cf


def test_stack_outputs_explain_unfinished_or_failed_stacks():
    import awsenv

    with pytest.raises(SystemExit, match="not found in us-east-1"):
        awsenv.outputs(FakeSession(FakeCF()), "snd-dev")
    with pytest.raises(SystemExit, match="still working on it"):
        awsenv.outputs(FakeSession(FakeCF({"StackStatus": "CREATE_IN_PROGRESS"})), "snd-dev")
    failed = FakeCF({"StackStatus": "ROLLBACK_COMPLETE"}, [
        {"LogicalResourceId": "snd-dev", "ResourceStatus": "ROLLBACK_IN_PROGRESS", "ResourceStatusReason": "x"},
        {"LogicalResourceId": "Distribution", "ResourceStatus": "CREATE_FAILED",
         "ResourceStatusReason": "Resource creation cancelled"},
        {"LogicalResourceId": "NewsAgentFunctionRole", "ResourceStatus": "CREATE_FAILED",
         "ResourceStatusReason": "User is not authorized to perform: iam:CreateRole"},
    ])
    with pytest.raises(SystemExit) as err:
        awsenv.outputs(FakeSession(failed), "snd-dev")
    text = str(err.value)
    assert "is ROLLBACK_COMPLETE" in text and "NewsAgentFunctionRole: User is not authorized" in text
    assert "aws cloudformation delete-stack --stack-name snd-dev --region us-east-1 --profile personal" in text
    ready = awsenv.outputs(FakeSession(FakeCF({"StackStatus": "CREATE_COMPLETE", "Outputs": [
        {"OutputKey": "SiteBucketName", "OutputValue": "b"}]})), "snd-dev")
    assert ready["SiteBucketName"] == "b" and ready.get("Missing") is None
    with pytest.raises(SystemExit, match="has no output 'NewsAgentFunctionName'"):
        ready["NewsAgentFunctionName"]


TRANSFORM_DENIED = ("User: arn:aws:sts::123456789012:assumed-role/stock-news-dashboard-bootstrap-CloudFormationExecRole"
                    "-AbC/AWSCloudFormation is not authorized to perform: cloudformation:CreateChangeSet on resource: "
                    "arn:aws:cloudformation:us-east-1:aws:transform/Serverless-2016-10-31")


def test_review_in_progress_shows_the_rejected_change_set_and_needs_no_delete():
    import awsenv

    cf = FakeCF({"StackStatus": "REVIEW_IN_PROGRESS", "StackStatusReason": "User Initiated"}, change_sets=[
        {"ChangeSetName": "samcli-deploy1", "Status": "FAILED", "CreationTime": "2026-11-22 10:00:00+00:00",
         "StatusReason": "older failure"},
        {"ChangeSetName": "samcli-deploy2", "Status": "FAILED", "CreationTime": "2026-11-23 10:00:00+00:00",
         "StatusReason": TRANSFORM_DENIED},
    ])
    with pytest.raises(SystemExit) as err:
        awsenv.outputs(FakeSession(cf), "snd-dev")
    text = str(err.value)
    assert "samcli-deploy2: User: arn:aws:sts" in text and "older failure" not in text
    assert "Update the bootstrap stack" in text and "nothing to delete" in text and "delete-stack" not in text
    never_executed = FakeCF({"StackStatus": "REVIEW_IN_PROGRESS"})
    with pytest.raises(SystemExit, match="never executed"):
        awsenv.outputs(FakeSession(never_executed), "snd-dev")


def test_empty_change_sets_are_not_reported_as_rejections():
    import awsenv

    cf = FakeCF({"StackStatus": "UPDATE_COMPLETE"}, change_sets=[
        {"ChangeSetName": "samcli-deploy3", "Status": "FAILED", "CreationTime": "2026-11-23 10:00:00+00:00",
         "StatusReason": "The submitted information didn't contain changes. Submit different information to "
                         "create a change set."}])
    assert awsenv.rejected_change_set(FakeSession(cf), "snd-dev") is None


def test_failed_sam_deploy_explains_the_stack(monkeypatch):
    import subprocess

    import awsenv

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(awsenv, "run", boom)
    cf = FakeCF({"StackStatus": "UPDATE_ROLLBACK_COMPLETE"}, [
        {"LogicalResourceId": "NewsAgentFunctionAliaslive", "ResourceStatus": "UPDATE_FAILED",
         "ResourceStatusReason": "CodeDeploy deployment failed: the PreTraffic hook reported Failed"}])
    with pytest.raises(SystemExit) as err:
        awsenv.sam_deploy(FakeSession(cf), "snd-dev", ["sam", "deploy"])
    text = str(err.value)
    assert text.startswith("sam deploy failed") and "PreTraffic hook reported Failed" in text
    assert "previous release is still live" in text


def test_bootstrap_rerun_keeps_the_oidc_provider_and_current_values():
    import argparse

    rerun = argparse.Namespace(github_repo="o/r", create_oidc_provider=False, budget=None, email=None)
    previous = {"CreateOidcProvider": "true", "MonthlyBudgetUsd": "50", "BudgetEmail": "me@example.com"}
    values = deploy.bootstrap_overrides(rerun, previous)
    assert values["CreateOidcProvider"] == "true" and values["MonthlyBudgetUsd"] == "50"
    assert values["BudgetEmail"] == "me@example.com"
    first = deploy.bootstrap_overrides(argparse.Namespace(github_repo="o/r", create_oidc_provider=False, budget=None,
                                                          email="me@example.com"), {})
    assert first["CreateOidcProvider"] == "false" and first["MonthlyBudgetUsd"] == "30"
    explicit = deploy.bootstrap_overrides(argparse.Namespace(github_repo="o/r", create_oidc_provider=True, budget=20,
                                                             email=""), previous)
    assert explicit["CreateOidcProvider"] == "true" and explicit["MonthlyBudgetUsd"] == "20"
    assert explicit["BudgetEmail"] == ""


def test_check_models_reports_whether_the_edition_schema_compiles():
    grammar = FakeClientError("ValidationException", "The compiled grammar is too large, which would cause "
                                                     "performance issues. Simplify your tool schemas.")
    fits = deploy.check_edition_schema(BedrockClaude("m", "us-east-1", "", "off", 64,
                                                     client=ScriptedBedrock([text_response("{")])))
    assert fits["ok"] and fits["structured"] and fits["strict"]
    assert "compiles for structured output, with strict" in deploy.describe_schema_check(fits)
    fake = ScriptedBedrock([grammar, grammar, text_response("{")])
    too_big = deploy.check_edition_schema(BedrockClaude("m", "us-east-1", "", "off", 64, client=fake))
    assert too_big["ok"] and not too_big["structured"] and not too_big["strict"]
    assert "describes the schema in the prompt" in deploy.describe_schema_check(too_big)
    sent = fake.requests[0]
    assert sent["output_config"]["format"]["schema"]["properties"]["top20"]["items"]["properties"]["ticker"]["enum"]
    assert [t["name"] for t in sent["tools"]] == ["get_company_news", "get_quotes"] and sent["max_tokens"] == 64
    denied = deploy.check_edition_schema(BedrockClaude("m", "us-east-1", "", "off", 64, client=ScriptedBedrock([
        FakeClientError("ThrottlingException", "Too many tokens")])))
    assert not denied["ok"] and "Service Quotas" in denied["advice"]


# ---- GitHub Actions setup (scripts/deploy.py github-setup) ---------------------------------------
BOOT = {"DeployRoleArn": "arn:aws:iam::1:role/deploy", "EvalsRoleArn": "arn:aws:iam::1:role/evals",
        "OpsBucketName": "ops-bucket", "ArtifactsBucketName": "artifacts", "CloudFormationExecRoleArn": "x"}


def test_github_variables_and_secrets_match_the_workflows():
    values = deploy.github_variables(BOOT, "us-east-1")
    assert values == {"AWS_ROLE_ARN": "arn:aws:iam::1:role/deploy", "AWS_EVALS_ROLE_ARN": "arn:aws:iam::1:role/evals",
                      "OPS_BUCKET": "ops-bucket", "AWS_REGION": "us-east-1"}
    secrets = deploy.github_secrets("me@example.com", "Me me@example.com")
    assert secrets == {"ALARM_EMAIL": "me@example.com", "SEC_USER_AGENT": "Me me@example.com"}
    assert deploy.github_secrets("", "") == {}
    workflows = "".join(p.read_text(encoding="utf-8") for p in (ROOT / ".github" / "workflows").glob("*.yml"))
    for name in values:  # every variable and secret is one the workflows read
        assert f"vars.{name}" in workflows
    for name in secrets:  # email addresses only through secrets: masked in the public logs
        assert f"secrets.{name}" in workflows
    assert f"name: {deploy.CI_CHECK}" in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_github_setup_applies_variables_environments_and_protection(monkeypatch):
    import argparse
    import subprocess

    calls = []

    def fake_gh(args, body=None):
        calls.append((args, body))
        out = "12345\n" if args[:2] == ["api", "user"] else ""
        if any("deployment-branch-policies" in a for a in args):  # a re-run: the policy is already there
            return subprocess.CompletedProcess(args, 1, "", "gh: Name already exists (HTTP 422)")
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(deploy, "gh", fake_gh)
    monkeypatch.setattr(deploy.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(deploy, "session", lambda region, profile: FakeSession(FakeCF()))
    monkeypatch.setattr(deploy, "_bootstrap", lambda sess: BOOT)
    monkeypatch.setattr(deploy, "previous_parameters", lambda sess, stack: {"GitHubRepository": "o/r"})
    deploy.cmd_github_setup(argparse.Namespace(region=None, profile="personal", repo=None, alarm_email="me@x.com",
                                               sec_user_agent=None, dry_run=False))
    variables = [a for a, _ in calls if a[:2] == ["variable", "set"]]
    assert [a[2] for a in variables] == ["AWS_ROLE_ARN", "AWS_EVALS_ROLE_ARN", "OPS_BUCKET", "AWS_REGION"]
    assert all(a[3:5] == ["--repo", "o/r"] for a in variables)
    assert [a[2:] for a, _ in calls if a[:2] == ["secret", "set"]] == [["ALARM_EMAIL", "--repo", "o/r", "--body",
                                                                         "me@x.com"]]
    bodies = {a[3]: b for a, b in calls if a[:3] == ["api", "--method", "PUT"]}
    assert bodies["repos/o/r/environments/production"]["reviewers"] == [{"type": "User", "id": 12345}]
    assert "reviewers" not in bodies["repos/o/r/environments/dev"]
    assert bodies["repos/o/r/environments/dev"]["deployment_branch_policy"]["custom_branch_policies"] is True
    checks = bodies["repos/o/r/branches/main/protection"]["required_status_checks"]["checks"]
    assert checks == [{"context": "lint, tests, templates"}]


def test_github_setup_without_the_cli_prints_the_values(monkeypatch, capsys):
    import argparse

    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    monkeypatch.setattr(deploy, "session", lambda region, profile: FakeSession(FakeCF()))
    monkeypatch.setattr(deploy, "_bootstrap", lambda sess: BOOT)
    deploy.cmd_github_setup(argparse.Namespace(region=None, profile=None, repo="o/r", alarm_email="", sec_user_agent="",
                                               dry_run=False))
    out = capsys.readouterr().out
    assert "winget install --id GitHub.cli" in out and "https://github.com/o/r/settings/variables/actions" in out
    assert "AWS_ROLE_ARN" in out and "arn:aws:iam::1:role/deploy" in out and "Required reviewers" in out
    assert "settings/secrets/actions" in out and "ALARM_EMAIL" in out
