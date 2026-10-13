"""Safe listing and removal of the media files the user may delete.

Design rules (see docs-planos/plano-gerenciador-arquivos-e-download-midia.md):

* **Allowlist, never a blocklist.** Only the two media folders below can ever be
  listed or touched: ``statics/media`` (received) and ``statics/operator`` (sent).
  Database, WhatsApp session, code, plugins and ``.env`` are outside these roots
  and therefore unreachable by construction.
* **The client never sends a path.** Listings hand out opaque ids; every
  operation decodes the id and re-validates it from scratch (inside the root, no
  ``..``, no symlink anywhere on the way, regular file or directory only), so a
  forged id gets the same answer as a bad one.
* **Symlinks are never followed, listed or deleted through.**
* **The root folders themselves are never removed.**

No FastAPI imports here: the module is plain filesystem logic, tested offline.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

logger = logging.getLogger(__name__)

# root key (what the API exposes) -> directory name under ``statics/``
ROOTS: dict[str, str] = {"received": "media", "sent": "operator"}

IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"})
AUDIO_EXT = frozenset({".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".amr", ".oga"})
VIDEO_EXT = frozenset({".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi", ".m4v"})

KINDS = ("image", "audio", "video", "document")
SORTS = ("date", "size", "name", "kind")

MAX_DELETE_IDS = 1000


class FileAccessError(ValueError):
    """The id does not point at something the user may see or delete."""


class Entry(NamedTuple):
    rel: str        # posix path relative to the root
    size: int
    mtime: float
    kind: str       # image | audio | video | document


@dataclass
class DeleteResult:
    files: int = 0
    bytes: int = 0
    failed: int = 0
    #: ``statics/<dir>/<rel>`` of every removed file, to flag the messages.
    removed_paths: list[str] = field(default_factory=list)


def kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "document"


def _encode_id(root: str, rel: str) -> str:
    raw = f"{root}:{rel}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_id(file_id: str) -> tuple[str, str]:
    if not isinstance(file_id, str) or not file_id or len(file_id) > 2048:
        raise FileAccessError("invalid id")
    try:
        padded = file_id + "=" * (-len(file_id) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise FileAccessError("invalid id") from exc
    root, sep, rel = raw.partition(":")
    if not sep or root not in ROOTS:
        raise FileAccessError("invalid id")
    return root, rel


def _clean_rel(rel: str) -> tuple[str, ...]:
    """Split a relative posix path into safe parts, or raise."""
    if "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise FileAccessError("invalid path")
    parts = PurePosixPath(rel).parts if rel else ()
    for part in parts:
        if part in ("", ".", "..") or "/" in part:
            raise FileAccessError("invalid path")
    return tuple(parts)


class FileManager:
    """Scans and deletes files under the two allowlisted media roots."""

    def __init__(self, data_dir: Path, *, cache_ttl: float = 30.0):
        self.data_dir = Path(data_dir)
        self.statics_dir = self.data_dir / "statics"
        self.roots: dict[str, Path] = {
            key: (self.statics_dir / name).resolve() for key, name in ROOTS.items()
        }
        self._ttl = cache_ttl
        self._lock = threading.Lock()
        self._scans: dict[str, tuple[float, list[Entry]]] = {}
        self._system: tuple[float, int] | None = None

    # ── scanning ──────────────────────────────────────────────────────

    def _walk(self, base: Path) -> list[Entry]:
        entries: list[Entry] = []
        if not base.is_dir():
            return entries
        stack: list[tuple[Path, str]] = [(base, "")]
        while stack:
            directory, prefix = stack.pop()
            try:
                it = os.scandir(directory)
            except OSError:
                continue
            with it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    rel = f"{prefix}{de.name}"
                    if stat.S_ISDIR(st.st_mode):
                        stack.append((Path(de.path), rel + "/"))
                    elif stat.S_ISREG(st.st_mode):
                        entries.append(Entry(rel, st.st_size, st.st_mtime, kind_of(de.name)))
                    # symlinks and special files: never listed
        return entries

    def scan(self, root: str, *, fresh: bool = False) -> list[Entry]:
        if root not in self.roots:
            raise FileAccessError("invalid root")
        now = time.monotonic()
        with self._lock:
            cached = self._scans.get(root)
            if cached and not fresh and now - cached[0] < self._ttl:
                return cached[1]
        entries = self._walk(self.roots[root])
        with self._lock:
            self._scans[root] = (time.monotonic(), entries)
        return entries

    def invalidate(self) -> None:
        with self._lock:
            self._scans.clear()
            self._system = None

    # ── usage ─────────────────────────────────────────────────────────

    def _system_bytes(self) -> int:
        """Size of everything the app keeps that is NOT deletable here."""
        now = time.monotonic()
        with self._lock:
            if self._system and now - self._system[0] < max(self._ttl, 60.0):
                return self._system[1]
        total = 0
        skip = {p for p in self.roots.values()}
        candidates = [self.data_dir / "storages", self.data_dir / "logs"]
        if self.statics_dir.is_dir():
            try:
                for de in os.scandir(self.statics_dir):
                    candidates.append(Path(de.path))
            except OSError:
                pass
        for base in candidates:
            if base.resolve() in skip:
                continue
            for e in self._walk(base):
                total += e.size
        with self._lock:
            self._system = (time.monotonic(), total)
        return total

    def usage(self) -> dict:
        out: dict = {"roots": {}}
        deletable = 0
        for key in ROOTS:
            entries = self.scan(key)
            size = sum(e.size for e in entries)
            deletable += size
            out["roots"][key] = {"files": len(entries), "bytes": size}
        system = self._system_bytes()
        out["deletable_bytes"] = deletable
        out["system_bytes"] = system
        out["total_bytes"] = deletable + system
        return out

    # ── listing ───────────────────────────────────────────────────────

    def db_path(self, root: str, rel: str) -> str:
        """The ``media_path`` form stored in ``messages`` for a file."""
        return f"statics/{ROOTS[root]}/{rel}"

    def url_for(self, root: str, rel: str) -> str:
        return "/" + self.db_path(root, rel)

    def list_entries(
        self,
        root: str,
        *,
        path: str = "",
        flat: bool = False,
        sort: str = "date",
        order: str = "desc",
        kind: str = "",
        known_paths: set[str] | None = None,
        orphans_only: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """One page of a folder (or of every file under it when ``flat``)."""
        if root not in self.roots:
            raise FileAccessError("invalid root")
        sort = sort if sort in SORTS else "date"
        reverse = order != "asc"
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 500))

        base_parts = _clean_rel(path.strip("/"))
        prefix = "/".join(base_parts)
        if prefix:
            prefix += "/"

        entries = [e for e in self.scan(root) if e.rel.startswith(prefix)]
        if kind in KINDS:
            entries = [e for e in entries if e.kind == kind]

        def orphan(e: Entry) -> bool:
            return known_paths is not None and self.db_path(root, e.rel) not in known_paths

        if orphans_only:
            entries = [e for e in entries if orphan(e)]

        rows: list[dict] = []
        if flat:
            for e in entries:
                rows.append(self._file_row(root, e, orphan(e)))
        else:
            dirs: dict[str, dict] = {}
            for e in entries:
                rest = e.rel[len(prefix):]
                head, slash, _ = rest.partition("/")
                if slash:
                    d = dirs.setdefault(head, {"size": 0, "count": 0, "mtime": 0.0})
                    d["size"] += e.size
                    d["count"] += 1
                    d["mtime"] = max(d["mtime"], e.mtime)
                else:
                    rows.append(self._file_row(root, e, orphan(e)))
            for name, d in dirs.items():
                rel = f"{prefix}{name}"
                rows.append({
                    "id": _encode_id(root, rel), "name": name, "type": "dir",
                    "kind": "dir", "size": d["size"], "count": d["count"],
                    "mtime": d["mtime"], "rel": rel,
                })

        sort_key = {
            "date": lambda r: r["mtime"],
            "size": lambda r: r["size"],
            "name": lambda r: r["name"].lower(),
            "kind": lambda r: (r["kind"], r["name"].lower()),
        }[sort]
        rows.sort(key=sort_key, reverse=reverse)

        total = len(rows)
        page = rows[offset:offset + limit]
        return {
            "items": page,
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "path": prefix.rstrip("/"),
            "total_bytes": sum(r["size"] for r in rows),
        }

    def _file_row(self, root: str, e: Entry, is_orphan: bool) -> dict:
        return {
            "id": _encode_id(root, e.rel),
            "name": e.rel.rsplit("/", 1)[-1],
            "rel": e.rel,
            "type": "file",
            "kind": e.kind,
            "size": e.size,
            "mtime": e.mtime,
            "url": self.url_for(root, e.rel),
            "orphan": is_orphan,
        }

    # ── id resolution (the security core) ─────────────────────────────

    def resolve(self, file_id: str) -> tuple[str, str, Path, bool]:
        """Validate an id; return ``(root, rel, absolute path, is_dir)``.

        Raises :class:`FileAccessError` for anything that is not a regular file
        or directory strictly inside an allowlisted root.
        """
        root, rel = _decode_id(file_id)
        parts = _clean_rel(rel)
        if not parts:
            raise FileAccessError("the root folder cannot be selected")
        base = self.roots[root]
        target = base.joinpath(*parts)
        try:
            st = os.lstat(target)
        except OSError as exc:
            raise FileAccessError("not found") from exc
        if stat.S_ISLNK(st.st_mode):
            raise FileAccessError("symlink")
        is_dir = stat.S_ISDIR(st.st_mode)
        if not (is_dir or stat.S_ISREG(st.st_mode)):
            raise FileAccessError("not a file")
        # A symlinked ancestor would make the resolved path differ from the
        # lexical one; refuse rather than guess where it really leads.
        try:
            if target.resolve() != base.joinpath(*parts):
                raise FileAccessError("symlink on path")
        except OSError as exc:
            raise FileAccessError("not found") from exc
        return root, "/".join(parts), target, is_dir

    # ── deletion ──────────────────────────────────────────────────────

    def delete(self, ids: list[str]) -> DeleteResult:
        """Delete the files/folders behind ``ids``. Never raises per item."""
        result = DeleteResult()
        if len(ids) > MAX_DELETE_IDS:
            raise FileAccessError("too many items")
        for file_id in dict.fromkeys(ids):
            try:
                root, rel, target, is_dir = self.resolve(file_id)
            except FileAccessError as exc:
                logger.warning("[Files] refused delete of an invalid id: %s", exc)
                result.failed += 1
                continue
            if is_dir:
                self._delete_dir(root, rel, target, result)
            else:
                self._delete_file(root, rel, target, result)
        self.invalidate()
        if result.files or result.failed:
            logger.info("[Files] removed %d files (%d bytes), %d failed",
                        result.files, result.bytes, result.failed)
        return result

    def _delete_file(self, root: str, rel: str, target: Path, result: DeleteResult) -> None:
        try:
            size = os.lstat(target).st_size
            os.unlink(target)
        except FileNotFoundError:
            return  # already gone (e.g. covered by a folder removed earlier)
        except OSError as exc:
            logger.warning("[Files] could not remove a file: %s", exc)
            result.failed += 1
            return
        result.files += 1
        result.bytes += size
        result.removed_paths.append(self.db_path(root, rel))

    def _delete_dir(self, root: str, rel: str, target: Path, result: DeleteResult) -> None:
        base = self.roots[root]
        # bottom-up: files first, then the directories that became empty
        dirs: list[Path] = []
        stack = [(target, rel)]
        while stack:
            directory, drel = stack.pop()
            dirs.append(directory)
            try:
                it = os.scandir(directory)
            except OSError:
                result.failed += 1
                continue
            with it:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=False)
                    except OSError:
                        result.failed += 1
                        continue
                    child_rel = f"{drel}/{de.name}"
                    if stat.S_ISDIR(st.st_mode):
                        stack.append((Path(de.path), child_rel))
                    elif stat.S_ISREG(st.st_mode):
                        self._delete_file(root, child_rel, Path(de.path), result)
                    # symlinks / special files are left alone
        for directory in reversed(dirs):
            if directory == base:
                continue
            try:
                directory.rmdir()  # only succeeds when empty
            except OSError:
                pass
