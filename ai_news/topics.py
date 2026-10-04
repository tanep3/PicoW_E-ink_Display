"""Read and save the selected topic ID in the root TOML config."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

from .archive import atomic_write
from .retry_config import load_retry_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config"


def topic_snapshot(path: Path = CONFIG_PATH) -> dict:
    config = load_retry_config(path)
    available = {topic.id for topic in config.topics}
    return {
        "selected_topic_id": config.selected_topic_id if config.selected_topic_id in available else None,
        "selection_valid": config.selected_topic_id in available,
        "topics": [{"id": topic.id, "label": topic.label, "prompt": topic.prompt}
                   for topic in config.topics],
    }


def save_selected_topic(topic_id: str, path: Path = CONFIG_PATH) -> str:
    """Atomically change only selected_topic_id; keep topic text and retries intact."""
    source = path if path.exists() else path.parent / "config.sample"
    before = tomllib.loads(source.read_text(encoding="utf-8"))
    config = load_retry_config(source)
    if topic_id not in {topic.id for topic in config.topics}:
        raise ValueError("topic ID is not configured")
    lines = source.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "[news]"), None)
    if start is None:
        lines.extend(["", "[news]"])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines))
                if re.fullmatch(r"\s*\[+[^]]+\]+\s*", lines[i])), len(lines))
    indices = [i for i in range(start + 1, end)
               if re.match(r"\s*selected_topic_id\s*=", lines[i])]
    replacement = 'selected_topic_id = "' + topic_id + '"'
    if indices:
        lines[indices[0]] = replacement
    else:
        lines.insert(start + 1, replacement)
    updated = "\n".join(lines) + "\n"
    after = tomllib.loads(updated)
    old_news = {k: v for k, v in before.get("news", {}).items() if k != "selected_topic_id"}
    new_news = {k: v for k, v in after["news"].items() if k != "selected_topic_id"}
    if (old_news != new_news or before.get("image", {}) != after.get("image", {})
            or after["news"]["selected_topic_id"] != topic_id):
        raise ValueError("config changed unexpectedly")
    atomic_write(path, updated.encode("utf-8"))
    return topic_id
