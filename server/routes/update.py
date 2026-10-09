"""WhatsBot-Lite — self-update endpoint (uses GitHub Releases API for versioning)."""

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from plugins.restart import schedule_restart
from server.helpers import _ok, _err

logger = logging.getLogger(__name__)

GITHUB_REPO = "Techify-one/whatsbot-lite"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_LATEST_RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_RAW_VERSION_URL_TEMPLATE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{{tag}}/WHATSBOT_VERSION"
GITHUB_TAG_ZIP_URL_TEMPLATE = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{{tag}}.zip"
RELEASE_CACHE_TTL = 300
RELEASE_FAILURE_CACHE_TTL = 60

VERSION_FILENAME = "WHATSBOT_VERSION"

PRESERVE_DIRS = {"storages", "statics", "logs", "venv", ".git", "bin"}
PRESERVE_FILES = {".env"}


def _get_project_root(settings) -> Path:
    return Path(settings.data_dir)


def _version_key(value: str) -> tuple[int, int, int]:
    numbers = [int(part) for part in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((numbers + [0, 0, 0])[:3])


POPUP_MARKER_PATH = "storages/update_popup.json"
_release_cache: dict = {"value": None, "fetched_at": 0.0}


def _popup_marker_path(project_root: Path) -> Path:
    return project_root / POPUP_MARKER_PATH


def _clear_popup_pending(project_root: Path) -> None:
    """Remove the marker used by releases before update notifications existed."""
    path = _popup_marker_path(project_root)
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Failed to clear update popup marker: %s", exc)


def _read_local_version(project_root: Path) -> dict:
    """Read the installed version + per-version changelog from WHATSBOT_VERSION.

    The file ships inside the repo and inside every release's tag zip (bumped
    by the /release-up flow before tagging, which PREPENDS a new entry to
    `changelog` instead of overwriting it), so history accumulates release
    over release. Reading from this tracked file — instead of `git describe`
    — also works whether the install is a git clone or a plain "Download ZIP":
    a ZIP install has no .git directory, so `git describe` silently fails and
    always reports "0.0.0".

    `changelog` is newest-first: `changelog[0]` is always the entry for the
    installed `version`. `popup_shown` remains True only for compatibility
    with frontend files cached from releases that showed post-update news.
    """
    path = project_root / VERSION_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        changelog = [
            {"version": str(e.get("version") or ""), "description": str(e.get("description") or "")}
            for e in (data.get("changelog") or [])
            if isinstance(e, dict)
        ]
        version = str(data.get("version") or (changelog[0]["version"] if changelog else "0.0.0"))
        return {"version": version, "changelog": changelog, "popup_shown": True}
    except Exception as exc:
        logger.debug("Failed to read %s: %s", VERSION_FILENAME, exc)
        return {"version": "0.0.0", "changelog": [], "popup_shown": True}


def _fetch_release_without_api() -> dict:
    """Resolve the latest tag and changelog without GitHub API rate limits."""
    req = urllib.request.Request(GITHUB_LATEST_RELEASE_URL, headers={"User-Agent": "WhatsBot"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        final_url = resp.geturl()
    tag = urllib.parse.unquote(urllib.parse.urlparse(final_url).path.rstrip("/").rsplit("/", 1)[-1])
    if not tag or "/" in tag or not tag.startswith("v"):
        raise RuntimeError("o GitHub não informou a tag da última release")

    description = ""
    version_url = GITHUB_RAW_VERSION_URL_TEMPLATE.format(tag=urllib.parse.quote(tag, safe=""))
    try:
        version_req = urllib.request.Request(version_url, headers={"User-Agent": "WhatsBot"})
        with urllib.request.urlopen(version_req, timeout=10) as resp:
            version_data = json.loads(resp.read().decode("utf-8"))
        version = tag.lstrip("v")
        entry = next(
            (item for item in version_data.get("changelog", []) if str(item.get("version")) == version),
            None,
        )
        if entry:
            description = str(entry.get("description") or "")
    except Exception as exc:
        logger.warning("Failed to fetch release changelog from tag %s: %s", tag, exc)

    return {
        "tag": tag,
        "version": tag.lstrip("v"),
        "description": description,
        "url": final_url,
    }


def _fetch_latest_release(force: bool = False) -> dict:
    """Fetch and cache the latest release, with a non-API fallback."""
    now = time.monotonic()
    cached = _release_cache.get("value")
    cache_ttl = RELEASE_CACHE_TTL if cached and cached.get("tag") else RELEASE_FAILURE_CACHE_TTL
    if not force and cached is not None and now - _release_cache["fetched_at"] < cache_ttl:
        return dict(cached)

    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "WhatsBot"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            result = {
                "tag": tag,
                "version": tag.lstrip("v"),
                "description": data.get("body") or "",
                "url": data.get("html_url", ""),
            }
    except Exception as exc:
        logger.warning("GitHub Releases API unavailable, using public release page: %s", exc)
        try:
            result = _fetch_release_without_api()
        except Exception as fallback_exc:
            logger.warning("Failed to fetch latest release: %s", fallback_exc)
            result = {"tag": "", "version": "", "description": "", "url": ""}

    _release_cache["value"] = dict(result)
    _release_cache["fetched_at"] = now
    return result


def _should_preserve(rel_path: str) -> bool:
    """Return True if *rel_path* must NOT be overwritten during update."""
    parts = Path(rel_path).parts
    if not parts:
        return True
    if parts[0] in PRESERVE_DIRS:
        return True
    if rel_path in PRESERVE_FILES:
        return True
    if "__pycache__" in parts:
        return True
    return False


def _perform_update(project_root: Path, tag: str) -> dict:
    """Download the given release tag's ZIP from GitHub and overwrite code files.

    Downloads the tag's snapshot (not `main` HEAD) so a self-update always
    lands on the stable, released state instead of whatever unreleased work
    happens to be on the branch.
    """
    zip_url = GITHUB_TAG_ZIP_URL_TEMPLATE.format(tag=tag)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "update.zip"

        # ── Download ──────────────────────────────────────────────
        logger.info("Downloading update (%s) from %s", tag, zip_url)
        try:
            urllib.request.urlretrieve(zip_url, str(zip_path))
        except Exception as exc:
            raise RuntimeError(f"Erro ao baixar a release {tag}: {exc}") from exc

        # ── Extract ───────────────────────────────────────────────
        try:
            zf = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as exc:
            raise RuntimeError("Arquivo de atualização inválido.") from exc

        with zf:
            names = zf.namelist()
            if not names:
                raise RuntimeError("ZIP vazio.")

            # GitHub tag ZIPs have a top-level folder like "whatsbot-0.1.1/"
            top_folder = names[0].split("/")[0] + "/"

            extract_dir = tmp_path / "extracted"
            zf.extractall(extract_dir)

        source_root = extract_dir / top_folder.rstrip("/")
        if not source_root.is_dir():
            raise RuntimeError(f"Estrutura inesperada no ZIP (pasta {top_folder} não encontrada).")

        # ── Copy new files ────────────────────────────────────────
        copied = 0
        for src_file in source_root.rglob("*"):
            if src_file.is_dir():
                continue

            rel = src_file.relative_to(source_root).as_posix()

            # Security: reject path traversal
            if ".." in rel:
                logger.warning("Skipping suspicious path: %s", rel)
                continue

            if _should_preserve(rel):
                continue

            dest = project_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            copied += 1

        # The tag's own WHATSBOT_VERSION file was just copied in above, so this
        # already reflects the new version + its changelog history.
        info = _read_local_version(project_root)
        # Releases before v0.2.4 left a marker for a post-update popup. The
        # current flow announces available releases before installation.
        _clear_popup_pending(project_root)
        logger.info("Update applied: %d files updated. New version: %s", copied, info["version"])
        return {
            "version": info["version"],
            "changelog": info["changelog"],
            "files_updated": copied,
            "message": f"Atualizado para v{info['version']} — {copied} arquivos atualizados.",
        }


def register_routes(app, deps):
    settings = deps.settings

    @app.get("/api/update/check")
    async def check_update(force: bool = False):
        project_root = _get_project_root(settings)
        current = await asyncio.to_thread(_read_local_version, project_root)
        latest = await asyncio.to_thread(_fetch_latest_release, force)
        has_update = bool(
            latest["version"] and _version_key(latest["version"]) > _version_key(current["version"])
        )
        notifications_enabled = bool(settings.get("whatsbot_update_notifications_enabled", True))
        skipped_version = str(settings.get("whatsbot_skipped_version", "") or "")
        return _ok({
            "current_version": current["version"],
            "latest_version": latest["version"],
            "latest_description": latest["description"],
            "release_url": latest["url"],
            "update_available": has_update,
            "notifications_enabled": notifications_enabled,
            "skipped_version": skipped_version,
            "should_notify": bool(
                has_update and notifications_enabled and latest["version"] != skipped_version
            ),
        })

    @app.get("/api/update/local-version")
    async def local_version():
        # Local-only read (no GitHub call) — safe to call on every app boot to
        # drive the "what's new" popup without hitting the GitHub API rate limit.
        # Returns the full changelog history (newest-first) so the frontend can
        # show everything a user missed if they skipped several releases.
        project_root = _get_project_root(settings)
        info = await asyncio.to_thread(_read_local_version, project_root)
        return _ok(info)

    @app.post("/api/update/popup-seen")
    async def mark_popup_seen():
        # Backward compatibility for a cached frontend from an older release.
        project_root = _get_project_root(settings)
        await asyncio.to_thread(_clear_popup_pending, project_root)
        info = await asyncio.to_thread(_read_local_version, project_root)
        return _ok(info)

    @app.post("/api/update/skip-version")
    async def skip_version(body: dict):
        version = str(body.get("version") or "").strip().lstrip("v")
        if not version or len(version) > 50 or any(char in version for char in "/\\\r\n"):
            return _err("versão inválida", 400)
        settings["whatsbot_skipped_version"] = version
        settings.save()
        return _ok({"skipped_version": version})

    @app.post("/api/update")
    async def apply_update():
        project_root = _get_project_root(settings)
        latest = await asyncio.to_thread(_fetch_latest_release)
        if not latest["tag"]:
            return _err("Não foi possível encontrar uma release publicada no GitHub.", 502)
        try:
            result = await asyncio.to_thread(_perform_update, project_root, latest["tag"])
        except RuntimeError as exc:
            return _err(str(exc), 500)
        except Exception as exc:
            logger.exception("Unexpected error during update")
            return _err(f"Erro inesperado: {exc}", 500)
        result["restarting"] = True
        result["message"] += " O WhatsBot-Lite será reiniciado automaticamente para aplicar a atualização."
        schedule_restart(reason=f"WhatsBot-Lite updated to v{result['version']}")
        return _ok(result)
