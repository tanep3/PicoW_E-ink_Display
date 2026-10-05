"""Model selection across config, jobs, CLI calls, and durable records."""

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news.archive import Archive
from ai_news import generator
from ai_news.manual import ManualManager
from ai_news.prompt_evidence import PromptTarget
from ai_news.retry_config import DEFAULT_CODEX_MODEL, load_retry_config
from ai_news.topics import save_selected_style, save_selected_topic


def png() -> bytes:
    image = Image.new("RGB", (500, 244), "white")
    image.putpixel((12, 12), (0, 0, 0))
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class ModelConfigTests(unittest.TestCase):
    NEWS = {"source_url": "https://example.com/story", "summary": "A sourced story"}

    def test_defaults_independent_values_and_existing_selection_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text("[news]\nselected_topic_id='ai_news'\n[image]\nretry_count=2\n")
            default = load_retry_config(path)
            self.assertEqual((default.news_model, default.image_model),
                             (DEFAULT_CODEX_MODEL, DEFAULT_CODEX_MODEL))
            path.write_text("[news]\nmodel='gpt-6.1-sol'\nselected_topic_id='ai_news'\n"
                            "[image]\nmodel='provider/model.v2'\nretry_count=2\n")
            config = load_retry_config(path)
            self.assertEqual((config.news_model, config.image_model),
                             ("gpt-6.1-sol", "provider/model.v2"))
            save_selected_topic("ai_news", path)
            save_selected_style("newspaper_cartoon", path)
            self.assertEqual((load_retry_config(path).news_model,
                              load_retry_config(path).image_model),
                             ("gpt-6.1-sol", "provider/model.v2"))
            for bad in ("", "-bad", "has space", "bad\nline"):
                path.write_text("[news]\nmodel=" + json.dumps(bad) + "\n")
                with self.assertRaisesRegex(ValueError, "news.model"):
                    load_retry_config(path)

    def test_existing_database_rows_migrate_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with sqlite3.connect(root / "jobs.sqlite3") as conn:
                conn.execute("""CREATE TABLE job_selections (
                    slot INTEGER PRIMARY KEY, topic_id TEXT NOT NULL,
                    topic_label TEXT NOT NULL, topic_prompt TEXT NOT NULL,
                    style_id TEXT NOT NULL, style_label TEXT NOT NULL,
                    style_prompt TEXT NOT NULL)""")
                conn.execute("INSERT INTO job_selections VALUES(1,'ai_news','AI','news',"
                             "'newspaper_cartoon','Cartoon','style')")
            store = Archive(root)
            self.assertEqual(store.job_selection(1)["news_model"], DEFAULT_CODEX_MODEL)
            self.assertEqual(store.job_selection(1)["image_model"], DEFAULT_CODEX_MODEL)
            Archive(root)  # Migration is repeatable.

    def test_hourly_job_snapshots_both_models_into_cli_and_metadata(self):
        class Backend(generator.CodexImageBackend):
            def __init__(self):
                super().__init__(generated_root=Path("/unused"))
                self.calls = []
            def probe(self, *, model=None):
                self.calls.append(("probe", model))
                return {"backend": "codex-headless-imagegen", "requested_model": model}
            def generate(self, news, work, *, feedback, timeout, style_prompt, evidence, model=None):
                self.calls.append(("generate", model))
                return png()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            path = Path(tmp) / "config"
            path.write_text("[news]\nmodel='gpt-6.1-sol'\n"
                            "[image]\nmodel='provider/model.v2'\n")
            backend = Backend()
            editor_models = []
            def editor(*args, **kwargs):
                editor_models.append(kwargs["model"])
                return self.NEWS
            now = datetime(2026, 10, 5, tzinfo=timezone.utc)
            with patch.object(generator, "codex_editor", side_effect=editor), \
                 patch.object(generator, "public_url", side_effect=lambda x: x):
                self.assertEqual(generator.run_once(root, backend, now=now,
                                                     config=load_retry_config(path)), "published")
            self.assertEqual(editor_models, ["gpt-6.1-sol"])
            self.assertEqual(backend.calls,
                             [("probe", "provider/model.v2"),
                              ("generate", "provider/model.v2")])
            store = Archive(root)
            slot = int(now.timestamp()) // 3600
            selected = store.job_selection(slot)
            self.assertEqual((selected["news_model"], selected["image_model"]),
                             ("gpt-6.1-sol", "provider/model.v2"))
            frame_id = store.latest()["frame_id"]
            metadata = json.loads(store.frame_path(frame_id, "json").read_text())["metadata"]
            self.assertEqual((metadata["news_model"], metadata["image_model"], metadata["model"]),
                             ("gpt-6.1-sol", "provider/model.v2", "provider/model.v2"))

    def test_manual_acceptance_snapshots_models_before_config_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            path = Path(tmp) / "config"
            path.write_text("[news]\nmodel='gpt-6.1-sol'\n"
                            "[image]\nmodel='provider/model.v2'\n")
            manager = ManualManager(Archive(root), path)
            with patch("ai_news.manual.subprocess.Popen", return_value=object()):
                status, started = manager.start("a custom scene")
            self.assertTrue(started)
            request = json.loads((manager.requests / (status["run_id"] + ".json")).read_text())
            self.assertEqual((request["selection"]["news_model"],
                              request["selection"]["image_model"]),
                             ("gpt-6.1-sol", "provider/model.v2"))
            path.write_text("[news]\nmodel='newer-news'\n[image]\nmodel='newer-image'\n")
            captured = []
            with patch("ai_news.manual.run_once", side_effect=lambda *a, **kw: captured.append(kw) or "already attempted"), \
                 patch("ai_news.manual.sys.argv", ["manual", str(root), str(path), status["run_id"]]):
                from ai_news import manual
                manual.main()
            self.assertEqual(captured[0]["selection_override"]["image_model"],
                             "provider/model.v2")
            self.assertEqual(captured[0]["selection_override"]["news_model"],
                             "gpt-6.1-sol")

    def test_cli_evidence_uses_requested_models_and_nonzero_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            seen = []
            def news_run(command, **kwargs):
                seen.append(command)
                Path(command[command.index("--output-last-message") + 1]).write_text(
                    json.dumps(self.NEWS))
                return types.SimpleNamespace(returncode=0, stderr=b"")
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=news_run):
                generator.codex_editor(work, datetime(2026, 10, 5, tzinfo=timezone.utc),
                                       model="gpt-6.1-sol",
                                       evidence=PromptTarget(root, 1, "news", 1))
            self.assertEqual(seen[0][seen[0].index("--model") + 1], "gpt-6.1-sol")
            news_proof = json.loads((root / "prompt_evidence/1/news-1.json").read_text())
            self.assertEqual(news_proof["model"], "gpt-6.1-sol")

            generated = root / "generated"
            backend = generator.CodexImageBackend(generated)
            with self.assertRaisesRegex(ValueError, "model"):
                backend.probe(model="")
            with self.assertRaisesRegex(ValueError, "model"):
                backend.generate(self.NEWS, work, model="")
            def image_run(command, **kwargs):
                seen.append(command)
                target = generated / "this-run" / "original.png"
                target.parent.mkdir(parents=True)
                target.write_bytes(png())
                Path(command[command.index("--output-last-message") + 1]).write_text(str(target))
                return types.SimpleNamespace(returncode=0, stderr=b"")
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=image_run):
                self.assertEqual(backend.generate(self.NEWS, work, model="provider/model.v2",
                                                  evidence=PromptTarget(root, 1, "image", 1)), png())
            self.assertEqual(seen[1][seen[1].index("--model") + 1], "provider/model.v2")
            image_proof = json.loads((root / "prompt_evidence/1/image-1.json").read_text())
            self.assertEqual(image_proof["model"], "provider/model.v2")

            def failure(command, **kwargs):
                seen.append(command)
                return types.SimpleNamespace(returncode=1, stderr=b"unsupported model")
            with patch.object(generator.shutil, "which", return_value="/usr/bin/codex"), \
                 patch.object(generator.subprocess, "run", side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, "news execution failed for model missing-news"):
                    generator.codex_editor(work, datetime(2026, 10, 5, tzinfo=timezone.utc),
                                           model="missing-news")
                with self.assertRaisesRegex(RuntimeError, "image execution failed for model missing-image"):
                    backend.generate(self.NEWS, work, model="missing-image")
            self.assertEqual(seen[-2][seen[-2].index("--model") + 1], "missing-news")
            self.assertEqual(seen[-1][seen[-1].index("--model") + 1], "missing-image")


if __name__ == "__main__":
    unittest.main()
