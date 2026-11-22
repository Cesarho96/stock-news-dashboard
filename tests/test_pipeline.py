"""End-to-end pipeline, publishing, demo edition and the Lambda handler."""

import io
import json

from conftest import NOW, FakeClientError, FakeHttp, ScriptedBedrock, text_response
from test_agent import good_analysis

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
    publisher = LocalPublisher(tmp_path)
    edition = run_edition(settings, publisher, llm=llm, http=FakeHttp(), fh=fake_fh, now=NOW, cfg=cfg)
    return edition, fake


def test_pipeline_end_to_end(tmp_path, settings, cfg, fake_fh):
    from stocknews.clock import build_context
    from stocknews.collect import collect_all

    # Build the same pool the pipeline will build, to script a valid answer with real IDs.
    _, pool, _ = collect_all(build_context(NOW), cfg, settings, fake_fh, None, FakeHttp())
    edition, fake = _run(tmp_path, settings, cfg, fake_fh, [text_response(good_analysis(cfg, pool))])

    assert edition["id"] == "2026-11-20-close" and edition["qa"]["status"] == "ok"
    assert [p["type"] for p in edition["pages"]] == PAGE_TYPES
    top = edition["pages"][0]["cards"]
    assert [c["slot"] for c in top] == ["lead", "alert", "mover"]
    assert top[0]["sources"][0]["url"].startswith("https://news.example/")
    assert top[2]["ticker"]["symbol"] == "NVDA"
    assert len(edition["strip"]["tiles"]) == 8
    watch = edition["pages"][2]["cards"]
    assert [c["symbol"] for c in watch] == ["AAPL", "MSFT", "NVDA", "AVGO"]
    nvda = next(c for c in watch if c["symbol"] == "NVDA")
    assert nvda["earnings"].startswith("Earnings: ")
    assert {s["name"] for s in edition["footer"]["sources"]} >= {"Reuters", "Bloomberg"}
    assert edition["qa"]["usage"]["calls"] == 1

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["latest"] == edition["id"]
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
    assert edition["pages"][0]["cards"][2]["ticker"] is not None
    assert "AI SUMMARY UNAVAILABLE" in edition["intro"]["stamp"]
    assert json.loads((tmp_path / "manifest.json").read_text())["editions"][0]["status"] == "fallback"


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
    assert urls and all(u.startswith("https://example.com/demo/") for u in urls)


def test_model_label():
    assert model_label("global.anthropic.claude-sonnet-5-5") == "Claude Sonnet 5.5"
    assert model_label("us.anthropic.claude-haiku-5-5") == "Claude Haiku 5.5"
    assert model_label("anthropic.claude-opus-5") == "Claude Opus 5"


def test_lambda_handler(monkeypatch):
    seen = {}

    def fake_run(settings, publisher, **kw):
        seen.update(kw, bucket=publisher.bucket, model=settings.model_id, key=settings.finnhub_api_key)
        return {"id": "2026-11-20-close", "file": "editions/a.json", "qa": {"status": "ok", "evidenceCount": 9}}

    monkeypatch.setattr(handler, "run_edition", fake_run)
    monkeypatch.setenv("SITE_BUCKET", "my-bucket")
    monkeypatch.setenv("MODEL_ID", "us.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    class Ctx:
        def get_remaining_time_in_millis(self):
            return 600000

    out = handler.lambda_handler({"slot": "midday"}, Ctx())
    assert out == {"edition": "2026-11-20-close", "file": "editions/a.json", "status": "ok", "evidence": 9,
                   "toolCalls": None, "repairs": None, "estimatedCostUsd": None}
    assert seen["slot"] == "midday" and seen["use_llm"] is True and seen["bucket"] == "my-bucket"
    assert seen["model"] == "us.anthropic.claude-haiku-5-5" and seen["key"] == "k" and seen["deadline"]


def test_pipeline_degraded_when_repairs_run_out(tmp_path, settings, cfg, fake_fh):
    from stocknews.clock import build_context
    from stocknews.collect import collect_all

    _, pool, _ = collect_all(build_context(NOW), cfg, settings, fake_fh, None, FakeHttp())
    bad = good_analysis(cfg, pool)
    bad["watchlist"] = bad["watchlist"][:-1]  # AVGO note keeps missing
    edition, fake = _run(tmp_path, settings, cfg, fake_fh, [text_response(bad)] * 3)
    assert len(fake.requests) == 3 and edition["qa"]["status"] == "degraded"
    assert edition["qa"]["repairs"] == 2
    avgo = next(c for c in edition["pages"][2]["cards"] if c["symbol"] == "AVGO")
    assert avgo["sentiment"] is None and avgo["summary"].startswith("No validated AI note")
