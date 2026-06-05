"""Tests for find_media() recursive scanning behaviour."""
import tempfile
import unittest
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from describe_videos import find_media


class TestFindMediaRecursive(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, *parts):
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        return p

    # ------------------------------------------------------------------
    # Recursive scan
    # ------------------------------------------------------------------

    def test_flat_folder_finds_files(self):
        self._touch('a.mp4')
        self._touch('b.insv')
        result = find_media([self.root])
        names = [f.name for f, _ in result]
        self.assertIn('a.mp4', names)
        self.assertIn('b.insv', names)

    def test_recursive_scan_finds_files_in_subfolders(self):
        self._touch('day1', 'clip1.mp4')
        self._touch('day2', 'clip2.insv')
        self._touch('day2', 'photo.jpg')
        result = find_media([self.root])
        names = [f.name for f, _ in result]
        self.assertIn('clip1.mp4', names)
        self.assertIn('clip2.insv', names)
        self.assertIn('photo.jpg', names)

    def test_recursive_scan_nested_three_levels(self):
        self._touch('trip', 'day1', 'morning', 'video.mp4')
        result = find_media([self.root])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].name, 'video.mp4')

    def test_unsupported_extensions_excluded(self):
        self._touch('day1', 'clip.mp4')
        self._touch('day1', 'document.pdf')
        self._touch('day1', 'photo.heic')
        result = find_media([self.root])
        names = [f.name for f, _ in result]
        self.assertIn('clip.mp4', names)
        self.assertNotIn('document.pdf', names)
        self.assertNotIn('photo.heic', names)

    def test_hidden_files_excluded(self):
        self._touch('._GX010001.mp4')
        self._touch('GX010001.mp4')
        result = find_media([self.root])
        names = [f.name for f, _ in result]
        self.assertIn('GX010001.mp4', names)
        self.assertNotIn('._GX010001.mp4', names)

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def test_sort_by_full_path_not_just_name(self):
        # Same filename in two different subdirs — sort by full path
        self._touch('day2', 'GX010001.mp4')
        self._touch('day1', 'GX010001.mp4')
        result = find_media([self.root])
        self.assertEqual(len(result), 2)
        # day1 comes before day2 lexicographically
        self.assertIn('day1', str(result[0][0]))
        self.assertIn('day2', str(result[1][0]))

    def test_flat_folder_order_stable(self):
        self._touch('c.mp4')
        self._touch('a.mp4')
        self._touch('b.mp4')
        result = find_media([self.root])
        names = [f.name for f, _ in result]
        self.assertEqual(names, sorted(names))

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_dedup_same_path_twice(self):
        p = self._touch('clip.mp4')
        result = find_media([p, p])
        self.assertEqual(len(result), 1)

    def test_dedup_via_two_paths_to_same_file(self):
        p = self._touch('clip.mp4')
        result = find_media([self.root, p])
        names = [f.name for f, _ in result]
        self.assertEqual(names.count('clip.mp4'), 1)

    # ------------------------------------------------------------------
    # Type tagging
    # ------------------------------------------------------------------

    def test_video_tagged_correctly(self):
        self._touch('clip.mp4')
        result = find_media([self.root])
        self.assertEqual(result[0][1], 'video')

    def test_photo_tagged_correctly(self):
        self._touch('photo.jpg')
        result = find_media([self.root])
        self.assertEqual(result[0][1], 'photo')

    # ------------------------------------------------------------------
    # Path objects accepted directly (from registry)
    # ------------------------------------------------------------------

    def test_accepts_path_objects(self):
        p = self._touch('clip.mp4')
        result = find_media([p])  # Path object, not string
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].name, 'clip.mp4')

    def test_accepts_mixed_paths_and_strings(self):
        p1 = self._touch('clip1.mp4')
        p2 = self._touch('clip2.insv')
        result = find_media([p1, str(p2)])
        self.assertEqual(len(result), 2)

    # ------------------------------------------------------------------
    # File filter
    # ------------------------------------------------------------------

    def test_file_filter_restricts_results(self):
        self._touch('keep.mp4')
        self._touch('skip.mp4')
        result = find_media([self.root], file_filter=['keep.mp4'])
        names = [f.name for f, _ in result]
        self.assertEqual(names, ['keep.mp4'])


if __name__ == '__main__':
    unittest.main()
