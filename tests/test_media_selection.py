"""Tests for the media_selection path registry module."""
import time
import unittest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import media_selection


class TestMediaSelectionRegistry(unittest.TestCase):

    def setUp(self):
        media_selection.clear()

    def tearDown(self):
        media_selection.clear()

    def test_register_returns_string_id(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths)
        self.assertIsInstance(sel_id, str)
        self.assertTrue(len(sel_id) > 0)

    def test_register_and_lookup_same_paths(self):
        paths = [Path('/tmp/a.mp4'), Path('/tmp/b.insv')]
        sel_id = media_selection.register(paths)
        result = media_selection.lookup(sel_id)
        self.assertEqual(result, paths)

    def test_lookup_missing_returns_none(self):
        result = media_selection.lookup('nonexistent-id')
        self.assertIsNone(result)

    def test_lookup_after_clear_returns_none(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths)
        media_selection.clear()
        self.assertIsNone(media_selection.lookup(sel_id))

    def test_two_registrations_have_different_ids(self):
        paths = [Path('/tmp/video.mp4')]
        id1 = media_selection.register(paths)
        id2 = media_selection.register(paths)
        self.assertNotEqual(id1, id2)

    def test_source_picker_default(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths)
        sel = media_selection._REGISTRY[sel_id]
        self.assertEqual(sel.source, 'picker')

    def test_source_manual(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths, source='manual')
        sel = media_selection._REGISTRY[sel_id]
        self.assertEqual(sel.source, 'manual')

    def test_eviction_removes_expired(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths)
        # Manually expire the entry
        old = media_selection._REGISTRY[sel_id]
        media_selection._REGISTRY[sel_id] = media_selection.MediaSelection(
            id=old.id,
            paths=old.paths,
            created_at=time.monotonic() - media_selection._TTL_SECONDS - 1,
            source=old.source,
        )
        # Trigger eviction by registering a new entry
        media_selection.register([Path('/tmp/other.mp4')])
        self.assertIsNone(media_selection.lookup(sel_id))

    def test_lookup_returns_list_not_tuple(self):
        paths = [Path('/tmp/video.mp4')]
        sel_id = media_selection.register(paths)
        result = media_selection.lookup(sel_id)
        self.assertIsInstance(result, list)


if __name__ == '__main__':
    unittest.main()
