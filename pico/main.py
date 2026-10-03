"""Hourly pull client for MicroPython Pico W. Never clear on fetch failure."""

import network
import socket
import time
import machine

from protocol import validate_response, validate_manifest, validate_wire
from panel_v4 import Panel
from config import HOST, PORT

from secrets import WIFI_SSID, WIFI_PASSWORD

WAKE_BUDGET_MS = 120_000
WIFI_BUDGET_MS = 20_000
BUSY_BUDGET_MS = 60_000
MAINTENANCE_MS = 86_400_000
MIN_REFRESH_MS = 180_000
USB_ACCESS_MS = 180_000


def _remaining(deadline):
    result = time.ticks_diff(deadline, time.ticks_ms())
    if result <= 0:
        raise TimeoutError("wake deadline")
    return result


def _read_response(sock, deadline, max_body):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        sock.settimeout(min(10, _remaining(deadline) / 1000))
        chunk = sock.recv(512)
        if not chunk:
            raise ValueError("early HTTP EOF")
        data.extend(chunk)
        if len(data) > 4096 + max_body:
            raise ValueError("HTTP response oversized")
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    if len(head) > 4096:
        raise ValueError("HTTP headers too large")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) < 2 or parts[0] != b"HTTP/1.1":
        raise ValueError("HTTP status invalid")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            raise ValueError("HTTP header invalid")
        name, value = line.split(b":", 1)
        name = name.decode("ascii").lower()
        if name in headers:
            raise ValueError("duplicate HTTP header")
        headers[name] = value.strip().decode("ascii")
    if "transfer-encoding" in headers or headers.get("content-encoding", "identity") != "identity":
        raise ValueError("unsupported HTTP encoding")
    length_text = headers.get("content-length", "")
    if not length_text.isdigit():
        raise ValueError("missing Content-Length")
    length = int(length_text)
    if length > max_body or len(body) > length:
        raise ValueError("HTTP body length invalid")
    while len(body) < length:
        sock.settimeout(min(10, _remaining(deadline) / 1000))
        chunk = sock.recv(min(512, length - len(body)))
        if not chunk:
            raise ValueError("short HTTP body")
        body += chunk
    sock.settimeout(min(1, _remaining(deadline) / 1000))
    if sock.recv(1):
        raise ValueError("extra HTTP body data")
    return status, headers, body


class Client:
    def get(self, path, deadline, max_body, expected_type):
        sock = socket.socket()
        try:
            sock.settimeout(min(3, _remaining(deadline) / 1000))
            sock.connect((HOST, PORT))
            sock.settimeout(min(10, _remaining(deadline) / 1000))
            lines = ["GET " + path + " HTTP/1.1", "Host: " + HOST,
                     "Connection: close", "Accept-Encoding: identity"]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            status, received, body = _read_response(sock, deadline, max_body)
            validate_response(status, received, body, expected_type)
            return status, body
        finally:
            sock.close()


def fetch_with_retry(client, path, deadline, max_body, expected_type):
    for attempt in range(2):
        try:
            return client.get(path, deadline, max_body, expected_type)
        except (OSError, TimeoutError):
            if attempt or _remaining(deadline) < 1000:
                raise


def connect_wifi(deadline):
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        wifi_deadline = time.ticks_add(time.ticks_ms(), WIFI_BUDGET_MS)
        while not wlan.isconnected():
            _remaining(deadline)
            if time.ticks_diff(wifi_deadline, time.ticks_ms()) <= 0:
                raise TimeoutError("Wi-Fi connect timeout")
            time.sleep_ms(100)
        return wlan
    except Exception:
        wlan.active(False)
        raise


def _next_wait(server_time):
    # This unauthenticated LAN timestamp is only a scheduling hint.
    try:
        if server_time[-6:] not in ("+00:00",) and not server_time.endswith("Z"):
            raise ValueError("not UTC")
        minute = int(server_time[14:16])
        second = int(server_time[17:19])
        if not 0 <= minute < 60 or not 0 <= second < 60:
            raise ValueError("time invalid")
        delta = 20 * 60 - (minute * 60 + second)
        return (delta if delta > 0 else delta + 3600) * 1000
    except (ValueError, TypeError, IndexError):
        return 3_600_000


def run_wake(last_hash=None, last_display_ms=None, panel_factory=Panel, client=None):
    """Return (result, hash, display_tick, next_wait_ms). Missing hash means UNKNOWN."""
    if not WIFI_SSID or not WIFI_PASSWORD:
        raise RuntimeError("Pico Wi-Fi settings are not configured")
    client = client or Client()
    deadline = time.ticks_add(time.ticks_ms(), WAKE_BUDGET_MS)
    wlan = None
    wait_ms = 3_600_000
    try:
        wlan = connect_wifi(deadline)
        status, body = fetch_with_retry(client, "/v1/latest", deadline, 2048, "application/json")
        if status == 404:
            return "no-frame", last_hash, last_display_ms, wait_ms
        if status != 200:
            raise OSError("manifest HTTP status " + str(status))
        manifest = validate_manifest(body)
        wait_ms = _next_wait(manifest.get("server_time"))
        same = manifest["wire_sha256"] == last_hash
        elapsed = (time.ticks_diff(time.ticks_ms(), last_display_ms)
                   if last_display_ms is not None else None)
        if same and elapsed is not None and elapsed < MAINTENANCE_MS:
            return "unchanged", last_hash, last_display_ms, wait_ms
        if elapsed is not None and elapsed < MIN_REFRESH_MS:
            return "guard", last_hash, last_display_ms, wait_ms
        path = "/v1/frames/" + manifest["frame_id"] + ".raw"
        status, wire = fetch_with_retry(client, path, deadline, 4000, "application/octet-stream")
        if status == 404:
            # Do not draw on a missing immutable frame.
            return "missing-frame", last_hash, last_display_ms, wait_ms
        if status != 200:
            raise OSError("RAW HTTP status " + str(status))
        validate_wire(wire, manifest["wire_sha256"])
    finally:
        if wlan is not None:
            try:
                wlan.disconnect()
            finally:
                wlan.active(False)
    # Panel construction and init occur only after complete integrity checks.
    _remaining(deadline)
    panel_deadline = time.ticks_add(time.ticks_ms(), min(BUSY_BUDGET_MS, _remaining(deadline)))
    panel = panel_factory()
    try:
        panel.init(panel_deadline)
        panel.display(wire, panel_deadline)
        panel.sleep(panel_deadline)
    except Exception:
        # A display failure leaves physical state UNKNOWN; do not record the hash.
        raise
    return "displayed", manifest["wire_sha256"], time.ticks_ms(), wait_ms


def main():
    last_hash = None
    last_display_ms = None
    # Reset means display state UNKNOWN. Fetch the latest frame immediately;
    # run_wake only initializes the panel after the full frame is verified.
    while True:
        start = time.ticks_ms()
        wait_ms = 3_600_000
        try:
            _, last_hash, last_display_ms, wait_ms = run_wake(last_hash, last_display_ms)
        except Exception as exc:
            print("wake failed:", type(exc).__name__)
        # Keep USB REPL available after every wake, including failures, so the
        # program can be inspected or replaced before entering lightsleep.
        access_deadline = time.ticks_add(time.ticks_ms(), USB_ACCESS_MS)
        while time.ticks_diff(access_deadline, time.ticks_ms()) > 0:
            time.sleep_ms(min(1000, time.ticks_diff(access_deadline, time.ticks_ms())))
        next_due = time.ticks_add(start, wait_ms)
        while time.ticks_diff(next_due, time.ticks_ms()) > 0:
            machine.lightsleep(min(60_000, time.ticks_diff(next_due, time.ticks_ms())))


if __name__ == "__main__":
    main()
