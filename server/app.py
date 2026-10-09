"""WhatsBot-Lite — FastAPI backend with REST API, WebSocket and background tasks."""

import asyncio
import dataclasses
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.auth import auth_required, verify_token
from server.helpers import _get_web_dir
from server.pwa import register_pwa_routes
from server.state import MemoryLogHandler, ConnectionManager, AppState
from server.background import start_gowa_task, status_poll_loop, qr_poll_loop, avatar_fetch_task, gowa_update_check_loop
from server.routes import logs, sandbox, config, whatsapp, websocket, usage, contacts, webhook, auth, tags, executions, update, setup as setup_routes, plugins as plugins_routes, tools as tools_routes, admin as admin_routes, ai_engine as ai_engine_routes, gowa_update as gowa_update_routes, chat as chat_routes
from db.repositories import tool_override_repo
from agent import group_mentions, agent_factory
from agent import ai_tool_installer
from plugins.loader import bootstrap_initial_plugins, discover_and_load, PluginRegistry
from plugins.context import set_runtime as _set_plugin_runtime
from plugins.events import (
    set_runtime as _set_events_runtime,
    register_plugin_events,
    register_plugin_filters,
    emit as emit_event,
)
from server.balance_monitor import set_runtime as _set_balance_runtime

logger = logging.getLogger(__name__)

# ── In-memory log capture (attach to root logger) ────────────────────────

_memory_log_handler = MemoryLogHandler()
_memory_log_handler.setLevel(logging.DEBUG)
_root = logging.getLogger()
_root.addHandler(_memory_log_handler)
# Ensure root logger level allows INFO+ through to handlers
if _root.level == logging.NOTSET or _root.level > logging.INFO:
    _root.setLevel(logging.INFO)


# ── Server Dependencies ──────────────────────────────────────────────────

@dataclasses.dataclass
class ServerDeps:
    """Container for shared dependencies passed to route modules."""
    settings: object
    gowa_manager: object
    gowa_client: object
    agent_handler: object
    ws_manager: ConnectionManager
    state: AppState
    memory_log_handler: MemoryLogHandler
    statics_senditems_dir: Path
    plugins_dir: Path = None
    plugins_registry: PluginRegistry = None
    # Dynamically set by webhook route for cross-module access
    broadcast_tool_calls: object = None


# ── Factory ───────────────────────────────────────────────────────────────

def create_app(
    settings,
    gowa_manager,
    gowa_client,
    agent_handler,
) -> FastAPI:
    """Create and configure the FastAPI application."""

    ws_manager = ConnectionManager()
    state = AppState()
    web_dir = _get_web_dir()

    # Prepare statics directories
    statics_dir = settings.data_dir / "statics"
    statics_media_dir = statics_dir / "media"
    # Operator-sent media (image/audio/document uploaded from the panel) is
    # persisted here. This MUST NOT be GOWA's own working folder: GOWA runs with
    # cwd=project root and uses ``statics/senditems`` for outgoing sends, saving
    # the uploaded file under the multipart filename and RemoveFile-ing it after
    # send — which would delete our persisted copy (images/audio broke on
    # refresh). Keeping operator media in a separate ``statics/operator`` folder
    # that GOWA never touches avoids the collision.
    statics_senditems_dir = statics_dir / "operator"
    statics_media_dir.mkdir(parents=True, exist_ok=True)
    statics_senditems_dir.mkdir(parents=True, exist_ok=True)

    # Plugin discovery + load. Runs synchronously before route registration so
    # plugin routes/tools/prompts are wired into the app before the first request.
    plugins_dir = settings.data_dir / "storages" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_initial_plugins(
        plugins_dir,
        settings.data_dir / "assets" / "plugin_examples",
    )
    registry = discover_and_load(plugins_dir)
    for loaded in registry.loaded.values():
        if loaded.tools:
            agent_handler.register_plugin_tools(loaded.id, loaded.tools)
        if loaded.prompt_fragments:
            agent_handler.register_plugin_prompts(loaded.id, loaded.prompt_fragments)
        if loaded.event_handlers:
            register_plugin_events(loaded.id, loaded.event_handlers)
        if loaded.filters:
            register_plugin_filters(loaded.id, loaded.filters)

    # AI engine (config-in-DB + code-in-DB). Seed the default agent/prompt from
    # the current config (idempotent), then materialise/install/register the
    # DB-defined tools. Tools are registered AFTER core + plugin tools so the
    # registry's collision no-op gives precedence to code over the DB. Both
    # steps are best-effort: a failure never blocks the app from booting.
    try:
        agent_factory.seed_default_agent(settings)
    except Exception as e:
        logger.warning("AI engine seed failed: %s", e)
    try:
        ai_tool_installer.install_and_register(agent_handler, settings.data_dir)
    except Exception as e:
        logger.warning("AI tool installer failed: %s", e)

    # Tool override cleanup: drop rows for tools that no longer exist (renamed
    # in core, or belonging to a plugin that was removed). Then build the
    # effective tool list applied to the LLM.
    try:
        dropped = tool_override_repo.delete_orphans(agent_handler.known_tool_names())
        if dropped:
            logger.info("Removed %d orphan tool_overrides rows", dropped)
    except Exception as e:
        logger.warning("tool_overrides orphan cleanup failed: %s", e)
    agent_handler.refresh_tool_overrides()

    deps = ServerDeps(
        settings=settings,
        gowa_manager=gowa_manager,
        gowa_client=gowa_client,
        agent_handler=agent_handler,
        ws_manager=ws_manager,
        state=state,
        memory_log_handler=_memory_log_handler,
        statics_senditems_dir=statics_senditems_dir,
        plugins_dir=plugins_dir,
        plugins_registry=registry,
    )

    # Group @mention resolution service (members lookup + name/number mapping).
    group_mentions.init(gowa_client)

    # ── GOWA restart callback ──────────────────────────────────────────
    def _on_gowa_restart():
        gowa_client.reset()
        state.qr_data = None
        state.qr_fetched_at = 0
        state.bot_phone = ""
        state.bot_name = ""

    gowa_manager._on_restart = _on_gowa_restart

    # ── Lifespan ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import time as _time
        state.stop_event.clear()
        _loop = asyncio.get_running_loop()
        _set_plugin_runtime(ws_manager, _loop, gowa_client, agent_handler)
        _set_events_runtime(_loop, agent_handler)
        _set_balance_runtime(ws_manager, _loop, settings)
        # Lifecycle: plugins finished loading + bus is live, now broadcast
        for loaded in registry.loaded.values():
            emit_event("plugin.loaded", {
                "plugin_id": loaded.id,
                "version": loaded.manifest.version,
                "events": list(loaded.event_handlers.keys()),
                "filters": list(loaded.filters.keys()),
                "ts": _time.time(),
            })
        emit_event("app.startup", {
            "plugin_ids": list(registry.loaded.keys()),
            "ts": _time.time(),
        })
        tasks = [
            asyncio.create_task(start_gowa_task(deps)),
            asyncio.create_task(status_poll_loop(deps)),
            asyncio.create_task(qr_poll_loop(deps)),
            asyncio.create_task(avatar_fetch_task(deps)),
            asyncio.create_task(gowa_update_check_loop(deps)),
        ]
        yield
        # Shutdown
        emit_event("app.shutdown", {"ts": _time.time()})
        state.stop_event.set()
        for task in tasks:
            task.cancel()
        try:
            settings.save()
        except Exception:
            pass
        try:
            gowa_manager.stop()
        except Exception:
            pass
        logger.info("Server shutdown complete.")

    # ── FastAPI App ───────────────────────────────────────────────────

    _docs_enabled = os.getenv("WHATSBOT_ENABLE_DOCS", "0") == "1"
    app = FastAPI(
        title="WhatsBot-Lite",
        lifespan=lifespan,
        docs_url="/docs" if _docs_enabled else None,
        redoc_url="/redoc" if _docs_enabled else None,
        openapi_url="/openapi.json" if _docs_enabled else None,
    )

    # Mount static files (frontend assets)
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Mount statics/ for GOWA media files (auto-downloaded images, audio, etc.)
    app.mount("/statics", StaticFiles(directory=str(statics_dir)), name="statics")

    # ── Auth middleware ────────────────────────────────────────────────

    # Paths exempt from authentication
    _AUTH_EXEMPT_PREFIXES = ("/static/", "/statics/", "/plugins/", "/api/auth/")
    _AUTH_EXEMPT_EXACT = {"/api/webhook", "/health"}
    _PLUGIN_SPA_PATHS = {
        s["path"]
        for loaded in registry.loaded.values()
        for s in loaded.manifest.screens
        if s.get("path", "").startswith("/")
    }
    _SPA_PATHS = (
        {"/", "/painel", "/sandbox", "/costs", "/executions", "/plugins", "/tools", "/chat", "/wizard"}
        | _PLUGIN_SPA_PATHS
    )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # SPA pages, static assets, webhook, and auth endpoints are always open
        if path in _SPA_PATHS or path.startswith(("/contacts/", "/executions/", "/chat/")):
            return await call_next(request)
        if path in _AUTH_EXEMPT_EXACT:
            return await call_next(request)
        for prefix in _AUTH_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Only protect /api/* paths
        if path.startswith("/api/") and auth_required(settings):
            auth_header = request.headers.get("authorization", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            if not token or not verify_token(token, settings):
                return JSONResponse(
                    {"ok": False, "error": "Não autenticado."},
                    status_code=401,
                )

        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
            "worker-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
        return resp

    # ── Health endpoint (always open, used by Docker healthcheck) ──────

    @app.get("/health")
    async def healthcheck():
        return JSONResponse({"ok": True})

    register_pwa_routes(app, web_dir)

    # ── Frontend routes ────────────────────────────────────────────────

    @app.get("/")
    @app.get("/painel")
    @app.get("/sandbox")
    @app.get("/costs")
    @app.get("/executions")
    @app.get("/plugins")
    @app.get("/tools")
    @app.get("/chat")
    @app.get("/chat/projects/{project_id}")
    @app.get("/chat/projects/{project_id}/conversations/{conversation_id}")
    @app.get("/chat/projects/{project_id}/conversations/{conversation_id}/messages/{message_id:int}")
    @app.get("/wizard")
    @app.get("/contacts/{contact_id:int}")
    @app.get("/executions/{execution_id:int}")
    async def index(
        contact_id: int | None = None, execution_id: int | None = None,
        project_id: str | None = None, conversation_id: str | None = None,
        message_id: int | None = None,
    ):
        index_file = web_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"error": "Frontend not found"}, status_code=404)

    # Register dynamic SPA paths declared by plugin manifests so the frontend
    # router gets the same index.html on hard reload of those URLs.
    async def _plugin_spa_index():
        index_file = web_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"error": "Frontend not found"}, status_code=404)

    for _spa_path in _PLUGIN_SPA_PATHS:
        app.add_api_route(_spa_path, _plugin_spa_index, methods=["GET"])

    # ── Register route modules ─────────────────────────────────────────
    # Order matters: webhook must be registered before sandbox so
    # broadcast_tool_calls is available via deps.
    auth.register_routes(app, deps)
    webhook.register_routes(app, deps)
    logs.register_routes(app, deps)
    sandbox.register_routes(app, deps)
    config.register_routes(app, deps)
    whatsapp.register_routes(app, deps)
    setup_routes.register_routes(app, deps)
    websocket.register_routes(app, deps)
    usage.register_routes(app, deps)
    contacts.register_routes(app, deps)
    tags.register_routes(app, deps)
    executions.register_routes(app, deps)
    update.register_routes(app, deps)
    plugins_routes.register_routes(app, deps)
    tools_routes.register_routes(app, deps)
    admin_routes.register_routes(app, deps)
    ai_engine_routes.register_routes(app, deps)
    gowa_update_routes.register_routes(app, deps)
    chat_routes.register_routes(app, deps)

    # ── Plugin routers and static assets ──────────────────────────────
    for loaded in registry.loaded.values():
        if loaded.router is not None:
            app.include_router(loaded.router, prefix=f"/api/plugins/{loaded.id}")
        if loaded.static_dir is not None:
            app.mount(
                f"/plugins/{loaded.id}/static",
                StaticFiles(directory=str(loaded.static_dir)),
                name=f"plugin_{loaded.id}_static",
            )

    return app
