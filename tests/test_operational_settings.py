"""Persistent settings, request conflicts, and old/new normalization snapshots."""

from datetime import datetime, timezone
from datetime import timedelta
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news.archive import Archive, SettingsConflict
from ai_news import frame, generator
from ai_news.manual import ManualManager
from ai_news.retry_config import load_retry_config
from ai_news.server import FrameServer


NEWS = {"source_url": "https://example.com/unique-story", "summary": "a sourced fact"}


def source_png():
    image = Image.new("L", (500, 244), 255)
    for i in range(25, 400):
        image.putpixel((i, i // 3), 0)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class SettingsTests(unittest.TestCase):
    def test_config_defaults_validation_and_db_precedence_per_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text("[news]\nmodel='gpt-6-luna'\n[image]\n"
                            "model='gpt-6-luna'\ndpid_lambda=0.75\nthreshold=128\n")
            config = load_retry_config(path)
            store = Archive(Path(tmp) / "state")
            self.assertEqual(store.operational_settings(config)["values"], {
                "news_model": "gpt-6-luna", "image_model": "gpt-6-luna",
                "dpid_lambda": .75, "threshold": 128})
            store.save_operational_setting("image_model", "gpt-6.1-sol", 0)
            path.write_text("[news]\nmodel='news-next'\n[image]\n"
                            "model='image-next'\ndpid_lambda=0.25\nthreshold=111\n")
            data = store.operational_settings(load_retry_config(path))
            self.assertEqual(data["values"], {"news_model": "news-next",
                "image_model": "gpt-6.1-sol", "dpid_lambda": .25, "threshold": 111})
            self.assertEqual(data["source"]["image_model"], "database")
            self.assertEqual(data["source"]["news_model"], "config")
            for invalid in ("", "bad model", "-bad"):
                with self.assertRaises(ValueError):
                    store.save_operational_setting("news_model", invalid, 1)
            for invalid in (-.01, 1.01, float("nan"), True):
                with self.assertRaises(ValueError):
                    store.save_operational_setting("dpid_lambda", invalid, 1)
            for invalid in (0, 255, 12.5, True):
                with self.assertRaises(ValueError):
                    store.save_operational_setting("threshold", invalid, 1)
            with self.assertRaises(SettingsConflict):
                store.save_operational_setting("threshold", 130, 0)
            self.assertEqual(store.save_operational_setting("threshold", 130, 1), 2)
            self.assertEqual(Archive(Path(tmp) / "state").operational_settings(
                load_retry_config(path))["values"]["threshold"], 130)
            for bad_config in ("dpid_lambda=1.5", "threshold=0", "threshold=255"):
                path.write_text("[image]\n" + bad_config + "\n")
                with self.assertRaises(ValueError):
                    load_retry_config(path)

    def test_concurrent_db_saves_have_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Archive(Path(tmp))
            barrier = threading.Barrier(2)
            outcomes = []
            def save(value):
                barrier.wait()
                try:
                    store.save_operational_setting("threshold", value, 0)
                    outcomes.append("saved")
                except SettingsConflict:
                    outcomes.append("conflict")
            threads = [threading.Thread(target=save, args=(value,)) for value in (110, 150)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertCountEqual(outcomes, ["saved", "conflict"])

    def test_http_reload_validation_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text("[news]\nmodel='gpt-6-luna'\n[image]\ndpid_lambda=0.75\n")
            server = FrameServer(("127.0.0.1", 0), Archive(root / "state"),
                                 allowed_network="127.0.0.0/8", config_path=config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, payload=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {}
                if payload is not None:
                    headers = {"Origin": f"http://127.0.0.1:{server.server_port}",
                               "X-AI-News-Action": "save-setting", "Content-Type": "application/json"}
                conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None,
                             headers=headers)
                response = conn.getresponse(); data = response.read(); status = response.status
                conn.close(); return status, json.loads(data) if data else None
            try:
                status, loaded = request("GET", "/v1/settings")
                self.assertEqual(status, 200)
                self.assertEqual(loaded["values"]["dpid_lambda"], .75)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": "dpid_lambda", "value": .8, "revision": 0})[0], 200)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": "threshold", "value": 180, "revision": 0})[0], 409)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": "threshold", "value": 255, "revision": 1})[0], 400)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": "news_model", "value": "bad model", "revision": 1})[0], 400)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": [], "value": "x", "revision": 1})[0], 400)
                self.assertEqual(request("POST", "/v1/settings", {
                    "key": "threshold", "value": 180, "revision": 1})[0], 200)
                self.assertEqual(request("GET", "/v1/settings")[1]["values"]["threshold"], 180)
                config.write_text("[news]\nmodel='news-next'\n[image]\ndpid_lambda=0.25\n")
                refreshed = request("GET", "/v1/settings")[1]
                self.assertEqual(refreshed["values"]["news_model"], "news-next")
                self.assertEqual(refreshed["values"]["dpid_lambda"], .8)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_old_job_migration_and_new_job_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            root.mkdir()
            with sqlite3.connect(root / "jobs.sqlite3") as conn:
                conn.execute("""CREATE TABLE job_selections (
                    slot INTEGER PRIMARY KEY, topic_id TEXT NOT NULL, topic_label TEXT NOT NULL,
                    topic_prompt TEXT NOT NULL, style_id TEXT NOT NULL, style_label TEXT NOT NULL,
                    style_prompt TEXT NOT NULL)""")
                conn.execute("INSERT INTO job_selections VALUES(7,'ai_news','news','prompt',"
                             "'newspaper_cartoon','style','drawing')")
            store = Archive(root)
            legacy = store.job_selection(7)
            self.assertEqual((legacy["news_model"], legacy["image_model"]),
                             ("gpt-6-luna", "gpt-6-luna"))
            self.assertEqual((legacy["resize_method"], legacy["dpid_lambda"], legacy["threshold"]),
                             ("lanczos", None, 128))
            Archive(root)
            selection = {k: legacy[k] for k in ("topic_id", "topic_label", "topic_prompt",
                "style_id", "style_label", "style_prompt")}
            self.assertTrue(store.begin(8, {**selection, "news_model": "other-news",
                "image_model": "other-image", "resize_method": "dpid",
                "dpid_lambda": .75, "threshold": 140}))
            self.assertEqual(store.job_selection(8)["threshold"], 140)
            self.assertEqual(store.job_selection(8)["resize_method"], "dpid")

    def test_dpid_bytes_wire_and_missing_dependency(self):
        source = source_png()
        try:
            import pepedpid  # noqa: F401
        except ImportError:
            self.skipTest("pepedpid isolated venv required for positive DPID test")
        expected = frame.normalize(source, 128, method="dpid", dpid_lambda=.75)
        self.assertEqual(frame.validate_png(expected).size, (250, 122))
        wire = frame.png_to_wire(expected)
        self.assertEqual(len(wire), 4000)
        self.assertTrue(all((wire[16*i+15] & 0x3f) == 0x3f for i in range(250)))
        self.assertEqual(expected, frame.normalize(source, 128, method="dpid", dpid_lambda=.75))
        gradient = Image.linear_gradient("L").resize((500, 244))
        stream = BytesIO(); gradient.save(stream, "PNG")
        self.assertNotEqual(frame.normalize(stream.getvalue(), 128, method="dpid", dpid_lambda=.75),
                            frame.normalize(stream.getvalue(), 140, method="dpid", dpid_lambda=.75))
        with patch.dict("sys.modules", {"pepedpid": None}):
            with self.assertRaisesRegex(RuntimeError, "DPID requires"):
                frame.normalize(source, 128, method="dpid", dpid_lambda=.75)

    def test_dpid_extreme_aspect_ratios_and_small_source(self):
        try:
            import pepedpid  # noqa: F401
        except ImportError:
            self.skipTest("pepedpid isolated venv required")
        # Run each native call in a child process so a Rust panic is a test
        # failure rather than terminating the entire test suite.
        script = """from io import BytesIO
from PIL import Image
from ai_news.frame import normalize, validate_png, png_to_wire
import sys
width, height = map(int, sys.argv[1:])
image = Image.new('L', (width, height), 255)
buffer = BytesIO(); image.save(buffer, 'PNG')
result = normalize(buffer.getvalue(), method='dpid', dpid_lambda=.75)
assert validate_png(result).size == (250, 122)
assert len(png_to_wire(result)) == 4000
"""
        for size in ((10000, 1), (1, 10000), (1, 1), (20, 20)):
            with self.subTest(size=size):
                result = subprocess.run([sys.executable, "-c", script,
                                         str(size[0]), str(size[1])],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_dpid_stops_before_backend_probe_or_generation(self):
        class Backend:
            def probe(self):
                raise AssertionError("probe must not run without DPID")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            with patch.dict("sys.modules", {"pepedpid": None}):
                with self.assertRaisesRegex(RuntimeError, "DPID requires"):
                    generator.run_once(root, Backend(),
                        now=datetime(2026, 10, 5, 9, tzinfo=timezone.utc),
                        config=load_retry_config(Path(tmp) / "missing-config"))
            self.assertIsNone(Archive(root).latest())

    def test_manual_request_captures_settings_before_later_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"; path = Path(tmp) / "config"
            path.write_text("[news]\nmodel='gpt-6-luna'\n[image]\ndpid_lambda=0.75\n")
            store = Archive(root)
            store.save_operational_setting("image_model", "gpt-6.1-sol", 0)
            store.save_operational_setting("threshold", 150, 1)
            manager = ManualManager(store, path)
            with patch("ai_news.manual.subprocess.Popen", return_value=object()):
                status, started = manager.start("A custom scene")
            self.assertTrue(started)
            request = json.loads((manager.requests / (status["run_id"] + ".json")).read_text())
            self.assertEqual(request["selection"]["image_model"], "gpt-6.1-sol")
            self.assertEqual(request["selection"]["dpid_lambda"], .75)
            self.assertEqual(request["selection"]["threshold"], 150)
            store.save_operational_setting("threshold", 200, 2)
            self.assertEqual(request["selection"]["threshold"], 150)

    def test_new_hourly_jobs_snapshot_settings_and_metadata(self):
        class Backend(generator.CodexImageBackend):
            def __init__(self):
                super().__init__(generated_root=Path("/unused"))
                self.models = []
            def probe(self, *, model=None):
                self.models.append(("probe", model))
                return {"backend": "test", "requested_model": model}
            def generate(self, news, work, *, feedback, timeout, style_prompt, evidence, model=None):
                self.models.append(("image", model))
                image = Image.open(BytesIO(source_png())).convert("L")
                if news["summary"].endswith("2"):
                    image.paste(0, (40, 40, 100, 80))
                output = BytesIO(); image.save(output, "PNG")
                return output.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"; path = Path(tmp) / "config"
            path.write_text("[news]\nmodel='gpt-6-luna'\n[image]\n"
                            "model='gpt-6-luna'\ndpid_lambda=0.75\nthreshold=128\n")
            store = Archive(root)
            store.save_operational_setting("news_model", "custom-news", 0)
            store.save_operational_setting("image_model", "custom-image", 1)
            store.save_operational_setting("dpid_lambda", .5, 2)
            store.save_operational_setting("threshold", 141, 3)
            now = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)
            seen = []
            def editor(*args, **kwargs):
                seen.append(kwargs["model"])
                return {"source_url": "https://example.com/" + str(len(seen)),
                        "summary": "a sourced fact " + str(len(seen))}
            backend = Backend()
            with patch.object(generator, "codex_editor", side_effect=editor), \
                 patch.object(generator, "public_url", side_effect=lambda value: value):
                self.assertEqual(generator.run_once(root, backend, now=now,
                                                     config=load_retry_config(path)), "published")
                first_id = Archive(root).latest()["frame_id"]
                store.save_operational_setting("image_model", "new-image", 4)
                store.save_operational_setting("threshold", 160, 5)
                self.assertEqual(generator.run_once(root, backend, now=now + timedelta(hours=1),
                                                     config=load_retry_config(path)), "published")
            first = json.loads(store.frame_path(first_id, "json").read_text())["metadata"]
            second = json.loads(store.frame_path(store.latest()["frame_id"], "json").read_text())["metadata"]
            self.assertEqual(first["normalizer"], {"fit": "contain", "version": 2,
                "method": "dpid", "dpid_lambda": .5, "threshold": 141})
            self.assertEqual(second["normalizer"]["threshold"], 160)
            self.assertEqual(first["image_model"], "custom-image")
            self.assertEqual(second["image_model"], "new-image")
            self.assertEqual(first["news_model"], second["news_model"])
            self.assertEqual(backend.models, [("probe", "custom-image"),
                ("image", "custom-image"), ("probe", "new-image"), ("image", "new-image")])
            self.assertEqual(seen, ["custom-news", "custom-news"])

    def test_legacy_job_resumes_with_lanczos_and_threshold_128(self):
        class Backend:
            def probe(self): return {"backend": "test"}
            def generate(self, *args, **kwargs): return source_png()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"; store = Archive(root)
            when = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
            slot = int(when.timestamp()) // 3600
            legacy = {"topic_id": "ai_news", "topic_label": "news", "topic_prompt": "prompt",
                      "style_id": "newspaper_cartoon", "style_label": "style", "style_prompt": "draw"}
            store.begin(slot, legacy)
            store.save_operational_setting("threshold", 200, 0)
            store.save_operational_setting("dpid_lambda", .75, 1)
            self.assertEqual(generator.run_once(root, Backend(), now=when,
                config=load_retry_config(Path(tmp)/"missing-config"),
                news_fetcher=lambda *_: NEWS), "published")
            metadata = json.loads(store.frame_path(store.latest()["frame_id"], "json").read_text())["metadata"]
            self.assertEqual(metadata["normalizer"], {"fit": "contain", "threshold": 128,
                                                       "version": 1})
            self.assertEqual(store.frame_path(store.latest()["frame_id"], "png").read_bytes(),
                             frame.normalize(source_png()))


if __name__ == "__main__": unittest.main()
