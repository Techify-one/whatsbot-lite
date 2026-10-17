"""Auto-download policy for received media.

GOWA only has a global on/off switch for auto-download, so it stays ON and the
app decides, per message, whether the file is kept. This module is the pure
part of that decision (no FastAPI, no I/O) so it can be tested offline.
"""

from __future__ import annotations

import copy
import logging
import os
import uuid
from pathlib import Path

from server import file_manager

logger = logging.getLogger(__name__)

# The four rows of the settings matrix. Stickers share the "image" row.
POLICY_KINDS: tuple[str, ...] = ("audio", "image", "video", "document")

# Lean default for everybody (local installs included): only audio is kept.
DEFAULT_AUTODOWNLOAD: dict[str, dict[str, bool]] = {
    "audio": {"individual": True, "group": True},
    "image": {"individual": False, "group": False},
    "video": {"individual": False, "group": False},
    "document": {"individual": False, "group": False},
}

_MEDIA_TYPE_TO_KIND = {
    "audio": "audio",
    "image": "image",
    "sticker": "image",
    "video": "video",
    "document": "document",
}

def default_autodownload() -> dict[str, dict[str, bool]]:
    return copy.deepcopy(DEFAULT_AUTODOWNLOAD)

def normalize(raw) -> dict[str, dict[str, bool]]:
    """Merge a stored/received value over the defaults, dropping anything unknown.

    Safe for junk input (None, wrong types, extra keys): the result always has
    the four kinds with a boolean ``individual`` and ``group``.
    """
    result = default_autodownload()
    if not isinstance(raw, dict):
        return result
    for kind in POLICY_KINDS:
        row = raw.get(kind)
        if not isinstance(row, dict):
            continue
        for scope in ("individual", "group"):
            if isinstance(row.get(scope), bool):
                result[kind][scope] = row[scope]
    return result

def policy_kind(media_type: str | None) -> str | None:
    """Row of the matrix a ``media_type`` belongs to (None = not file media)."""
    return _MEDIA_TYPE_TO_KIND.get(media_type or "")

def should_keep(policy, media_type: str | None, is_group: bool) -> bool:
    """True when the downloaded file must stay on disk.

    Types the matrix does not cover (location, poll, contact...) have no file,
    so there is nothing to discard and the answer is True.
    """
    kind = policy_kind(media_type)
    if kind is None:
        return True
    return normalize(policy)[kind]["group" if is_group else "individual"]

def is_discardable_path(path: str | None, statics_media_dir: Path, data_dir: Path) -> Path | None:
    """Resolve a webhook ``media_path`` to a real file inside ``statics/media``.

    Returns the absolute path only when it is a regular, non-symlink file
    located under ``statics_media_dir``; anything else returns None. This is the
    guard that keeps a forged or odd payload path from pointing the discard at
    a file outside the media folder.
    """
    if not path or path.startswith("geo:"):
        return None
    try:
        p = Path(path)
        if not p.is_absolute():
            p = data_dir / p
        root = statics_media_dir.resolve()
        if p.is_symlink():
            return None
        resolved = p.resolve()
        if root not in resolved.parents:
            return None
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def settle_download(path: Path, statics_media_dir: Path) -> Path:
    """Put a file fetched by the **Baixar** button where the others live.

    GOWA's auto-download writes straight into ``statics/media/``, but its
    on-demand download files the result under ``<phone>/<date>/`` and, because
    it does not know the mimetype, without an extension. The Files screen would
    show that as a folder holding a "document". This moves the file to the media
    root, adds the extension the content calls for, and prunes the folders left
    empty. Returns the final absolute path; on any filesystem error the file is
    left where it was and that path is returned, so the message still points at
    a file that exists.

    ``path`` must already have been validated by :func:`is_discardable_path`.
    """
    root = statics_media_dir.resolve()
    name = path.name
    if not path.suffix:
        name += file_manager.extension_for(file_manager.preview_mime(path))
    target = root / name
    try:
        if path != target:
            if target.exists():
                # Names carry a uuid, so this is not expected; never overwrite.
                target = root / f"{uuid.uuid4()}{target.suffix}"
            os.replace(path, target)
    except OSError as e:
        logger.warning("Could not move a downloaded file to the media root: %s", e)
        return path
    _prune_empty_dirs(path.parent, root)
    return target


def _prune_empty_dirs(directory: Path, root: Path) -> None:
    """Remove the (now empty) folder tree a file was moved out of.

    Starts at the top-level folder under ``root`` that contains ``directory``
    and only ever calls ``rmdir``, which refuses anything that still has content.
    The root itself is never touched.
    """
    try:
        top = root / directory.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return  # already at the root, or outside it
    if top.is_symlink() or not top.is_dir():
        return
    for current, _dirs, _files in os.walk(top, topdown=False, followlinks=False):
        try:
            os.rmdir(current)
        except OSError:
            pass
