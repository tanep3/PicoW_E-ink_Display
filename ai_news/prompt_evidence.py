"""Private, durable evidence of the exact prompt passed to each Codex exec call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .archive import atomic_write


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PromptTarget:
    root: Path
    slot: int
    stage: str
    attempt: int


class PromptEvidence:
    """Prepare before spawn; finalize after output validation.

    A remaining ``prepared`` record means that process launch or outcome is unknown.
    It must never be presented as proof that Codex ran successfully.
    """

    def __init__(self, target: PromptTarget, command: list[str]):
        if (type(target.slot) is not int or target.stage not in ("news", "image")
                or type(target.attempt) is not int or target.attempt < 1):
            raise ValueError("invalid prompt evidence key")
        if not command or not all(isinstance(item, str) for item in command):
            raise ValueError("invalid Codex command")
        if command.count("--model") != 1 or command.index("--model") + 1 >= len(command) - 1:
            raise ValueError("Codex model missing")
        prompt = command[-1]
        if not prompt:
            raise ValueError("Codex prompt missing")
        self.path = Path(target.root) / "prompt_evidence" / str(target.slot) / (
            f"{target.stage}-{target.attempt}.json")
        self.record = {
            "schema_version": 1,
            "slot": target.slot,
            "stage": target.stage,
            "attempt": target.attempt,
            "model": command[command.index("--model") + 1],
            "argv_without_prompt": command[:-1],
            "submitted_prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prepared_at": _utcnow(),
            "status": "prepared",
            "cli_returncode": None,
            "artifact_kind": None,
            "artifact_sha256": None,
            "error_type": None,
            "finished_at": None,
        }
        self._artifact: tuple[str, str] | None = None

    def _write(self):
        atomic_write(self.path, (json.dumps(self.record, ensure_ascii=False, sort_keys=True)
                                 + "\n").encode("utf-8"))

    def __enter__(self):
        private_root = self.path.parent.parent
        for directory in (private_root, self.path.parent):
            if directory.is_symlink():
                raise ValueError("prompt evidence directory is a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError("prompt evidence for this attempt already exists")
        self._write()
        return self

    def exited(self, returncode: int):
        self.record["cli_returncode"] = returncode

    def accepted(self, kind: str, artifact: bytes):
        if kind not in ("news_json", "image_png") or not isinstance(artifact, bytes):
            raise ValueError("invalid prompt evidence artifact")
        self._artifact = (kind, hashlib.sha256(artifact).hexdigest())

    def __exit__(self, error_type, error, traceback):
        if error_type is None and self._artifact is not None:
            self.record["status"] = "artifact_returned"
            self.record["artifact_kind"], self.record["artifact_sha256"] = self._artifact
        elif error_type is not None:
            if issubclass(error_type, subprocess.TimeoutExpired):
                self.record["status"] = "timeout"
            elif issubclass(error_type, OSError):
                self.record["status"] = "launch_error"
            elif not issubclass(error_type, Exception):
                self.record["status"] = "interrupted"
            else:
                self.record["status"] = "failed"
            self.record["error_type"] = error_type.__name__
        else:
            self.record["status"] = "failed"
        self.record["finished_at"] = _utcnow()
        self._write()
        return False
