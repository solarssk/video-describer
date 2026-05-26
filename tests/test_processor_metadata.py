import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import processor
from batch_metadata import split_metadata_footer


class _SleepBlockStub:
    def release(self):
        pass


class _ProviderStub:
    def verify(self):
        return True, ""


class ProcessorMetadataTests(unittest.TestCase):
    def test_photo_output_gets_metadata_without_nle_warn_name_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            photo = tmp_path / "photo.jpg"
            photo.write_text("fake image")
            state_path = tmp_path / "batch_state.json"
            cfg = {
                "connectors": {"anthropic": {"api_key": "sk-test"}},
                "ai": {
                    "provider": "anthropic",
                    "anthropic": {
                        "model": "claude-test",
                        "price_input_per_mtok_usd": 1,
                        "price_output_per_mtok_usd": 1,
                    },
                },
                "nle_export": {"fcpxml": False, "edl": False, "fcp7xml": False},
                "frames": {"max_per_video": 100},
                "whisper": {"default_model": "tiny", "fallback_tiers": ["tiny"]},
            }
            events = []

            with patch.object(processor, "BATCH_STATE_PATH", state_path), \
                    patch.object(processor, "_prevent_sleep", return_value=_SleepBlockStub()), \
                    patch("processor.config_loader.load_config", return_value=cfg), \
                    patch("processor.config_loader.load_system_prompt", return_value="system"), \
                    patch("processor.make_provider", return_value=_ProviderStub()), \
                    patch("processor.describe_photo", return_value="photo.jpg - opis"):
                processor.run_processing(
                    {
                        "path": str(tmp_path),
                        "people": "",
                        "context": "",
                        "analyze_images": True,
                        "transcribe": False,
                        "generate_summary": False,
                    },
                    events.append,
                    logging.getLogger("test_processor_metadata"),
                    threading.Event(),
                    {},
                )

            output = tmp_path / "photo.jpg.txt"
            self.assertTrue(output.exists())
            _body, metadata = split_metadata_footer(output.read_text(encoding="utf-8"))
            self.assertEqual(metadata["source"], "photo.jpg")
            self.assertEqual(metadata["model"], "claude-test")
            self.assertTrue(any(event.get("type") == "done" for event in events))
            self.assertFalse(any(event.get("type") == "error_file" for event in events))


if __name__ == "__main__":
    unittest.main()
