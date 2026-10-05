"""Selected artwork styles remain independent and stable across a job's lifetime."""
from datetime import datetime, timezone
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import tomllib
import types
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news import archive, generator, manual, topics
from ai_news.retry_config import DEFAULT_STYLE, load_retry_config
from ai_news.server import FrameServer


SAMPLE = Path(__file__).resolve().parents[1] / "config.sample"
EXPECTED_LABELS = (
    "新聞風刺画", "鳥山明風の冒険漫画", "昭和のキラキラ少女漫画", "昭和のギャグ漫画",
    "木版画・浮世絵調", "影絵", "レトロゲームのドット絵", "取扱説明書のピクトグラム",
    "昔の科学雑誌の挿絵", "白黒の旅行ポスター", "ゆるい一コマ漫画", "リーニュクレール",
    "切り絵・ステンシル", "ミニマルな輪郭線画", "昭和の看板・広告画", "昭和の映画ポスター風",
)
NEWS = {"source_url": "https://example.com/story", "summary": "A sourced AI news story"}


def art():
    image = Image.new("L", (250, 122), 255)
    image.putpixel((17, 21), 0)
    output = BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class StyleTests(unittest.TestCase):
    def test_exact_catalog_and_legacy_default(self):
        sample = tomllib.loads(SAMPLE.read_text())
        styles = sample["styles"]["items"]
        self.assertEqual(EXPECTED_LABELS, tuple(item["label"] for item in styles))
        self.assertEqual(16, len({item["id"] for item in styles}))
        self.assertEqual("newspaper_cartoon", sample["styles"]["selected_style_id"])
        self.assertEqual(DEFAULT_STYLE.prompt, styles[0]["prompt"])
        descriptions = {item["id"]: item["prompt"] for item in styles}
        self.assertIn("シルエット", descriptions["shadow_play"])
        self.assertIn("切り抜いた", descriptions["papercut_stencil"])
        self.assertIn("背景", descriptions["ligne_claire"])
        self.assertIn("余白", descriptions["minimal_outline"])
        self.assertIn("鳥山明", descriptions["toriyama_adventure"])
        self.assertIn("複製しない", descriptions["toriyama_adventure"])
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "config"
            old.write_text("[news]\nselected_topic_id='ai_news'\n[image]\nretry_count=2\n")
            config = load_retry_config(old)
            self.assertEqual("newspaper_cartoon", config.style.id)
            self.assertEqual(1, len(config.styles))

    def test_validation_and_deleted_style_requires_reselection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            for text in ("[styles]\nitems=[]\n", "[styles]\nselected_style_id='@'\n",
                         "[styles]\n[[styles.items]]\nid='a'\nlabel='A'\n",
                         "[styles]\n[[styles.items]]\nid='a'\nlabel='A'\nprompt='x'\n"
                         "[[styles.items]]\nid='a'\nlabel='B'\nprompt='y'\n"):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_retry_config(path)
            path.write_text("[styles]\nselected_style_id='removed'\n"
                            "[[styles.items]]\nid='one'\nlabel='一'\nprompt='太い線'\n")
            self.assertFalse(topics.style_snapshot(path)["selection_valid"])
            self.assertIsNone(topics.style_snapshot(path)["selected_style_id"])
            self.assertEqual("invalid_style", manual.ManualManager(
                archive.Archive(Path(tmp) / "state"), path).status()["state"])
            self.assertEqual("one", topics.save_selected_style("one", path))
            self.assertTrue(topics.style_snapshot(path)["selection_valid"])

    def test_dynamic_config_reads_and_independent_atomic_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text(SAMPLE.read_text())
            original = tomllib.loads(path.read_text())
            self.assertEqual("shadow_play", topics.save_selected_style("shadow_play", path))
            self.assertEqual("travel", topics.save_selected_topic("travel", path))
            after = tomllib.loads(path.read_text())
            self.assertEqual("shadow_play", after["styles"]["selected_style_id"])
            self.assertEqual("travel", after["news"]["selected_topic_id"])
            self.assertEqual(original["image"], after["image"])
            self.assertEqual(original["push"], after["push"])
            self.assertEqual(original["styles"]["items"], after["styles"]["items"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            unchanged = path.read_bytes()
            with patch.object(topics, "atomic_write", side_effect=OSError("full")):
                with self.assertRaises(OSError):
                    topics.save_selected_style("pixel_art", path)
            self.assertEqual(unchanged, path.read_bytes())
            with self.assertRaises(ValueError):
                topics.save_selected_style("absent", path)
            self.assertEqual(unchanged, path.read_bytes())
            # Edit and reorder entries while the server is conceptually running.
            blocks = path.read_text().split("[[styles.items]]")
            header, entries = blocks[0], ["[[styles.items]]" + item for item in blocks[1:]]
            path.write_text(header + "".join(reversed(entries)) +
                            "\n[[styles.items]]\nid='new_style'\nlabel='新しい画風'\n"
                            "prompt='太い斜線と大きな黒面'\n")
            snap = topics.style_snapshot(path)
            self.assertEqual("new_style", snap["styles"][-1]["id"])
            self.assertEqual("shadow_play", snap["selected_style_id"])
            self.assertEqual("太い斜線と大きな黒面", snap["styles"][-1]["prompt"])
            self.assertEqual("new_style", topics.save_selected_style("new_style", path))

    def test_prompts_put_style_only_in_image_call(self):
        style = load_retry_config(SAMPLE).styles[5]
        news_prompt = generator.build_news_prompt("AIニュースを探す", set(),
                                                   datetime(2026, 10, 4, tzinfo=timezone.utc))
        image_prompt = generator.build_image_prompt(NEWS, style.prompt)
        self.assertNotIn(style.prompt, news_prompt)
        self.assertIn(style.prompt, image_prompt)
        self.assertIn("250x122", image_prompt)
        self.assertIn("1-bit", image_prompt)
        self.assertIn("original composition", image_prompt)
        self.assertNotIn("Usually make a witty editorial cartoon", image_prompt)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generated = root / "generated_images"
            output = generated / "new" / "image.png"
            seen = []
            def fake_run(command, **kwargs):
                seen.append(command[-1])
                output.parent.mkdir(parents=True)
                output.write_bytes(art())
                Path(command[command.index("--output-last-message") + 1]).write_text(str(output))
                return types.SimpleNamespace(stderr=b"", returncode=0)
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=fake_run):
                self.assertEqual(art(), generator.CodexImageBackend(generated).generate(
                    NEWS, root, style_prompt=style.prompt))
            self.assertIn(style.prompt, seen[0])

    def test_style_and_topic_snapshot_survive_image_crash_and_config_edit(self):
        class Backend:
            def __init__(self): self.styles = []
            def probe(self): return {"schema_version": 1, "output_png": True}
            def generate(self, news, work, *, feedback, timeout, style_prompt):
                self.styles.append(style_prompt)
                if len(self.styles) == 1:
                    raise KeyboardInterrupt("simulated crash")
                return art()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generator, "public_url", side_effect=lambda url: url):
            root, path = Path(tmp) / "state", Path(tmp) / "config"
            path.write_text(SAMPLE.read_text().replace(
                'selected_style_id = "newspaper_cartoon"',
                'selected_style_id = "shadow_play"'))
            first_config = load_retry_config(path)
            backend = Backend()
            now = datetime(2026, 10, 4, tzinfo=timezone.utc)
            with self.assertRaises(KeyboardInterrupt):
                generator.run_once(root, backend, now=now, config=first_config,
                                   news_fetcher=lambda *a: NEWS)
            path.write_text(path.read_text().replace(
                'selected_style_id = "shadow_play"', 'selected_style_id = "minimal_outline"'
            ).replace('selected_topic_id = "ai_news"', 'selected_topic_id = "travel"'))
            self.assertEqual("published", generator.run_once(
                root, backend, now=now, config=load_retry_config(path),
                news_fetcher=lambda *a: self.fail("news must be reused")))
            self.assertEqual([first_config.style.prompt] * 2, backend.styles)
            store = archive.Archive(root)
            selected = store.job_selection(int(now.timestamp()) // 3600)
            self.assertEqual(("ai_news", "shadow_play"),
                             (selected["topic_id"], selected["style_id"]))
            latest = store.latest()
            record = json.loads(store.frame_path(latest["frame_id"], "json").read_text())
            self.assertEqual("shadow_play", record["metadata"]["style_id"])
            self.assertEqual(first_config.style.prompt, record["metadata"]["style_prompt"])

    def test_topic_snapshot_survives_news_crash_and_config_edit(self):
        class Backend:
            def probe(self): return {"schema_version": 1, "output_png": True}
            def generate(self, *a, **kw): return art()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(generator, "public_url", side_effect=lambda url: url):
            root, path = Path(tmp) / "state", Path(tmp) / "config"
            path.write_text(SAMPLE.read_text())
            now = datetime(2026, 10, 4, tzinfo=timezone.utc)
            with self.assertRaises(KeyboardInterrupt):
                generator.run_once(root, Backend(), now=now,
                                   config=load_retry_config(path),
                                   news_fetcher=lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
            path.write_text(path.read_text().replace(
                'selected_topic_id = "ai_news"', 'selected_topic_id = "travel"'))
            chosen = []
            def fake_editor(work, now, timeout, feedback, *, topic_prompt, excluded_urls, evidence):
                chosen.append(topic_prompt)
                return NEWS
            with patch.object(generator, "codex_editor", side_effect=fake_editor):
                self.assertEqual("published", generator.run_once(
                    root, Backend(), now=now, config=load_retry_config(path)))
            self.assertEqual([load_retry_config(SAMPLE).topic.prompt], chosen)

    def test_http_style_api_reads_fresh_config_and_preserves_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, path = Path(tmp) / "state", Path(tmp) / "config"
            path.write_text(SAMPLE.read_text())
            server = FrameServer(("127.0.0.1", 0), archive.Archive(root),
                                 allowed_network="127.0.0.0/8", config_path=path)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = "127.0.0.1:" + str(server.server_port)
                def request(method, url, body=None, headers=None):
                    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request(method, url, body=body, headers=headers or {})
                    response = conn.getresponse()
                    result = response.status, response.read()
                    conn.close()
                    return result
                headers = {"Origin": "http://" + host, "X-AI-News-Action": "save-style",
                           "Content-Type": "application/json"}
                self.assertEqual(200, request("GET", "/settings/")[0])
                current = json.loads(request("GET", "/v1/styles")[1])
                self.assertEqual(16, len(current["styles"]))
                self.assertEqual(403, request("POST", "/v1/styles",
                                              b'{"style_id":"shadow_play"}',
                                              {**headers, "Origin": "http://elsewhere"})[0])
                self.assertEqual(200, request("POST", "/v1/styles",
                                              b'{"style_id":"shadow_play"}', headers)[0])
                self.assertEqual("shadow_play", topics.style_snapshot(path)["selected_style_id"])
                self.assertEqual("ai_news", topics.topic_snapshot(path)["selected_topic_id"])
                self.assertEqual(400, request("POST", "/v1/styles",
                                              b'{"style_id":"unknown"}', headers)[0])
                path.write_text(path.read_text().replace("label = \"影絵\"",
                                                       "label = \"新しい影絵\""))
                fresh = json.loads(request("GET", "/v1/styles")[1])
                self.assertEqual("新しい影絵", next(item["label"] for item in fresh["styles"]
                                               if item["id"] == "shadow_play"))
                path.write_text(path.read_text().replace(
                    'selected_style_id = "shadow_play"', 'selected_style_id = "missing"'))
                missing = json.loads(request("GET", "/v1/styles")[1])
                self.assertFalse(missing["selection_valid"])
                self.assertIsNone(missing["selected_style_id"])
                self.assertEqual("invalid_style", json.loads(request(
                    "GET", "/v1/generate/status")[1])["state"])
                self.assertEqual(409, request("POST", "/v1/generate", b"{}", {
                    "Origin": "http://" + host, "X-AI-News-Action": "generate",
                    "Content-Type": "application/json"})[0])
            finally:
                server.shutdown();server.server_close();thread.join(3)


if __name__ == "__main__":
    unittest.main()
