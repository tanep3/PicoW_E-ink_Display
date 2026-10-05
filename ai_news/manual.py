"""Asynchronous, process-isolated manual generation with a small public status."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from .archive import Archive, atomic_write, json_bytes, utcnow
from .generator import (CUSTOM_TOPIC_ID, CodexImageBackend, CommandImageBackend, run_once,
                        validate_custom_text)
from .retry_config import RetryConfig, RetryPolicy, load_retry_config
from .topics import CONFIG_PATH

STALE_SECONDS = 35 * 60


class ManualManager:
    def __init__(self, archive: Archive, config_path: Path = CONFIG_PATH):
        self.archive = archive
        self.config_path = config_path
        self.path = archive.root / "manual_status.json"
        self.last_custom_path = archive.root / "last_custom.json"
        self.requests = archive.root / "manual_requests"
        self.mutex = threading.RLock()
        self.child = None

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"state": "idle"}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def status(self) -> dict:
        value = self._read()
        if value.get("state") == "running":
            started = datetime.fromisoformat(value["started_at"])
            if (datetime.now(timezone.utc) - started).total_seconds() > STALE_SECONDS:
                return {"state": "failed", "run_id": value["run_id"]}
            return {"state": "running", "run_id": value["run_id"],
                    "kind": value.get("kind", "news")}
        try:
            config = load_retry_config(self.config_path)
        except (ValueError, OSError):
            return {"state": "invalid_config"}
        try:
            config.topic_prompt
        except ValueError:
            return {"state": "invalid_topic"}
        try:
            config.style
        except ValueError:
            return {"state": "invalid_style"}
        return {key: value[key] for key in ("state", "run_id", "frame_id", "kind")
                if key in value}

    def last_custom(self) -> dict:
        if not self.last_custom_path.is_file():
            return {"custom_text": ""}
        value = json.loads(self.last_custom_path.read_text(encoding="utf-8"))
        return {"custom_text": validate_custom_text(value["custom_text"])}

    def start(self, custom_text: str | None = None) -> tuple[dict, bool]:
        if custom_text is not None:
            validate_custom_text(custom_text)
        with self.mutex:
            current = self.status()
            if current["state"] == "running":
                return current, False
            if current["state"] in ("invalid_config", "invalid_style") or (
                    current["state"] == "invalid_topic" and custom_text is None):
                return current, False
            config = load_retry_config(self.config_path)
            settings = self.archive.operational_settings(config)["values"]
            style = config.style
            if custom_text is None:
                topic = config.topic
                selection = {"topic_id": topic.id, "topic_label": topic.label,
                             "topic_prompt": topic.prompt}
            else:
                selection = {"topic_id": CUSTOM_TOPIC_ID, "topic_label": "カスタム文章",
                             "topic_prompt": "ユーザー入力の文章から作画"}
            selection.update(style_id=style.id, style_label=style.label,
                             style_prompt=style.prompt,
                             news_model=settings["news_model"],
                             image_model=settings["image_model"],
                             resize_method="dpid",
                             dpid_lambda=settings["dpid_lambda"],
                             threshold=settings["threshold"])
            try:
                with self.archive.lock(blocking=False):
                    pass
            except BlockingIOError:
                return {"state": "busy"}, False
            run_id = secrets.token_hex(12)
            kind = "custom" if custom_text is not None else "news"
            request = {"selection": selection, "kind": kind,
                       "news_retry": asdict(config.news),
                       "image_retry": asdict(config.image)}
            if custom_text is not None:
                request["custom_text"] = custom_text
            request_path = self.requests / (run_id + ".json")
            value = {"state": "running", "run_id": run_id,
                     "kind": kind, "started_at": utcnow()}
            previous_custom = (self.last_custom_path.read_bytes()
                               if custom_text is not None and self.last_custom_path.is_file()
                               else None)
            try:
                atomic_write(request_path, json_bytes(request))
                if custom_text is not None:
                    atomic_write(self.last_custom_path,
                                 json_bytes({"custom_text": custom_text}))
                atomic_write(self.path, json_bytes(value))
                self.child = subprocess.Popen(
                    [sys.executable, "-m", "ai_news.manual", str(self.archive.root),
                     str(self.config_path), run_id],
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                request_path.unlink(missing_ok=True)
                if custom_text is not None:
                    if previous_custom is None:
                        self.last_custom_path.unlink(missing_ok=True)
                    else:
                        atomic_write(self.last_custom_path, previous_custom)
                if self._read().get("run_id") == run_id:
                    self.finish(run_id, "failed")
                return {"state": "failed", "run_id": run_id, "kind": kind}, False
            return {"state": "running", "run_id": run_id, "kind": kind}, True

    def finish(self, run_id: str, state: str, frame_id: str | None = None) -> None:
        with self.mutex:
            current = self._read()
            if current.get("run_id") != run_id:
                return
            value = {"state": state, "run_id": run_id, "completed_at": utcnow()}
            if current.get("kind"):
                value["kind"] = current["kind"]
            if frame_id:
                value["frame_id"] = frame_id
            atomic_write(self.path, json_bytes(value))


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    root, config_path, run_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    manager = ManualManager(Archive(root), config_path)
    if manager._read().get("run_id") != run_id:
        return
    request_path = manager.requests / (run_id + ".json")
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        config = RetryConfig(news=RetryPolicy(**request["news_retry"]),
                             image=RetryPolicy(**request["image_retry"]))
        before = manager.archive.latest()
        command = os.environ.get("AI_NEWS_IMAGE_COMMAND", "codex")
        backend = CodexImageBackend() if command == "codex" else CommandImageBackend(command)
        result = run_once(root, backend, manual=True, config=config,
                          custom_text=request.get("custom_text"),
                          selection_override=request["selection"])
        after = manager.archive.latest()
        changed = after is not None and (before is None or
                                        after.get("publish_seq") != before.get("publish_seq"))
        manager.finish(run_id, "succeeded" if result == "published" and changed else "unchanged",
                       after.get("frame_id") if after else None)
    except BlockingIOError:
        manager.finish(run_id, "busy")
    except Exception:
        manager.finish(run_id, "failed")
    finally:
        request_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
