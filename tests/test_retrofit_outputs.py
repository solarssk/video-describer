import tempfile
import unittest
from pathlib import Path

from batch_metadata import split_metadata_footer
from retrofit_outputs import plan_retrofit, retrofit_existing_outputs


class RetrofitOutputsTests(unittest.TestCase):
    """Regression tests for upgrading existing .txt outputs in place."""

    def test_legacy_output_is_renamed_and_metadata_is_added(self):
        """Legacy stem.txt files move to name.ext.txt and receive metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "clip.mp4"
            legacy = tmp_path / "clip.txt"
            new_output = tmp_path / "clip.mp4.txt"
            source.write_text("")
            legacy.write_text("clip.mp4 - desc\n00:01 event\n", encoding="utf-8")

            result = retrofit_existing_outputs(
                [(source, "video")],
                batch_id="retrofit-test",
                model="test-model",
            )

            self.assertFalse(legacy.exists())
            self.assertTrue(new_output.exists())
            body, metadata = split_metadata_footer(new_output.read_text(encoding="utf-8"))
            self.assertIn("00:01 event", body)
            self.assertEqual(metadata["source"], "clip.mp4")
            self.assertEqual(metadata["batch"], "retrofit-test")
            self.assertEqual(metadata["model"], "test-model")
            self.assertEqual(result.renamed, 1)
            self.assertEqual(result.metadata_added, 1)

    def test_current_output_gets_metadata_without_rename(self):
        """Current name.ext.txt files only receive a footer when missing one."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "photo.jpg"
            output = tmp_path / "photo.jpg.txt"
            source.write_text("")
            output.write_text("photo.jpg - desc\n", encoding="utf-8")

            result = retrofit_existing_outputs([(source, "photo")], batch_id="retrofit-test")

            _body, metadata = split_metadata_footer(output.read_text(encoding="utf-8"))
            self.assertEqual(metadata["source"], "photo.jpg")
            self.assertEqual(metadata["model"], "unknown")
            self.assertEqual(result.renamed, 0)
            self.assertEqual(result.metadata_added, 1)

    def test_existing_footer_is_left_unchanged(self):
        """Outputs that already have canonical metadata are not rewritten."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "clip.mp4"
            output = tmp_path / "clip.mp4.txt"
            source.write_text("")
            output.write_text(
                "clip.mp4 - desc\n\n"
                "---\n"
                "source: clip.mp4\n"
                "uuid: file-1\n"
                "batch: batch-1\n"
                "processed: then\n"
                "model: model-1\n",
                encoding="utf-8",
            )

            result = retrofit_existing_outputs([(source, "video")], batch_id="batch-2")

            text = output.read_text(encoding="utf-8")
            self.assertEqual(text.count("\n---\n"), 1)
            self.assertIn("uuid: file-1", text)
            self.assertEqual(result.unchanged, 1)

    def test_ambiguous_legacy_stem_is_not_renamed_or_modified(self):
        """Shared legacy stem outputs are skipped instead of guessed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mp4 = tmp_path / "clip.mp4"
            jpg = tmp_path / "clip.jpg"
            legacy = tmp_path / "clip.txt"
            mp4.write_text("")
            jpg.write_text("")
            original = "legacy desc\n"
            legacy.write_text(original, encoding="utf-8")

            result = retrofit_existing_outputs([(mp4, "video"), (jpg, "photo")])

            self.assertTrue(legacy.exists())
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertFalse((tmp_path / "clip.mp4.txt").exists())
            self.assertFalse((tmp_path / "clip.jpg.txt").exists())
            self.assertEqual(result.skipped_ambiguous, 2)
            self.assertEqual(result.metadata_added, 0)

    def test_sibling_media_make_subset_retrofit_ambiguous(self):
        """A subset scan still skips shared legacy output when a sibling exists."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mp4 = tmp_path / "clip.mp4"
            jpg = tmp_path / "clip.jpg"
            legacy = tmp_path / "clip.txt"
            mp4.write_text("")
            jpg.write_text("")
            original = "legacy desc\n"
            legacy.write_text(original, encoding="utf-8")

            result = retrofit_existing_outputs([(mp4, "video")])

            self.assertTrue(legacy.exists())
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertFalse((tmp_path / "clip.mp4.txt").exists())
            self.assertEqual(result.skipped_ambiguous, 1)
            self.assertEqual(result.metadata_added, 0)

    def test_dry_run_does_not_write_changes(self):
        """Dry-run reports planned work while leaving files untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "clip.mp4"
            legacy = tmp_path / "clip.txt"
            source.write_text("")
            original = "legacy desc\n"
            legacy.write_text(original, encoding="utf-8")

            result = retrofit_existing_outputs([(source, "video")], dry_run=True)

            self.assertTrue(legacy.exists())
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertFalse((tmp_path / "clip.mp4.txt").exists())
            self.assertEqual(result.renamed, 1)
            self.assertEqual(result.metadata_added, 1)

    def test_plan_marks_missing_outputs(self):
        """Missing .txt outputs are counted as skipped missing files."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_text("")

            actions = plan_retrofit([(source, "video")])
            result = retrofit_existing_outputs([(source, "video")])

            self.assertFalse(actions[0].exists)
            self.assertEqual(result.skipped_missing, 1)

    def test_io_error_increments_failed_and_continues(self):
        """An unreadable .txt increments failed and does not abort the batch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad = tmp_path / "bad.mp4"
            bad_txt = tmp_path / "bad.mp4.txt"
            good = tmp_path / "good.mp4"
            good_txt = tmp_path / "good.mp4.txt"
            bad.write_text("")
            good.write_text("")
            bad_txt.write_bytes(b"\xff\xfe bad utf-16 garbage")
            good_txt.write_text("good desc\n", encoding="utf-8")

            result = retrofit_existing_outputs([(bad, "video"), (good, "video")])

            self.assertEqual(result.failed, 1)
            self.assertEqual(result.metadata_added, 1)

    def test_rename_rolled_back_on_footer_write_failure(self):
        """If footer write fails after rename, the rename is rolled back."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "clip.mp4"
            legacy = tmp_path / "clip.txt"
            source.write_text("")
            legacy.write_bytes(b"\xff\xfe bad utf-16")

            result = retrofit_existing_outputs([(source, "video")])

            self.assertTrue(legacy.exists())
            self.assertFalse((tmp_path / "clip.mp4.txt").exists())
            self.assertEqual(result.failed, 1)
            self.assertEqual(result.renamed, 0)


if __name__ == "__main__":
    unittest.main()
