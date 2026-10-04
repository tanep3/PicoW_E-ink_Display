"""Read and save the selected topic ID in the root TOML config."""

from __future__ import annotations

from copy import deepcopy
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


def style_snapshot(path: Path = CONFIG_PATH) -> dict:
    config = load_retry_config(path)
    available = {style.id for style in config.styles}
    return {
        "selected_style_id": config.selected_style_id if config.selected_style_id in available else None,
        "selection_valid": config.selected_style_id in available,
        "styles": [{"id": style.id, "label": style.label, "prompt": style.prompt}
                   for style in config.styles],
    }


def _save_selection(ident: str, section: str, field: str, available: set[str],
                    path: Path) -> str:
    source = path if path.exists() else path.parent / "config.sample"
    before = tomllib.loads(source.read_text(encoding="utf-8"))
    if ident not in available:
        raise ValueError(section + " ID is not configured")
    lines = source.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "[" + section + "]"), None)
    if start is None:
        lines.extend(["", "[" + section + "]"])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines))
                if re.fullmatch(r"\s*\[+[^]]+\]+\s*", lines[i])), len(lines))
    indices = [i for i in range(start + 1, end)
               if re.match(r"\s*" + field + r"\s*=", lines[i])]
    replacement = field + ' = "' + ident + '"'
    if indices:
        lines[indices[0]] = replacement
    else:
        lines.insert(start + 1, replacement)
    updated = "\n".join(lines) + "\n"
    after = tomllib.loads(updated)
    expected = deepcopy(before)
    expected.setdefault(section, {})[field] = ident
    if after != expected:
        raise ValueError("config changed unexpectedly")
    atomic_write(path, updated.encode("utf-8"))
    return ident


def save_selected_topic(topic_id: str, path: Path = CONFIG_PATH) -> str:
    """Atomically change only the selected topic while preserving style settings."""
    source = path if path.exists() else path.parent / "config.sample"
    config = load_retry_config(source)
    return _save_selection(topic_id, "news", "selected_topic_id",
                           {topic.id for topic in config.topics}, path)


def save_selected_style(style_id: str, path: Path = CONFIG_PATH) -> str:
    """Atomically change only the selected style while preserving topic settings."""
    source = path if path.exists() else path.parent / "config.sample"
    config = load_retry_config(source)
    return _save_selection(style_id, "styles", "selected_style_id",
                           {style.id for style in config.styles}, path)
