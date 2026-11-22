"""Minimal RSS 2.0 / Atom parser (stdlib only, DTDs rejected)."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


class FeedError(ValueError):
    pass


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _TAGS.sub(" ", value)
    text = html.unescape(text)
    return _SPACES.sub(" ", text).strip()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)  # RFC 822 (RSS pubDate)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        except ValueError:
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(el: ET.Element | None) -> str:
    return (el.text or "") if el is not None else ""


def parse_feed(raw: bytes) -> list[dict]:
    """Return items as dicts: title, url, summary, published (aware datetime or None)."""
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw.lower():
        raise FeedError("feeds with DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedError(f"invalid XML: {exc}") from None

    items: list[dict] = []
    if root.tag == f"{ATOM}feed":
        for entry in root.findall(f"{ATOM}entry"):
            link = ""
            for ln in entry.findall(f"{ATOM}link"):
                if ln.get("rel") in (None, "alternate") and ln.get("href"):
                    link = ln.get("href", "")
                    break
            summary = _text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content"))
            published = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
            items.append({
                "title": clean_text(_text(entry.find(f"{ATOM}title"))),
                "url": link.strip(),
                "summary": clean_text(summary),
                "published": parse_time(published),
            })
        return items

    channel = root.find("channel")
    nodes = channel.findall("item") if channel is not None else root.findall(".//item")
    for item in nodes:
        link = _text(item.find("link")).strip()
        if not link:
            guid = item.find("guid")
            if guid is not None and (guid.get("isPermaLink", "true") == "true") and _text(guid).startswith("http"):
                link = _text(guid).strip()
        summary = _text(item.find("description")) or _text(item.find(f"{CONTENT}encoded"))
        published = _text(item.find("pubDate")) or _text(item.find(f"{DC}date"))
        items.append({
            "title": clean_text(_text(item.find("title"))),
            "url": link,
            "summary": clean_text(summary),
            "published": parse_time(published),
        })
    return items
