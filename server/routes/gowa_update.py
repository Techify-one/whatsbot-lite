"""GOWA binary version, update and outbound-proxy endpoints.

The heavy lifting lives in ``gowa/updater.py``; this module only adapts it to
FastAPI: offloads the blocking work to a thread and pipes progress back to the
frontend over the existing WebSocket.
"""

import asyncio
import logging
import os

from gowa import binary as gowa_binary
from gowa import proxy as gowa_proxy
from gowa import updater as gowa_updater
from server.helpers import _ok, _err

logger = logging.getLogger(__name__)


def register_routes(app, deps):
    settings = deps.settings
    gowa_manager = deps.gowa_manager
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager

    def _make_progress_reporter():
        """Bridge the updater's sync callback to an async WS broadcast.

        The updater runs in a worker thread, so it cannot await. Capturing the
        running loop here lets it schedule broadcasts thread-safely.
        """
        loop = asyncio.get_running_loop()

        def _report(phase: str, progress: int, version: str, message: str = ""):
            try:
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast("gowa_update_progress", {
                        "phase": phase,
                        "progress": progress,
                        "version": version,
                        "message": message,
                    }),
                    loop,
                )
            except Exception as exc:  # never let telemetry break an update
                logger.debug("gowa progress broadcast failed: %s", exc)

        return _report

    def _version_payload() -> dict:
        path, source, installed = gowa_binary.resolve_binary()
        combo = gowa_updater.detect_platform()
        return {
            # "" when the binary could not be identified (never a guess).
            "installed_version": installed,
            "source": source,
            "bundled_version": gowa_binary.read_bundled_version(),
            "supported": gowa_updater.is_supported(installed),
            "supported_range": gowa_updater.GOWA_SUPPORTED_RANGE,
            "backup_version": gowa_binary.backup_version(),
            "can_rollback": gowa_binary.can_rollback(),
            "running": gowa_manager.is_running,
            "busy": gowa_updater.is_busy(),
            "binary_path": str(path),
            "platform_supported": combo is not None,
            **gowa_updater.recommended_info(installed, source, settings),
        }

    @app.get("/api/gowa/version")
    async def gowa_version():
        return _ok(await asyncio.to_thread(_version_payload))

    @app.get("/api/gowa/update/check")
    async def gowa_update_check(force: bool = False):
        state = await asyncio.to_thread(gowa_updater.check, settings, force)
        state.update(await asyncio.to_thread(_version_payload))
        return _ok(state)

    @app.post("/api/gowa/update")
    async def gowa_update(body: dict | None = None):
        body = body or {}
        version = str(body.get("version") or "").strip()
        force_unsupported = bool(body.get("force_unsupported"))

        if not version:
            latest = await asyncio.to_thread(gowa_updater.check, settings, False)
            version = latest.get("latest_version") or ""
        if not version:
            return _err("Não consegui determinar a versão a instalar.")

        report = _make_progress_reporter()
        try:
            result = await asyncio.to_thread(
                gowa_updater.install, version,
                manager=gowa_manager, client=gowa_client,
                on_progress=report, force_unsupported=force_unsupported,
            )
        except gowa_updater.GowaUpdateBusy as exc:
            return _err(str(exc), status=409)

        result.update(await asyncio.to_thread(_version_payload))
        await ws_manager.broadcast("gowa_update_done", {
            "ok": result.get("ok", False),
            "version": result.get("version", ""),
            "previous_version": result.get("previous_version", ""),
            "rolled_back": result.get("rolled_back", False),
            "message": result.get("message", ""),
        })

        if not result.get("ok"):
            # A pending confirmation is not an error, it is a 200 the UI acts on.
            if result.get("requires_confirmation"):
                return _ok(result)
            return _err(result.get("message", "Falha ao atualizar o GOWA."))
        return _ok(result)

    @app.post("/api/gowa/rollback")
    async def gowa_rollback():
        report = _make_progress_reporter()
        try:
            result = await asyncio.to_thread(
                gowa_updater.rollback,
                manager=gowa_manager, client=gowa_client, on_progress=report,
            )
        except gowa_updater.GowaUpdateBusy as exc:
            return _err(str(exc), status=409)

        result.update(await asyncio.to_thread(_version_payload))
        await ws_manager.broadcast("gowa_update_done", {
            "ok": result.get("ok", False),
            "version": result.get("version", ""),
            "previous_version": "",
            "rolled_back": True,
            "message": result.get("message", ""),
        })
        if not result.get("ok"):
            return _err(result.get("message", "Falha ao reverter o GOWA."))
        return _ok(result)

    # ── Proxy de saída ────────────────────────────────────────────────

    def _proxy_payload(cfg: dict, error: str = "") -> dict:
        """Public view of the proxy config: password masked, never echoed."""
        installed = gowa_binary.installed_version()
        effective = ""
        try:
            effective = gowa_proxy.build_url(cfg)
        except gowa_proxy.ProxyConfigError:
            effective = ""
        return {
            "enabled": cfg.get("enabled", False),
            "mode": cfg.get("mode", "fields"),
            "scheme": cfg.get("scheme", "socks5"),
            "host": cfg.get("host", ""),
            "port": cfg.get("port", 0) or "",
            "username": cfg.get("username", ""),
            "has_password": bool(cfg.get("password")),
            "password": gowa_proxy.PASSWORD_MASK if cfg.get("password") else "",
            "url": gowa_proxy.mask_url(cfg.get("url", "")),
            "effective_url": gowa_proxy.mask_url(effective),
            "schemes": list(gowa_proxy.SCHEMES),
            "supported": gowa_proxy.supports_proxy(installed),
            "min_version": gowa_proxy.MIN_GOWA_VERSION,
            "installed_version": installed,
            "installed_version_known": gowa_binary.is_known_version(installed),
            "env_locked": bool(os.environ.get(gowa_proxy.ENV_OVERRIDE, "").strip()),
            "running": gowa_manager.is_running,
            "error": error,
        }

    @app.get("/api/gowa/proxy")
    async def gowa_proxy_get():
        cfg = await asyncio.to_thread(gowa_proxy.current_config)
        return _ok(_proxy_payload(cfg))

    @app.put("/api/gowa/proxy")
    async def gowa_proxy_save(body: dict | None = None):
        body = body or {}
        stored = await asyncio.to_thread(gowa_proxy.current_config)
        try:
            cfg = gowa_proxy.normalize(body, stored)
        except gowa_proxy.ProxyConfigError as exc:
            return _err(str(exc))

        await asyncio.to_thread(gowa_proxy.save_config, cfg)

        before = gowa_proxy.build_url(stored) if stored.get("enabled") else ""
        after = gowa_proxy.build_url(cfg)
        changed = before != after
        restarted = False
        # Restart only when the effective proxy actually moved: the WhatsApp
        # socket is only re-dialed on process start, so nothing else applies it.
        if changed and bool(body.get("restart", True)) and gowa_manager.is_running:
            try:
                await asyncio.to_thread(gowa_manager.restart)
                restarted = True
            except Exception as exc:
                logger.error("Falha ao reiniciar o GOWA após salvar o proxy: %s", exc)
                payload = _proxy_payload(cfg, f"Proxy salvo, mas o GOWA não reiniciou: {exc}")
                payload["changed"] = changed
                payload["restarted"] = False
                return _ok(payload)

        logger.info(
            "Proxy do GOWA %s (%s)%s",
            "ativado" if cfg["enabled"] else "desativado",
            gowa_proxy.mask_url(after) or "sem proxy",
            " - GOWA reiniciado" if restarted else "",
        )
        payload = _proxy_payload(cfg)
        payload["changed"] = changed
        payload["restarted"] = restarted
        return _ok(payload)

    @app.post("/api/gowa/proxy/test")
    async def gowa_proxy_test(body: dict | None = None):
        body = body or {}
        stored = await asyncio.to_thread(gowa_proxy.current_config)
        # Test what is on screen (falling back to what is saved), so the user
        # can validate credentials before committing them.
        probe = dict(body)
        probe.setdefault("enabled", True)
        try:
            cfg = gowa_proxy.normalize(probe, stored)
            url = gowa_proxy.build_url({**cfg, "enabled": True})
        except gowa_proxy.ProxyConfigError as exc:
            return _err(str(exc))
        if not url:
            return _err("Configure o proxy antes de testar.")

        result = await asyncio.to_thread(gowa_proxy.test_connection, url)
        result["proxy"] = gowa_proxy.mask_url(url)
        result["supported"] = gowa_proxy.supports_proxy()
        return _ok(result)

    @app.post("/api/gowa/skip-version")
    async def gowa_skip_version(body: dict):
        version = str(body.get("version") or "").strip().lstrip("vV")
        if not version:
            return _err("Informe a versão a pular.")
        settings["gowa_skipped_version"] = version
        logger.info("GOWA version %s skipped by the user.", version)
        return _ok({"skipped_version": version})

    @app.post("/api/gowa/skip-recommended")
    async def gowa_skip_recommended(body: dict | None = None):
        """"Agora não" on the modal for the version this release recommends.

        Stored in the database (not localStorage) so it holds across browsers
        and restarts; a release that recommends a different number asks again.
        Kept apart from ``gowa_skipped_version``, which is about GitHub's latest.
        """
        body = body or {}
        version = str(body.get("version") or "").strip().lstrip("vV")
        if not version:
            version = gowa_binary.read_recommended_version()
        if not gowa_updater._VERSION_RE.match(version):
            return _err("Informe a versão recomendada a adiar.")
        settings["gowa_recommended_skipped_version"] = version
        logger.info("Recommended GOWA version %s postponed by the user.", version)
        return _ok({"skipped_version": version})
