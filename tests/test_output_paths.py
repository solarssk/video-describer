import tempfile
import unittest
from pathlib import Path
from output_paths import find_existing_output, legacy_output_txt_path, output_txt_path


class OutputTxtPathTests(unittest.TestCase):
    def test_preserves_original_extension(self):
        self.assertEqual(output_txt_path(Path("/x/video.mp4")).name, "video.mp4.txt")
        self.assertEqual(output_txt_path(Path("/x/video.jpg")).name, "video.jpg.txt")
        self.assertEqual(output_txt_path(Path("/x/VID_001.MOV")).name, "VID_001.MOV.txt")

    def test_no_collision_between_mp4_and_jpg(self):
        mp4 = output_txt_path(Path("/x/test.mp4")).name
        jpg = output_txt_path(Path("/x/test.jpg")).name
        self.assertNotEqual(mp4, jpg)

    def test_legacy_uses_stem(self):
        self.assertEqual(legacy_output_txt_path(Path("/x/video.mp4")).name, "video.txt")
        self.assertEqual(legacy_output_txt_path(Path("/x/video.jpg")).name, "video.txt")


class FindExistingOutputTests(unittest.TestCase):
    def test_returns_new_format_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "video.mp4"
            src.write_text("")
            new_txt = tmp_path / "video.mp4.txt"
            old_txt = tmp_path / "video.txt"
            new_txt.write_text("new")
            old_txt.write_text("old")
            self.assertEqual(find_existing_output(src), new_txt)

    def test_legacy_fallback_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "video.mp4"
            src.write_text("")
            legacy = tmp_path / "video.txt"
            legacy.write_text("old")
            self.assertEqual(find_existing_output(src), legacy)

    def test_returns_none_when_neither_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "video.mp4"
            self.assertIsNone(find_existing_output(src))

    def test_returns_none_with_out_dir_when_neither_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path("/original/path/video.mp4")
            self.assertIsNone(find_existing_output(src, Path(tmp)))

    def test_respects_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = Path("/original/path/video.mp4")
            txt = tmp_path / "video.mp4.txt"
            txt.write_text("desc")
            self.assertEqual(find_existing_output(src, tmp_path), txt)

    def test_respects_out_dir_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = Path("/original/path/video.mp4")
            legacy = tmp_path / "video.txt"
            legacy.write_text("legacy desc")
            self.assertEqual(find_existing_output(src, tmp_path), legacy)

    def test_mp4_and_jpg_with_same_stem_get_different_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mp4 = tmp_path / "clip.mp4"
            jpg = tmp_path / "clip.jpg"
            mp4.write_text("")
            jpg.write_text("")
            (tmp_path / "clip.mp4.txt").write_text("video desc")
            (tmp_path / "clip.jpg.txt").write_text("photo desc")
            self.assertNotEqual(find_existing_output(mp4), find_existing_output(jpg))
            self.assertEqual(find_existing_output(mp4).read_text(), "video desc")
            self.assertEqual(find_existing_output(jpg).read_text(), "photo desc")


if __name__ == '__main__':
    unittest.main()
