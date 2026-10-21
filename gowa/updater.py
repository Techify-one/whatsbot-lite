"""GOWA binary updater.

Downloads a release from the official ``go-whatsapp-web-multidevice`` GitHub
repo, verifies its SHA-256 against the published checksums, swaps the binary in
``storages/bin/`` and restarts the subprocess, rolling back automatically when
the new build fails its health check.

Everything here is synchronous and meant to be driven from FastAPI through
``asyncio.to_thread``. Progress is reported through the ``on_progress``
callback so the caller decides how to surface it (WebSocket, log, nothing).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from gowa import binary as gowa_binary

logger = logging.getLogger(__name__)

GOWA_GH_REPO = "aldinokemal/go-whatsapp-web-multidevice"
GOWA_RELEASES_API = f"https://api.github.com/repos/{GOWA_GH_REPO}/releases/latest"
GOWA_RELEASES_LIST_API = f"https://api.github.com/repos/{GOWA_GH_REPO}/releases?per_page=100"
GOWA_COMPARE_API = f"https://api.github.com/repos/{GOWA_GH_REPO}/compare/v{{current}}...v{{latest}}"
GOWA_RAW_GO_MOD = f"https://raw.githubusercontent.com/{GOWA_GH_REPO}/v{{version}}/src/go.mod"
WHATSMEOW_COMPARE_API = "https://api.github.com/repos/tulir/whatsmeow/compare/{current}...{latest}"
GOWA_RELEASE_DOWNLOAD = (
    f"https://github.com/{GOWA_GH_REPO}/releases/download/v{{version}}/{{asset}}"
)
GOWA_RELEASE_PAGE = f"https://github.com/{GOWA_GH_REPO}/releases/tag/v{{version}}"

# Versions the WhatsBot-Lite client is known to talk to. Anything outside this range
# is still offered, but flagged as "não homologada": it needs an explicit
# confirmation and never triggers the proactive modal.
GOWA_SUPPORTED_RANGE = ">=8.8.0,<10.0.0"

_CHECK_TTL_SEC = 3600        # cache of the GitHub releases API
_FORCE_MIN_INTERVAL = 60     # floor between manual "Verificar" clicks
_HEALTH_TIMEOUT_SEC = 45     # must stay under the watchdog's 3-crashes-in-60s budget
_DOWNLOAD_TIMEOUT_SEC = 120
_API_TIMEOUT_SEC = 15
_CHUNK = 64 * 1024
_NOTES_MAX_CHARS = 12000

# The download URL is always built server-side from a version matched by this
# regex. Never accept a URL from the client: it would turn into SSRF / path
# traversal on the release host.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

_UA = "WhatsBot"

# GOWA writes the WhatsApp session here (NOT storages/whatsbot.db, which is
# ours). A major upgrade can migrate this schema irreversibly, so we snapshot it
# before crossing a major boundary or the rollback button would be theatre.
_SESSION_DB_PREFIXES = ("whatsapp.db", "chatstorage.db")


class GowaUpdateBusy(RuntimeError):
    """Raised when an install/rollback is already running."""


class GowaUpdateError(RuntimeError):
    """User-facing failure during an update."""


_UPDATE_LOCK = threading.Lock()
_release_cache: dict = {"data": None, "fetched_at": 0.0, "etag": ""}
_release_list_cache: dict = {"data": None, "fetched_at": 0.0, "etag": ""}
_compare_cache: dict[tuple[str, str], dict] = {}
_whatsmeow_cache: dict[tuple[str, str], dict] = {}
_state: dict = {"status": "idle", "phase": "", "progress": 0, "version": "", "message": ""}


# ── Platform / assets ──────────────────────────────────────────────────

_OS_MAP = {"linux": "linux", "darwin": "darwin", "win32": "windows"}
_ARCH_MAP = {
    "x86_64": "amd64", "amd64": "amd64",
    "aarch64": "arm64", "arm64": "arm64",
    "i386": "386", "i686": "386", "x86": "386",
    "armv7l": "armv7", "armv7": "armv7", "armv6l": "armv7",
}
# armv7 and 386 builds only exist for linux and windows.
_VALID_COMBOS = {
    ("linux", "amd64"), ("linux", "arm64"), ("linux", "386"), ("linux", "armv7"),
    ("darwin", "amd64"), ("darwin", "arm64"),
    ("windows", "amd64"), ("windows", "386"),
}


def detect_platform() -> tuple[str, str] | None:
    """Return ``(os, arch)`` in GOWA release naming, or None when unsupported."""
    os_ = _OS_MAP.get(sys.platform)
    arch = _ARCH_MAP.get(platform.machine().lower())
    if not os_ or not arch or (os_, arch) not in _VALID_COMBOS:
        return None
    return os_, arch


def asset_name(version: str, os_: str, arch: str) -> str:
    return f"whatsapp_{version}_{os_}_{arch}.zip"


def inner_binary_name(os_: str, arch: str) -> str:
    """Name of the binary inside the release zip: ``linux-amd64``, ``windows-amd64.exe``."""
    return f"{os_}-{arch}.exe" if os_ == "windows" else f"{os_}-{arch}"


def checksums_asset(os_: str) -> str:
    """macOS checksums live in a separate file; using the wrong one 404s."""
    return "checksums-macos.txt" if os_ == "darwin" else "checksums.txt"


# ── Supported range ────────────────────────────────────────────────────

_RANGE_RE = re.compile(r"^\s*(>=|<=|==|>|<)\s*(\S+)\s*$")


def is_supported(version: str, spec: str = GOWA_SUPPORTED_RANGE) -> bool:
    """Evaluate ``version`` against a comma-separated range like ``>=8.8.0,<10.0.0``."""
    if not version:
        return False
    for clause in spec.split(","):
        match = _RANGE_RE.match(clause)
        if not match:
            continue
        op, bound = match.group(1), match.group(2)
        cmp = gowa_binary.version_cmp(version, bound)
        if op == ">=" and cmp < 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == "<" and cmp >= 0:
            return False
        if op == "==" and cmp != 0:
            return False
    return True


def _is_major_jump(current: str, target: str) -> bool:
    return gowa_binary.parse_version(current)[0] != gowa_binary.parse_version(target)[0]


# ── GitHub releases API ────────────────────────────────────────────────


def _api_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": _UA,
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_latest_release(force: bool = False) -> dict:
    """Return ``{version, published_at, notes, url, rate_limited, cached}``.

    Cached for an hour. The unauthenticated GitHub API allows 60 requests per
    hour per IP, and a panel open in several tabs plus the daily task would burn
    through that, so a rate-limit response falls back to the cache instead of
    surfacing an error.
    """
    now = time.monotonic()
    age = now - float(_release_cache.get("fetched_at") or 0.0)
    cached = _release_cache.get("data")

    if cached and not force and age < _CHECK_TTL_SEC:
        return {**cached, "cached": True, "rate_limited": False}
    if cached and force and age < _FORCE_MIN_INTERVAL:
        return {**cached, "cached": True, "rate_limited": False}

    headers = _api_headers()
    etag = _release_cache.get("etag") or ""
    if etag and cached:
        headers["If-None-Match"] = etag

    req = urllib.request.Request(GOWA_RELEASES_API, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            new_etag = resp.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached:
            _release_cache["fetched_at"] = now
            return {**cached, "cached": True, "rate_limited": False}
        if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
            logger.warning("GitHub API rate limit reached; serving cached GOWA release info.")
            if cached:
                return {**cached, "cached": True, "rate_limited": True}
            raise GowaUpdateError(
                "Limite de consultas do GitHub atingido. Tente de novo em alguns minutos."
            ) from exc
        raise GowaUpdateError(f"GitHub respondeu {exc.code} ao consultar a release.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        if cached:
            logger.warning("GOWA release check failed (%s); serving cache.", exc)
            return {**cached, "cached": True, "rate_limited": False}
        raise GowaUpdateError(f"Não consegui consultar as releases do GOWA: {exc}") from exc

    notes = (payload.get("body") or "").strip()
    data = {
        "version": (payload.get("tag_name") or "").lstrip("vV"),
        "published_at": payload.get("published_at") or "",
        "notes": notes[:_NOTES_MAX_CHARS],
        "url": payload.get("html_url") or "",
    }
    _release_cache.update({"data": data, "fetched_at": now, "etag": new_etag})
    return {**data, "cached": False, "rate_limited": False}


def _release_from_api(payload: dict) -> dict:
    """Normalize one GitHub release without exposing asset metadata downstream."""
    version = str(payload.get("tag_name") or "").lstrip("vV")
    return {
        "version": version,
        "published_at": payload.get("published_at") or "",
        "notes": str(payload.get("body") or "").strip()[:_NOTES_MAX_CHARS],
        "url": payload.get("html_url") or GOWA_RELEASE_PAGE.format(version=version),
    }


def fetch_release_interval(current: str, latest: str, force: bool = False) -> list[dict]:
    """Return every stable release in ``(current, latest]``.

    Looking only at ``releases/latest`` loses important compatibility fixes when
    the installed version is several releases behind. The list is cached with
    ETag just like the latest-release request and failures fall back to the most
    recently cached list.
    """
    now = time.monotonic()
    age = now - float(_release_list_cache.get("fetched_at") or 0.0)
    cached = _release_list_cache.get("data")
    cache_fresh = (
        (not force and age < _CHECK_TTL_SEC)
        or (force and age < _FORCE_MIN_INTERVAL)
    )
    if cached and cache_fresh:
        payloads = cached
    else:
        headers = _api_headers()
        etag = _release_list_cache.get("etag") or ""
        if etag and cached:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(GOWA_RELEASES_LIST_API, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
                payloads = json.loads(resp.read().decode("utf-8"))
                new_etag = resp.headers.get("ETag", "")
            if not isinstance(payloads, list):
                raise json.JSONDecodeError("expected a release list", "", 0)
            _release_list_cache.update({
                "data": payloads, "fetched_at": now, "etag": new_etag,
            })
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                _release_list_cache["fetched_at"] = now
                payloads = cached
            elif cached:
                logger.warning("GOWA release-list request failed (%s); serving cache.", exc)
                payloads = cached
            else:
                raise GowaUpdateError(
                    f"GitHub respondeu {exc.code} ao consultar o histórico de releases."
                ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            if cached:
                logger.warning("GOWA release-list request failed (%s); serving cache.", exc)
                payloads = cached
            else:
                raise GowaUpdateError(
                    f"Não consegui consultar o histórico de releases do GOWA: {exc}"
                ) from exc

    releases = []
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
            continue
        release = _release_from_api(payload)
        version = release["version"]
        if not _VERSION_RE.match(version):
            continue
        if (gowa_binary.version_cmp(version, current) > 0
                and gowa_binary.version_cmp(version, latest) <= 0):
            releases.append(release)
    releases.sort(key=lambda item: gowa_binary.parse_version(item["version"]))
    return releases


def fetch_compare_commits(current: str, latest: str, force: bool = False) -> list[str]:
    """Return commit subjects between two release tags (best effort)."""
    if not _VERSION_RE.match(current or "") or not _VERSION_RE.match(latest or ""):
        return []
    key = (current, latest)
    now = time.monotonic()
    cached = _compare_cache.get(key)
    if cached:
        age = now - float(cached.get("fetched_at") or 0.0)
        cache_fresh = (
            (not force and age < _CHECK_TTL_SEC)
            or (force and age < _FORCE_MIN_INTERVAL)
        )
        if cache_fresh:
            return list(cached.get("data") or [])

    req = urllib.request.Request(
        GOWA_COMPARE_API.format(current=current, latest=latest), headers=_api_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        commits = payload.get("commits") if isinstance(payload, dict) else []
        subjects = []
        for commit in commits or []:
            message = ((commit.get("commit") or {}).get("message") or "").splitlines()[0].strip()
            if message:
                subjects.append(message[:300])
        _compare_cache[key] = {"data": subjects[:250], "fetched_at": now}
        return subjects[:250]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not compare GOWA v%s...v%s: %s", current, latest, exc)
        return list((cached or {}).get("data") or [])


_WHATSMEOW_VERSION_RE = re.compile(
    r"^\s*go\.mau\.fi/whatsmeow\s+v\S*?-([0-9a-f]{12,40})\s*$", re.MULTILINE
)


def _extract_whatsmeow_revision(go_mod: str) -> str:
    match = _WHATSMEOW_VERSION_RE.search(go_mod or "")
    return match.group(1) if match else ""


def _fetch_gowa_go_mod(version: str) -> str:
    if not _VERSION_RE.match(version or ""):
        return ""
    req = urllib.request.Request(
        GOWA_RAW_GO_MOD.format(version=version), headers={"User-Agent": _UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("Could not fetch GOWA v%s go.mod: %s", version, exc)
        return ""


def fetch_whatsmeow_commits(current: str, latest: str,
                            force: bool = False) -> list[str]:
    """Describe upstream whatsmeow changes between the two GOWA tags.

    GOWA release notes often contain only ``update whatsmeow to latest``. The
    upstream subjects provide the classifier with the protocol-level detail
    needed to decide whether that generic dependency bump matters.
    """
    if not _VERSION_RE.match(current or "") or not _VERSION_RE.match(latest or ""):
        return []
    key = (current, latest)
    now = time.monotonic()
    cached = _whatsmeow_cache.get(key)
    if cached:
        age = now - float(cached.get("fetched_at") or 0.0)
        cache_fresh = (
            (not force and age < _CHECK_TTL_SEC)
            or (force and age < _FORCE_MIN_INTERVAL)
        )
        if cache_fresh:
            return list(cached.get("data") or [])

    current_rev = _extract_whatsmeow_revision(_fetch_gowa_go_mod(current))
    latest_rev = _extract_whatsmeow_revision(_fetch_gowa_go_mod(latest))
    if not current_rev or not latest_rev or current_rev == latest_rev:
        _whatsmeow_cache[key] = {"data": [], "fetched_at": now}
        return []

    req = urllib.request.Request(
        WHATSMEOW_COMPARE_API.format(current=current_rev, latest=latest_rev),
        headers=_api_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        commits = payload.get("commits") if isinstance(payload, dict) else []
        subjects = []
        for commit in commits or []:
            message = ((commit.get("commit") or {}).get("message") or "").splitlines()[0].strip()
            if message:
                subjects.append(f"[whatsmeow] {message[:280]}")
        _whatsmeow_cache[key] = {"data": subjects[:250], "fetched_at": now}
        return subjects[:250]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not compare whatsmeow %s...%s: %s", current_rev, latest_rev, exc
        )
        return list((cached or {}).get("data") or [])


def recommended_info(installed: str, source: str, settings) -> dict:
    """Local-only "this release recommends a newer GOWA than the installed one".

    Reads files and the config table only: no GitHub, no LLM. That is what lets
    the panel open its modal right after boot instead of waiting for
    ``check()``, which can block for minutes on the release assessment.

    ``installed`` unknown (``''``) compares as older than everything, so a
    binary nobody could identify still gets the recommendation.
    """
    recommended = gowa_binary.read_recommended_version()
    skipped = str(
        (settings.get("gowa_recommended_skipped_version", "") if settings else "") or ""
    ).strip().lstrip("vV")
    # WHATSBOT_GOWA_BINARY pins a binary on purpose (dev/CI); installing into
    # storages/bin would not change what runs, so never recommend over it.
    available = (
        bool(recommended)
        and source != "env"
        and gowa_binary.version_cmp(recommended, installed) > 0
    )
    return {
        "recommended_version": recommended,
        "installed_version_known": gowa_binary.is_known_version(installed),
        "recommended_update_available": available,
        "recommended_supported": bool(recommended) and is_supported(recommended),
        "recommended_skipped": bool(recommended) and skipped == recommended,
        "recommended_release_url": (
            GOWA_RELEASE_PAGE.format(version=recommended) if recommended else ""
        ),
    }


def check(settings, force: bool = False) -> dict:
    """Snapshot of "what is installed vs. what is available"."""
    _, source, installed = gowa_binary.resolve_binary()
    installed_known = gowa_binary.is_known_version(installed)
    combo = detect_platform()

    result = {
        "installed_version": installed,
        "installed_version_known": installed_known,
        "source": source,
        "bundled_version": gowa_binary.read_bundled_version(),
        "supported_range": GOWA_SUPPORTED_RANGE,
        "platform_supported": combo is not None,
        "platform": "/".join(combo) if combo else f"{sys.platform}/{platform.machine()}",
        "skipped_version": settings.get("gowa_skipped_version", "") if settings else "",
        "latest_version": "",
        "update_available": False,
        "latest_supported": False,
        "release_url": "",
        "release_notes": "",
        "published_at": "",
        "rate_limited": False,
        "checked_at": 0.0,
        "error": "",
        "whatsapp_update_recommended": False,
        "update_risk_level": "none",
        "update_reason": "",
        "update_evidence": [],
        "assessment_source": "not_needed",
        "assessment_error": "",
        "assessed_versions": [],
    }

    try:
        latest = fetch_latest_release(force=force)
    except GowaUpdateError as exc:
        result["error"] = str(exc)
        if settings:
            result["latest_version"] = settings.get("gowa_latest_version", "")
            result["checked_at"] = settings.get("gowa_last_check_at", 0.0)
        return result

    version = latest["version"]
    result.update({
        "latest_version": version,
        "release_url": latest["url"] or GOWA_RELEASE_PAGE.format(version=version),
        "release_notes": latest["notes"],
        "published_at": latest["published_at"],
        "rate_limited": latest.get("rate_limited", False),
        "latest_supported": is_supported(version),
        "update_available": bool(version) and gowa_binary.version_cmp(version, installed) > 0,
        "checked_at": time.time(),
    })

    if settings and version:
        settings["gowa_latest_version"] = version
        settings["gowa_last_check_at"] = result["checked_at"]

    # The assessment needs a real (installed, latest] interval. With an
    # unidentified binary there is none: the interval would span every release
    # ever published and the compare endpoints would be fed an empty tag.
    if result["update_available"] and installed_known:
        try:
            releases = fetch_release_interval(installed, version, force=force)
        except GowaUpdateError as exc:
            logger.warning("Could not load complete GOWA release interval: %s", exc)
            releases = []
        if not any(release.get("version") == version for release in releases):
            releases.append({
                "version": version,
                "published_at": result["published_at"],
                "notes": result["release_notes"],
                "url": result["release_url"],
            })
            releases.sort(key=lambda item: gowa_binary.parse_version(item["version"]))
        commits = fetch_compare_commits(installed, version, force=force)
        commits.extend(fetch_whatsmeow_commits(installed, version, force=force))
        try:
            from gowa.release_analyzer import assess_update
            assessment = assess_update(
                settings, installed=installed, latest=version,
                releases=releases, commits=commits,
            )
            result.update(assessment)
        except Exception as exc:
            # A classifier failure must never hide the manual updater or make
            # the release check endpoint fail.
            logger.exception("Unexpected GOWA release assessment failure")
            result["assessment_source"] = "error"
            result["assessment_error"] = str(exc)[:300]

    return result


# ── Download helpers ───────────────────────────────────────────────────


def _download(url: str, dest: Path, on_progress=None, label: str = "") -> Path:
    """Stream ``url`` into ``dest``, reporting progress by percentage."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    last_report = 0.0
    last_pct = -1
    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            read = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    read += len(chunk)
                    if not on_progress or not total:
                        continue
                    pct = int(read * 100 / total)
                    now = time.monotonic()
                    # Throttle so a 12 MB download does not flood the WebSocket.
                    if pct >= last_pct + 5 or now - last_report >= 0.5:
                        last_pct, last_report = pct, now
                        on_progress("downloading", min(pct, 99), label)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise GowaUpdateError(f"Falha ao baixar {url}: {exc}") from exc
    return dest


def _fetch_checksums(version: str, os_: str) -> dict[str, str]:
    """Return ``{asset_name: sha256}`` from the release's checksums file."""
    url = GOWA_RELEASE_DOWNLOAD.format(version=version, asset=checksums_asset(os_))
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("Could not fetch GOWA checksums (%s): %s", url, exc)
        return {}

    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sums[parts[-1]] = parts[0].lower()
    return sums


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_binary(zip_path: Path, member: str, dest: Path) -> None:
    """Extract exactly one member, guarding against zip-slip."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        target = member if member in names else None
        if target is None:
            candidates = [
                info for info in zf.infolist()
                if not info.is_dir() and not info.filename.lower().endswith(".md")
            ]
            if not candidates:
                raise GowaUpdateError(f"Zip do GOWA não contém o binário {member}.")
            target = max(candidates, key=lambda i: i.file_size).filename
            logger.warning("Member %s not found in zip, falling back to %s", member, target)

        if ".." in Path(target).parts or Path(target).is_absolute():
            raise GowaUpdateError(f"Entrada suspeita no zip do GOWA: {target}")

        with zf.open(target) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)


def _clear_quarantine(path: Path) -> None:
    """Strip the macOS Gatekeeper quarantine flag.

    Without this the freshly downloaded binary is SIGKILLed on launch, the
    watchdog burns its 3-restarts-in-60s budget and gives up silently.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("xattr failed on %s: %s", path, exc)


def _smoke_test(path: Path) -> None:
    """Run the binary once to catch a wrong-architecture download.

    The return code is irrelevant; what matters is that the OS can execute it
    at all, checked before we touch the binary currently in use.
    """
    try:
        subprocess.run(
            [str(path), "--help"], capture_output=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired:
        pass  # It ran, it just did not exit fast. Good enough.
    except OSError as exc:
        raise GowaUpdateError(
            f"O binário baixado não executa nesta máquina ({exc}). "
            "Provavelmente é de outra arquitetura."
        ) from exc


def _replace_with_retry(src: Path, dest: Path, attempts: int = 10) -> None:
    """os.replace with retries: Windows keeps a lock on a just-closed exe."""
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


# ── Session DB snapshot (major upgrades only) ──────────────────────────


def _session_files() -> list[Path]:
    storages = gowa_binary.repo_root() / "storages"
    if not storages.is_dir():
        return []
    return [
        entry for entry in storages.iterdir()
        if entry.is_file() and entry.name.startswith(_SESSION_DB_PREFIXES)
    ]


def _backup_session(version: str) -> str:
    """Copy the GOWA session DBs aside. Returns the backup dir, or ''."""
    files = _session_files()
    if not files:
        return ""
    target = gowa_binary.session_backup_dir(version or "desconhecida", create=True)
    for entry in files:
        try:
            shutil.copy2(entry, target / entry.name)
        except OSError as exc:
            logger.warning("Could not back up %s: %s", entry.name, exc)
    logger.info("GOWA session snapshot saved to %s", target)
    return str(target)


def _restore_session(backup_dir: str) -> None:
    if not backup_dir:
        return
    source = Path(backup_dir)
    if not source.is_dir():
        return
    storages = gowa_binary.repo_root() / "storages"
    for entry in source.iterdir():
        try:
            shutil.copy2(entry, storages / entry.name)
        except OSError as exc:
            logger.warning("Could not restore %s: %s", entry.name, exc)
    logger.info("GOWA session restored from %s", backup_dir)


# ── Lifecycle helpers ──────────────────────────────────────────────────


def _wait_healthy(client, timeout: int = _HEALTH_TIMEOUT_SEC) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if client.health_check():
                return True
        except Exception as exc:  # client failures must not abort the rollback path
            logger.debug("health_check raised: %s", exc)
        time.sleep(1)
    return False


def _confirm_running_version(client) -> str:
    """Ask GOWA which version it is (``/app/info``, available from 9.0.0)."""
    try:
        result = client.get_app_info()
    except Exception as exc:
        logger.debug("/app/info unavailable: %s", exc)
        return ""
    if isinstance(result, dict):
        return str(result.get("version") or "").lstrip("vV")
    return ""


def _noop_progress(*_args, **_kwargs) -> None:
    return None


def _set_state(status: str, phase: str = "", progress: int = 0,
               version: str = "", message: str = "") -> None:
    _state.update({
        "status": status, "phase": phase, "progress": progress,
        "version": version, "message": message,
    })


def get_state() -> dict:
    return dict(_state)


def is_busy() -> bool:
    return _state.get("status") == "running"


# ── Install ────────────────────────────────────────────────────────────


def install(version: str, *, manager, client, on_progress=None,
            force_unsupported: bool = False) -> dict:
    """Download, verify and swap in GOWA ``version``. Rolls back on failure."""
    report = on_progress or _noop_progress

    if not _UPDATE_LOCK.acquire(blocking=False):
        raise GowaUpdateBusy("Já existe uma atualização do GOWA em andamento.")

    version = (version or "").strip().lstrip("vV")
    tmp_root = gowa_binary.tmp_dir(create=True)
    session_backup = ""
    previous_version = ""
    previous_source = ""

    try:
        if not _VERSION_RE.match(version):
            raise GowaUpdateError(f"Versão inválida: {version!r}. Use o formato x.y.z.")

        supported = is_supported(version)
        if not supported and not force_unsupported:
            return {
                "ok": False,
                "requires_confirmation": True,
                "version": version,
                "supported_range": GOWA_SUPPORTED_RANGE,
                "message": (
                    f"A versão {version} está fora da faixa homologada "
                    f"({GOWA_SUPPORTED_RANGE}). Confirme para instalar mesmo assim."
                ),
            }

        combo = detect_platform()
        if combo is None:
            raise GowaUpdateError(
                f"Atualização automática indisponível em {sys.platform}/{platform.machine()}: "
                "o GOWA não publica binário para esta combinação."
            )
        os_, arch = combo

        _, previous_source, previous_version = gowa_binary.resolve_binary()
        _set_state("running", "downloading", 0, version)

        # 1. Download the release zip next to its final destination so the
        #    later os.replace stays on the same filesystem.
        asset = asset_name(version, os_, arch)
        zip_path = tmp_root / asset
        _download(
            GOWA_RELEASE_DOWNLOAD.format(version=version, asset=asset),
            zip_path, report, version,
        )

        # 2. Verify the checksum.
        report("verifying", 100, version)
        _set_state("running", "verifying", 100, version)
        sums = _fetch_checksums(version, os_)
        expected = sums.get(asset, "")
        actual = _sha256(zip_path)
        if expected and actual != expected:
            raise GowaUpdateError(
                "O arquivo baixado não confere com o checksum publicado. "
                "Atualização cancelada."
            )
        if not expected:
            logger.warning("No published checksum for %s; proceeding unverified.", asset)

        # 3. Extract, make runnable, and prove it runs BEFORE touching the
        #    binary currently in use.
        staged = tmp_root / (gowa_binary.binary_name() + ".new")
        staged.unlink(missing_ok=True)
        _extract_binary(zip_path, inner_binary_name(os_, arch), staged)
        staged.chmod(0o755)
        _clear_quarantine(staged)
        _smoke_test(staged)

        # 4. Snapshot the WhatsApp session before a major boundary.
        if _is_major_jump(previous_version, version):
            session_backup = _backup_session(previous_version)

        # 5. Swap. GOWA must be stopped first: Windows locks the running exe.
        report("installing", 100, version)
        _set_state("running", "installing", 100, version)
        manager.stop()

        managed = gowa_binary.managed_binary_path()
        backup = gowa_binary.backup_binary_path()
        had_managed = managed.exists()
        if had_managed:
            backup.unlink(missing_ok=True)
            _replace_with_retry(managed, backup)
        _replace_with_retry(staged, managed)
        managed.chmod(0o755)
        _clear_quarantine(managed)

        gowa_binary.write_meta({
            "version": version,
            "installed_at": time.time(),
            "asset": asset,
            "sha256": actual,
            "checksum": "verified" if expected else "skipped",
            "supported": supported,
            "previous_version": previous_version,
            "previous_source": previous_source,
            "session_backup": session_backup,
        })

        # 6. Restart and prove it is alive.
        report("restarting", 100, version)
        _set_state("running", "restarting", 100, version)
        manager.start()

        report("health_check", 100, version)
        _set_state("running", "health_check", 100, version)
        if not _wait_healthy(client):
            raise GowaUpdateError(
                f"O GOWA {version} não respondeu em {_HEALTH_TIMEOUT_SEC}s após a atualização."
            )

        # /app/info exists from 9.0.0 onwards; use it to self-heal the metadata.
        running = _confirm_running_version(client)
        if running and running != version:
            logger.info("GOWA reports version %s (expected %s); recording the real one.",
                        running, version)
            meta = gowa_binary.read_meta()
            meta["version"] = running
            gowa_binary.write_meta(meta)

        logger.info("GOWA updated: %s -> %s", previous_version, version)
        return {
            "ok": True,
            "version": running or version,
            "previous_version": previous_version,
            "supported": supported,
            "checksum_verified": bool(expected),
            "rolled_back": False,
            "message": f"GOWA atualizado para a versão {running or version}.",
        }

    except Exception as exc:
        # Deliberately broad: between manager.stop() and manager.start() the
        # WhatsApp bridge is down, so ANY failure must still reach the rollback
        # path instead of bubbling up and leaving GOWA stopped.
        message = str(exc) if isinstance(exc, GowaUpdateError) else f"Erro inesperado: {exc}"
        logger.error("GOWA update to %s failed: %s", version, message)
        if not isinstance(exc, (GowaUpdateError, OSError, zipfile.BadZipFile)):
            logger.exception("Unexpected error while updating GOWA")
        rolled_back = False
        if _state.get("phase") in {"installing", "restarting", "health_check"}:
            report("rollback", 100, version)
            _set_state("running", "rollback", 100, version)
            rolled_back = _restore(manager, client, session_backup)
        return {
            "ok": False,
            "version": version,
            "previous_version": previous_version,
            "rolled_back": rolled_back,
            "message": message,
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        _set_state("idle")
        _UPDATE_LOCK.release()


def _restore(manager, client, session_backup: str = "") -> bool:
    """Put the previous binary back and restart. Best effort, never raises."""
    managed = gowa_binary.managed_binary_path()
    backup = gowa_binary.backup_binary_path()
    try:
        manager.stop()
        if backup.exists():
            managed.unlink(missing_ok=True)
            _replace_with_retry(backup, managed)
            meta = gowa_binary.read_meta()
            gowa_binary.write_meta({
                "version": meta.get("previous_version", ""),
                "installed_at": time.time(),
                "rolled_back_from": meta.get("version", ""),
                "previous_version": "",
                "previous_source": "bundled",
            })
        else:
            # No .bak means the previous binary was the bundled one, which is
            # always present. Dropping the managed copy falls back to it.
            managed.unlink(missing_ok=True)
            gowa_binary.meta_path().unlink(missing_ok=True)
        _restore_session(session_backup)
        manager.start()
        _wait_healthy(client, timeout=30)
        logger.info("GOWA rolled back to %s", gowa_binary.installed_version())
        return True
    except Exception as exc:
        logger.error("GOWA rollback failed: %s", exc)
        return False


def rollback(*, manager, client, on_progress=None) -> dict:
    """Restore the previous GOWA binary on the user's request."""
    report = on_progress or _noop_progress

    if not _UPDATE_LOCK.acquire(blocking=False):
        raise GowaUpdateBusy("Já existe uma atualização do GOWA em andamento.")
    try:
        if not gowa_binary.can_rollback():
            raise GowaUpdateError("Não há versão anterior do GOWA para restaurar.")

        target = gowa_binary.backup_version()
        meta = gowa_binary.read_meta()
        report("rollback", 100, target)
        _set_state("running", "rollback", 100, target)

        if not _restore(manager, client, meta.get("session_backup", "")):
            raise GowaUpdateError("Falha ao restaurar a versão anterior do GOWA.")

        current = gowa_binary.installed_version()
        return {
            "ok": True,
            "version": current,
            "rolled_back": True,
            "message": f"GOWA revertido para a versão {current}.",
        }
    except GowaUpdateError as exc:
        return {"ok": False, "rolled_back": False, "message": str(exc)}
    finally:
        _set_state("idle")
        _UPDATE_LOCK.release()
