"""
Path selection registry for video-describer.

Stores server-side Path objects registered when the user picks files or folders
via the native macOS picker. Web requests carry only a selection_id token — the
raw filesystem path never flows directly from the HTTP request to a path sink,
which prevents CodeQL py/path-injection false positives and is sound security
practice even for a local app.

Usage:
    sel_id = register([Path('/Volumes/cam/trip')], source='picker')
    paths  = lookup(sel_id)  # → [PosixPath('/Volumes/cam/trip')]
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaSelection:
    id: str
    paths: tuple[Path, ...]
    created_at: float
    source: Literal['picker', 'manual']


# ---------------------------------------------------------------------------
# In-process registry (single-process app, no persistence needed)
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, MediaSelection] = {}
_TTL_SECONDS: float = 3600.0  # selections expire after 1 hour of inactivity


def register(paths: list[Path], source: Literal['picker', 'manual'] = 'picker') -> str:
    """Store *paths* and return an opaque selection_id token.

    The caller (picker routes) stores real Path objects here; subsequent
    HTTP endpoints only receive the token and look up paths via :func:`lookup`.
    """
    sel_id = str(uuid.uuid4())
    _REGISTRY[sel_id] = MediaSelection(
        id=sel_id,
        paths=tuple(paths),
        created_at=time.monotonic(),
        source=source,
    )
    _evict_expired()
    return sel_id


def lookup(sel_id: str) -> list[Path] | None:
    """Return the Path list for *sel_id*, or ``None`` if unknown/expired."""
    sel = _REGISTRY.get(sel_id)
    if sel is None:
        return None
    return list(sel.paths)


def clear() -> None:
    """Remove all registrations (used in tests)."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evict_expired() -> None:
    now = time.monotonic()
    expired = [k for k, v in _REGISTRY.items() if now - v.created_at > _TTL_SECONDS]
    for k in expired:
        del _REGISTRY[k]
