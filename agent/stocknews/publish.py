"""Publish editions: data/editions/<id>-<time>.json + data/manifest.json.

Edition files are immutable (unique name per run), so CloudFront can cache them for a
year; only the small manifest is re-read every minute.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

EDITION_CACHE = "public, max-age=31536000, immutable"
MANIFEST_CACHE = "public, max-age=60"


def edition_file(edition: dict) -> str:
    stamp = edition["generatedAt"].replace("-", "").replace(":", "").replace("Z", "")[-6:]
    return f"editions/{edition['id']}-{stamp}.json"


def update_manifest(manifest: dict | None, edition: dict, max_editions: int = 120) -> dict:
    manifest = dict(manifest or {})
    entries = [e for e in manifest.get("editions", []) if isinstance(e, dict) and e.get("id") != edition["id"]]
    entries.append({
        "id": edition["id"],
        "file": edition["file"],
        "date": edition["date"],
        "slot": edition["slot"],
        "label": edition.get("timelineLabel", edition["id"]),
        "generatedAt": edition["generatedAt"],
        "status": (edition.get("qa") or {}).get("status", "ok"),
        "demo": bool(edition.get("demo")),
    })
    entries.sort(key=lambda e: e.get("generatedAt", ""), reverse=True)
    entries = entries[:max_editions]
    return {
        "schemaVersion": 1,
        "latest": entries[0]["id"] if entries else None,
        "updatedAt": edition["generatedAt"],
        "editions": entries,
    }


def _dumps(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class LocalPublisher:
    """Writes into a folder laid out like the S3 bucket (default: web/data)."""

    def __init__(self, data_dir: Path, max_editions: int = 120):
        self.data_dir = Path(data_dir)
        self.max_editions = max_editions

    def publish(self, edition: dict) -> dict:
        edition["file"] = edition_file(edition)
        target = self.data_dir / edition["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_dumps(edition))
        manifest_path = self.data_dir / "manifest.json"
        current = None
        if manifest_path.exists():
            try:
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("manifest.json was unreadable; starting a new one")
        manifest = update_manifest(current, edition, self.max_editions)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("published %s -> %s", edition["id"], target)
        return manifest


class S3Publisher:
    def __init__(self, bucket: str, s3_client=None, prefix: str = "data/", max_editions: int = 120):
        if not bucket:
            raise ValueError("SITE_BUCKET is not set")
        self.bucket = bucket
        self.prefix = prefix
        self.max_editions = max_editions
        self._s3 = s3_client

    @property
    def s3(self):
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def _read_manifest(self) -> dict | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.prefix + "manifest.json")
        except Exception as exc:  # NoSuchKey on the first run
            code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        try:
            return json.loads(obj["Body"].read())
        except json.JSONDecodeError:
            log.warning("manifest.json in S3 was unreadable; starting a new one")
            return None

    def publish(self, edition: dict) -> dict:
        edition["file"] = edition_file(edition)
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + edition["file"], Body=_dumps(edition),
                           ContentType="application/json; charset=utf-8", CacheControl=EDITION_CACHE)
        manifest = update_manifest(self._read_manifest(), edition, self.max_editions)
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + "manifest.json", Body=_dumps(manifest),
                           ContentType="application/json; charset=utf-8", CacheControl=MANIFEST_CACHE)
        log.info("published %s to s3://%s/%s%s", edition["id"], self.bucket, self.prefix, edition["file"])
        return manifest
