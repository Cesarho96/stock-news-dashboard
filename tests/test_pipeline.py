"""End-to-end pipeline, publishing, demo edition and the Lambda handler."""

import io
import json

from conftest import NOW, FakeClientError, FakeHttp, ScriptedBedrock, judge_step, text_response, valid_analysis

from stocknews import handler
from stocknews.compose import model_label
from stocknews.demo import build_demo_edition
from stocknews.llm import BedrockClaude
from stocknews.pipeline import run_edition
from stocknews.publish import LocalPublisher, S3Publisher, update_manifest

PAGE_TYPES = ["top", "latest", "watchlist", "markets", "sectors", "radar"]


def _run(tmp_path, settings, cfg, fake_fh, script):
    settings.research_tools = False
    fake = ScriptedBedrock(script)
    llm = BedrockClaude(settings.model_id, "us-east-1", effort="medium", client=fake)
    judge = BedrockClaude(settings.judge_model_id, "us-east-1", effort="low", client=fake)
    publisher = LocalPublisher(tmp_path)
    edition = run_edition(settings, publisher, llm=llm, judge_llm=judge, http=FakeHttp(), fh=fake_fh, now=NOW,
                          cfg=cfg)
    return edition, fake


def _world(settings, cfg, fake_fh):
    from stocknews.clock import build_context
    from stocknews.collect import collect_all

    # Build the same pool the pipeline will build, to script a valid answer with real IDs.
    snapshot, pool, _ = collect_all(build_context(NOW), cfg, settings, fake_fh, None, FakeHttp())
    return snapshot, pool


def test_pipeline_end_to_end(tmp_path, settings, cfg, fake_fh):
    snapshot, pool = _world(settings, cfg, fake_fh)
    edition, fake = _run(tmp_path, settings, cfg, fake_fh,
                         [text_response(valid_analysis(snapshot, pool, cfg, mover="TSLA"))])

    assert edition["id"] == "2026-11-20-close" and edition["qa"]["status"] == "ok"
    assert edition["schemaVersion"] == 2
    assert [p["type"] for p in edition["pages"]] == PAGE_TYPES
    assert [p["number"] for p in edition["pages"]] == ["01", "02", "03", "04", "05", "06"]
    top = edition["pages"][0]
    assert [c["slot"] for c in top["cards"]] == ["lead", "alert", "mover"]
    assert top["cards"][0]["sources"][0]["url"].startswith("https://news.example/")
    assert top["cards"][0]["chips"][0]["symbol"] == snapshot.top20[0]
    assert top["cards"][2]["ticker"]["symbol"] == "TSLA"
    board = top["board"]
    assert [r["symbol"] for r in board["rows"]] == snapshot.top20 and len(board["rows"]) == 20
    assert board["rows"][0]["rank"] == 1 and board["rows"][0]["marketCap"] == "$4.45T"
    assert all(r["noteKind"] == "ai" for r in board["rows"])
    assert board["rows"][0]["source"]["url"].startswith("https://news.example/")
    assert board["rows"][0]["sentiment"] == {"key": "neutral", "label": "Neutral"}
    assert len(edition["strip"]["tiles"]) == 8
    watch = edition["pages"][2]["cards"]
    assert [c["symbol"] for c in watch] == ["NFLX"]  # only watchlist names outside the top 20
    nvda = next(r for r in board["rows"] if r["symbol"] == "NVDA")
    assert nvda["earnings"].startswith("Earnings: ")
    movers = edition["pages"][3]["movers"]["items"]
    assert 0 < len(movers) <= 6 and all(m["symbol"] in snapshot.top20 + ["NFLX"] for m in movers)
    assert {s["name"] for s in edition["footer"]["sources"]} >= {"Reuters"}
    qa = edition["qa"]
    assert qa["usage"]["calls"] == 1 and qa["release"] == "local" and qa["promptVersion"]
    assert qa["quality"]["gate"] == "publish" and qa["quality"]["top20Coverage"] == 1.0
    assert qa["quality"]["groundedness"] is None  # judge disabled in this test

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["latest"] == edition["id"] and manifest["editions"][0]["release"] == "local"
    saved = json.loads((tmp_path / edition["file"]).read_text())
    assert saved["id"] == edition["id"]
    # The model never sees or writes URLs: only IDs go in, URLs come from the pool.
    prompt = json.dumps(fake.requests[0]["messages"])
    assert "https://news.example" not in prompt and "E001" in prompt


def test_pipeline_falls_back_when_bedrock_fails(tmp_path, settings, cfg, fake_fh):
    edition, _ = _run(tmp_path, settings, cfg, fake_fh,
                      [FakeClientError("AccessDeniedException", "Model access is denied")])
    qa = edition["qa"]
    assert qa["status"] == "fallback" and "AccessDeniedException" in qa["reason"]
    assert [p["type"] for p in edition["pages"]] == PAGE_TYPES
    latest = edition["pages"][1]
    assert latest["blocks"] and latest["blocks"][0]["id"] == "headlines"
    cards = edition["pages"][0]["cards"]
    top20 = {r["symbol"] for r in edition["pages"][0]["board"]["rows"]}
    assert cards[2]["ticker"]["symbol"] in top20
    # headline cards are still about top-20 companies only
    assert cards[0]["chips"] and {c["symbol"] for c in cards[0]["chips"] + cards[1]["chips"]} <= top20
    rows = edition["pages"][0]["board"]["rows"]
    assert {r["noteKind"] for r in rows} <= {"headline", "none"} and rows[0]["noteLabel"] == "Latest headline"
    assert edition["pages"][2]["cards"][0]["summary"].startswith("AI summary unavailable")
    assert "AI SUMMARY UNAVAILABLE" in edition["intro"]["stamp"]
    assert json.loads((tmp_path / "manifest.json").read_text())["editions"][0]["status"] == "fallback"


def test_pipeline_degraded_when_repairs_run_out(tmp_path, settings, cfg, fake_fh):
    snapshot, pool = _world(settings, cfg, fake_fh)
    bad = valid_analysis(snapshot, pool, cfg)
    bad["top20"] = bad["top20"][:-1]  # the 20th note keeps missing
    edition, fake = _run(tmp_path, settings, cfg, fake_fh, [text_response(bad)] * 3)
    assert len(fake.requests) == 3 and edition["qa"]["status"] == "degraded"
    assert edition["qa"]["repairs"] == 2 and edition["qa"]["quality"]["gate"] == "degrade"
    last = edition["pages"][0]["board"]["rows"][-1]
    assert last["noteKind"] == "headline" and last["sentiment"] is None


def test_pipeline_judge_withholds_unsupported_sections(tmp_path, settings, cfg, fake_fh):
    settings.judge_enabled = True
    snapshot, pool = _world(settings, cfg, fake_fh)
    a = valid_analysis(snapshot, pool, cfg)
    weak = snapshot.top20[3]
    verdict = {f"top20:{weak}": "unsupported", "top.keyAlert": "partially_supported"}
    edition, fake = _run(tmp_path, settings, cfg, fake_fh, [
        text_response(a), judge_step(lambda uid: verdict.get(uid, "supported"),
                                     lambda uid: ["cause not in evidence"] if uid in verdict else [])])
    qa = edition["qa"]
    assert len(fake.requests) == 2 and "haiku" in settings.judge_model_id
    q = qa["quality"]
    assert q["gate"] == "degrade" and q["withheld"] == [f"top20:{weak}"]
    assert q["unsupported"] == 1 and q["partial"] == 1 and 0.9 < q["groundedness"] < 1.0
    assert qa["status"] == "degraded" and qa["judgeCostUsd"] > 0
    row = next(r for r in edition["pages"][0]["board"]["rows"] if r["symbol"] == weak)
    assert row["noteKind"] == "headline"  # corrective action: source headline instead of the AI note
    method = " ".join(edition["pages"][-1]["method"]["lines"])
    assert "groundedness" in method and "withheld 1 AI section" in method


def test_pipeline_judge_below_floor_publishes_data_only(tmp_path, settings, cfg, fake_fh):
    settings.judge_enabled = True
    snapshot, pool = _world(settings, cfg, fake_fh)
    edition, _ = _run(tmp_path, settings, cfg, fake_fh, [
        text_response(valid_analysis(snapshot, pool, cfg)), judge_step(lambda uid: "unsupported")])
    qa = edition["qa"]
    assert qa["status"] == "fallback" and qa["quality"]["gate"] == "withhold"
    assert "groundedness 0.00 below the floor 0.60" in qa["reason"]
    assert edition["intro"]["headline"].startswith("AI summary unavailable")
    method = " ".join(edition["pages"][-1]["method"]["lines"])
    assert "The quality gate withheld the AI text of this edition (groundedness 0.00" in method


def test_pipeline_survives_judge_errors(tmp_path, settings, cfg, fake_fh):
    settings.judge_enabled = True
    snapshot, pool = _world(settings, cfg, fake_fh)
    edition, _ = _run(tmp_path, settings, cfg, fake_fh, [
        text_response(valid_analysis(snapshot, pool, cfg)),
        FakeClientError("AccessDeniedException", "no access to the judge model")])
    q = edition["qa"]["quality"]
    assert edition["qa"]["status"] == "ok" and q["gate"] == "publish"
    assert "AccessDeniedException" in q["judgeError"] and q["groundedness"] is None


def test_manifest_update_orders_and_caps():
    m = None
    for i in range(5):
        ed = {"id": f"2026-11-1{i}-close", "file": f"editions/x{i}.json", "date": f"2026-11-1{i}", "slot": "close",
              "generatedAt": f"2026-11-1{i}T21:35:00Z", "qa": {"status": "ok"}}
        m = update_manifest(m, ed, max_editions=3)
    assert [e["id"] for e in m["editions"]] == ["2026-11-14-close", "2026-11-13-close", "2026-11-12-close"]
    rerun = {"id": "2026-11-13-close", "file": "editions/rerun.json", "date": "2026-11-13", "slot": "close",
             "generatedAt": "2026-11-15T09:00:00Z", "qa": {"status": "ok"}}
    m = update_manifest(m, rerun, max_editions=3)
    assert m["latest"] == "2026-11-13-close" and [e["file"] for e in m["editions"]].count("editions/rerun.json") == 1


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey", "missing")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def put_object(self, **kw):
        self.objects[kw["Key"]] = kw


def test_s3_publisher_cache_headers():
    s3 = FakeS3()
    ed = build_demo_edition(now=NOW, slot="close")
    manifest = S3Publisher("bucket", s3_client=s3).publish(ed)
    assert manifest["latest"] == ed["id"]
    assert s3.objects["data/manifest.json"]["CacheControl"] == "public, max-age=60"
    assert s3.objects["data/" + ed["file"]]["CacheControl"].endswith("immutable")


def test_demo_edition_is_labeled():
    ed = build_demo_edition(now=NOW, slot="close")
    assert ed["demo"] is True and ed["qa"]["status"] == "demo"
    titles = [c["title"] for c in ed["pages"][0]["cards"]]
    assert all(t.startswith("[Demo]") for t in titles)
    urls = [s["url"] for c in ed["pages"][0]["cards"] for s in c["sources"]]
    urls += [r["source"]["url"] for r in ed["pages"][0]["board"]["rows"] if r["source"]]
    assert urls and all(u.startswith("https://example.com/demo/") for u in urls)
    assert len(ed["pages"][0]["board"]["rows"]) == 20


def test_model_label():
    assert model_label("global.anthropic.claude-sonnet-5-5") == "Claude Sonnet 5.5"
    assert model_label("us.anthropic.claude-haiku-5-5") == "Claude Haiku 5.5"
    assert model_label("anthropic.claude-opus-5") == "Claude Opus 5"


def test_lambda_handler(monkeypatch, capsys):
    seen = {}

    def fake_run(settings, publisher, **kw):
        seen.update(kw, bucket=publisher.bucket, model=settings.model_id, key=settings.finnhub_api_key)
        return {"id": "2026-11-20-close", "file": "editions/a.json",
                "qa": {"status": "ok", "evidenceCount": 9, "release": "abc123",
                       "quality": {"gate": "publish", "groundedness": 0.93, "unverifiedNumbers": 0,
                                   "firstPassValid": True, "top20Coverage": 1.0},
                       "usage": {"calls": 2, "input": 1000, "output": 300}, "estimatedCostUsd": 0.05}}

    monkeypatch.setattr(handler, "run_edition", fake_run)
    monkeypatch.setenv("SITE_BUCKET", "my-bucket")
    monkeypatch.setenv("MODEL_ID", "us.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("RELEASE_ID", "abc123")

    class Ctx:
        function_version = "7"

        def get_remaining_time_in_millis(self):
            return 600000

    out = handler.lambda_handler({"slot": "midday"}, Ctx())
    assert out["edition"] == "2026-11-20-close" and out["status"] == "ok" and out["gate"] == "publish"
    assert out["groundedness"] == 0.93 and out["release"] == "abc123" and out["validated"] is False
    assert seen["slot"] == "midday" and seen["use_llm"] is True and seen["bucket"] == "my-bucket"
    assert seen["model"] == "us.anthropic.claude-haiku-5-5" and seen["key"] == "k" and seen["deadline"]
    emf = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Service", "Environment"]]
    assert emf["Environment"] == "prod" and emf["Groundedness"] == 0.93 and emf["EditionPublished"] == 1
    names = {m["Name"] for m in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert {"FallbackEdition", "WithheldByGate", "UnverifiedNumbers", "EstimatedCostUsd", "Top20Coverage"} <= names


def test_lambda_handler_kill_switch(monkeypatch):
    seen = {}

    class Reg:
        def __init__(self, prefix):
            seen["prefix"] = prefix

        def ai_enabled(self):
            return False

        def record_edition(self, *args, **kw):
            seen["counted"] = kw.get("counted")

    def fake_run(settings, publisher, **kw):
        seen.update(kw)
        return {"id": "x", "qa": {"status": "fallback", "reason": kw["skip_reason"]}}

    import stocknews.registry as registry_module

    monkeypatch.setattr(registry_module, "Registry", Reg)
    monkeypatch.setattr(handler, "run_edition", fake_run)
    monkeypatch.setenv("SITE_BUCKET", "b")
    monkeypatch.setenv("REGISTRY_PREFIX", "/stock-news-dashboard/prod")
    out = handler.lambda_handler({}, None)
    assert seen["use_llm"] is False and "kill switch" in seen["skip_reason"] and seen["counted"] is False
    assert out["status"] == "fallback" and seen["prefix"] == "/stock-news-dashboard/prod"
    values = handler.metric_values({"status": "fallback", "aiSkipped": True})
    assert values["FallbackEdition"][0] == 0 and values["AiSkipped"][0] == 1  # no rollback alarm
