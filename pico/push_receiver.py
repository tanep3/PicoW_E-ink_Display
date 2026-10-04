"""Pico W HTTP PUSH receiver; no polling of the image server."""

import socket
import time

from panel_v4 import Panel
from protocol import FORMAT_ID, WIRE_LENGTH, validate_wire
from config import PUSH_PORT, SENDER_IP, STATIC_IP, SUBNET_MASK, GATEWAY, DNS_SERVER

HEADER_LIMIT = 2048
RECEIVE_BUDGET_MS = 60_000
POLL_MS = 1000
STATE_PATH = "push_state.json"

try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os


def ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.monotonic() * 1000)


def ticks_add(value, amount):
    return time.ticks_add(value, amount) if hasattr(time, "ticks_add") else value + amount


def ticks_diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else a - b


def remaining(deadline):
    value = ticks_diff(deadline, ticks_ms())
    if value <= 0:
        raise TimeoutError("PUSH receive deadline")
    return value


def valid_hex(value, length):
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def parse_head(head):
    if len(head) > HEADER_LIMIT:
        raise ValueError("headers too large")
    lines = head.split(b"\r\n")
    if lines[0] != b"POST /v1/frame HTTP/1.1":
        raise ValueError("wrong request path")
    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            raise ValueError("bad header")
        name, value = line.split(b":", 1)
        try:
            name = name.decode("ascii").lower()
            value = value.strip().decode("ascii")
        except UnicodeError:
            raise ValueError("non-ASCII header")
        if name in headers:
            raise ValueError("duplicate header")
        headers[name] = value
    if headers.get("content-type") != "application/octet-stream" or headers.get("content-length") != str(WIRE_LENGTH):
        raise ValueError("wrong body type or length")
    if headers.get("transfer-encoding") or headers.get("content-encoding"):
        raise ValueError("unsupported encoding")
    seq_text = headers.get("x-push-seq", "")
    if not seq_text.isdigit() or len(seq_text) > 18 or int(seq_text) < 1:
        raise ValueError("bad PUSH sequence")
    frame_id = headers.get("x-frame-id", "")
    digest = headers.get("x-wire-sha256", "")
    if len(frame_id) != 67 or frame_id[64:] != "-a1" or not valid_hex(frame_id[:64], 64):
        raise ValueError("bad frame ID")
    if not valid_hex(digest, 64) or headers.get("x-format-id") != FORMAT_ID:
        raise ValueError("bad RAW format")
    return int(seq_text), frame_id, digest


def read_frame(sock, deadline):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        sock.settimeout(remaining(deadline) / 1000)
        chunk = sock.recv(512)
        if not chunk:
            raise ValueError("cut header")
        data.extend(chunk)
        if len(data) > HEADER_LIMIT + 4 + WIRE_LENGTH:
            raise ValueError("request too large")
        if b"\r\n\r\n" not in data and len(data) > HEADER_LIMIT:
            raise ValueError("headers too large")
    head, first = bytes(data).split(b"\r\n\r\n", 1)
    seq, frame_id, digest = parse_head(head)
    if len(first) > WIRE_LENGTH:
        raise ValueError("body too long")
    wire = bytearray(first)
    while len(wire) < WIRE_LENGTH:
        sock.settimeout(remaining(deadline) / 1000)
        chunk = sock.recv(min(512, WIRE_LENGTH - len(wire)))
        if not chunk:
            raise ValueError("cut body")
        wire.extend(chunk)
    validate_wire(wire, digest)
    return seq, frame_id, digest, wire


def load_state(path=STATE_PATH):
    try:
        with open(path, "r") as source:
            value = json.load(source)
        seq, digest = value["seq"], value["wire_sha256"]
        if type(seq) is not int or seq < 0 or not isinstance(digest, str) or not valid_hex(digest, 64):
            raise ValueError("invalid PUSH state")
        return seq, digest
    except OSError as exc:
        code = getattr(exc, "errno", None)
        if code is None and exc.args:
            code = exc.args[0]
        if code == 2:
            return 0, ""
        raise


def save_state(seq, digest, path=STATE_PATH):
    temporary = path + ".new"
    with open(temporary, "w") as stream:
        json.dump({"seq": seq, "wire_sha256": digest}, stream)
    os.rename(temporary, path)


def response(sock, status, seq=0, digest=""):
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 409: "Conflict",
              413: "Content Too Large", 503: "Service Unavailable",
              504: "Gateway Timeout"}.get(status, "Error")
    body = json.dumps({"seq": seq, "wire_sha256": digest,
                       "state": "displayed" if status == 200 else "rejected"}).encode()
    sock.sendall(("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                  % (status, reason, len(body))).encode() + body)


def handle_connection(sock, peer, panel_factory=Panel, state_path=STATE_PATH):
    deadline = ticks_add(ticks_ms(), RECEIVE_BUDGET_MS)
    validated = False
    try:
        if peer != SENDER_IP:
            response(sock, 403)
            return "forbidden"
        seq, frame_id, digest, wire = read_frame(sock, deadline)
        validated = True
        previous_seq, previous_digest = load_state(state_path)
        if seq < previous_seq or (seq == previous_seq and digest != previous_digest):
            response(sock, 409, previous_seq, previous_digest)
            return "stale"
        if seq == previous_seq:
            response(sock, 200, seq, digest)
            return "duplicate"
        panel_deadline = ticks_add(ticks_ms(), min(remaining(deadline), 55_000))
        panel = panel_factory()
        panel.init(panel_deadline)
        panel.display(wire, panel_deadline)
        panel.sleep(panel_deadline)
        save_state(seq, digest, state_path)
        response(sock, 200, seq, digest)
        return "displayed"
    except (ValueError, TimeoutError) as exc:
        try:
            response(sock, 504 if validated and isinstance(exc, TimeoutError)
                     else 503 if validated else 400)
        except OSError:
            pass
        return "panel-failed" if validated else "invalid"
    except OSError:
        # A socket or flash error after drawing may leave the physical result
        # unknown. The sender will retry the same sequence, not claim failure.
        try:
            response(sock, 504)
        except OSError:
            pass
        return "unknown"
    except Exception:
        try:
            response(sock, 503)
        except OSError:
            pass
        return "panel-failed"
    finally:
        sock.close()


def connect_wifi():
    import network
    from secrets import WIFI_SSID, WIFI_PASSWORD
    if not WIFI_SSID or not WIFI_PASSWORD:
        raise RuntimeError("Wi-Fi settings absent")
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        wlan.ifconfig((STATIC_IP, SUBNET_MASK, GATEWAY, DNS_SERVER))
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        deadline = ticks_add(ticks_ms(), 20_000)
        while not wlan.isconnected():
            remaining(deadline)
            time.sleep_ms(100)
        if wlan.ifconfig()[0] != STATIC_IP:
            raise RuntimeError("Pico static IP was not applied")
        wlan.config(pm=network.WLAN.PM_POWERSAVE)
        return wlan
    except Exception:
        wlan.active(False)
        raise


def serve_forever():
    import select
    while True:
        wlan = None
        listener = None
        try:
            wlan = connect_wifi()
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", PUSH_PORT))
            listener.listen(1)
            listener.setblocking(False)
            poller = select.poll()
            poller.register(listener, select.POLLIN)
            while wlan.isconnected():
                if poller.poll(POLL_MS):
                    client, address = listener.accept()
                    handle_connection(client, address[0])
        except Exception as exc:
            print("PUSH listener reset:", type(exc).__name__)
        finally:
            if listener:
                listener.close()
            if wlan:
                try:
                    wlan.disconnect()
                finally:
                    wlan.active(False)
        # Connection loss retains the physical image. Reconnect without a GET.
        time.sleep_ms(1000)
