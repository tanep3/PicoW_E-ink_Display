"""Durable, serial PUSH delivery to the Pico W; image publication stays independent."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
import tomllib

from .archive import Archive, FRAME_ID, atomic_write, utcnow
from .frame import FORMAT_ID, WIRE_LENGTH, png_to_wire, sha256
from .gallery import DEMO_ID
from .topics import CONFIG_PATH


REGISTRATION_ERROR = "push_registration_error.json"


def record_registration_error(archive: Archive, publication: dict) -> None:
    """Expose failed delivery registration without changing publication success."""
    atomic_write(archive.root / REGISTRATION_ERROR, json.dumps({
        "state": "failed", "error_code": "registration_failed",
        "source_id": publication["frame_id"], "frame_id": publication["frame_id"],
        "publish_seq": publication["publish_seq"], "attempts": 0,
        "created_at": utcnow(),
    }).encode())


@dataclass(frozen=True)
class PushConfig:
    host: str = "192.168.0.172"
    port: int = 16151
    retry_count: int = 2
    interval_seconds: int = 30
    attempt_timeout_seconds: int = 60
    deadline_seconds: int = 0


def load_push_config(path: Path = CONFIG_PATH) -> PushConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PushConfig()
    values = data.get("push", {})
    if not isinstance(values, dict) or set(values) - set(PushConfig.__dataclass_fields__):
        raise ValueError("invalid [push] section")
    defaults = PushConfig()
    result = {}
    for name in PushConfig.__dataclass_fields__:
        value = values.get(name, getattr(defaults, name))
        if name == "host":
            if value != "192.168.0.172":
                raise ValueError("Pico PUSH host must be the reserved 192.168.0.172")
        elif type(value) is not int or value < (1 if name in ("port", "attempt_timeout_seconds") else 0):
            raise ValueError("invalid push." + name)
        result[name] = value
    if result["port"] > 65535 or result["retry_count"] > 10:
        raise ValueError("push port or retry count out of range")
    return PushConfig(**result)


def _source_wire(archive: Archive, source_id: str) -> tuple[str, bytes]:
    if FRAME_ID.fullmatch(source_id):
        if source_id not in archive.published_frame_ids():
            raise ValueError("frame is not published")
        path = archive.frame_path(source_id, "raw")
        record = json.loads(archive.frame_path(source_id, "json").read_text(encoding="utf-8"))
        wire = path.read_bytes()
        if (record.get("frame_id") != source_id or record.get("format_id") != FORMAT_ID
                or record.get("length") != WIRE_LENGTH or len(wire) != WIRE_LENGTH
                or record.get("wire_sha256") != sha256(wire)):
            raise ValueError("stored frame failed integrity checks")
        return source_id, wire
    if DEMO_ID.fullmatch(source_id):
        folder = archive.root / "demo" / source_id
        record = json.loads((folder / "frame.json").read_text(encoding="utf-8"))
        if record.get("frame_id") != source_id:
            raise ValueError("demo frame mismatch")
        png = (folder / "frame.png").read_bytes()
        # Demo PNG is for viewing; validate and derive a real RAW without publishing it.
        wire = png_to_wire(png)
        return sha256(png) + "-a1", wire
    raise ValueError("unknown gallery frame")


class PushQueue:
    def __init__(self, archive: Archive):
        self.archive = archive
        with sqlite3.connect(archive.db) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS push_requests (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
                frame_id TEXT NOT NULL, wire_sha256 TEXT NOT NULL, wire BLOB NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                retry_after REAL NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def enqueue(self, source_id: str) -> dict:
        frame_id, wire = _source_wire(self.archive, source_id)
        now = utcnow()
        with sqlite3.connect(self.archive.db, timeout=5) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE push_requests SET state='superseded',updated_at=? WHERE state IN ('queued','sending')", (now,))
            previous = conn.execute("SELECT MAX(seq) FROM push_requests").fetchone()[0] or 0
            seq = max(time.time_ns() // 1000, previous + 1)
            conn.execute("""INSERT INTO push_requests
                (seq,source_id,frame_id,wire_sha256,wire,state,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?)""",
                (seq, source_id, frame_id, sha256(wire), wire, now, now))
        try:
            (self.archive.root / REGISTRATION_ERROR).unlink(missing_ok=True)
        except OSError:
            pass  # A stale marker is ignored once a newer queued request exists.
        return self.status(seq)

    def status(self, seq: int | None = None) -> dict:
        if seq is None:
            try:
                marker = json.loads((self.archive.root / REGISTRATION_ERROR).read_text(encoding="utf-8"))
                latest = self.archive.latest()
                if latest and marker.get("publish_seq") == latest.get("publish_seq"):
                    with sqlite3.connect(self.archive.db) as conn:
                        newer = conn.execute("SELECT 1 FROM push_requests WHERE created_at>=? LIMIT 1",
                                             (marker.get("created_at", ""),)).fetchone()
                    if not newer:
                        return marker
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        with sqlite3.connect(self.archive.db) as conn:
            if seq is None:
                row = conn.execute("""SELECT seq,source_id,frame_id,state,attempts,error_code FROM push_requests
                    ORDER BY seq DESC LIMIT 1""").fetchone()
            else:
                row = conn.execute("""SELECT seq,source_id,frame_id,state,attempts,error_code FROM push_requests
                    WHERE seq=?""", (seq,)).fetchone()
        if row is None:
            return {"state": "idle"}
        return dict(zip(("seq", "source_id", "frame_id", "state", "attempts", "error_code"), row))

    def pending(self):
        with sqlite3.connect(self.archive.db) as conn:
            return conn.execute("""SELECT seq,frame_id,wire_sha256,wire,attempts,retry_after FROM push_requests
                WHERE state IN ('queued','sending') ORDER BY seq DESC LIMIT 1""").fetchone()

    def is_latest(self, seq: int) -> bool:
        with sqlite3.connect(self.archive.db) as conn:
            row = conn.execute("SELECT MAX(seq) FROM push_requests").fetchone()
        return row[0] == seq

    def update(self, seq: int, state: str, *, attempt=False, error_code="", retry_after=0):
        if error_code not in ("", "connection", "timeout", "data_invalid", "panel_failed",
                              "unknown_result", "config_invalid"):
            raise ValueError("invalid PUSH error code")
        if state not in ("queued", "sending", "displayed", "failed", "superseded"):
            raise ValueError("invalid PUSH state")
        with sqlite3.connect(self.archive.db) as conn:
            conn.execute("""UPDATE push_requests SET state=?,attempts=attempts+?,error_code=?,retry_after=?,updated_at=? WHERE seq=?""",
                         (state, int(attempt), error_code, retry_after, utcnow(), seq))


class PushDeliveryError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def send_frame(host: str, port: int, seq: int, frame_id: str, digest: str,
               wire: bytes, timeout: int) -> None:
    """A deadline covering TCP connect, RAW transfer and final displayed ACK."""
    if len(wire) != WIRE_LENGTH or sha256(wire) != digest:
        raise PushDeliveryError("data_invalid", "local PUSH payload integrity failed")
    deadline = time.monotonic() + timeout
    packet = ("POST /v1/frame HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/octet-stream\r\n"
              "Content-Length: %d\r\nX-Push-Seq: %d\r\nX-Frame-ID: %s\r\nX-Wire-SHA256: %s\r\n"
              "X-Format-ID: %s\r\nConnection: close\r\n\r\n"
              % (host, port, WIRE_LENGTH, seq, frame_id, digest, FORMAT_ID)).encode() + wire
    def left():
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise socket.timeout("PUSH attempt deadline")
        return remain
    try:
        sock = socket.create_connection((host, port), timeout=left())
    except (OSError, TimeoutError) as exc:
        code = "timeout" if isinstance(exc, TimeoutError) else "connection"
        raise PushDeliveryError(code, str(exc)) from exc
    sent = 0
    try:
        while sent < len(packet):
            sock.settimeout(left())
            count = sock.send(packet[sent:])
            if count <= 0:
                raise OSError("PUSH send stopped")
            sent += count
        data = bytearray()
        while b"\r\n\r\n" not in data:
            sock.settimeout(left())
            part = sock.recv(512)
            if not part:
                raise OSError("PUSH ACK cut")
            data.extend(part)
            if len(data) > 2048:
                raise PushDeliveryError("unknown_result", "PUSH ACK oversized")
        head, body = bytes(data).split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        if lines[0] != b"HTTP/1.1 200 OK":
            if b" 400 " in lines[0] or b" 413 " in lines[0]:
                raise PushDeliveryError("data_invalid", "Pico rejected RAW data")
            if b" 503 " in lines[0]:
                raise PushDeliveryError("panel_failed", "Pico reported display failure")
            raise PushDeliveryError("unknown_result", "Pico did not confirm display")
        lengths = [value.strip() for name, value in
                   (line.split(b":", 1) for line in lines[1:] if b":" in line)
                   if name.lower() == b"content-length"]
        if len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > 256:
            raise PushDeliveryError("unknown_result", "PUSH ACK length invalid")
        size = int(lengths[0])
        while len(body) < size:
            sock.settimeout(left())
            part = sock.recv(size - len(body))
            if not part:
                raise OSError("PUSH ACK short")
            body += part
        if len(body) != size:
            raise PushDeliveryError("unknown_result", "PUSH ACK extra data")
        try:
            ack = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise PushDeliveryError("unknown_result", "PUSH ACK malformed") from exc
        if ack != {"seq": seq, "wire_sha256": digest, "state": "displayed"}:
            raise PushDeliveryError("unknown_result", "PUSH ACK mismatch")
    except PushDeliveryError:
        raise
    except (OSError, TimeoutError) as exc:
        # Once the complete body has left the host, a lost ACK cannot prove
        # whether the panel displayed the frame. Retry with the same ID.
        code = "unknown_result" if sent == len(packet) else (
            "timeout" if isinstance(exc, TimeoutError) else "connection")
        raise PushDeliveryError(code, str(exc)) from exc
    finally:
        sock.close()


class PushWorker:
    def __init__(self, queue: PushQueue, config_path: Path = CONFIG_PATH, transport=send_frame):
        self.queue = queue
        self.config_path = config_path
        self.transport = transport

    def process_one(self, sleep=time.sleep) -> bool:
        item = self.queue.pending()
        if item is None:
            return False
        seq, frame_id, digest, wire, attempts, retry_after = item
        if not self.queue.is_latest(seq):
            self.queue.update(seq, "superseded")
            return True
        try:
            policy = load_push_config(self.config_path)
        except (ValueError, OSError):
            self.queue.update(seq, "failed", error_code="config_invalid")
            return True
        if retry_after > time.time() and sleep(retry_after - time.time()):
            return True
        started = time.monotonic()
        while attempts < policy.retry_count + 1:
            if not self.queue.is_latest(seq):
                self.queue.update(seq, "superseded")
                return True
            if policy.deadline_seconds and time.monotonic() - started >= policy.deadline_seconds:
                break
            self.queue.update(seq, "sending", attempt=True, error_code="unknown_result")
            attempts += 1
            timeout = policy.attempt_timeout_seconds
            if policy.deadline_seconds:
                timeout = min(timeout, max(1, policy.deadline_seconds - int(time.monotonic() - started)))
            try:
                self.transport(policy.host, policy.port, seq, frame_id, digest, wire, timeout)
            except PushDeliveryError as exc:
                print("PUSH attempt failed:", seq, exc.code, str(exc)[:120])
                if not self.queue.is_latest(seq):
                    self.queue.update(seq, "superseded")
                    return True
                self.queue.update(seq, "sending", error_code=exc.code,
                                  retry_after=time.time() + policy.interval_seconds)
                if attempts >= policy.retry_count + 1:
                    break
                for _ in range(policy.interval_seconds):
                    if not self.queue.is_latest(seq):
                        self.queue.update(seq, "superseded")
                        return True
                    if sleep(1):
                        return True
            except Exception as exc:
                print("PUSH attempt error:", seq, type(exc).__name__, str(exc)[:120])
                if not self.queue.is_latest(seq):
                    self.queue.update(seq, "superseded")
                    return True
                self.queue.update(seq, "sending", error_code="unknown_result",
                                  retry_after=time.time() + policy.interval_seconds)
                if attempts >= policy.retry_count + 1:
                    break
                for _ in range(policy.interval_seconds):
                    if not self.queue.is_latest(seq):
                        self.queue.update(seq, "superseded")
                        return True
                    if sleep(1):
                        return True
            else:
                self.queue.update(seq, "displayed" if self.queue.is_latest(seq) else "superseded")
                return True
        self.queue.update(seq, "superseded" if not self.queue.is_latest(seq) else "failed",
                          error_code=self.queue.status(seq).get("error_code", "unknown_result"))
        return True

    def serve(self, stop_event):
        with open(self.queue.archive.root / ".push.lock", "a+b") as lock:
            while not stop_event.is_set():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    stop_event.wait(1)
            else:
                return
            try:
                while not stop_event.is_set():
                    try:
                        if not self.process_one(sleep=lambda seconds: stop_event.wait(seconds)):
                            stop_event.wait(1)
                    except Exception as exc:
                        print("PUSH worker error:", type(exc).__name__, str(exc)[:120])
                        stop_event.wait(1)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
