"""Immutable frames, durable job ledger, and atomic latest pointer."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .frame import ADAPTER_VERSION, FORMAT_ID, WIRE_LENGTH, png_to_wire, sha256

FRAME_ID = re.compile(r"^[0-9a-f]{64}-a1$")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def slot_storage_name(slot: int) -> str:
    """Keep manual (negative) job slots usable as plain shell path arguments."""
    if type(slot) is not int:
        raise ValueError("invalid job slot")
    return "slot" + str(slot) if slot < 0 else str(slot)


class Archive:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "archive").mkdir(exist_ok=True)
        (self.root / "published").mkdir(exist_ok=True)
        (self.root / "news").mkdir(exist_ok=True)
        (self.root / "custom").mkdir(exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  slot INTEGER PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen (
                  dedup_key TEXT PRIMARY KEY, frame_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS published_frames (
                  frame_id TEXT PRIMARY KEY, published_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stage_attempts (
                  slot INTEGER NOT NULL, stage TEXT NOT NULL, attempts INTEGER NOT NULL,
                  last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY(slot, stage));
                CREATE TABLE IF NOT EXISTS job_selections (
                  slot INTEGER PRIMARY KEY, topic_id TEXT NOT NULL, topic_label TEXT NOT NULL,
                  topic_prompt TEXT NOT NULL, style_id TEXT NOT NULL,
                  style_label TEXT NOT NULL, style_prompt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telemetry (
                  device_id TEXT NOT NULL, request_id TEXT NOT NULL, received_at TEXT NOT NULL,
                  payload TEXT NOT NULL, PRIMARY KEY(device_id, request_id));
            """)
        self.reconcile_latest()

    def reconcile_latest(self):
        """Recover a pointer switched just before a process/DB crash."""
        latest = self.latest()
        if not latest:
            return
        frame_id = latest.get("frame_id")
        if not isinstance(frame_id, str) or not FRAME_ID.fullmatch(frame_id):
            raise ValueError("latest frame ID invalid")
        record = json.loads(self.frame_path(frame_id, "json").read_text(encoding="utf-8"))
        if record.get("frame_id") != frame_id:
            raise ValueError("latest archive mismatch")
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO published_frames VALUES(?,?)",
                         (frame_id, latest["published_at"]))
            event_key = record.get("metadata", {}).get("event_key")
            if isinstance(event_key, str) and event_key:
                conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (event_key, frame_id))

    def published_frame_ids(self) -> set[str]:
        with self.connect() as conn:
            return {row[0] for row in conn.execute("SELECT frame_id FROM published_frames")}

    def recent_source_urls(self, reference: datetime) -> set[str]:
        """Sources of frames successfully published in the preceding rolling 24h."""
        self.reconcile_latest()
        cutoff = reference.astimezone(timezone.utc) - timedelta(hours=24)
        with self.connect() as conn:
            publications = conn.execute(
                "SELECT frame_id,published_at FROM published_frames").fetchall()
        urls = set()
        for frame_id, published_at in publications:
            when = datetime.fromisoformat(published_at)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= cutoff:
                continue
            record = json.loads(self.frame_path(frame_id, "json").read_text(encoding="utf-8"))
            sources = record.get("metadata", {}).get("source_urls", [])
            if isinstance(sources, list):
                urls.update(url for url in sources if isinstance(url, str))
        return urls

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def lock(self, *, blocking=True):
        with open(self.root / ".generator.lock", "a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def job_exists(self, slot: int) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE slot=?", (slot,)).fetchone() is not None

    def next_manual_slot(self) -> int:
        """Allocate an unused negative slot while holding the generator lock."""
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(slot) FROM jobs WHERE slot < 0").fetchone()
        return (row[0] or 0) - 1

    def begin(self, slot: int, selection: dict | None = None) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE slot=?", (slot,)).fetchone()
            if row is not None and row[0] not in (
                    "COLLECTING", "SELECTED", "GENERATING", "VALIDATED"):
                return False
            if selection is not None:
                fields = ("topic_id", "topic_label", "topic_prompt", "style_id",
                          "style_label", "style_prompt")
                if set(selection) != set(fields) or any(
                        not isinstance(selection[field], str) or not selection[field]
                        for field in fields):
                    raise ValueError("invalid job selection snapshot")
                conn.execute("""INSERT OR IGNORE INTO job_selections
                    (slot,topic_id,topic_label,topic_prompt,style_id,style_label,style_prompt)
                    VALUES(?,?,?,?,?,?,?)""", (slot,) + tuple(selection[field] for field in fields))
            cursor = conn.execute("INSERT OR IGNORE INTO jobs(slot,state,updated_at) VALUES(?,?,?)",
                                  (slot, "COLLECTING", utcnow()))
            if cursor.rowcount == 1:
                return True
            # A process interrupted mid-job can resume; completed jobs stay final.
            return row is not None

    def job_selection(self, slot: int) -> dict | None:
        fields = ("topic_id", "topic_label", "topic_prompt", "style_id",
                  "style_label", "style_prompt")
        with self.connect() as conn:
            row = conn.execute("SELECT " + ",".join(fields) +
                               " FROM job_selections WHERE slot=?", (slot,)).fetchone()
        return dict(zip(fields, row)) if row else None

    def save_news(self, slot: int, news: dict) -> None:
        path = self.root / "news" / (slot_storage_name(slot) + ".json")
        legacy = self.root / "news" / (str(slot) + ".json")
        data = json_bytes(news)
        if legacy != path and legacy.exists() and legacy.read_bytes() != data:
            raise ValueError("saved news differs from current selection")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("saved news differs from current selection")
            if legacy != path:
                legacy.unlink(missing_ok=True)
            return
        if legacy != path and legacy.exists():
            os.replace(legacy, path)
            return
        atomic_write(path, data)

    def load_news(self, slot: int) -> dict | None:
        path = self.root / "news" / (slot_storage_name(slot) + ".json")
        if not path.exists():
            path = self.root / "news" / (str(slot) + ".json")
        return json.loads(path.read_bytes()) if path.exists() else None

    def save_custom(self, slot: int, text: str) -> None:
        path = self.root / "custom" / ("slot" + str(slot) + ".json")
        legacy = self.root / "custom" / (str(slot) + ".json")
        data = json_bytes({"custom_text": text})
        for existing in (path, legacy):
            if existing.exists() and existing.read_bytes() != data:
                raise ValueError("saved custom text differs from current input")
        if path.exists():
            legacy.unlink(missing_ok=True)
            return
        if legacy.exists():
            os.replace(legacy, path)
            return
        atomic_write(path, data)

    def load_custom(self, slot: int) -> str | None:
        path = self.root / "custom" / ("slot" + str(slot) + ".json")
        if not path.exists():
            path = self.root / "custom" / (str(slot) + ".json")
        return json.loads(path.read_bytes())["custom_text"] if path.exists() else None

    def attempt_info(self, slot: int, stage: str) -> tuple[int, str]:
        if stage not in ("news", "image"):
            raise ValueError("unknown stage")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts,last_error FROM stage_attempts WHERE slot=? AND stage=?",
                (slot, stage)).fetchone()
            return (row[0], row[1]) if row else (0, "")

    def begin_attempt(self, slot: int, stage: str) -> None:
        if stage not in ("news", "image"):
            raise ValueError("unknown stage")
        with self.connect() as conn:
            conn.execute("""INSERT INTO stage_attempts(slot,stage,attempts) VALUES(?,?,1)
                ON CONFLICT(slot,stage) DO UPDATE SET attempts=attempts+1""", (slot, stage))

    def attempt_failed(self, slot: int, stage: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE stage_attempts SET last_error=? WHERE slot=? AND stage=?",
                         (reason[:200], slot, stage))

    def latest_is_job(self, slot: int) -> bool:
        latest = self.latest()
        if not latest:
            return False
        record = json.loads(self.frame_path(latest["frame_id"], "json").read_text())
        return record.get("metadata", {}).get("job_slot") == slot

    def state(self, slot: int, state: str, reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET state=?,reason=?,updated_at=? WHERE slot=?",
                         (state, reason[:500], utcnow(), slot))

    def seen(self, key: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM seen WHERE dedup_key=?", (key,)).fetchone() is not None

    def latest(self) -> dict | None:
        path = self.root / "published/latest.json"
        return json.loads(path.read_bytes()) if path.exists() else None

    def frame_path(self, frame_id: str, suffix: str) -> Path:
        if not FRAME_ID.fullmatch(frame_id) or suffix not in ("raw", "png", "json"):
            raise ValueError("invalid frame path")
        return self.root / "archive" / frame_id / ("frame." + suffix)

    def publish(self, png: bytes, metadata: dict, dedup_key: str | None = None) -> dict:
        self.reconcile_latest()
        wire = png_to_wire(png)
        png_hash, wire_hash = sha256(png), sha256(wire)
        frame_id = png_hash + "-a1"
        directory = self.root / "archive" / frame_id
        directory.mkdir(exist_ok=True)
        old = self.latest()
        if old and old["frame_id"] == frame_id:
            return old
        created = utcnow()
        frame_manifest = {
            "schema_version": 1, "frame_id": frame_id, "created_at": created,
            "png_sha256": png_hash, "wire_sha256": wire_hash,
            "format_id": FORMAT_ID, "adapter_version": ADAPTER_VERSION,
            "length": WIRE_LENGTH, "raw_path": f"/v1/frames/{frame_id}.raw",
            "metadata": metadata,
        }
        for suffix, data in (("png", png), ("raw", wire), ("json", json_bytes(frame_manifest))):
            path = self.frame_path(frame_id, suffix)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError("immutable frame collision")
            else:
                atomic_write(path, data)
        latest = {key: frame_manifest[key] for key in frame_manifest if key != "metadata"}
        latest.update(publish_seq=(old["publish_seq"] + 1 if old else 1),
                      published_at=utcnow())
        # Read back immutable bytes before exposing the new pointer.
        if sha256(self.frame_path(frame_id, "raw").read_bytes()) != wire_hash:
            raise ValueError("archived RAW hash mismatch")
        atomic_write(self.root / "published/latest.json", json_bytes(latest))
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO published_frames VALUES(?,?)",
                         (frame_id, latest["published_at"]))
            if dedup_key:
                conn.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (dedup_key, frame_id))
        return latest
