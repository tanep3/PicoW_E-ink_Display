"""Codex argv evidence survives results, retries, errors and abrupt termination."""

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news import generator
from ai_news.generator import CodexImageBackend, codex_editor
from ai_news.prompt_evidence import PromptEvidence, PromptTarget
from ai_news.retry_config import load_retry_config


NEWS = {"source_url": "https://example.com/story", "summary": "An example source summary"}


def png_bytes():
    output = BytesIO()
    Image.new("1", (250, 122), 1).save(output, "PNG")
    return output.getvalue()


class PromptEvidenceTests(unittest.TestCase):
    def test_news_full_prompt_matches_actual_subprocess_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            captured = []

            def fake_run(command, **kwargs):
                captured.append(command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(NEWS))
                return subprocess.CompletedProcess(command, 0, stderr=b"")

            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.generator.subprocess.run", side_effect=fake_run):
                self.assertEqual(NEWS, codex_editor(
                    work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                    topic_prompt="example topic", evidence=PromptTarget(root, -8, "news", 1)))

            path = root / "prompt_evidence/-8/news-1.json"
            record = json.loads(path.read_text())
            self.assertEqual(captured[0][-1], record["submitted_prompt"])
            self.assertEqual(captured[0][:-1], record["argv_without_prompt"])
            self.assertEqual("gpt-6-luna", record["model"])
            self.assertEqual("artifact_returned", record["status"])
            self.assertEqual("news_json", record["artifact_kind"])
            self.assertEqual(0, record["cli_returncode"])
            self.assertEqual(hashlib.sha256(captured[0][-1].encode()).hexdigest(),
                             record["prompt_sha256"])
            self.assertIsNotNone(record["finished_at"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o700, path.parent.parent.stat().st_mode & 0o777)
            self.assertNotIn("environment", record)

    def test_image_full_prompt_matches_actual_subprocess_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            captured = []
            image = png_bytes()

            def fake_run(command, **kwargs):
                captured.append(command)
                result = Path(command[command.index("--output-last-message") + 1])
                path = work / "actual.png"
                path.write_bytes(image)
                result.write_text(str(path))
                return subprocess.CompletedProcess(command, 0, stderr=b"")

            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.generator.subprocess.run", side_effect=fake_run):
                output = CodexImageBackend(root / "generated").generate(
                    NEWS, work, style_prompt="original shadow style",
                    evidence=PromptTarget(root, -8, "image", 1))
            self.assertEqual(image, output)
            record = json.loads((root / "prompt_evidence/-8/image-1.json").read_text())
            self.assertEqual(captured[0][-1], record["submitted_prompt"])
            self.assertEqual(captured[0][:-1], record["argv_without_prompt"])
            self.assertIn("original shadow style", record["submitted_prompt"])
            self.assertEqual("image_png", record["artifact_kind"])
            self.assertEqual(hashlib.sha256(image).hexdigest(), record["artifact_sha256"])
            self.assertEqual("artifact_returned", record["status"])

    def test_failure_timeout_and_retry_have_distinct_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            seen = []

            def fake_run(command, **kwargs):
                seen.append(command)
                if len(seen) == 1:
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(NEWS))
                return subprocess.CompletedProcess(command, 0, stderr=b"")

            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.generator.subprocess.run", side_effect=fake_run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    codex_editor(work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                                 evidence=PromptTarget(root, 40, "news", 1))
                codex_editor(work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                             feedback="prior timeout",
                             evidence=PromptTarget(root, 40, "news", 2))
            first = json.loads((root / "prompt_evidence/40/news-1.json").read_text())
            second = json.loads((root / "prompt_evidence/40/news-2.json").read_text())
            self.assertEqual("timeout", first["status"])
            self.assertEqual("TimeoutExpired", first["error_type"])
            self.assertEqual("artifact_returned", second["status"])
            self.assertNotEqual(first["prompt_sha256"], second["prompt_sha256"])
            self.assertEqual(seen[0][-1], first["submitted_prompt"])
            self.assertEqual(seen[1][-1], second["submitted_prompt"])

    def test_bad_result_and_launch_error_remain_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.generator.subprocess.run", return_value=subprocess.CompletedProcess(
                        ["codex"], 7, stderr=b"failed")):
                with self.assertRaises(RuntimeError):
                    codex_editor(work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                                 evidence=PromptTarget(root, 41, "news", 1))
            failed = json.loads((root / "prompt_evidence/41/news-1.json").read_text())
            self.assertEqual("failed", failed["status"])
            self.assertEqual(7, failed["cli_returncode"])
            self.assertEqual("RuntimeError", failed["error_type"])
            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.generator.subprocess.run", side_effect=OSError("spawn refused")):
                with self.assertRaises(OSError):
                    codex_editor(work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                                 evidence=PromptTarget(root, 41, "news", 2))
            launch = json.loads((root / "prompt_evidence/41/news-2.json").read_text())
            self.assertEqual("launch_error", launch["status"])
            self.assertIsNone(launch["cli_returncode"])
            self.assertNotIn("spawn refused", json.dumps(launch))

    def test_abrupt_process_exit_leaves_prepared_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ("import os,sys;from pathlib import Path;"
                      "from ai_news.prompt_evidence import PromptEvidence,PromptTarget;"
                      "target=PromptTarget(Path(sys.argv[1]),12,'image',1);"
                      "command=['/usr/bin/codex','exec','--model','gpt-6-luna','prompt'];"
                      "with PromptEvidence(target,command):os._exit(9)")
            # A newline is needed before the compound with statement.
            script = script.replace(";with PromptEvidence", "\nwith PromptEvidence")
            result = subprocess.run([sys.executable, "-c", script, tmp],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(9, result.returncode, result.stderr.decode())
            record = json.loads((Path(tmp) / "prompt_evidence/12/image-1.json").read_text())
            self.assertEqual("prepared", record["status"])
            self.assertIsNone(record["finished_at"])

    def test_evidence_write_failure_prevents_codex_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            with patch("ai_news.generator.shutil.which", return_value="/usr/bin/codex"), \
                    patch("ai_news.prompt_evidence.atomic_write", side_effect=OSError("disk full")), \
                    patch("ai_news.generator.subprocess.run") as run:
                with self.assertRaises(OSError):
                    codex_editor(work, datetime(2026, 10, 4, tzinfo=timezone.utc),
                                 evidence=PromptTarget(root, 42, "news", 1))
                run.assert_not_called()

    def test_interruption_and_duplicate_attempt_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = PromptTarget(Path(tmp), -9, "image", 3)
            command = ["/usr/bin/codex", "exec", "--model", "gpt-6-luna", "prompt"]
            with self.assertRaises(KeyboardInterrupt):
                with PromptEvidence(target, command):
                    raise KeyboardInterrupt()
            path = Path(tmp) / "prompt_evidence/-9/image-3.json"
            original = path.read_bytes()
            record = json.loads(original)
            self.assertEqual("interrupted", record["status"])
            self.assertEqual("KeyboardInterrupt", record["error_type"])
            with self.assertRaises(FileExistsError):
                with PromptEvidence(target, command):
                    pass
            self.assertEqual(original, path.read_bytes())

    def test_run_once_binds_news_retries_and_image_to_job_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            targets = []

            def fake_editor(*args, **kwargs):
                targets.append(kwargs["evidence"])
                if len(targets) == 1:
                    raise RuntimeError("temporary news failure")
                return NEWS

            class Backend(CodexImageBackend):
                def probe(self):
                    return {"backend": "codex-headless-imagegen"}

                def generate(self, news, work, *, feedback, timeout, style_prompt, evidence):
                    targets.append(evidence)
                    return png_bytes()

            now = datetime(2026, 10, 4, tzinfo=timezone.utc)
            sample = Path(__file__).resolve().parents[1] / "config.sample"
            with patch.object(generator, "codex_editor", side_effect=fake_editor), \
                    patch.object(generator, "public_url", side_effect=lambda value: value):
                self.assertEqual("published", generator.run_once(
                    root, Backend(), now=now, config=load_retry_config(sample)))
            slot = int(now.timestamp()) // 3600
            self.assertEqual([(slot, "news", 1), (slot, "news", 2), (slot, "image", 1)],
                             [(t.slot, t.stage, t.attempt) for t in targets])
            self.assertTrue(all(t.root == root for t in targets))


if __name__ == "__main__":
    unittest.main()
