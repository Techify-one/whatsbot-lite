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

Two different questions, never mixed:

* **recommended version**: the ``GOWA_VERSION`` file. It changes with every
  WhatsBot-Lite release (the self-updater overwrites it) and says what this
  code was validated against.
* **installed version**: what is really in ``bin/`` (see
  ``identify_bundled_version``). The self-updater preserves ``bin/``, so after
  an update ``GOWA_VERSION`` describes the new code, not the old binary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Escape hatch for dev/CI: point at an arbitrary GOWA binary.
ENV_BINARY_OVERRIDE = "WHATSBOT_GOWA_BINARY"

_META_FILENAME = "gowa_meta.json"
_STAMP_FILENAME = ".gowa_stamp"
_VERSION_FILENAME = "GOWA_VERSION"
# ``bin/<binary>.version`` (e.g. ``bin/gowa.exe.version``): tracked in git and
# replaced together with the binary. NOT ``.gowa_stamp``, which is gitignored
# because the launchers write it per machine.
_SIDECAR_SUFFIX = ".version"

# sha256 → GOWA version of every ``bin/gowa.exe`` that has shipped in this
# repository. It identifies binaries whose installs never received a
# ``bin/gowa.exe.version`` (the self-updater preserves ``bin/`` and so never
# delivers new files into it). Checked against the official release zips
# (``whatsapp_<v>_windows_amd64.zip``, member ``windows-amd64.exe``).
# When ``bin/gowa.exe`` is replaced: add its row here AND update
# ``bin/gowa.exe.version`` (tests/test_gowa_update.py guards both).
KNOWN_BINARY_HASHES: dict[str, str] = {
    "aee59b056f9ab2ce6bb46a4faa3db596dfc2c1f374af758d30c824646b36961e": "8.3.3",  # v0.0.1 – v0.0.5
    "eb10b27f271a932d3923a27421af6b974edd40c88547ebd1d7b428df89875825": "8.5.0",  # v0.1.0 – v0.1.1
    "85ae4f63bdbbfb0205c3fa8f4dab7aa9ce72e898e445d739dca857a8e25ec473": "8.8.0",  # v0.2.0 – v0.2.12
    "23e7f8d22ab377174b54b11e13f2f7e6fded56f389c091971aa2976ee7173f4c": "9.6.0",  # v0.2.13
}

_HASH_CHUNK = 1024 * 1024
# path → (size, mtime_ns, sha256). resolve_binary() runs on almost every
# request that touches GOWA and the exe is ~30 MB, so hash once per file state.
_hash_cache: dict[str, tuple[int, int, str]] = {}
_warned_unknown: set[str] = set()


# ── Paths ──────────────────────────────────────────────────────────────


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def binary_name() -> str:
    return "gowa.exe" if sys.platform == "win32" else "gowa"


def bundled_binary_path() -> Path:
    return repo_root() / "bin" / binary_name()


def bundled_stamp_path() -> Path:
    return repo_root() / "bin" / _STAMP_FILENAME


def bundled_version_file_path() -> Path:
    return repo_root() / "bin" / (binary_name() + _SIDECAR_SUFFIX)


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


def is_known_version(value: str) -> bool:
    """True when ``value`` parses as a real version (``''`` / garbage → False)."""
    return parse_version(value) != (0, 0, 0)


def _read_version_file(path: Path) -> str:
    """Contents of a one-line version file, or ``''`` when absent/unusable."""
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8-sig").strip().lstrip("vV")
            if is_known_version(value):
                return value
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
    return ""


def file_sha256(path: Path) -> str:
    """sha256 of ``path`` (``''`` if unreadable), cached by ``(size, mtime_ns)``."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = str(path)
    cached = _hash_cache.get(key)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
        return cached[2]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.debug("Could not hash %s: %s", path, exc)
        return ""
    value = digest.hexdigest()
    _hash_cache[key] = (stat.st_size, stat.st_mtime_ns, value)
    return value


def identify_bundled_version() -> tuple[str, str]:
    """``(version, origin)`` of the binary sitting in ``bin/``.

    First hit wins:

    1. ``bin/.gowa_stamp``: written per machine by the Linux/macOS launchers and
       the Dockerfile after a download.
    2. sha256 of the binary against ``KNOWN_BINARY_HASHES``: covers installs
       that updated from an older release and never got file 3 (the
       self-updater preserves ``bin/``). Objective, so it also beats a stale
       file 3 left behind when only the ``.exe`` was swapped by hand.
    3. ``bin/<binary>.version``: tracked in git, replaced with the binary.
    4. Unknown: ``('', 'unknown')``.

    It deliberately never falls back to ``GOWA_VERSION``: that file describes
    the code, and the self-updater overwrites it while preserving ``bin/``.
    Unknown parses as ``(0, 0, 0)``, so it loses every comparison.
    """
    stamp = _read_version_file(bundled_stamp_path())
    if stamp:
        return stamp, "stamp"

    binary = bundled_binary_path()
    sidecar = _read_version_file(bundled_version_file_path())
    digest = file_sha256(binary)
    known = KNOWN_BINARY_HASHES.get(digest) if digest else None
    if known:
        if sidecar and sidecar != known and str(binary) not in _warned_unknown:
            _warned_unknown.add(str(binary))
            logger.warning(
                "%s diz %s, mas o hash do binário em %s é do GOWA %s; valendo o hash.",
                bundled_version_file_path().name, sidecar, binary, known,
            )
        return known, "hash"
    if sidecar:
        return sidecar, "version_file"

    if digest and str(binary) not in _warned_unknown:
        _warned_unknown.add(str(binary))
        logger.warning(
            "Não foi possível identificar a versão do GOWA em %s (sha256 %s…): "
            "sem stamp, sem arquivo .version e hash desconhecido.",
            binary, digest[:12],
        )
    return "", "unknown"


def read_bundled_version() -> str:
    """Version of the binary in ``bin/``; ``''`` when it cannot be identified."""
    return identify_bundled_version()[0]


def read_recommended_version() -> str:
    """Version this WhatsBot-Lite release recommends (``GOWA_VERSION``).

    Never use it to decide what is installed; see ``identify_bundled_version``.
    """
    return _read_version_file(version_file_path())


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
        # An unidentified bundled binary compares as (0, 0, 0), so any managed
        # copy with a real version wins over it.
        if is_known_version(managed_version) and version_cmp(managed_version, bundled_version) >= 0:
            return managed, "managed", managed_version
        logger.info(
            "GOWA gerenciado (%s) é mais antigo que o do sistema (%s); usando bin/.",
            managed_version or "sem metadados",
            bundled_version or "versão desconhecida",
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
        return read_bundled_version() or "anterior"
    return ""


def can_rollback() -> bool:
    return managed_binary_path().exists()
