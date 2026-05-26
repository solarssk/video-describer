from __future__ import annotations

import copy
import datetime
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from output_paths import find_existing_output, output_txt_path

SCHEMA_VERSION = 2
FOOTER_SEPARATOR = "---"
REQUIRED_FOOTER_KEYS = {"source", "uuid", "batch", "processed", "model"}
ALLOWED_STATUSES = {"pending", "in_progress", "done", "skipped", "error"}
_SECRET_KEYS = (
    "api_key",
    "webhook_url",
    "token",
    "secret",
    "password",
)


def utc_timestamp() -> str:
    """Return an ISO timestamp for persisted metadata."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def redact_secrets(value):
    """Return a deep copy with nested secrets removed from dict/list structures."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(secret_key in key_text for secret_key in _SECRET_KEYS):
                continue
            redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return copy.deepcopy(value)


def build_manifest_files(media: list, out_dir: Optional[Path] = None,
                         previous_state: Optional[dict] = None,
                         resume_from_index: int = 0) -> list:
    """Build or migrate a manifest file list for the scanned media."""
    previous_by_path = {}
    if previous_state and isinstance(previous_state.get("files"), list):
        for item in previous_state["files"]:
            if isinstance(item, dict) and item.get("path"):
                previous_by_path[str(item["path"])] = item

    files = []
    for idx, (source_path, _media_type) in enumerate(media):
        source_path = Path(source_path)
        existing_output = find_existing_output(source_path, out_dir)
        default_output = output_txt_path(source_path, out_dir)
        previous = previous_by_path.get(str(source_path))

        if previous:
            entry_uuid = previous.get("uuid") or str(uuid.uuid4())
            status = previous.get("status") or "pending"
            if status == "in_progress":
                status = "pending"
            output = previous.get("output") or str(existing_output or default_output)
            error = previous.get("error")
        else:
            entry_uuid = str(uuid.uuid4())
            if idx < resume_from_index and existing_output:
                status = "done"
                output = str(existing_output)
            else:
                status = "pending"
                output = str(existing_output or default_output)
            error = None

        files.append({
            "uuid": entry_uuid,
            "path": str(source_path),
            "output": output,
            "status": status if status in ALLOWED_STATUSES else "pending",
            "error": error,
        })
    return files


def counts_from_files(files: list) -> dict:
    """Derive batch counters from manifest statuses."""
    done = sum(1 for item in files if item.get("status") == "done")
    skipped = sum(1 for item in files if item.get("status") == "skipped")
    errors = sum(1 for item in files if item.get("status") == "error")
    return {
        "total": len(files),
        "processed": done,
        "skipped": skipped,
        "errors": errors,
    }


def next_retry_index(files: list) -> int:
    """Return the first retryable index for legacy resume compatibility."""
    for idx, item in enumerate(files):
        if item.get("status") in {"pending", "in_progress", "error"}:
            return idx
    return len(files)


def next_retry_path(files: list) -> Optional[str]:
    """Return the source path for the first retryable manifest entry."""
    idx = next_retry_index(files)
    if idx >= len(files):
        return None
    return files[idx].get("path")


def mark_file(files: list, source_path: Path, status: str,
              output: Optional[Path] = None, error: Optional[str] = None) -> dict:
    """Update one manifest entry and return it."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"Invalid file status '{status}'. "
            "mark_file status must be one of the values consumed by "
            "counts_from_files and next_retry_index."
        )
    source_text = str(source_path)
    for item in files:
        if item.get("path") == source_text:
            item["status"] = status
            if output is not None:
                item["output"] = str(output)
            item["error"] = error
            return item
    raise KeyError(f"File not found in manifest: {source_text}")


def build_batch_state(config: dict, files: list, usage: dict,
                      batch_id: str, timestamp: Optional[str] = None) -> dict:
    """Build the persisted schema v2 batch state."""
    safe_config = redact_secrets(config)
    counts = counts_from_files(files)
    state = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "config": safe_config,
        "cost_usd": float(usage.get("cost_usd") or 0),
        "timestamp": timestamp or utc_timestamp(),
        "files": redact_secrets(files),
        **counts,
        "next_index": next_retry_index(files),
        "next_filepath": next_retry_path(files),
    }
    return state


def write_json_atomic(path: Path, data: dict) -> None:
    """Persist JSON via temp file + fsync + rename."""
    tmp_path = None
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            f.write(payload)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def has_metadata_footer(text: str) -> bool:
    """Return True when text ends with a parseable metadata footer."""
    body, metadata = split_metadata_footer(text)
    return metadata != {} and body != text


def split_metadata_footer(text: str) -> tuple[str, dict]:
    """Split a human description from a trailing metadata footer if present."""
    lines = text.rstrip("\n").splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() != FOOTER_SEPARATOR:
            continue
        footer_lines = lines[idx + 1:]
        metadata = {}
        valid = bool(footer_lines)
        for line in footer_lines:
            if not line.strip():
                continue
            if ":" not in line:
                valid = False
                break
            key, value = line.split(":", 1)
            key = key.strip()
            if not key:
                valid = False
                break
            metadata[key] = value.strip()
        if valid and REQUIRED_FOOTER_KEYS.issubset(metadata.keys()):
            return "\n".join(lines[:idx]).rstrip(), metadata
    return text.rstrip("\n"), {}


def append_metadata_footer(description: str, *, source: str, file_uuid: str,
                           batch_id: str, processed: str, model: str) -> str:
    """Append metadata footer unless one already exists."""
    body, metadata = split_metadata_footer(description)
    if metadata:
        return description.rstrip("\n") + "\n"
    lines = [
        body.rstrip(),
        "",
        FOOTER_SEPARATOR,
        f"source: {source}",
        f"uuid: {file_uuid}",
        f"batch: {batch_id}",
        f"processed: {processed}",
        f"model: {model}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def summary_description(text: str) -> str:
    """Return the first description line, ignoring metadata footer."""
    body, _metadata = split_metadata_footer(text)
    first_line = body.splitlines()[0] if body.splitlines() else ""
    if " - " in first_line:
        first_line = first_line.split(" - ", 1)[1]
    return first_line
