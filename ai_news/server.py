"""LAN-only gallery and explicit same-origin manual generation controls."""

from __future__ import annotations

from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .archive import Archive, FRAME_ID, json_bytes, utcnow
from .frame import WIRE_LENGTH
from .gallery import DEMO_ID, month_view, day_view
from .manual import ManualManager
from .topics import CONFIG_PATH, save_selected_topic, topic_snapshot


class FrameServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, archive: Archive, allowed_network="192.168.0.0/24",
                 config_path: Path = CONFIG_PATH):
        self.archive = archive
        self.config_path = config_path
        self.allowed_network = ipaddress.ip_network(allowed_network, strict=False)
        self.rate = defaultdict(deque)
        self.manual = ManualManager(archive, config_path)
        self.settings_lock = threading.Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Deliberately do not log user-controlled request data.
        pass

    def _allow_peer(self):
        peer = ipaddress.ip_address(self.client_address[0])
        if peer not in self.server.allowed_network:
            raise ValueError("peer outside allowed network")
        now = time.monotonic()
        recent = self.server.rate[str(peer)]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 240:
            raise ValueError("rate exceeded")
        recent.append(now)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _web_send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _gallery(self):
        url = urlsplit(self.path)
        try:
            if url.path in ("/", "/gallery", "/gallery/") and not url.query:
                body = (Path(__file__).resolve().parent.parent / "web/gallery.html").read_bytes()
                self._web_send(200, body, "text/html; charset=utf-8")
                return
            if url.path == "/settings/" and not url.query:
                body = (Path(__file__).resolve().parent.parent / "web/settings.html").read_bytes()
                self._web_send(200, body, "text/html; charset=utf-8")
                return
            if url.path == "/gallery/api/month":
                query = parse_qs(url.query, strict_parsing=True)
                if set(query) != {"month"} or len(query["month"]) != 1:
                    raise ValueError("invalid month query")
                body = json_bytes(month_view(self.server.archive, query["month"][0]))
                self._web_send(200, body, "application/json")
                return
            if url.path == "/gallery/api/day":
                query = parse_qs(url.query, strict_parsing=True)
                if set(query) != {"date"} or len(query["date"]) != 1:
                    raise ValueError("invalid day query")
                body = json_bytes(day_view(self.server.archive, query["date"][0]))
                self._web_send(200, body, "application/json")
                return
            if url.path.startswith("/gallery/image/") and not url.query:
                frame_id = url.path[len("/gallery/image/"):]
                path = self.server.archive.frame_path(frame_id, "png")
                if frame_id in self.server.archive.published_frame_ids() and path.is_file():
                    self._web_send(200, path.read_bytes(), "image/png")
                    return
            if url.path.startswith("/gallery/demo/") and not url.query:
                frame_id = url.path[len("/gallery/demo/"):]
                if DEMO_ID.fullmatch(frame_id):
                    path = self.server.archive.root / "demo" / frame_id / "frame.png"
                    if path.is_file():
                        self._web_send(200, path.read_bytes(), "image/png")
                        return
            self._web_send(404, b"Not found", "text/plain; charset=utf-8")
        except ValueError:
            self._web_send(400, b"Invalid request", "text/plain; charset=utf-8")
        except OSError:
            self._web_send(503, b"Unavailable", "text/plain; charset=utf-8")

    def do_GET(self):
        try:
            if len(str(self.headers)) > 4096:
                raise ValueError("headers too large")
            if "?" in self.path and not self.path.startswith("/gallery/api/"):
                raise ValueError("invalid query")
            self._allow_peer()
        except (ValueError, IndexError):
            self.send_error(403)
            self.close_connection = True
            return
        if self.path == "/" or self.path.startswith(("/gallery", "/settings")):
            self._gallery()
            return
        try:
            if self.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if self.path == "/v1/latest":
                latest = self.server.archive.latest()
                if latest is None:
                    self._send(404, b"", "application/json")
                    return
                self._send(200, json_bytes({**latest, "server_time": utcnow()}),
                           "application/json")
                return
            if self.path == "/v1/status":
                latest = self.server.archive.latest()
                self._send(200, json_bytes({"has_frame": latest is not None,
                                            "frame_id": latest["frame_id"] if latest else None,
                                            "server_time": utcnow()}), "application/json")
                return
            if self.path == "/v1/generate/status":
                status = self.server.manual.status()
                if status["state"] != "running":
                    try:
                        with self.server.archive.lock(blocking=False):
                            pass
                    except BlockingIOError:
                        status = {"state": "busy"}
                self._web_send(200, json_bytes(status), "application/json")
                return
            if self.path == "/v1/topics":
                body = topic_snapshot(self.server.config_path)
                self._web_send(200, json_bytes(body), "application/json")
                return
            parts = self.path.split("/")
            if len(parts) == 4 and parts[:3] == ["", "v1", "frames"]:
                name = parts[3]
                if "." in name:
                    frame_id, suffix = name.rsplit(".", 1)
                    if FRAME_ID.fullmatch(frame_id) and suffix in ("raw", "png"):
                        path = self.server.archive.frame_path(frame_id, suffix)
                        if frame_id in self.server.archive.published_frame_ids() and path.is_file():
                            body = path.read_bytes()
                            if suffix == "raw":
                                if len(body) != WIRE_LENGTH:
                                    raise ValueError("stored frame length")
                                content_type = "application/octet-stream"
                            else:
                                content_type = "image/png"
                            self._send(200, body, content_type)
                            return
            self._send(404, b"", "application/json")
        except (OSError, ValueError):
            self._send(503, b"", "application/json")

    def do_POST(self):
        if self.path not in ("/v1/generate", "/v1/topics"):
            self.send_error(405)
            self.close_connection = True
            return
        try:
            if len(str(self.headers)) > 4096:
                raise ValueError("headers too large")
            self._allow_peer()
            host, port = self.server.server_address[:2]
            expected = f"{host}:{port}"
            action = "generate" if self.path == "/v1/generate" else "save-topic"
            if (self.headers.get("Host") != expected
                    or self.headers.get("Origin") != "http://" + expected
                    or self.headers.get("X-AI-News-Action") != action
                    or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
                    or self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Type") != "application/json"):
                self._web_send(403, b'{"state":"forbidden"}\n', "application/json")
                return
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= 256:
                raise ValueError("invalid body length")
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
            if action == "generate":
                if body:
                    raise ValueError("unexpected request fields")
                status, started = self.server.manual.start()
                self._web_send(202 if started else 409, json_bytes(status), "application/json")
            else:
                if set(body) != {"topic_id"} or not isinstance(body["topic_id"], str):
                    raise ValueError("invalid topic selection")
                with self.server.settings_lock:
                    selected = save_selected_topic(body["topic_id"], self.server.config_path)
                self._web_send(200, json_bytes({"selected_topic_id": selected}), "application/json")
        except (ValueError, IndexError, json.JSONDecodeError):
            self._web_send(400, b'{"state":"invalid_request"}\n', "application/json")
        except (OSError, BlockingIOError):
            self._web_send(503, b'{"state":"unavailable"}\n', "application/json")


def main():
    root = Path(os.environ.get("AI_NEWS_STATE", "./state"))
    host = os.environ.get("AI_NEWS_BIND", "192.168.0.120")
    port = int(os.environ.get("AI_NEWS_PORT", "16150"))
    if not 1 <= port <= 65535 or port == 8080:
        raise SystemExit("AI_NEWS_PORT must be 1..65535 and cannot be 8080")
    server = FrameServer((host, port), Archive(root))
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
