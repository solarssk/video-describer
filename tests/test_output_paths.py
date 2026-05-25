from pathlib import Path
import pytest
from output_paths import find_existing_output, legacy_output_txt_path, output_txt_path


def test_output_txt_path_preserves_original_extension():
    assert output_txt_path(Path("/x/video.mp4")).name == "video.mp4.txt"
    assert output_txt_path(Path("/x/video.jpg")).name == "video.jpg.txt"
    assert output_txt_path(Path("/x/VID_001.MOV")).name == "VID_001.MOV.txt"


def test_output_txt_path_no_collision():
    mp4 = output_txt_path(Path("/x/test.mp4")).name
    jpg = output_txt_path(Path("/x/test.jpg")).name
    assert mp4 != jpg


def test_legacy_output_txt_path_uses_stem():
    assert legacy_output_txt_path(Path("/x/video.mp4")).name == "video.txt"
    assert legacy_output_txt_path(Path("/x/video.jpg")).name == "video.txt"


def test_find_existing_output_returns_new_format_first(tmp_path):
    src = tmp_path / "video.mp4"
    src.write_text("")
    new_txt = tmp_path / "video.mp4.txt"
    old_txt = tmp_path / "video.txt"
    new_txt.write_text("new")
    old_txt.write_text("old")
    assert find_existing_output(src) == new_txt


def test_legacy_output_fallback_is_supported(tmp_path):
    src = tmp_path / "video.mp4"
    src.write_text("")
    legacy = tmp_path / "video.txt"
    legacy.write_text("old")
    assert find_existing_output(src) == legacy


def test_find_existing_output_returns_none_when_neither_exists(tmp_path):
    src = tmp_path / "video.mp4"
    assert find_existing_output(src) is None


def test_find_existing_output_respects_out_dir(tmp_path):
    src = Path("/original/path/video.mp4")
    txt = tmp_path / "video.mp4.txt"
    txt.write_text("desc")
    assert find_existing_output(src, tmp_path) == txt


def test_mp4_and_jpg_with_same_stem_get_different_outputs(tmp_path):
    mp4 = tmp_path / "clip.mp4"
    jpg = tmp_path / "clip.jpg"
    mp4.write_text("")
    jpg.write_text("")
    (tmp_path / "clip.mp4.txt").write_text("video desc")
    (tmp_path / "clip.jpg.txt").write_text("photo desc")
    assert find_existing_output(mp4) != find_existing_output(jpg)
    assert find_existing_output(mp4).read_text() == "video desc"
    assert find_existing_output(jpg).read_text() == "photo desc"
