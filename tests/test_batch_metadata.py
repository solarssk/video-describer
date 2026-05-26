import json
import tempfile
import unittest
from pathlib import Path

from batch_metadata import (
    append_metadata_footer,
    build_batch_state,
    build_manifest_files,
    counts_from_files,
    has_metadata_footer,
    mark_file,
    next_retry_index,
    redact_secrets,
    split_metadata_footer,
    summary_description,
    write_json_atomic,
)


class BatchManifestTests(unittest.TestCase):
    def test_new_batch_state_has_schema_version_batch_id_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "video.mp4"
            src.write_text("")
            files = build_manifest_files([(src, "video")])
            state = build_batch_state(
                {"path": tmp},
                files,
                {"cost_usd": 0.25},
                batch_id="batch-1",
                timestamp="2026-05-26T12:00:00+00:00",
            )

        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["batch_id"], "batch-1")
        self.assertEqual(state["cost_usd"], 0.25)
        self.assertEqual(len(state["files"]), 1)
        self.assertIn("uuid", state["files"][0])
        self.assertEqual(state["files"][0]["status"], "pending")

    def test_counts_are_derived_from_file_statuses(self):
        files = [
            {"status": "done"},
            {"status": "skipped"},
            {"status": "error"},
            {"status": "pending"},
            {"status": "in_progress"},
        ]
        self.assertEqual(
            counts_from_files(files),
            {"total": 5, "processed": 1, "skipped": 1, "errors": 1},
        )
        self.assertEqual(next_retry_index(files), 2)

    def test_old_flat_batch_state_is_migrated_to_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            first = tmp_path / "a.mp4"
            second = tmp_path / "b.mp4"
            first.write_text("")
            second.write_text("")
            (tmp_path / "a.mp4.txt").write_text("done")

            files = build_manifest_files(
                [(first, "video"), (second, "video")],
                previous_state={"next_index": 1},
                resume_from_index=1,
            )

        self.assertEqual(files[0]["status"], "done")
        self.assertEqual(files[1]["status"], "pending")
        self.assertTrue(files[0]["output"].endswith("a.mp4.txt"))

    def test_resume_uses_manifest_output_path_not_stem_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "clip.mp4"
            src.write_text("")
            custom = Path(tmp) / "custom-output.txt"
            previous = {
                "files": [{
                    "uuid": "file-1",
                    "path": str(src),
                    "output": str(custom),
                    "status": "error",
                    "error": "boom",
                }]
            }
            files = build_manifest_files([(src, "video")], previous_state=previous)

        self.assertEqual(files[0]["uuid"], "file-1")
        self.assertEqual(files[0]["output"], str(custom))
        self.assertEqual(files[0]["status"], "error")

    def test_mark_file_rejects_unknown_status(self):
        files = [{
            "uuid": "file-1",
            "path": "/x/video.mp4",
            "output": "/x/video.mp4.txt",
            "status": "pending",
            "error": None,
        }]
        with self.assertRaises(ValueError):
            mark_file(files, Path("/x/video.mp4"), "finished")
        self.assertEqual(files[0]["status"], "pending")


class SecretRedactionTests(unittest.TestCase):
    def test_batch_state_never_persists_nested_api_key(self):
        state = build_batch_state(
            {
                "api_key": "sk-top",
                "connectors": {
                    "anthropic": {"api_key": "sk-ant"},
                    "webhook": {"webhook_url": "https://secret.example/hook"},
                },
                "nested": [{"token": "secret-token", "safe": "ok"}],
            },
            [{
                "uuid": "file-1",
                "path": "/x/video.mp4",
                "output": "/x/video.mp4.txt",
                "status": "pending",
                "error": None,
            }],
            {"cost_usd": 0},
            batch_id="batch-1",
        )
        payload = json.dumps(state)
        self.assertNotIn("sk-top", payload)
        self.assertNotIn("sk-ant", payload)
        self.assertNotIn("secret.example", payload)
        self.assertNotIn("secret-token", payload)
        self.assertEqual(state["config"]["nested"][0]["safe"], "ok")

    def test_redact_secrets_returns_copy(self):
        original = {"connectors": {"openai": {"api_key": "sk", "label": "x"}}}
        redacted = redact_secrets(original)
        self.assertIn("api_key", original["connectors"]["openai"])
        self.assertNotIn("api_key", redacted["connectors"]["openai"])


class MetadataFooterTests(unittest.TestCase):
    def test_txt_metadata_footer_is_appended_and_summary_ignores_it(self):
        text = append_metadata_footer(
            "video.mp4 - opis startowy\n00:15 zdarzenie",
            source="video.mp4",
            file_uuid="file-1",
            batch_id="batch-1",
            processed="2026-05-26T12:00:00+00:00",
            model="claude-sonnet-4-6",
        )
        body, metadata = split_metadata_footer(text)

        self.assertTrue(has_metadata_footer(text))
        self.assertEqual(summary_description(text), "opis startowy")
        self.assertIn("00:15 zdarzenie", body)
        self.assertEqual(metadata["source"], "video.mp4")
        self.assertEqual(metadata["uuid"], "file-1")
        self.assertEqual(metadata["batch"], "batch-1")
        self.assertEqual(metadata["model"], "claude-sonnet-4-6")

    def test_footer_is_not_appended_twice(self):
        text = append_metadata_footer(
            "clip.mp4 - desc",
            source="clip.mp4",
            file_uuid="file-1",
            batch_id="batch-1",
            processed="now",
            model="unknown",
        )
        again = append_metadata_footer(
            text,
            source="clip.mp4",
            file_uuid="file-2",
            batch_id="batch-2",
            processed="later",
            model="other",
        )

        self.assertEqual(again.count("\n---\n"), 1)
        self.assertIn("uuid: file-1", again)
        self.assertNotIn("uuid: file-2", again)

    def test_legacy_text_without_footer_remains_valid(self):
        body, metadata = split_metadata_footer("clip.mp4 - desc\n00:01 event\n")
        self.assertEqual(body, "clip.mp4 - desc\n00:01 event")
        self.assertEqual(metadata, {})
        self.assertEqual(summary_description(body), "desc")

    def test_atomic_json_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            write_json_atomic(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
