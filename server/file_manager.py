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
import codecs
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, NamedTuple

logger = logging.getLogger(__name__)

# root key (what the API exposes) -> directory name under ``statics/``
ROOTS: dict[str, str] = {"received": "media", "sent": "operator"}

IMAGE_EXT = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg", ".pjp", ".png", ".gif", ".webp", ".avif",
    ".bmp", ".tif", ".tiff", ".heic", ".heif",
})
AUDIO_EXT = frozenset({".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".amr", ".oga"})
VIDEO_EXT = frozenset({".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi", ".m4v"})

KINDS = ("image", "audio", "video", "document")
SORTS = ("date", "size", "name", "kind")

MAX_DELETE_IDS = 1000

MAX_NAME_LEN = 200

#: Names our own frontend invents when the browser gives none (mic recording,
#: pasted screenshot). They say nothing about the file, so they are not kept.
_GENERIC_UPLOAD_NAMES = frozenset({"image.png", "voice.ogg", "blob"})

#: Characters Windows refuses in a file name (a download saved on Windows would
#: otherwise be renamed by the browser or fail), plus every control character.
_NAME_FORBIDDEN = frozenset('<>:"|?*') | {chr(c) for c in range(32)} | {chr(127)}


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


def clean_name(raw) -> str:
    """The name a person gave a file, made safe to show and to download as.

    Keeps only the last path component (a hostile ``..\\..\\x.exe`` becomes
    ``x.exe``), drops characters no file system accepts, and caps the length
    without cutting the extension. Returns ``""`` when nothing usable is left,
    which callers read as "this file has no name of its own". Idempotent.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch not in _NAME_FORBIDDEN)
    name = name.strip().rstrip(" .")
    if not name:
        return ""
    if len(name) > MAX_NAME_LEN:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[: MAX_NAME_LEN - len(ext)].rstrip(" .") + ext
    return name


def upload_name(raw) -> str:
    """:func:`clean_name` for a file the operator picked, minus app-made defaults."""
    name = clean_name(raw)
    return "" if name.lower() in _GENERIC_UPLOAD_NAMES else name


def final_name(disk_name: str, wanted) -> str:
    """The name to show and to download a file as.

    ``wanted`` is what the sender called it; with no usable name the on-disk name
    stays. A sender name without an extension borrows the one on disk, so the
    download still opens ("Relatorio" -> "Relatorio.pdf").
    """
    name = clean_name(wanted)
    if not name:
        return disk_name
    if not os.path.splitext(name)[1]:
        name += os.path.splitext(disk_name)[1]
    return name


# Documents saved before ``messages.media_name`` existed carry the name only in
# the text: "[Documento recebido: nome.ext]" (+ caption on the next lines). The
# name may itself contain "]", so the closing bracket must end the line.
_DOC_NAME_RE = re.compile(r"^\[Documento (?:recebido|enviado): (.+?)\](?:\n|$)")


def name_from_content(content: str) -> str:
    """File name out of a document message's text, or ``""`` when it has none."""
    m = _DOC_NAME_RE.match(content or "")
    if not m:
        return ""
    name = clean_name(m.group(1))
    # The placeholder the webhook writes when GOWA never told the name.
    return "" if name.lower() == "documento" else name


# What the webhook wraps around the words a person can actually search for: the
# placeholder it writes when there is no caption ("[Áudio recebido]", "[Imagem
# enviada]", "[Documento recebido: nome.ext]", "[Sticker]") and the labels it puts
# in front of a transcription or description. Left in, a search for "recebido" or
# "áudio" would match every file.
_CONTENT_TAG_RE = re.compile(
    r"\[(?:Áudio|Vídeo|Imagem|Documento) [^\]\n]*\]"
    r"|\[Sticker\]"
    r"|\[(?:Transcrição do áudio|Descrição da imagem|Conteúdo do documento)\]:?"
)


def searchable_text(content: str) -> str:
    """The words in a media message's text that a search should look at.

    Caption, audio transcription and image description, without the bracketed
    placeholders and labels the app adds around them. Returns ``""`` when nothing
    but a placeholder is left.
    """
    return _CONTENT_TAG_RE.sub(" ", content or "").strip()


def kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "document"


# ── what the viewer may show inline ────────────────────────────────────
#
# The viewer decides by the Content-Type this module picks, never by the file
# name, so the choice lives in one place. The OS ``mimetypes`` table is not used
# first because it differs between Windows and Linux and misses .jfif/.pjpeg.

#: Sent inline even when some browsers cannot decode it; the viewer falls back
#: to a download button then (HEIC and TIFF outside Safari, AMR, ...).
_MIME_BY_EXT: dict[str, str] = {
    **dict.fromkeys((".jpg", ".jpeg", ".jpe", ".jfif", ".pjpeg", ".pjp"), "image/jpeg"),
    ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heif",
    **dict.fromkeys((".ogg", ".oga", ".opus"), "audio/ogg"),
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
    ".flac": "audio/flac", ".amr": "audio/amr",
    **dict.fromkeys((".mp4", ".m4v"), "video/mp4"),
    ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".3gp": "video/3gpp", ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    **dict.fromkeys(
        (".txt", ".log", ".csv", ".tsv", ".json", ".md", ".xml", ".yaml", ".yml", ".ini", ".srt", ".vtt"),
        "text/plain",
    ),
}

#: Active content: shown as a download whatever it contains, so a file a contact
#: sent can never run inside the app's origin.
_NEVER_INLINE_EXT = frozenset({".html", ".htm", ".xhtml", ".svg", ".svgz"})

_SNIFF_BYTES = 4096
#: Control characters that do not occur in text (tab, LF, FF, CR and ESC do).
_BINARY_BYTES = bytes(b for b in range(32) if b not in (9, 10, 12, 13, 27))


def _sniff_media(head: bytes) -> str | None:
    """Recognise a media/PDF file by its first bytes (no extension, or a wrong one)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand == b"avif":
            return "image/avif"
        return "audio/mp4" if brand in (b"M4A ", b"M4B ") else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head or head.translate(None, _BINARY_BYTES) != head:
        return False
    try:
        # Incremental, so a multibyte character cut by the 4 KB window is fine.
        codecs.getincrementaldecoder("utf-8")().decode(head)
    except UnicodeDecodeError:
        return False
    return True


def preview_mime(path: Path) -> str | None:
    """Content-Type to show ``path`` inline, or ``None`` when it is download-only.

    ``path`` must already be validated by :meth:`FileManager.resolve`. Order:
    extension table, then the file's first bytes (media signature, else "is it
    plain UTF-8 text?"). HTML and SVG return ``None`` whatever their content.
    """
    ext = path.suffix.lower()
    if ext in _NEVER_INLINE_EXT:
        return None
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return None
    return _sniff_media(head) or ("text/plain" if _looks_like_text(head) else None)


#: Extension to give a file that arrived without one, by what it really is.
#: Plain text is left out on purpose: there is no honest guess between .txt/.csv/...
_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/avif": ".avif", "image/heic": ".heic",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
    "video/mp4": ".mp4", "video/webm": ".webm",
    "application/pdf": ".pdf",
}


def extension_for(mime: str | None) -> str:
    """``.jpg`` for ``image/jpeg`` and so on; ``""`` when the type is unknown."""
    return _EXT_BY_MIME.get(mime or "", "")


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
        date_from: float | None = None,
        date_to: float | None = None,
        selector: Callable[[Entry, str], dict | None] | None = None,
        offset: int = 0,
        limit: int = 100,
        name_resolver: Callable[[list[str]], dict[str, str]] | None = None,
    ) -> dict:
        """One page of a folder (or of every file under it when ``flat``).

        ``date_from`` (inclusive) and ``date_to`` (exclusive) bound the file's
        modified time, the same date the listing shows. ``selector`` is the hook
        for filters that need the database (conversation, text search): it gets
        each file and its ``media_path`` and returns ``None`` to drop it, or a
        dict of extra fields to merge into its row (``{}`` keeps it as is). This
        module stays free of database access; the caller supplies the knowledge.

        ``name_resolver`` maps ``media_path`` strings to the name the sender gave
        the file. When it knows one the row's ``name`` becomes that instead of the
        on-disk name. Sorting by name needs every name up front; any other sort
        resolves just the page that is returned.
        """
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
        if date_from is not None:
            entries = [e for e in entries if e.mtime >= date_from]
        if date_to is not None:
            entries = [e for e in entries if e.mtime < date_to]

        extras: dict[str, dict] = {}
        if selector is not None:
            kept: list[Entry] = []
            for e in entries:
                extra = selector(e, self.db_path(root, e.rel))
                if extra is None:
                    continue
                kept.append(e)
                if extra:
                    extras[e.rel] = extra
            entries = kept

        def orphan(e: Entry) -> bool:
            return known_paths is not None and self.db_path(root, e.rel) not in known_paths

        if orphans_only:
            entries = [e for e in entries if orphan(e)]

        def file_row(e: Entry) -> dict:
            row = self._file_row(root, e, orphan(e))
            row.update(extras.get(e.rel, {}))
            return row

        rows: list[dict] = []
        if flat:
            for e in entries:
                rows.append(file_row(e))
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
                    rows.append(file_row(e))
            for name, d in dirs.items():
                rel = f"{prefix}{name}"
                rows.append({
                    "id": _encode_id(root, rel), "name": name, "type": "dir",
                    "kind": "dir", "size": d["size"], "count": d["count"],
                    "mtime": d["mtime"], "rel": rel,
                })

        named = False
        if name_resolver and sort == "name":
            self._apply_names(root, rows, name_resolver)
            named = True

        sort_key = {
            "date": lambda r: r["mtime"],
            "size": lambda r: r["size"],
            "name": lambda r: r["name"].lower(),
            "kind": lambda r: (r["kind"], r["name"].lower()),
        }[sort]
        rows.sort(key=sort_key, reverse=reverse)

        total = len(rows)
        page = rows[offset:offset + limit]
        if name_resolver and not named:
            self._apply_names(root, page, name_resolver)
        return {
            "items": page,
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "path": prefix.rstrip("/"),
            "total_bytes": sum(r["size"] for r in rows),
        }

    def _apply_names(self, root: str, rows: list[dict], resolver) -> None:
        files = [r for r in rows if r["type"] == "file"]
        if not files:
            return
        wanted = resolver([self.db_path(root, r["rel"]) for r in files])
        for r in files:
            r["name"] = final_name(r["name"], wanted.get(self.db_path(root, r["rel"])))

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
