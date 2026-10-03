from datetime import datetime, timezone
from http.client import HTTPConnection
from io import BytesIO
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news import archive, demo, frame, gallery, generator
from ai_news.server import FrameServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pico"))
import protocol
import panel_v4


def source_png():
    image = Image.new("L", (250, 122), 255)
    for point in ((0, 0), (249, 0), (0, 121), (249, 121), (50, 17)):
        image.putpixel(point, 0)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class FrameTests(unittest.TestCase):
    def test_golden_vectors_and_padding(self):
        png = frame.normalize(source_png())
        self.assertEqual((250, 122), frame.validate_png(png).size)
        wire = frame.png_to_wire(png)
        self.assertEqual(4000, len(wire))
        restored = frame.wire_to_pixels(wire)
        for x, y in ((0, 0), (249, 0), (0, 121), (249, 121), (50, 17)):
            self.assertEqual(0, restored[y][x])
        self.assertEqual(1, restored[30][100])
        self.assertTrue(all(wire[i * 16 + 15] & 0x3f == 0x3f for i in range(250)))
        landscape = protocol.landscape_buffer(wire)
        self.assertTrue(all(landscape[i + (15 - j) * 250] == wire[i * 16 + j]
                            for i in range(250) for j in range(16)))
        protocol.validate_wire(wire, frame.sha256(wire))
        with self.assertRaises(ValueError):
            protocol.validate_wire(wire[:-1], frame.sha256(wire))
        broken = bytearray(wire)
        broken[15] &= 0xfe
        with self.assertRaises(ValueError):
            frame.validate_wire(broken)

    def test_png_crc_rejected(self):
        png = bytearray(frame.normalize(source_png()))
        png[29] ^= 1
        with self.assertRaises(ValueError):
            frame.validate_png(bytes(png))


class ArchiveTests(unittest.TestCase):
    def test_codex_image_backend_uses_real_generated_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated_root = root / "generated_images"
            output = generated_root / "run" / "image.png"
            backend = generator.CodexImageBackend(generated_root)

            def fake_run(*args, **kwargs):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(source_png())
                return types.SimpleNamespace(stderr=("image_gen__imagegen output_hint: " +
                    str(output) + " by default").encode())

            story = {"facts": ["fact"], "source_url": "https://example.com",
                     "satirical_metaphor": "a scale", "visual_composition": "one scene",
                     "forbidden_claims": []}
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=fake_run):
                self.assertEqual(source_png(), backend.generate(story, root))

    def test_codex_image_backend_correlates_new_session_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated_images"
            generated.mkdir()
            (generated / "older").mkdir()
            backend = generator.CodexImageBackend(generated)

            def fake_run(*args, **kwargs):
                output = generated / "new-session" / "image.png"
                output.parent.mkdir()
                output.write_bytes(source_png())
                return types.SimpleNamespace(stderr=b"image_gen__imagegen completed")

            story = {"facts": ["fact"], "source_url": "https://example.com",
                     "satirical_metaphor": "a scale", "visual_composition": "one scene",
                     "forbidden_claims": []}
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=fake_run):
                self.assertEqual(source_png(), backend.generate(story, root))

    def test_publish_and_failed_switch_preserves_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = archive.Archive(Path(tmp))
            one = store.publish(frame.normalize(source_png()), {"title": "first"}, "event-1")
            self.assertEqual(1, one["publish_seq"])
            self.assertTrue(store.seen("event-1"))
            self.assertEqual(4000, store.frame_path(one["frame_id"], "raw").stat().st_size)
            changed = Image.new("L", (250, 122), 255)
            changed.putpixel((100, 60), 0)
            buf = BytesIO(); changed.save(buf, "PNG")
            original = archive.atomic_write

            def interrupted(path, data):
                if path.name == "latest.json":
                    raise OSError("injected power loss")
                return original(path, data)

            with patch.object(archive, "atomic_write", side_effect=interrupted):
                with self.assertRaises(OSError):
                    store.publish(frame.normalize(buf.getvalue()), {}, "event-2")
            self.assertEqual(one, store.latest())
            self.assertEqual(1, len(list(gallery.entries(store))))

    def test_slot_unique_and_image_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(RuntimeError):
                generator.run_once(root, generator.CommandImageBackend(""),
                                   now=datetime(2026, 10, 3, 0, tzinfo=timezone.utc), candidates=[])
            store = archive.Archive(root)
            with store.connect() as db:
                self.assertEqual("FAILED", db.execute("SELECT state FROM jobs").fetchone()[0])
            self.assertIsNone(store.latest())

    def test_generation_publish_and_duplicate_event_skip(self):
        class Backend:
            calls = 0

            def probe(self):
                return {"schema_version": 1, "output_png": True}

            def generate(self, story, work):
                self.calls += 1
                return source_png()

        candidate = {"hn_id": 42, "title": "AI story", "url": "https://example.com/story"}
        def editorial(*args):
            return {"outcome": "selected", "reason": "verified", "story": {
                "hn_id": 42, "source_url": candidate["url"],
                "source_published_at": "2026-10-03",
                "event_key": "same-event", "facts": ["A verified fact"],
                "fact_summary": "summary", "satirical_metaphor": "a scale",
                "visual_composition": "one scene", "forbidden_claims": []}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(generator, "public_url", side_effect=lambda url: url):
            root, backend = Path(tmp), Backend()
            first = datetime(2026, 10, 3, 1, tzinfo=timezone.utc)
            self.assertEqual("published", generator.run_once(root, backend, now=first,
                                                               candidates=[candidate], editorial=editorial))
            latest = archive.Archive(root).latest()
            self.assertEqual(1, latest["publish_seq"])
            record = json.loads(archive.Archive(root).frame_path(latest["frame_id"], "json").read_text())
            self.assertEqual("date", record["metadata"]["source_date_precision"])
            self.assertEqual("2026-10-03", record["metadata"]["source_published_at"])
            self.assertEqual("skipped", generator.run_once(root, backend,
                                now=datetime(2026, 10, 3, 2, tzinfo=timezone.utc),
                                candidates=[candidate], editorial=editorial))
            self.assertEqual(1, backend.calls)
            self.assertEqual(latest, archive.Archive(root).latest())


class IntegrityTests(unittest.TestCase):
    def test_response_length_and_manifest_rejection(self):
        body = b"{}"
        protocol.validate_response(200, {"content-type": "application/json",
                                         "content-length": "2"}, body, "application/json")
        with self.assertRaises(ValueError):
            protocol.validate_response(200, {"content-type": "application/json",
                                             "content-length": "3"}, body, "application/json")
        manifest = {"schema_version": 1, "format_id": frame.FORMAT_ID, "length": 4000,
                    "frame_id": "a" * 64 + "-a1", "png_sha256": "a" * 64,
                    "wire_sha256": "b" * 64, "publish_seq": 1,
                    "raw_path": "/v1/frames/" + "a" * 64 + "-a1.raw"}
        protocol.validate_manifest(json.dumps(manifest).encode())
        manifest["raw_path"] = "http://evil.test/private"
        with self.assertRaises(ValueError):
            protocol.validate_manifest(json.dumps(manifest).encode())


class PicoFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        fake_secrets = types.SimpleNamespace(WIFI_SSID="test", WIFI_PASSWORD="test")
        with patch.dict(sys.modules, {"network": types.SimpleNamespace(STA_IF=0),
                                      "machine": types.SimpleNamespace(), "secrets": fake_secrets}):
            spec = importlib.util.spec_from_file_location("pico_test_main",
                      Path(__file__).resolve().parents[1] / "pico/main.py")
            cls.main = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.main)

    def test_skip_and_reject_without_panel_or_clear(self):
        main = self.main
        wire = frame.png_to_wire(frame.normalize(source_png()))
        manifest = {"schema_version": 1, "format_id": frame.FORMAT_ID, "length": 4000,
                    "frame_id": frame.sha256(frame.normalize(source_png())) + "-a1",
                    "png_sha256": "a" * 64, "wire_sha256": frame.sha256(wire),
                    "publish_seq": 1, "server_time": "2026-10-03T01:00:00+00:00"}
        manifest["raw_path"] = "/v1/frames/" + manifest["frame_id"] + ".raw"
        class WLAN:
            active_values = []
            def active(self, value): self.active_values.append(value)
            def disconnect(self): pass
        wlan = WLAN()
        class Client:
            calls = []
            def __init__(self, raw): self.raw = raw
            def get(self, path, deadline, max_body, expected_type):
                self.calls.append(path)
                return (200, json.dumps(manifest).encode()) if path == "/v1/latest" else (200, self.raw)
        clock = types.SimpleNamespace(ticks_ms=lambda: 1000, ticks_add=lambda a,b:a+b,
                                      ticks_diff=lambda a,b:a-b)
        def forbidden_panel():
            self.fail("panel initialized before valid frame")
        with patch.object(main, "time", clock), patch.object(main, "connect_wifi", return_value=wlan):
            client = Client(wire)
            result = main.run_wake(last_hash=manifest["wire_sha256"], last_display_ms=0,
                                   panel_factory=forbidden_panel, client=client)
            self.assertEqual("unchanged", result[0])
            self.assertEqual(["/v1/latest"], client.calls)
            self.assertIn(False, wlan.active_values)
            with self.assertRaises(ValueError):
                main.run_wake(panel_factory=forbidden_panel, client=Client(wire[:-1]))
            self.assertIn(False, wlan.active_values)

    def test_wifi_connect_timeout_disables_radio(self):
        main = self.main
        class WLAN:
            states = []
            def active(self, value): self.states.append(value)
            def connect(self, *args): pass
            def isconnected(self): return False
        wlan = WLAN()
        clock = [0]
        ticks = types.SimpleNamespace(ticks_ms=lambda: clock[0],
                  ticks_add=lambda a,b:a+b, ticks_diff=lambda a,b:a-b,
                  sleep_ms=lambda ms:clock.__setitem__(0,clock[0]+ms))
        network = types.SimpleNamespace(STA_IF=0, WLAN=lambda _:wlan)
        with patch.object(main,"network",network),patch.object(main,"time",ticks):
            with self.assertRaises(TimeoutError):
                main.connect_wifi(120_000)
        self.assertEqual([True, False], wlan.states)

    def test_busy_timeout_is_finite(self):
        panel = panel_v4.Panel.__new__(panel_v4.Panel)
        panel.busy = types.SimpleNamespace(value=lambda:1)
        clock = [0]
        ticks = types.SimpleNamespace(ticks_ms=lambda:clock[0],
                  ticks_diff=lambda a,b:a-b,
                  sleep_ms=lambda ms:clock.__setitem__(0,clock[0]+ms))
        with patch.object(panel_v4,"time",ticks):
            with self.assertRaises(panel_v4.PanelError):
                panel._wait(60)
        self.assertLessEqual(clock[0],70)


class GalleryTests(unittest.TestCase):
    def test_gallery_only_demo_never_changes_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = archive.Archive(Path(tmp))
            paths = demo.create_demo(Path(tmp), datetime(2026, 10, 3, 6, tzinfo=timezone.utc))
            self.assertEqual(3, len(paths))
            self.assertIsNone(store.latest())
            today = gallery.day_view(store, "2026-10-03")
            self.assertEqual(2, len(today["images"]))
            self.assertTrue(all(item["is_demo"] for item in today["images"]))
            self.assertEqual([], gallery.day_view(store, "2026-10-04")["images"])

    def test_jst_boundary_empty_month_multiple_images_and_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = archive.Archive(Path(tmp))
            for index, when in enumerate(("2026-09-30T14:59:00+00:00",
                                          "2026-09-30T15:01:00+00:00",
                                          "2026-09-30T16:00:00+00:00")):
                image = Image.new("L", (250, 122), 255)
                image.putpixel((index + 1, 1), 0)
                source = BytesIO(); image.save(source, "PNG")
                with patch.object(archive, "utcnow", return_value=when):
                    store.publish(frame.normalize(source.getvalue()),
                                  {"title": "story " + str(index), "source_urls": [] if index == 1 else ["https://example.com/a"]},
                                  "event-" + str(index))
            self.assertEqual(["2026-09-30"], [x["date"] for x in gallery.month_view(store, "2026-09")["days"]])
            october = gallery.month_view(store, "2026-10")
            self.assertEqual(2, october["days"][0]["count"])
            items = gallery.day_view(store, "2026-10-01")["images"]
            self.assertEqual(["story 1", "story 2"], [x["title"] for x in items])
            self.assertEqual([], items[0]["source_urls"])
            self.assertEqual([], gallery.month_view(store, "2026-11")["days"])
            self.assertEqual([], gallery.day_view(store, "2026-11-01")["images"])
            with self.assertRaises(ValueError):
                gallery.parse_day("2026-02-30")

    def test_http_lan_gallery_and_read_only_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = archive.Archive(Path(tmp))
            published = store.publish(frame.normalize(source_png()), {"title": "hello"}, "event")
            server = FrameServer(("127.0.0.1", 0), store, allowed_network="127.0.0.0/8")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def get(path, headers=None):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("GET", path, headers=headers or {})
                    response = conn.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    conn.close()
                    return result
                status, _, html = get("/", {})
                self.assertEqual(200, status)
                self.assertIn(b"image-rendering:pixelated", html)
                status, _, body = get("/gallery/api/day?date=2026-10-03")
                self.assertEqual(200, status)
                self.assertIn(b'"images"', body)
                status, _, image = get("/gallery/image/" + published["frame_id"])
                self.assertEqual(200, status)
                self.assertEqual(b"\x89PNG", image[:4])
                status, headers, body = get("/v1/latest")
                self.assertEqual(200, status)
                self.assertEqual(published["frame_id"], json.loads(body)["frame_id"])
                self.assertNotIn("X-Response-Mac", headers)
            finally:
                server.shutdown(); server.server_close(); thread.join(3)


if __name__ == "__main__":
    unittest.main()
