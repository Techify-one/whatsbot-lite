"""Plugin management endpoints (list, manifest, enable/disable, settings, import/export)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import StreamingResponse

from db.repositories import config_repo, plugin_repo, tool_override_repo
from plugins.manifest import (
    WHATSBOT_API_VERSION,
    find_manifest_file,
    load_manifest,
)
from plugins.migrator import run_pending_migrations
from plugins.restart import schedule_restart
from plugins.events import emit as emit_event
from server.helpers import _err, _ok
import time as _time

logger = logging.getLogger(__name__)


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Version sanitized for use in a filename / HTTP header. The manifest's own
# ``_is_semver`` is NOT enough: its regex ends in ``(?:[-+].*)?``, so a build
# tag carrying a quote or a newline would pass and poison Content-Disposition.
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")


def _export_filename(plugin_id: str, plugin_dir: Path) -> str:
    """Export zip name: ``<id>-<version>-plugin.zip``.

    Degrades to the legacy name (``<id>-plugin.zip``) whenever the version
    cannot be read safely -- the download must never fail over its own name.

    The broad ``except`` is deliberate: ``load_manifest`` also raises
    ``ValueError`` when ``whatsbot_api_version`` is incompatible with the
    running core, which is exactly the plugin an operator wants to export in
    order to fix it elsewhere.
    """
    try:
        version = (load_manifest(plugin_dir).version or "").strip()
    except Exception:
        return f"{plugin_id}-plugin.zip"
    if not _SAFE_VERSION_RE.match(version):
        return f"{plugin_id}-plugin.zip"
    return f"{plugin_id}-{version}-plugin.zip"


# Cap on the total UNCOMPRESSED size of an uploaded plugin zip (anti zip-bomb).
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024  # 256 MiB


def _read_zip_manifest(contents: bytes) -> tuple[zipfile.ZipFile, dict, str]:
    """Open an uploaded plugin zip and return (zipfile, manifest dict, plugin id).

    Raises ValueError(<mensagem amigável>) on any malformed input so callers can
    surface it via ``_err``.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile:
        raise ValueError("arquivo .zip inválido")

    names = zf.namelist()
    manifest_name = next(
        (c for c in ("plugin.yaml", "plugin.yml", "plugin.json") if c in names), None
    )
    if manifest_name is None:
        raise ValueError("manifest (plugin.yaml/plugin.json) ausente na raiz do zip")
    try:
        manifest_text = zf.read(manifest_name).decode("utf-8")
    except Exception as e:
        raise ValueError(f"falha ao ler manifest: {e}")

    if manifest_name.endswith(".json"):
        try:
            meta = json.loads(manifest_text)
        except Exception as e:
            raise ValueError(f"manifest JSON inválido: {e}")
    else:
        from plugins.manifest import _parse_yaml  # type: ignore
        try:
            meta = _parse_yaml(manifest_text) or {}
        except Exception as e:
            raise ValueError(f"manifest YAML inválido: {e}")

    pid = meta.get("id") if isinstance(meta, dict) else None
    if not isinstance(pid, str) or not _PLUGIN_ID_RE.match(pid):
        raise ValueError("manifest sem id válido")
    return zf, meta, pid


def _reject_unsafe_zip_paths(zf: zipfile.ZipFile) -> None:
    """Block path traversal (absolute paths / ``..``) before extracting."""
    for member in zf.infolist():
        name = member.filename
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            raise ValueError(f"caminho perigoso no zip: {name}")


def _version_key(version: str) -> tuple[int, ...]:
    """Best-effort numeric key for comparing semver-ish versions.

    Only the leading dotted integer components are compared (``1.2.3`` →
    ``(1, 2, 3)``); pre-release/build suffixes are ignored. Good enough to flag a
    downgrade — not a strict semver gate.
    """
    raw = str(version or "0").strip()
    if raw[:1] in ("v", "V"):          # tolera prefixo "v" (ex.: "v1.2.3")
        raw = raw[1:]
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", raw):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)


def _swap_plugin_dir(
    zf: zipfile.ZipFile, plugins_dir: Path, pid: str, *, keep_backup: bool = False,
) -> Path | None:
    """Replace ``plugins_dir/pid`` with the zip's contents, atomically-ish.

    Extracts into a dot-prefixed staging dir (ignored by the loader), then renames
    the current folder aside and the staging folder into place. On failure the
    original folder is restored. NEVER touches the plugin's DB tables, settings or
    migration history — only the on-disk code is swapped.
    """
    # Guard against a zip-bomb: cap the total uncompressed size before extracting.
    total_uncompressed = sum(getattr(i, "file_size", 0) for i in zf.infolist())
    if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"zip grande demais: {total_uncompressed} bytes descomprimidos "
            f"(limite {_MAX_UNCOMPRESSED_BYTES})"
        )

    target = plugins_dir / pid
    staging = plugins_dir / f".{pid}.update-staging"
    backup = plugins_dir / f".{pid}.update-backup"
    for tmp in (staging, backup):
        shutil.rmtree(tmp, ignore_errors=True)

    staging.mkdir(parents=True)
    try:
        zf.extractall(staging)
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"falha ao extrair zip: {e}")

    # Backward compatibility for plugins that stored user-owned files inside
    # their code folder before ``plugins.context.plugin_data_dir`` existed.
    # New plugins should use that external durable directory instead.
    for name in ("data", "uploads", "media", "storage", ".data"):
        source = target / name
        destination = staging / name
        if source.exists() and not destination.exists():
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)

    os.replace(target, backup)          # current code aside (same fs → atomic)
    try:
        os.replace(staging, target)     # new code into place
    except Exception:
        os.replace(backup, target)      # restore original on failure
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if keep_backup:
        return backup
    shutil.rmtree(backup, ignore_errors=True)
    return None


def register_routes(app, deps):
    plugins_dir: Path = deps.plugins_dir
    registry = deps.plugins_registry

    @app.get("/api/plugins")
    async def list_plugins():
        """List every plugin known on disk, with DB metadata merged in."""
        rows = await asyncio.to_thread(plugin_repo.list_all)
        by_id = {r["id"]: r for r in rows}
        items = []
        # walk filesystem so we can also report folders the loader rejected
        for child in sorted(plugins_dir.iterdir()) if plugins_dir.is_dir() else []:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if find_manifest_file(child) is None:
                continue
            manifest_view: dict | None = None
            try:
                manifest_view = load_manifest(child).to_public_dict()
            except Exception as e:
                manifest_view = {"id": child.name, "name": child.name, "error": str(e)}
            db_row = by_id.get(child.name, {})
            loaded = registry.loaded.get(child.name) if registry else None
            items.append({
                **manifest_view,
                "enabled": bool(db_row.get("enabled")),
                "load_error": db_row.get("load_error"),
                "loaded": loaded is not None,
                "installed_at": db_row.get("installed_at"),
                "updated_at": db_row.get("updated_at"),
            })
        return _ok({
            "plugins": items,
            "whatsbot_api_version": WHATSBOT_API_VERSION,
        })

    @app.get("/api/plugins/manifest")
    async def public_manifest():
        """Return only loaded plugins, in the shape the frontend uses to mount screens."""
        out = []
        for loaded in (registry.loaded.values() if registry else []):
            out.append({
                "id": loaded.id,
                "name": loaded.manifest.name,
                "version": loaded.manifest.version,
                "frontend_extends": loaded.manifest.frontend_extends,
                "screens": [
                    {**s, "pluginId": loaded.id} for s in loaded.manifest.screens
                ],
            })
        return _ok({"plugins": out})

    @app.post("/api/plugins/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str):
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("plugin id inválido")
        ok = await asyncio.to_thread(plugin_repo.set_enabled, plugin_id, True)
        if not ok:
            return _err("plugin não encontrado", 404)
        # Emit BEFORE schedule_restart — after the os._exit the bus is gone.
        emit_event("plugin.enabled", {"plugin_id": plugin_id, "ts": _time.time()})
        schedule_restart(reason=f"plugin {plugin_id} enabled")
        return _ok({"id": plugin_id, "enabled": True, "restarting": True})

    @app.post("/api/plugins/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str):
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("plugin id inválido")
        ok = await asyncio.to_thread(plugin_repo.set_enabled, plugin_id, False)
        if not ok:
            return _err("plugin não encontrado", 404)
        emit_event("plugin.disabled", {"plugin_id": plugin_id, "ts": _time.time()})
        schedule_restart(reason=f"plugin {plugin_id} disabled")
        return _ok({"id": plugin_id, "enabled": False, "restarting": True})

    @app.delete("/api/plugins/{plugin_id}")
    async def delete_plugin(plugin_id: str):
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("plugin id inválido")

        def _do_delete() -> dict:
            target = plugins_dir / plugin_id
            # DB cleanup first (the parts most likely to fail); only remove the
            # folder once it succeeds, so an error leaves a retry-able state
            # instead of a vanished plugin with orphaned tables.
            dropped = plugin_repo.drop_plugin_tables(plugin_id)
            plugin_repo.delete(plugin_id)
            config_repo.delete_prefix(f"plugin.{plugin_id}.")
            overrides_removed = tool_override_repo.delete_for_plugin(plugin_id)
            had_dir = target.is_dir()
            if had_dir:
                shutil.rmtree(target)
            return {
                "folder_removed": had_dir,
                "tables_dropped": dropped,
                "tool_overrides_removed": overrides_removed,
            }

        result = await asyncio.to_thread(_do_delete)
        schedule_restart(reason=f"plugin {plugin_id} deleted")
        return _ok({"id": plugin_id, **result, "restarting": True})

    # ── Settings ─────────────────────────────────────────────────────

    @app.get("/api/plugins/{plugin_id}/settings")
    async def get_plugin_settings(plugin_id: str):
        loaded = registry.loaded.get(plugin_id) if registry else None
        if not loaded or not loaded.settings_cls:
            return _err("plugin sem settings declaradas", 404)
        schema = loaded.settings_cls.model_json_schema()
        # current values: read namespaced keys; fall back to defaults
        prefix = f"plugin.{plugin_id}."
        all_cfg = await asyncio.to_thread(config_repo.get_all)
        values: dict = {}
        defaults = loaded.settings_cls().model_dump()
        for field, default_val in defaults.items():
            values[field] = all_cfg.get(prefix + field, default_val)
        return _ok({"schema": schema, "values": values})

    @app.put("/api/plugins/{plugin_id}/settings")
    async def update_plugin_settings(plugin_id: str, request: Request):
        loaded = registry.loaded.get(plugin_id) if registry else None
        if not loaded or not loaded.settings_cls:
            return _err("plugin sem settings declaradas", 404)
        body = await request.json()
        try:
            validated = loaded.settings_cls(**(body or {}))
        except Exception as e:
            return _err(f"valores inválidos: {e}")
        prefix = f"plugin.{plugin_id}."
        kv = {prefix + k: v for k, v in validated.model_dump().items()}
        await asyncio.to_thread(config_repo.set_many, kv)
        emit_event("plugin.settings.changed", {
            "plugin_id": plugin_id,
            "values": validated.model_dump(),
            "ts": _time.time(),
        })
        return _ok({"id": plugin_id, "values": validated.model_dump()})

    # ── Import / Export ───────────────────────────────────────────────

    @app.get("/api/plugins/{plugin_id}/export")
    async def export_plugin(plugin_id: str):
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("plugin id inválido")
        target = plugins_dir / plugin_id
        if not target.is_dir():
            return _err("plugin não encontrado", 404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in target.rglob("*"):
                if path.is_dir():
                    continue
                if "__pycache__" in path.parts:
                    continue
                if path.suffix in (".db", ".db-wal", ".db-shm"):
                    continue
                arc = path.relative_to(target).as_posix()
                zf.write(path, arc)
        size = buf.getbuffer().nbytes
        buf.seek(0)
        filename = _export_filename(plugin_id, target)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\"",
                "Content-Length": str(size),
            },
        )

    @app.post("/api/plugins/import")
    async def import_plugin(file: UploadFile):
        contents = await file.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(contents))
        except zipfile.BadZipFile:
            return _err("arquivo .zip inválido")

        # find manifest at root
        names = zf.namelist()
        manifest_name = None
        for candidate in ("plugin.yaml", "plugin.yml", "plugin.json"):
            if candidate in names:
                manifest_name = candidate
                break
        if manifest_name is None:
            return _err("manifest (plugin.yaml/plugin.json) ausente na raiz do zip")
        try:
            manifest_text = zf.read(manifest_name).decode("utf-8")
        except Exception as e:
            return _err(f"falha ao ler manifest: {e}")

        # extract id without full validation (it'll be validated when loading)
        if manifest_name.endswith(".json"):
            try:
                meta = json.loads(manifest_text)
            except Exception as e:
                return _err(f"manifest JSON inválido: {e}")
        else:
            from plugins.manifest import _parse_yaml  # type: ignore
            try:
                meta = _parse_yaml(manifest_text) or {}
            except Exception as e:
                return _err(f"manifest YAML inválido: {e}")
        pid = meta.get("id") if isinstance(meta, dict) else None
        if not isinstance(pid, str) or not _PLUGIN_ID_RE.match(pid):
            return _err("manifest sem id válido")

        target = plugins_dir / pid
        if target.exists():
            return _err(f"plugin '{pid}' já instalado — desinstale antes")

        # path traversal protection
        for member in zf.infolist():
            name = member.filename
            if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                return _err(f"caminho perigoso no zip: {name}")

        target.mkdir(parents=True)
        try:
            zf.extractall(target)
        except Exception as e:
            shutil.rmtree(target, ignore_errors=True)
            return _err(f"falha ao extrair zip: {e}")

        version = str(meta.get("version") or "0.0.0") if isinstance(meta, dict) else "0.0.0"
        await asyncio.to_thread(plugin_repo.upsert, pid, version, enabled=False)
        return _ok({"id": pid, "version": version, "enabled": False})

    @app.post("/api/plugins/{plugin_id}/update")
    async def update_plugin(plugin_id: str, file: UploadFile):
        """Update an already-installed plugin from a new .zip WITHOUT data loss.

        Unlike import (which refuses an existing plugin) + delete (which drops the
        plugin's tables), this swaps only the on-disk code. The DB tables, settings
        (``plugin.<id>.*``) and migration history (``plugin_migrations``) are left
        untouched, so on the next boot the loader applies ONLY the new pending
        migrations (the migrator skips already-applied versions) — existing rows
        are preserved. Plugin authors must ship NEW additive migration files
        (``NNN_*.sql`` with ``ALTER TABLE`` / ``CREATE TABLE IF NOT EXISTS``).

        Two contracts the author must respect (review findings #4 / P1):
        * Migrations are keyed by their NUMBER only — re-using an applied number
          with different SQL is silently skipped. Always add a higher-numbered
          file; never edit an already-shipped migration.
        * The plugin FOLDER is fully replaced. Runtime state must live in the
          plugin's DB tables (``plugin_<id>_*``), never in files inside the
          folder — those are overwritten by the new bundle.
        """
        if not _PLUGIN_ID_RE.match(plugin_id):
            return _err("plugin id inválido")
        target = plugins_dir / plugin_id
        if not target.is_dir():
            return _err("plugin não instalado — use Importar (.zip)", 404)

        contents = await file.read()
        try:
            zf, meta, pid = _read_zip_manifest(contents)
            if pid != plugin_id:
                return _err(f"o .zip é do plugin '{pid}', não '{plugin_id}'")
            _reject_unsafe_zip_paths(zf)
        except ValueError as e:
            return _err(str(e))

        existing = await asyncio.to_thread(plugin_repo.get, plugin_id)
        old_version = str((existing or {}).get("version") or "0.0.0")
        new_version = str(meta.get("version") or "0.0.0")
        downgrade = _version_key(new_version) < _version_key(old_version)

        def _do_update() -> None:
            backup = _swap_plugin_dir(zf, plugins_dir, plugin_id, keep_backup=True)
            try:
                # Preserve ``enabled`` (enabled=None) — an update keeps the plugin's
                # current on/off state; only the version + updated_at change.
                plugin_repo.upsert(plugin_id, new_version)
                # Disabled plugins that already own data are migrated here too.
                if plugin_repo.applied_migrations(plugin_id):
                    run_pending_migrations(load_manifest(target), target)
            except Exception:
                # Schema migrations are transactional; restore the previous code
                # and DB metadata before surfacing the failure.
                shutil.rmtree(target, ignore_errors=True)
                if backup and backup.exists():
                    os.replace(backup, target)
                plugin_repo.upsert(
                    plugin_id, old_version,
                    enabled=bool((existing or {}).get("enabled")),
                )
                raise
            if backup:
                shutil.rmtree(backup, ignore_errors=True)

        try:
            await asyncio.to_thread(_do_update)
        except ValueError as e:
            return _err(str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("update of plugin %s failed", plugin_id)
            return _err(f"falha ao atualizar: {e}")

        emit_event("plugin.updated", {
            "plugin_id": plugin_id, "ts": _time.time(),
            "from_version": old_version, "to_version": new_version,
        })
        warning = (
            f"downgrade: versão enviada (v{new_version}) é menor que a instalada "
            f"(v{old_version})"
        ) if downgrade else None
        schedule_restart(
            reason=f"plugin {plugin_id} updated v{old_version} -> v{new_version}",
        )
        return _ok({
            "id": plugin_id,
            "from_version": old_version,
            "to_version": new_version,
            "downgrade": downgrade,
            "warning": warning,
            "restarting": True,
        })

    @app.post("/api/plugins/restart")
    async def restart_server():
        schedule_restart(reason="manual restart from /api/plugins/restart")
        return _ok({"restarting": True})
