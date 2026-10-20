"""GOWA binary resolution.

The GOWA binary can live in two places:

* ``bin/<name>``: the **bundled** baseline. Whatever the Docker image, the
  platform launcher or the repository itself shipped. Read-only at runtime:
  in Docker it is a symlink into the image layer (lost on redeploy) and on
  Windows ``bin/gowa.exe`` is tracked by git (writing there would dirty the
  working tree and conflict on ``git pull``).
* ``storages/bin/<name>``: the **managed** override, installed by the panel.
  ``storages/`` is gitignored, is a named volume in Docker and is preserved by
  the WhatsBot-Lite self-updater, so it is the only writable location that survives
  in all four environments.

``resolve_binary()`` compares versions instead of blindly preferring the
managed copy, so a rebuilt image carrying a *newer* GOWA wins over a stale
override.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Fallback used only when the GOWA_VERSION file is missing (e.g. a partial
# checkout). Keep in sync with the GOWA_VERSION file at the repo root.
GOWA_BASELINE_VERSION = "9.6.0"

# Escape hatch for dev/CI: point at an arbitrary GOWA binary.
ENV_BINARY_OVERRIDE = "WHATSBOT_GOWA_BINARY"

_META_FILENAME = "gowa_meta.json"
_STAMP_FILENAME = ".gowa_stamp"
_VERSION_FILENAME = "GOWA_VERSION"


# ── Paths ──────────────────────────────────────────────────────────────


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def binary_name() -> str:
    return "gowa.exe" if sys.platform == "win32" else "gowa"


def bundled_binary_path() -> Path:
    return repo_root() / "bin" / binary_name()


def bundled_stamp_path() -> Path:
    return repo_root() / "bin" / _STAMP_FILENAME


def version_file_path() -> Path:
    return repo_root() / _VERSION_FILENAME


def managed_dir(create: bool = False) -> Path:
    path = repo_root() / "storages" / "bin"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def managed_binary_path() -> Path:
    return managed_dir() / binary_name()


def backup_binary_path() -> Path:
    return managed_dir() / (binary_name() + ".bak")


def meta_path() -> Path:
    return managed_dir() / _META_FILENAME


def tmp_dir(create: bool = False) -> Path:
    """Staging dir for downloads.

    Deliberately inside ``storages/bin`` so the final ``os.replace`` is atomic
    (same filesystem). ``/tmp`` is frequently a different mount, and tiny in
    containers.
    """
    path = managed_dir(create=create) / ".tmp"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def session_backup_dir(version: str, create: bool = False) -> Path:
    path = managed_dir() / "session_backup" / version
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


# ── Versions ───────────────────────────────────────────────────────────


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse ``9.1.0`` / ``v9.1.0`` / ``9.1.0-rc1`` into a comparable tuple.

    Anything unparseable becomes ``(0, 0, 0)`` so it always loses a comparison
    rather than raising in the middle of an update.
    """
    if not value:
        return (0, 0, 0)
    text = str(value).strip().lstrip("vV")
    # Drop pre-release / build suffixes: 9.1.0-rc1 → 9.1.0
    for sep in ("-", "+", " "):
        if sep in text:
            text = text.split(sep, 1)[0]
    parts = text.split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except (TypeError, ValueError):
            return (0, 0, 0)
    if not numbers:
        return (0, 0, 0)
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def version_cmp(a: str, b: str) -> int:
    """Return -1/0/1 comparing two version strings."""
    va, vb = parse_version(a), parse_version(b)
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def read_bundled_version() -> str:
    """Version of the binary sitting in ``bin/``.

    ``bin/.gowa_stamp`` is written by the launchers/Dockerfile after a download,
    so it is authoritative for that machine. The tracked ``GOWA_VERSION`` file is
    the fallback (and the source of truth for what *should* be there).
    """
    stamp = bundled_stamp_path()
    try:
        if stamp.is_file():
            value = stamp.read_text(encoding="utf-8").strip()
            if value:
                return value.lstrip("vV")
    except OSError as exc:
        logger.debug("Could not read %s: %s", stamp, exc)

    version_file = version_file_path()
    try:
        if version_file.is_file():
            value = version_file.read_text(encoding="utf-8").strip()
            if value:
                return value.lstrip("vV")
    except OSError as exc:
        logger.debug("Could not read %s: %s", version_file, exc)

    return GOWA_BASELINE_VERSION


# ── Metadata of the managed binary ─────────────────────────────────────


def read_meta() -> dict:
    """Read ``storages/bin/gowa_meta.json``. Never raises."""
    try:
        raw = meta_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("gowa_meta.json is corrupted, ignoring it.")
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(data: dict) -> None:
    """Write the metadata atomically (tmp file + os.replace)."""
    managed_dir(create=True)
    target = meta_path()
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, target)


# ── Resolution ─────────────────────────────────────────────────────────


def resolve_binary() -> tuple[Path, str, str]:
    """Return ``(path, source, version)`` for the GOWA binary to run.

    ``source`` is one of ``env``, ``managed`` or ``bundled``.
    """
    override = os.environ.get(ENV_BINARY_OVERRIDE, "").strip()
    if override:
        return Path(override), "env", read_meta().get("version", "") or "desconhecida"

    bundled_version = read_bundled_version()
    managed = managed_binary_path()
    if managed.exists():
        managed_version = read_meta().get("version", "")
        if managed_version and version_cmp(managed_version, bundled_version) >= 0:
            return managed, "managed", managed_version
        logger.info(
            "GOWA gerenciado (%s) é mais antigo que o do sistema (%s); usando bin/.",
            managed_version or "sem metadados",
            bundled_version,
        )

    return bundled_binary_path(), "bundled", bundled_version


def installed_version() -> str:
    return resolve_binary()[2]


def backup_version() -> str:
    """Version the rollback would restore, or '' when rollback is unavailable."""
    meta = read_meta()
    if backup_binary_path().exists():
        return meta.get("previous_version", "") or "anterior"
    # No .bak means the previous binary was the bundled one, which is always
    # present, so rolling back just means dropping the managed copy.
    if managed_binary_path().exists():
        return read_bundled_version()
    return ""


def can_rollback() -> bool:
    return managed_binary_path().exists()
