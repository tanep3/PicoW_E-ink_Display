"""Manual slot records stay readable and survive legacy filename migration."""

import json
from pathlib import Path
import tempfile
import unittest

from ai_news.archive import Archive, json_bytes
from ai_news.prompt_evidence import PromptEvidence, PromptTarget


NEWS = {"source_url": "https://example.com/news", "summary": "Example"}


class SlotNameTests(unittest.TestCase):
    def test_news_uses_safe_manual_name_and_keeps_hourly_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root)
            archive.save_news(-1, NEWS)
            archive.save_news(40, NEWS)
            self.assertTrue((root / "news/slot-1.json").is_file())
            self.assertFalse((root / "news/-1.json").exists())
            self.assertTrue((root / "news/40.json").is_file())
            self.assertEqual(NEWS, archive.load_news(-1))

    def test_legacy_news_is_read_migrated_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = Archive(root)
            legacy = root / "news/-2.json"
            legacy.write_bytes(json_bytes(NEWS))
            self.assertEqual(NEWS, archive.load_news(-2))
            with self.assertRaisesRegex(ValueError, "saved news differs"):
                archive.save_news(-2, {**NEWS, "summary": "Different"})
            self.assertTrue(legacy.is_file())
            archive.save_news(-2, NEWS)
            self.assertFalse(legacy.exists())
            self.assertEqual(NEWS, archive.load_news(-2))
            self.assertTrue((root / "news/slot-2.json").is_file())

    def test_legacy_evidence_moves_with_previous_attempt_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "prompt_evidence/-3"
            legacy.mkdir(parents=True)
            prior = json_bytes({"status": "timeout", "attempt": 1})
            (legacy / "image-1.json").write_bytes(prior)
            command = ["codex", "exec", "--model", "gpt-6-luna", "test prompt"]
            with PromptEvidence(PromptTarget(root, -3, "image", 2), command) as proof:
                proof.exited(0)
                proof.accepted("image_png", b"test image")
            folder = root / "prompt_evidence/slot-3"
            self.assertFalse(legacy.exists())
            self.assertEqual(prior, (folder / "image-1.json").read_bytes())
            self.assertEqual("artifact_returned", json.loads(
                (folder / "image-2.json").read_text())["status"])

    def test_conflicting_evidence_directories_block_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompt_evidence/-3").mkdir(parents=True)
            (root / "prompt_evidence/slot-3").mkdir()
            target = PromptTarget(root, -3, "news", 1)
            command = ["codex", "exec", "--model", "gpt-6-luna", "test prompt"]
            with self.assertRaisesRegex(FileExistsError, "conflicting"):
                with PromptEvidence(target, command):
                    pass


if __name__ == "__main__":
    unittest.main()
