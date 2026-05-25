from __future__ import annotations

from pathlib import Path
from typing import Optional


def output_txt_path(source_path: Path, out_dir: Optional[Path] = None) -> Path:
    """New format: video.mp4 → video.mp4.txt (avoids stem collision).

    If out_dir is given the result is placed there; otherwise alongside source_path.
    """
    base = out_dir if out_dir else source_path.parent
    return base / (source_path.name + ".txt")


def legacy_output_txt_path(source_path: Path) -> Path:
    """Old format: video.mp4 → video.txt (stem only, kept for backwards compat).

    WARNING: ambiguous in mixed directories — video.mp4 and video.jpg both map
    to video.txt. Use only for reading pre-existing files, never for new output.
    """
    return source_path.with_suffix(".txt")


def find_existing_output(source_path: Path, out_dir: Optional[Path] = None) -> Optional[Path]:
    """Return existing output .txt for source_path, new format first then legacy fallback."""
    base = out_dir if out_dir else source_path.parent
    new_path = output_txt_path(source_path, out_dir)
    if new_path.exists():
        return new_path
    old_path = base / legacy_output_txt_path(source_path).name
    if old_path.exists():
        return old_path
    return None
