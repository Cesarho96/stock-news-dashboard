"""LLMOps: quality checks and gate, LLM judge, eval suite, run records, release registry and rollback."""

import io
import json

import pytest
from conftest import NOW, FakeClientError, FakeHttp, ScriptedBedrock, judge_step, text_response, valid_analysis

from stocknews import ops, quality
from stocknews.evals import runner
from stocknews.evals.dataset import load_cases, materialize
from stocknews.evals.judge import build_prompt, judge_edition
from stocknews.evals.metrics import aggregate, check
from stocknews.evals.seed import seed_case
from stocknews.llm import BedrockClaude
from stocknews.publish import LocalPublisher
from stocknews.registry import Registry


@pytest.fixture
def seed(cfg):
    case = seed_case(cfg)
    cfg_case, ctx, snapshot, pool, tool_items = materialize(case, cfg)
    return case, cfg_case, ctx, snapshot, pool


def _first(pool, symbol):
    return pool.by_ticker(symbol, in_window_only=True)[0].id


# ---- seed dataset ----------------------------------------------------------------------------
def test_seed_case_is_self_consistent(seed):
    case, cfg_case, ctx, snapshot, pool = seed
    assert case["caseId"] == "seed-2026-11-20-close" and ctx.slot == "close"
    assert snapshot.top20[:3] == ["NVDA", "AAPL", "MSFT"] and "NFLX" not in snapshot.top20
    assert snapshot.quote("NVDA")["changePct"] == 3.1 and len(pool) == 21
    assert all(e.url.startswith("https://example.com/synthetic/") for e in pool.items.values())


# ---- deterministic checks --------------------------------------------------------------------
def test_unverified_numbers(seed):
    _, cfg_case, _, snapshot, pool = seed
    a = {
        "top": {"whatHappened": {"title": "Nvidia leads", "tickers": ["NVDA"], "evidence": [_first(pool, "NVDA")],
                                 "paragraph": "Nvidia rose 3.1% and the S&P 500 ETF gained 0.8%; its market value "
                                              "is $4.45 trillion and the stock closed at $182.40."}},
        "top20": [{"ticker": "TSLA", "note": "Tesla fell 9.9% after a recall of 120,000 vehicles.",
                   "evidence": [_first(pool, "TSLA")]},
                  {"ticker": "AVGO", "note": "Broadcom reports next week; analysts expect EPS of $1.87.",
                   "evidence": []}],
        "latest": [{"title": "Lilly jumps", "whatHappened": "Shares jumped 5,3 % after the panel voted 14-2.",
                    "whyItMatters": "", "tickers": ["LLY"], "evidence": [_first(pool, "LLY")]},
                   {"title": "JPMorgan outlook", "whatHappened": "It expects net interest income of $95 billion.",
                    "whyItMatters": "", "tickers": ["JPM"], "evidence": [_first(pool, "JPM")]}],
    }
    found = quality.unverified_numbers(a, snapshot, pool, cfg_case)
    assert found == [{"unit": "top20:TSLA", "value": "9.9%"}]


def test_advice_flags_and_gate_decisions():
    a = {"headline": "Markets rise", "summary": "Calm session.",
         "top20": [{"ticker": "NVDA", "note": "Investors should buy the dip in Nvidia.", "evidence": []}],
         "radar": [{"kind": "risk", "text": "Recomendamos vender antes del reporte.", "evidence": []}]}
    flags = quality.advice_flags(a)
    assert set(flags) == {"top20:NVDA", "radar:0"}
    checks = quality.Checks(advice=flags)
    d = quality.decide(checks)
    assert d.action == "degrade" and d.withheld == ["radar:0", "top20:NVDA"]
    assert quality.decide(quality.Checks(advice={"intro": ["we recommend"]})).action == "withhold"

    class J:
        def __init__(self, g, unsupported=()):
            self.groundedness, self.unsupported = g, list(unsupported)

    ok = quality.Checks()
    assert quality.decide(ok, J(0.95)).action == "publish"
    assert quality.decide(ok, J(0.75)).action == "degrade"
    assert quality.decide(ok, J(0.5)).action == "withhold"
    assert quality.decide(ok, J(0.9, ["latest:1"])).withheld == ["latest:1"]
    assert quality.decide(quality.Checks(final_valid=False, hard_issues=2)).action == "degrade"


def test_withhold_removes_units():
    a = {"top": {"whatHappened": {"title": "x"}, "keyAlert": {"title": "y"}},
         "top20": [{"ticker": "NVDA"}, {"ticker": "AAPL"}], "latest": [{"title": "a"}, {"title": "b"}],
         "watchlist": [{"ticker": "NFLX"}], "sectors": [{"title": "s"}], "radar": [{"text": "r"}]}
    removed = quality.withhold(a, ["top.keyAlert", "top20:AAPL", "latest:0", "watchlist:NFLX", "radar:0"])
    assert removed == ["top.keyAlert", "top20:AAPL", "watchlist:NFLX", "latest:0", "radar:0"]
    assert a["top"]["keyAlert"] == {} and [n["ticker"] for n in a["top20"]] == ["NVDA"]
    assert a["latest"] == [{"title": "b"}] and a["watchlist"] == [] and a["radar"] == []


# ---- judge -------------------------------------------------------------------------------------
def test_judge_prompt_and_scoring(seed):
    _, cfg_case, ctx, snapshot, pool = seed
    a = valid_analysis(snapshot, pool, cfg_case)
    prompt, ids = build_prompt(a, snapshot, pool, cfg_case, ctx)
    assert ids["U01"] == "top.whatHappened" and len(ids) == 3 + 20 + len(a["latest"]) + 1
    assert "MARKET DATA:" in prompt and "NVDA (NVIDIA): price 182.40, change +3.10%" in prompt
    assert "https://example.com" not in prompt  # the judge sees text, not links
    verdicts = {"top20:TSLA": "unsupported", "latest:0": "partially_supported"}
    fake = ScriptedBedrock([judge_step(lambda uid: verdicts.get(uid, "supported"),
                                       lambda uid: ["not in evidence"] if uid in verdicts else [])])
    result = judge_edition(BedrockClaude("global.anthropic.claude-haiku-5-5", "us-east-1", "low", client=fake),
                           a, snapshot, pool, cfg_case, ctx)
    assert result.error is None and not result.missing
    assert result.unsupported == ["top20:TSLA"] and result.partial == ["latest:0"]
    n = len(ids)
    assert result.groundedness == round((n - 1.5) / n, 4)
    request = fake.requests[0]
    assert request["output_config"]["format"]["schema"]["properties"]["verdicts"]
    assert "fact-checker" in request["system"][0]["text"]


def test_judge_falls_back_to_prompt_schema(seed):
    _, cfg_case, ctx, snapshot, pool = seed
    a = valid_analysis(snapshot, pool, cfg_case)
    fake = ScriptedBedrock([
        FakeClientError("ValidationException", "output_config.format: json_schema is not supported"),
        FakeClientError("ValidationException", "output_config.format: json_schema is not supported"),
        text_response("I think they are mostly fine."),  # prose despite the schema in the prompt: ask again
        judge_step(),
    ])
    result = judge_edition(BedrockClaude("m", "us-east-1", "low", client=fake), a, snapshot, pool, cfg_case, ctx)
    assert result.error is None and result.groundedness == 1.0
    for request in fake.requests[2:]:  # both calls without structured output describe the schema
        assert "output_config" not in request
        assert "must match this JSON schema" in request["messages"][0]["content"][-1]["text"]


# ---- eval suite ------------------------------------------------------------------------------
def _factories(cfg, verdict=lambda uid: "supported"):
    case = seed_case(cfg)
    cfg_case, _, snapshot, pool, _ = materialize(case, cfg)
    analysis = valid_analysis(snapshot, pool, cfg_case)
    writer = lambda: BedrockClaude("global.anthropic.claude-sonnet-5-5", "us-east-1", "medium",  # noqa: E731
                                   client=ScriptedBedrock([text_response("- research note"), text_response(analysis)]))
    judge = lambda: BedrockClaude("global.anthropic.claude-haiku-5-5", "us-east-1", "low",  # noqa: E731
                                  client=ScriptedBedrock([judge_step(verdict)]))
    return writer, judge


def test_eval_suite_passes_the_gate(settings, cfg):
    settings.judge_enabled = True
    writer, judge = _factories(cfg)
    report = runner.run_suite(["seed:"], settings, cfg, gate="gate", llm_factory=writer, judge_factory=judge)
    assert report["gate"]["ok"], report["gate"]["failures"]
    agg = report["aggregate"]
    assert agg["cases"] == 1 and agg["groundedness_mean"] == 1.0 and agg["final_valid_rate"] == 1.0
    assert agg["top20_coverage_mean"] == 1.0 and agg["unverified_numbers_total"] == 0
    case = report["cases"][0]
    assert case["status"] == "ok" and case["metrics"]["toolCalls"] == 0 and case["promptFingerprint"]
    md = runner.summary_markdown(report)
    assert md.startswith("## LLM evals: ✅ passed") and "| groundedness_mean | ≥ 0.85 | 1 | ✅ |" in md


def test_eval_suite_fails_on_ungrounded_output_and_regression(settings, cfg):
    settings.judge_enabled = True
    writer, judge = _factories(cfg, verdict=lambda uid: "unsupported" if uid.startswith("top") else "supported")
    baseline = {"release": "good", "aggregate": {"groundedness_mean": 0.97, "first_pass_valid_rate": 1.0}}
    report = runner.run_suite(["seed:"], settings, cfg, gate="gate", llm_factory=writer, judge_factory=judge,
                              baseline=baseline)
    failures = " | ".join(report["gate"]["failures"])
    assert not report["gate"]["ok"]
    assert "groundedness_mean" in failures and "vs baseline 0.97" in failures
    assert "❌" in runner.summary_markdown(report)


def test_metric_checks_treat_missing_values_as_failures():
    agg = aggregate([{"schemaValid": True, "firstPassValid": True, "finalValid": True, "groundedness": None,
                      "top20Coverage": 1.0, "unverifiedNumbers": 0, "adviceFlags": 0, "citedIds": 10,
                      "citationErrors": 1, "gate": "publish", "status": "ok", "repairs": 0, "costUsd": 0.2,
                      "seconds": 80}])
    assert agg["judged_rate"] == 0.0 and agg["citation_error_rate"] == round(1 / 11, 4)
    verdict = check(agg, {"checks": {"groundedness_min": {"min": 0.6}, "advice_flags_total": {"max": 0}}})
    assert not verdict.ok and verdict.failures == ["groundedness_min = None (needs ≥ 0.6)"]


def test_eval_cli_writes_report_and_summary(tmp_path, monkeypatch):
    report = {"gate": {"name": "gate", "ok": False, "checks": [], "failures": ["x"], "baselineRelease": None},
              "release": "abc", "promptVersion": "p", "promptFingerprint": "f", "model": "m", "judgeModel": None,
              "seconds": 1.0, "aggregate": {"cases": 0, "cost_usd_total": 0}, "cases": []}
    monkeypatch.setattr(runner, "run_suite", lambda *a, **kw: report)
    out, summary = tmp_path / "r.json", tmp_path / "s.md"
    assert runner.main(["--out", str(out), "--summary", str(summary)]) == 1
    assert json.loads(out.read_text())["release"] == "abc" and "FAILED" in summary.read_text()


# ---- run records: capture and replay -----------------------------------------------------------
def test_run_record_is_a_replayable_eval_case(tmp_path, settings, cfg, fake_fh):
    from stocknews.clock import build_context
    from stocknews.collect import collect_all
    from stocknews.pipeline import run_edition

    settings.records_dir = str(tmp_path / "records")
    settings.research_tools = False
    snapshot, pool, _ = collect_all(build_context(NOW), cfg, settings, fake_fh, None, FakeHttp())
    analysis = valid_analysis(snapshot, pool, cfg)
    llm = BedrockClaude(settings.model_id, "us-east-1", client=ScriptedBedrock([text_response(analysis)]))
    edition = run_edition(settings, LocalPublisher(tmp_path / "site"), llm=llm, http=FakeHttp(), fh=fake_fh,
                          now=NOW, cfg=cfg)
    files = list((tmp_path / "records" / "runs").rglob("*.json.gz"))
    assert len(files) == 1 and files[0].name.startswith(edition["id"])
    cases = load_cases([str(files[0].parent)], cfg)
    assert len(cases) == 1 and cases[0]["meta"]["status"] == "ok"
    replay = runner.evaluate_case(cases[0], settings, cfg, llm=BedrockClaude(
        settings.model_id, "us-east-1", client=ScriptedBedrock([text_response(analysis)])))
    assert replay["status"] == "ok" and replay["metrics"]["finalValid"]


# ---- release registry ------------------------------------------------------------------------
class FakeSSM:
    def __init__(self):
        self.params, self.history = {}, []

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.params:
            raise FakeClientError("ParameterNotFound", Name)
        return {"Parameter": {"Name": Name, "Value": self.params[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite, Description=""):
        self.params[Name] = Value
        self.history.append((Name, Description))


def test_registry_bake_promotes_release_to_last_good():
    reg = Registry("/app/prod", ssm=FakeSSM())
    assert reg.ai_enabled() and reg.current() is None
    reg.set_site("abc")
    reg.register("abc", "7", "codedeploy")
    assert reg.current()["state"] == "baking"
    assert reg.record_edition("abc", "7", "degraded", bake_editions=2) is None
    assert reg.record_edition("abc", "7", "ok", bake_editions=2) is None
    assert reg.record_edition("abc", "7", "ok", bake_editions=2) == "validated"
    good = reg.last_good()
    assert good["releaseId"] == "abc" and good["functionVersion"] == "7" and good["siteRelease"] == "abc"
    assert reg.current()["state"] == "validated" and reg.current()["badEditions"] == 1
    assert reg.record_edition("abc", "7", "ok", bake_editions=2) is None  # already validated
    assert reg.record_edition("abc", "$LATEST", "ok", bake_editions=2) is None
    reg.set_ai_enabled(False, "incident")
    assert not reg.ai_enabled()


def test_registry_first_edition_starts_tracking():
    reg = Registry("/app/dev", ssm=FakeSSM())
    assert reg.record_edition("abc", "1", "ok", bake_editions=1) == "validated"
    assert reg.last_good()["functionVersion"] == "1"


# ---- rollback ---------------------------------------------------------------------------------
class FakeLambda:
    def __init__(self, live="8", response=None, error=None):
        self.alias = {"live": live}
        self.updates, self.invokes = [], []
        self.response, self.error = response or {}, error

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": self.alias[Name]}

    def update_alias(self, FunctionName, Name, FunctionVersion, RoutingConfig, Description=""):
        self.alias[Name] = FunctionVersion
        self.updates.append((FunctionName, Name, FunctionVersion))

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invokes.append((FunctionName, InvocationType, json.loads(Payload)))
        out = {"Payload": io.BytesIO(json.dumps(self.response).encode())}
        if self.error:
            out["FunctionError"] = "Unhandled"
        return out


class FakeAws:
    """s3 / cloudfront / sns / codedeploy stand-ins that record their calls."""

    def __init__(self):
        self.calls = []
        self.objects = {}

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            if name == "get_object":
                return {"Body": io.BytesIO(self.objects[kw["Key"]])}
            if name == "put_object":
                self.objects[kw["Key"]] = kw["Body"]
            return {}

        return call


def _baking_registry(ssm):
    reg = Registry("/app/prod", ssm=ssm)
    reg.set_site("good")
    reg.register("good", "5", "codedeploy")
    reg.mark_good(reg.current(), "test")
    reg.set_site("bad")
    reg.register("bad", "8", "codedeploy")
    return reg


def test_manual_rollback_restores_function_site_and_blocks_release():
    reg, lam, aws = _baking_registry(FakeSSM()), FakeLambda(live="8"), FakeAws()
    result = ops.rollback(function_name="fn", alias="live", registry=reg, reason="manual", site_bucket="site",
                          distribution_id="DIST", lam=lam, s3=aws, cf=aws)
    assert result["changed"] and result["from"] == {"releaseId": "bad", "functionVersion": "8"}
    assert lam.alias["live"] == "5" and result["site"] == "releases/good/index.html"
    copy = next(kw for name, kw in aws.calls if name == "copy_object")
    assert copy["Key"] == "index.html" and copy["CopySource"]["Key"] == "releases/good/index.html"
    assert any(name == "create_invalidation" for name, _ in aws.calls)
    cur = reg.current()
    assert cur["functionVersion"] == "5" and cur["rolledBackFrom"]["releaseId"] == "bad"
    assert reg.blocked() == ["bad"] and reg.site_current()["siteRelease"] == "good"
    # nothing to do the second time
    assert not ops.rollback(function_name="fn", alias="live", registry=reg, reason="again", site_bucket="site",
                            lam=lam, s3=aws, cf=aws)["changed"]


def _wire_ops(monkeypatch, ssm, lam, aws):
    monkeypatch.setattr(ops, "Registry", lambda prefix: Registry(prefix, ssm=ssm))
    monkeypatch.setattr(ops, "_client", lambda name, **kw: lam if name == "lambda" else aws)
    for k, v in {"REGISTRY_PREFIX": "/app/prod", "FUNCTION_NAME": "fn", "SITE_BUCKET": "site",
                 "DISTRIBUTION_ID": "DIST", "TOPIC_ARN": "arn:aws:sns:us-east-1:1:t", "STACK_NAME": "snd-prod"}.items():
        monkeypatch.setenv(k, v)


def _alarm(state="ALARM"):
    return {"detail": {"alarmName": "snd-prod-rb-quality", "state": {"value": state, "reason": "Threshold crossed"}}}


def test_auto_rollback_during_bake(monkeypatch):
    ssm, lam, aws = FakeSSM(), FakeLambda(live="8"), FakeAws()
    _baking_registry(ssm)
    _wire_ops(monkeypatch, ssm, lam, aws)
    out = ops.auto_rollback_handler(_alarm(), None)
    assert out["action"] == "rolled-back" and lam.alias["live"] == "5"
    subject = next(kw["Subject"] for name, kw in aws.calls if name == "publish")
    assert subject == "[snd-prod] rolled back to good after snd-prod-rb-quality"
    assert lam.invokes == [("fn:live", "Event", {"source": "rollback"})]  # fresh edition from the good release
    assert ops.auto_rollback_handler(_alarm("OK"), None)["action"] == "ignored"


def test_auto_rollback_only_notifies_for_validated_release(monkeypatch):
    ssm, lam, aws = FakeSSM(), FakeLambda(live="5"), FakeAws()
    reg = _baking_registry(ssm)
    reg.mark_good(reg.current(), "bake done")  # 'bad' finished its bake: it is validated now
    _wire_ops(monkeypatch, ssm, lam, aws)
    out = ops.auto_rollback_handler(_alarm(), None)
    assert out["action"] == "notified" and not lam.updates
    subject = next(kw["Subject"] for name, kw in aws.calls if name == "publish")
    assert "no automatic rollback" in subject


def test_pre_traffic_hook_gates_the_alias(monkeypatch):
    ssm, aws = FakeSSM(), FakeAws()
    lam = FakeLambda(response={"ok": True, "release": "abc", "metrics": {}})
    _wire_ops(monkeypatch, ssm, lam, aws)
    monkeypatch.setenv("NEW_VERSION", "arn:aws:lambda:us-east-1:1:function:fn:12")
    out = ops.pre_traffic_handler({"DeploymentId": "d-1", "LifecycleEventHookExecutionId": "h-1"}, None)
    assert out["status"] == "Succeeded" and lam.invokes[0][2] == {"mode": "smoke"}
    status = next(kw for name, kw in aws.calls if name == "put_lifecycle_event_hook_execution_status")
    assert status == {"deploymentId": "d-1", "lifecycleEventHookExecutionId": "h-1", "status": "Succeeded"}
    cur = Registry("/app/prod", ssm=ssm).current()
    assert cur["releaseId"] == "abc" and cur["functionVersion"] == "12" and cur["state"] == "baking"

    aws.calls.clear()
    lam.response = {"ok": False, "failures": ["groundedness_min = 0.4 (needs ≥ 0.6)"]}
    out = ops.pre_traffic_handler({"DeploymentId": "d-2", "LifecycleEventHookExecutionId": "h-2"}, None)
    assert out["status"] == "Failed"
    names = [name for name, _ in aws.calls]
    assert "put_lifecycle_event_hook_execution_status" in names and "publish" in names


def test_withdraw_edition_moves_latest_back():
    aws = FakeAws()
    manifest = {"latest": "b", "editions": [{"id": "b", "file": "editions/b.json"}, {"id": "a", "file": "editions/a.json"}]}
    aws.objects["data/manifest.json"] = json.dumps(manifest).encode()
    out = ops.withdraw_edition("site", "b", s3=aws)
    saved = json.loads(aws.objects["data/manifest.json"])
    assert out == {"withdrawn": "b", "latest": "a"} and saved["withdrawn"] == ["b"]
    assert [e["id"] for e in saved["editions"]] == ["a"]
    with pytest.raises(ValueError):
        ops.withdraw_edition("site", "zzz", s3=aws)


def test_smoke_mode(monkeypatch):
    from stocknews import handler

    seen = {}

    def fake_suite(specs, settings, gate, thresholds, language):
        seen.update(specs=specs, gate=gate, language=language)
        return {"gate": {"ok": True, "failures": []},
                "cases": [{"metrics": {"groundedness": 0.95}, "problems": {}, "unverified": []}]}

    monkeypatch.setattr(runner, "run_suite", fake_suite)
    monkeypatch.setenv("RELEASE_ID", "abc")
    out = handler.lambda_handler({"mode": "smoke"}, None)
    assert out["ok"] and out["release"] == "abc" and seen == {"specs": ["seed:"], "gate": "smoke", "language": "en"}


def test_smoke_gate_passes_when_bedrock_cannot_compile_the_schemas(settings, cfg):
    """What the deployment smoke test sees on a model that rejects the schemas as too complex."""
    settings.judge_enabled = True
    cfg_case, _, snapshot, pool, _ = materialize(seed_case(cfg), cfg)
    grammar = FakeClientError("ValidationException", "The compiled grammar is too large, which would cause "
                                                     "performance issues. Simplify your tool schemas.")
    writer_fake = ScriptedBedrock([text_response("- research note"), grammar, grammar,
                                   text_response(valid_analysis(snapshot, pool, cfg_case))])
    judge_fake = ScriptedBedrock([grammar, judge_step()])
    report = runner.run_suite(["seed:"], settings, cfg, gate="smoke",
                              llm_factory=lambda: BedrockClaude("m", "us-east-1", "", "off", client=writer_fake),
                              judge_factory=lambda: BedrockClaude("j", "us-east-1", "", "off", client=judge_fake))
    assert report["gate"]["ok"], report["gate"]["failures"]
    assert report["cases"][0]["status"] == "ok" and report["aggregate"]["groundedness_mean"] == 1.0
    assert "output_config" not in writer_fake.requests[-1] and "output_config" not in judge_fake.requests[-1]


def test_gate_skips_judge_checks_when_the_judge_is_off(settings, cfg):
    settings.judge_enabled = False
    writer, _ = _factories(cfg)
    report = runner.run_suite(["seed:"], settings, cfg, gate="smoke", llm_factory=writer)
    assert report["gate"]["ok"], report["gate"]["failures"]
    assert not any(r["metric"].startswith(("groundedness", "judged")) for r in report["gate"]["checks"])


def test_eval_params_follow_the_stack_configuration(settings):
    lang = runner.apply_params(settings, {"ModelId": "us.anthropic.claude-opus-5-5", "Effort": "none",
                                          "JudgeEnabled": "false", "QualityFloor": "0.7", "Language": "es"})
    assert lang == "es" and settings.model_id == "us.anthropic.claude-opus-5-5" and settings.effort == ""
    assert settings.judge_enabled is False and settings.quality_floor == 0.7
