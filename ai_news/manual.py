"""Asynchronous, process-isolated manual generation with a small public status."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from .archive import Archive, atomic_write, json_bytes, utcnow
from .generator import CodexImageBackend, CommandImageBackend, run_once
from .retry_config import load_retry_config
from .topics import CONFIG_PATH

STALE_SECONDS = 35 * 60


class ManualManager:
    def __init__(self, archive: Archive, config_path: Path = CONFIG_PATH):
        self.archive = archive
        self.config_path = config_path
        self.path = archive.root / "manual_status.json"
        self.mutex = threading.RLock()

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
            return {"state": "running", "run_id": value["run_id"]}
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
        return {key: value[key] for key in ("state", "run_id", "frame_id") if key in value}

    def start(self) -> tuple[dict, bool]:
        with self.mutex:
            current = self.status()
            if current["state"] == "running":
                return current, False
            if current["state"] in ("invalid_config", "invalid_topic", "invalid_style"):
                return current, False
            try:
                with self.archive.lock(blocking=False):
                    pass
            except BlockingIOError:
                return {"state": "busy"}, False
            run_id = secrets.token_hex(12)
            value = {"state": "running", "run_id": run_id, "started_at": utcnow()}
            atomic_write(self.path, json_bytes(value))
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "ai_news.manual", str(self.archive.root),
                     str(self.config_path), run_id],
                    cwd=Path(__file__).resolve().parents[1],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                self.finish(run_id, "failed")
                return self.status(), False
            return {"state": "running", "run_id": run_id}, True

    def finish(self, run_id: str, state: str, frame_id: str | None = None) -> None:
        with self.mutex:
            current = self._read()
            if current.get("run_id") != run_id:
                return
            value = {"state": state, "run_id": run_id, "completed_at": utcnow()}
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
    before = manager.archive.latest()
    command = os.environ.get("AI_NEWS_IMAGE_COMMAND", "codex")
    backend = CodexImageBackend() if command == "codex" else CommandImageBackend(command)
    try:
        result = run_once(root, backend, manual=True, config=load_retry_config(config_path))
        after = manager.archive.latest()
        changed = after is not None and (before is None or
                                        after.get("publish_seq") != before.get("publish_seq"))
        manager.finish(run_id, "succeeded" if result == "published" and changed else "unchanged",
                       after.get("frame_id") if after else None)
    except BlockingIOError:
        manager.finish(run_id, "busy")
    except Exception:
        manager.finish(run_id, "failed")


if __name__ == "__main__":
    main()
