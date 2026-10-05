"""Read-only gallery projections from immutable archive metadata."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

JST = timezone(timedelta(hours=9))
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DAY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$")
DEMO_ID = re.compile(r"^demo-[0-9a-f]{16}-[0-3]$")


def parse_month(value):
    if not isinstance(value, str) or not MONTH.fullmatch(value):
        raise ValueError("month must be YYYY-MM")
    datetime.strptime(value + "-01", "%Y-%m-%d")
    return value


def parse_day(value):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _safe_url(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    return value if parts.scheme in ("http", "https") and parts.hostname else None


def entries(archive):
    for frame_id in archive.published_frame_ids():
        try:
            manifest_path = archive.frame_path(frame_id, "json")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload["frame_id"] != frame_id:
                continue
            if not archive.frame_path(frame_id, "png").is_file():
                continue
            yield _entry(payload, "/gallery/image/" + frame_id, False)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            continue
    for manifest_path in (archive.root / "demo").glob("*/frame.json"):
        try:
            frame_id = manifest_path.parent.name
            if not DEMO_ID.fullmatch(frame_id):
                continue
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.get("frame_id") != frame_id or not (manifest_path.parent / "frame.png").is_file():
                continue
            yield _entry(payload, "/gallery/demo/" + frame_id, True)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            continue


def _entry(payload, image_url, is_demo):
    created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("timestamp without zone")
    meta = payload.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    sources = meta.get("source_urls") or []
    if not isinstance(sources, list):
        sources = []
    kind = ("demo" if is_demo else meta.get("input_kind")
            if meta.get("input_kind") in ("news", "custom") else
            "news" if sources else "legacy")
    custom_text = meta.get("custom_text")
    return {
        "frame_id": payload["frame_id"],
        "created_at": created.isoformat(),
        "jst_at": created.astimezone(JST).isoformat(),
        "day": created.astimezone(JST).date().isoformat(),
        "title": str(meta.get("title") or meta.get("fact_summary") or "見出し未登録")[:200],
        "source_urls": [url for item in sources if (url := _safe_url(item))][:10],
        "input_kind": kind,
        "custom_text": custom_text if kind == "custom" and isinstance(custom_text, str) else None,
        "used_topic_label": meta.get("topic_label") if isinstance(meta.get("topic_label"), str) else None,
        "used_topic_prompt": meta.get("topic_prompt") if kind == "news" and isinstance(meta.get("topic_prompt"), str) else None,
        "used_style_label": meta.get("style_label") if isinstance(meta.get("style_label"), str) else None,
        "used_style_prompt": meta.get("style_prompt") if isinstance(meta.get("style_prompt"), str) else None,
        "image_url": image_url,
        "is_demo": is_demo,
    }


def month_view(archive, month):
    month = parse_month(month)
    days = {}
    for item in entries(archive):
        if item["day"][:7] != month:
            continue
        entry = days.setdefault(item["day"], {"date": item["day"], "count": 0,
                                                "thumbnail_url": None, "title": None,
                                                "latest_at": ""})
        entry["count"] += 1
        if item["jst_at"] > entry["latest_at"]:
            entry.update(thumbnail_url=item["image_url"], title=item["title"],
                         latest_at=item["jst_at"])
    return {"month": month, "days": [days[key] for key in sorted(days)]}


def day_view(archive, day):
    day = parse_day(day)
    images = sorted((item for item in entries(archive) if item["day"] == day),
                    key=lambda item: (item["jst_at"], item["frame_id"]))
    return {"date": day, "timezone": "Asia/Tokyo", "images": images}
