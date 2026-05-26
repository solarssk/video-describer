from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from batch_metadata import append_metadata_footer, has_metadata_footer, utc_timestamp
from output_paths import legacy_output_txt_path, output_txt_path

MEDIA_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.insv', '.jpg', '.jpeg', '.png'}


@dataclass
class RetrofitResult:
    """Counters returned after retrofitting existing text outputs."""

    scanned: int = 0
    renamed: int = 0
    metadata_added: int = 0
    unchanged: int = 0
    skipped_ambiguous: int = 0
    skipped_missing: int = 0
    failed: int = 0


@dataclass
class RetrofitAction:
    """One planned or applied retrofit action for a source media file."""

    source: Path
    legacy_output: Path
    output: Path
    ambiguous: bool
    exists: bool


def _legacy_target_counts(media: Iterable[tuple[Path, str]],
                          out_dir: Optional[Path]) -> dict[Path, int]:
    """Count how many media files would share each legacy stem-based output."""
    legacy_targets: dict[Path, set[Path]] = {}
    for source_path, _media_type in media:
        source_path = Path(source_path)
        sibling_media = {source_path}
        try:
            sibling_media.update(
                item for item in source_path.parent.iterdir()
                if item.is_file()
                and item.stem == source_path.stem
                and item.suffix.lower() in MEDIA_EXTENSIONS
            )
        except OSError:
            pass
        for sibling_path in sibling_media:
            legacy_path = legacy_output_txt_path(sibling_path, out_dir)
            legacy_targets.setdefault(legacy_path, set()).add(sibling_path)
    return {legacy_path: len(siblings) for legacy_path, siblings in legacy_targets.items()}


def plan_retrofit(media: list[tuple[Path, str]],
                  out_dir: Optional[Path] = None) -> list[RetrofitAction]:
    """Plan legacy-to-current output migrations for discovered media files."""
    legacy_counts = _legacy_target_counts(media, out_dir)
    actions = []
    for source_path, _media_type in media:
        source_path = Path(source_path)
        legacy_path = legacy_output_txt_path(source_path, out_dir)
        new_path = output_txt_path(source_path, out_dir)
        existing_path = new_path if new_path.exists() else legacy_path
        actions.append(RetrofitAction(
            source=source_path,
            legacy_output=legacy_path,
            output=new_path,
            ambiguous=legacy_counts.get(legacy_path, 0) > 1,
            exists=existing_path.exists(),
        ))
    return actions


def retrofit_existing_outputs(media: list[tuple[Path, str]],
                              out_dir: Optional[Path] = None, *,
                              model: str = "unknown",
                              batch_id: Optional[str] = None,
                              dry_run: bool = False) -> RetrofitResult:
    """Convert existing .txt outputs to current naming and metadata format."""
    result = RetrofitResult(scanned=len(media))
    batch_id = batch_id or f"retrofit-{uuid.uuid4()}"

    for action in plan_retrofit(media, out_dir):
        if not action.exists:
            result.skipped_missing += 1
            continue

        renamed_this_action = False
        try:
            current_path = action.output if action.output.exists() else action.legacy_output
            if current_path == action.legacy_output and not action.output.exists():
                if action.ambiguous:
                    result.skipped_ambiguous += 1
                    continue
                if not dry_run:
                    action.legacy_output.replace(action.output)
                    renamed_this_action = True
                    current_path = action.output
                result.renamed += 1

            text = current_path.read_text(encoding="utf-8")
            if has_metadata_footer(text):
                result.unchanged += 1
                continue

            updated = append_metadata_footer(
                text,
                source=action.source.name,
                file_uuid=str(uuid.uuid4()),
                batch_id=batch_id,
                processed=utc_timestamp(),
                model=model,
            )
            if not dry_run:
                current_path.write_text(updated, encoding="utf-8")
            result.metadata_added += 1

        except (OSError, UnicodeError) as exc:
            if renamed_this_action:
                try:
                    action.output.replace(action.legacy_output)
                except OSError:
                    pass
            if renamed_this_action:
                result.renamed -= 1
            result.failed += 1
            print(f"  ⚠ retrofit failed for {action.source.name}: {exc}")

    return result
