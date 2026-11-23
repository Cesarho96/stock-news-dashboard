"""Tiny storage helpers: JSON documents in S3 (ops bucket) or on local disk.

Used for the company-profile cache, run records and eval datasets/reports.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _s3():
    import boto3

    return boto3.client("s3")


def split_s3(uri: str) -> tuple[str, str]:
    """'s3://bucket/key/path' -> ('bucket', 'key/path')."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def dumps(doc: Any, gz: bool = False) -> bytes:
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return gzip.compress(raw) if gz else raw


def loads(raw: bytes) -> Any:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def read_json(location: str, s3=None) -> Any:
    """Read JSON (optionally gzipped) from an s3:// URI or a local path."""
    if location.startswith("s3://"):
        bucket, key = split_s3(location)
        body = (s3 or _s3()).get_object(Bucket=bucket, Key=key)["Body"].read()
        return loads(body)
    return loads(Path(location).read_bytes())


def write_json(location: str, doc: Any, s3=None, cache_control: str | None = None) -> None:
    gz = location.endswith(".gz")
    body = dumps(doc, gz=gz)
    if location.startswith("s3://"):
        bucket, key = split_s3(location)
        extra = {"CacheControl": cache_control} if cache_control else {}
        content_type = "application/gzip" if gz else "application/json"
        (s3 or _s3()).put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, **extra)
        return
    path = Path(location)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class JsonDoc:
    """One JSON document that may live in S3 or on disk; failures are logged, never raised."""

    def __init__(self, location: str | None, s3=None):
        self.location = location
        self._s3 = s3

    def load(self) -> dict | None:
        if not self.location:
            return None
        try:
            doc = read_json(self.location, self._s3)
            return doc if isinstance(doc, dict) else None
        except Exception as exc:  # missing on first use, or unreadable
            log.info("cache %s not loaded: %s", self.location, exc)
            return None

    def save(self, doc: dict) -> None:
        if not self.location:
            return
        try:
            write_json(self.location, doc, self._s3)
        except Exception as exc:
            log.warning("cache %s not saved: %s", self.location, exc)
