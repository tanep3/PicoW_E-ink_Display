"""Custom text is a separate drawing input, with the normal image publication path."""

from datetime import datetime, timezone
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import os
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from ai_news import gallery, generator, manual
from ai_news.archive import Archive, json_bytes
from ai_news.prompt_evidence import PromptTarget
from ai_news.retry_config import RetryConfig, Topic
from ai_news.server import FrameServer


ROOT = Path(__file__).resolve().parents[1]
TEXT = "丸いロボットが犬の帰宅を祝う。\n背景は白。"


def picture():
    image = Image.new("L", (250, 122), 255)
    ImageDraw.Draw(image).rectangle((20, 20, 90, 90), fill=0)
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class CustomGenerationTests(unittest.TestCase):
    def test_custom_slot_files_have_command_friendly_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp))
            archive.save_custom(-1, TEXT)
            self.assertTrue((Path(tmp) / "custom/slot-1.json").is_file())
            self.assertFalse((Path(tmp) / "custom/-1.json").exists())
            self.assertEqual(TEXT, archive.load_custom(-1))

    def test_legacy_custom_slot_file_is_read_and_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp))
            legacy = Path(tmp) / "custom/-1.json"
            legacy.write_bytes(json_bytes({"custom_text": TEXT}))
            self.assertEqual(TEXT, archive.load_custom(-1))
            archive.save_custom(-1, TEXT)
            self.assertFalse(legacy.exists())
            self.assertEqual(TEXT, archive.load_custom(-1))
            self.assertTrue((Path(tmp) / "custom/slot-1.json").is_file())

    def test_legacy_custom_slot_file_must_match_retry_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp))
            legacy = Path(tmp) / "custom/-1.json"
            legacy.write_bytes(json_bytes({"custom_text": "different"}))
            with self.assertRaisesRegex(ValueError, "saved custom text differs"):
                archive.save_custom(-1, TEXT)
            self.assertTrue(legacy.is_file())

    def test_user_topic_named_custom_remains_news(self):
        class Backend:
            def probe(self):
                return {"schema_version": 1, "output_png": True}
            def generate(self, content, work, **options):
                self.content = content
                return picture()
        config = RetryConfig(selected_topic_id="custom",
                             topics=(Topic("custom", "通常の題材", "ニュースを探す"),))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generator, "public_url", side_effect=lambda value: value):
            backend = Backend()
            result = generator.run_once(Path(tmp), backend, config=config, manual=True,
                news_fetcher=lambda *args: {"source_url": "https://example.com/story",
                                           "summary": "本物のニュース"})
            self.assertEqual("published", result)
            self.assertEqual("本物のニュース", backend.content["summary"])
            self.assertNotIn("custom_text", backend.content)
            self.assertIsNone(Archive(Path(tmp)).load_custom(-1))

    def test_custom_skips_news_and_preserves_full_text_without_source(self):
        class Backend:
            def __init__(self):
                self.inputs = []

            def probe(self):
                return {"schema_version": 1, "output_png": True}

            def generate(self, content, work, **options):
                self.inputs.append((content, options["style_prompt"]))
                return picture()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = Backend()
            now = datetime(2026, 10, 5, 3, tzinfo=timezone.utc)
            def forbidden_fetch(*args):
                self.fail("custom path performed news research")
            self.assertEqual("published", generator.run_once(
                root, backend, now=now, manual=True, custom_text=TEXT,
                config=RetryConfig(), news_fetcher=forbidden_fetch))
            store = Archive(root)
            slot = -1
            self.assertEqual(TEXT, store.load_custom(slot))
            self.assertIsNone(store.load_news(slot))
            self.assertEqual((0, ""), store.attempt_info(slot, "news"))
            self.assertEqual({"custom_text": TEXT}, backend.inputs[0][0])
            latest = store.latest()
            meta = json.loads(store.frame_path(latest["frame_id"], "json").read_text())["metadata"]
            self.assertEqual("custom", meta["input_kind"])
            self.assertEqual([], meta["source_urls"])
            self.assertEqual(TEXT, meta["custom_text"])
            self.assertNotIn("news_summary", meta)
            self.assertNotIn("source_url", meta)
            entry = next(gallery.entries(store))
            self.assertEqual(TEXT, entry["custom_text"])
            self.assertEqual("custom", entry["input_kind"])
            self.assertEqual([], entry["source_urls"])
            self.assertEqual(RetryConfig().style.prompt, entry["used_style_prompt"])
            self.assertIsNone(entry["used_topic_prompt"])

    def test_custom_prompt_and_codex_evidence_use_image_stage_only(self):
        prompt = generator.build_image_prompt({"custom_text": TEXT}, "one selected style")
        self.assertIn(json.dumps(TEXT, ensure_ascii=False), prompt)
        self.assertIn("one selected style", prompt)
        self.assertIn("Do not run news research", prompt)
        self.assertNotIn("Sourced text:", prompt)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            generated = root / "generated"
            backend = generator.CodexImageBackend(generated)

            def fake_run(command, **kwargs):
                image = generated / "new" / "image.png"
                image.parent.mkdir(parents=True)
                image.write_bytes(picture())
                Path(command[command.index("--output-last-message") + 1]).write_text(str(image))
                return types.SimpleNamespace(stderr=b"image generated", returncode=0)

            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=fake_run):
                self.assertEqual(picture(), backend.generate(
                    {"custom_text": TEXT}, work, style_prompt="one selected style",
                    evidence=PromptTarget(root, -1, "image", 1)))
            evidence = json.loads((root / "prompt_evidence/slot-1/image-1.json").read_text())
            self.assertEqual("artifact_returned", evidence["status"])
            self.assertIn(json.dumps(TEXT, ensure_ascii=False),
                          evidence["submitted_prompt"])
            self.assertFalse((root / "prompt_evidence/slot-1/news-1.json").exists())

    def test_manual_acceptance_snapshots_style_and_only_latest_custom_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text((ROOT / "config.sample").read_text().replace(
                'selected_style_id = "newspaper_cartoon"',
                'selected_style_id = "shadow_play"').replace(
                'selected_topic_id = "ai_news"', 'selected_topic_id = "removed_topic"'))
            manager = manual.ManualManager(Archive(root / "state"), config)
            self.assertEqual("invalid_topic", manager.status()["state"])
            with patch.object(manual.subprocess, "Popen"):
                status, started = manager.start(TEXT)
            self.assertTrue(started)
            self.assertEqual("custom", status["kind"])
            self.assertEqual(TEXT, manager.last_custom()["custom_text"])
            request_path = manager.requests / (status["run_id"] + ".json")
            request = json.loads(request_path.read_text())
            self.assertEqual("shadow_play", request["selection"]["style_id"])
            self.assertEqual(generator.CUSTOM_TOPIC_ID, request["selection"]["topic_id"])
            self.assertNotIn("source_url", request)
            config.write_text(config.read_text().replace(
                'selected_style_id = "shadow_play"',
                'selected_style_id = "newspaper_cartoon"'))
            seen = {}
            def fake_once(*args, **kwargs):
                seen.update(kwargs)
                return "already attempted"
            with patch.object(manual, "run_once", side_effect=fake_once), \
                 patch.object(manual.sys, "argv", ["manual", str(manager.archive.root),
                                                   str(config), status["run_id"]]):
                manual.main()
            self.assertEqual("shadow_play", seen["selection_override"]["style_id"])
            self.assertEqual(TEXT, seen["custom_text"])
            self.assertFalse(request_path.exists())
            self.assertEqual(TEXT, manual.ManualManager(Archive(root / "state"), config)
                             .last_custom()["custom_text"])

    def test_rejected_custom_text_does_not_replace_last_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text((ROOT / "config.sample").read_text())
            manager = manual.ManualManager(Archive(root / "state"), config)
            with patch.object(manual.subprocess, "Popen"):
                first, started = manager.start(TEXT)
            self.assertTrue(started)
            with self.assertRaises(ValueError):
                manager.start("   ")
            second, started = manager.start("次の文章")
            self.assertFalse(started)
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(TEXT, manager.last_custom()["custom_text"])
            manager.finish(first["run_id"], "failed")
            with patch.object(manual.subprocess, "Popen", side_effect=OSError("no process")):
                status, started = manager.start("未受理の文章")
            self.assertFalse(started)
            self.assertEqual("failed", status["state"])
            self.assertEqual(TEXT, manager.last_custom()["custom_text"])


class CustomHttpTests(unittest.TestCase):
    def test_http_to_worker_to_archive_without_news_or_external_image_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            original_config = (ROOT / "config.sample").read_text()
            config.write_text(original_config)
            source = root / "source.png"
            source.write_bytes(picture())
            command = root / "fake_image_backend.py"
            command.write_text(
                "#!/usr/bin/env python3\n"
                "import json,shutil,sys\n"
                "from pathlib import Path\n"
                "if sys.argv[1]=='--capabilities':\n"
                " print(json.dumps({'schema_version':1,'output_png':True}));sys.exit(0)\n"
                "request=json.loads(Path(sys.argv[2]).read_text())\n"
                "assert request['input_kind']=='custom'\n"
                "assert request['source_refs']==[]\n"
                f"shutil.copyfile({str(source)!r},sys.argv[3])\n")
            command.chmod(0o700)
            archive = Archive(root / "state")
            server = FrameServer(("127.0.0.1", 0), archive,
                                 allowed_network="127.0.0.0/8", config_path=config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = "127.0.0.1:" + str(server.server_port)
            def request(method, path, body=None, headers=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request(method, path, body=body, headers=headers or {})
                reply = conn.getresponse()
                result = (reply.status, reply.read())
                conn.close()
                return result
            headers = {"Content-Type": "application/json", "Origin": "http://" + host,
                       "Host": host, "X-AI-News-Action": "generate",
                       "Sec-Fetch-Site": "same-origin"}
            try:
                with patch.dict(os.environ, {"AI_NEWS_IMAGE_COMMAND": str(command)}):
                    status, result = request("POST", "/v1/generate",
                        json.dumps({"custom_text": TEXT}, ensure_ascii=False).encode(), headers)
                self.assertEqual(202, status)
                run_id = json.loads(result)["run_id"]
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    state = json.loads(request("GET", "/v1/generate/status")[1])
                    if state["state"] in ("succeeded", "failed", "unchanged"):
                        break
                    time.sleep(.1)
                self.assertEqual("succeeded", state["state"])
                self.assertEqual(run_id, state["run_id"])
                server.manual.child.wait(timeout=3)
                latest = archive.latest()
                self.assertIsNotNone(latest)
                self.assertEqual(4000, archive.frame_path(latest["frame_id"], "raw")
                                 .stat().st_size)
                entry = next(gallery.entries(archive))
                self.assertEqual(TEXT, entry["custom_text"])
                self.assertEqual([], entry["source_urls"])
                self.assertEqual(original_config, config.read_text())
                self.assertEqual([], list((archive.root / "news").glob("*.json")))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_post_acceptance_and_reload_restore_last_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text((ROOT / "config.sample").read_text())
            server = FrameServer(("127.0.0.1", 0), Archive(root / "state"),
                                 allowed_network="127.0.0.0/8", config_path=config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = "127.0.0.1:" + str(server.server_port)
            def request(method, path, body=None, headers=None):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                conn.request(method, path, body=body, headers=headers or {})
                reply = conn.getresponse()
                result = (reply.status, reply.read())
                conn.close()
                return result
            headers = {"Content-Type": "application/json", "Origin": "http://" + host,
                       "Host": host, "X-AI-News-Action": "generate",
                       "Sec-Fetch-Site": "same-origin"}
            try:
                self.assertEqual({"custom_text": ""}, json.loads(
                    request("GET", "/v1/generate/custom-text")[1]))
                self.assertEqual(400, request("POST", "/v1/generate",
                    json.dumps({"custom_text": " "}), headers)[0])
                with patch.object(manual.subprocess, "Popen"):
                    status, data = request("POST", "/v1/generate",
                        json.dumps({"custom_text": TEXT}, ensure_ascii=False).encode(), headers)
                self.assertEqual(202, status)
                self.assertEqual("custom", json.loads(data)["kind"])
                self.assertEqual(TEXT, json.loads(request(
                    "GET", "/v1/generate/custom-text")[1])["custom_text"])
                self.assertEqual(409, request("POST", "/v1/generate", b"{}", headers)[0])
                self.assertEqual(403, request("POST", "/v1/generate",
                    json.dumps({"custom_text": "bad"}),
                    {**headers, "Origin": "http://evil.example"})[0])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
