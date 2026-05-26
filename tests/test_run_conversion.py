"""Tests for run_conversion — NLE sidecar generation from existing .txt files."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class RunConversionTests(unittest.TestCase):

    def _run(self, path, nle_cfg=None, **kwargs):
        """Helper that calls run_conversion and collects emitted messages."""
        from processor import run_conversion

        if nle_cfg is None:
            nle_cfg = {'fcpxml': True, 'edl': False, 'fcp7xml': False}

        events = []
        config = {'path': str(path), 'nle_export': nle_cfg, **kwargs}
        stop = threading.Event()
        with patch('processor.get_video_metadata', return_value=(60.0, 25.0)):
            run_conversion(config, events.append, stop)
        return events

    def _done(self, events):
        return next((e for e in events if e.get('type') == 'done'), None)

    def test_generates_fcpxml_for_existing_txt(self):
        """An existing .txt beside a video produces a .fcpxml sidecar."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            vid = p / 'clip.mp4'
            vid.write_text('')
            txt = p / 'clip.mp4.txt'
            txt.write_text('clip.mp4 — desc\n\n00:15 first marker\n01:00 second marker\n', encoding='utf-8')

            events = self._run(p)

            done = self._done(events)
            self.assertIsNotNone(done)
            self.assertEqual(done['processed'], 1)
            self.assertEqual(done['errors'], 0)
            self.assertTrue((p / 'clip.mp4.fcpxml').exists())

    def test_skips_file_without_existing_txt(self):
        """A video with no .txt is counted as skipped, no error."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'clip.mp4').write_text('')

            events = self._run(p)

            done = self._done(events)
            self.assertIsNotNone(done)
            self.assertEqual(done['processed'], 0)
            self.assertEqual(done['skipped'], 1)
            self.assertEqual(done['errors'], 0)

    def test_photos_are_skipped(self):
        """Photos have no NLE export and are counted as skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'photo.jpg').write_text('')
            (p / 'photo.jpg.txt').write_text('photo.jpg — desc\n', encoding='utf-8')

            events = self._run(p)

            done = self._done(events)
            self.assertIsNotNone(done)
            self.assertEqual(done['processed'], 0)
            self.assertEqual(done['skipped'], 1)

    def test_no_formats_enabled_emits_error(self):
        """If no NLE formats are enabled an error is emitted immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            events = self._run(p, nle_cfg={'fcpxml': False, 'edl': False, 'fcp7xml': False})

        error_events = [e for e in events if e.get('type') == 'error']
        self.assertTrue(error_events, 'Expected an error event')
        done = self._done(events)
        self.assertIsNone(done, 'No done event expected when no formats enabled')

    def test_no_media_found_emits_error(self):
        """An empty folder emits an error, not a done event."""
        with tempfile.TemporaryDirectory() as tmp:
            events = self._run(Path(tmp))

        error_events = [e for e in events if e.get('type') == 'error']
        self.assertTrue(error_events)
        self.assertIsNone(self._done(events))

    def test_stop_event_aborts_loop(self):
        """Setting stop_event mid-run causes the loop to exit early."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for i in range(5):
                vid = p / f'clip{i}.mp4'
                vid.write_text('')
                (p / f'clip{i}.mp4.txt').write_text(f'clip{i}.mp4 — desc\n00:01 event\n', encoding='utf-8')

            from processor import run_conversion

            events = []
            stop = threading.Event()

            call_count = 0

            def _emit(msg):
                nonlocal call_count
                events.append(msg)
                if msg.get('type') == 'progress' and msg.get('current', 0) >= 2:
                    stop.set()
                call_count += 1

            with patch('processor.get_video_metadata', return_value=(60.0, 25.0)):
                run_conversion({'path': str(p), 'nle_export': {'fcpxml': True, 'edl': False, 'fcp7xml': False}},
                               _emit, stop)

            done = self._done(events)
            self.assertIsNotNone(done)
            self.assertLess(done['processed'] + done['skipped'], 5)

    def test_export_error_increments_errors(self):
        """An exception from export_sidecars increments errors and continues."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            vid = p / 'bad.mp4'
            vid.write_text('')
            (p / 'bad.mp4.txt').write_text('bad.mp4 — desc\n00:01 event\n', encoding='utf-8')

            from processor import run_conversion

            events = []
            stop = threading.Event()
            with patch('processor.get_video_metadata', return_value=(60.0, 25.0)), \
                 patch('processor.export_sidecars', side_effect=RuntimeError('boom')):
                run_conversion({'path': str(p), 'nle_export': {'fcpxml': True, 'edl': False, 'fcp7xml': False}},
                               events.append, stop)

            done = self._done(events)
            self.assertIsNotNone(done)
            self.assertEqual(done['errors'], 1)
            self.assertEqual(done['processed'], 0)


if __name__ == '__main__':
    unittest.main()
