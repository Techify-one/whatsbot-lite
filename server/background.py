"""Background tasks for the WhatsBot-Lite server (GOWA startup, status/QR polling)."""

import asyncio
import logging
import time
from pathlib import Path

from db.repositories import contact_repo
from agent import group_mentions
from gowa import updater as gowa_updater
from plugins.events import emit as emit_event, emit_with_filter
from server import ai_reactivation
from server.avatars import refresh_and_broadcast

# How often the AI re-enabling rule is evaluated, and the pause after boot. One
# minute is fine enough for a "HH:MM" schedule and for minute-based waits.
AI_REACTIVATION_INTERVAL = 60  # seconds
AI_REACTIVATION_DELAY = 20     # seconds

# How often the background sweep re-checks every contact's avatar for changes.
AVATAR_REFRESH_INTERVAL = 1800  # seconds (30 min)

# How often we ask GitHub whether a newer GOWA was released, and how long we
# wait after boot before the first check (lets GOWA and the panel settle).
GOWA_UPDATE_CHECK_INTERVAL = 86400  # seconds (24h)
GOWA_UPDATE_CHECK_DELAY = 120       # seconds

logger = logging.getLogger(__name__)


async def start_gowa_task(deps):
    """Start GOWA subprocess and register device."""
    gowa_manager = deps.gowa_manager
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    try:
        await asyncio.to_thread(gowa_manager.start)
        for _ in range(10):
            await asyncio.sleep(1)
            if await asyncio.to_thread(gowa_client.health_check):
                break
        if await asyncio.to_thread(gowa_client.ensure_device):
            state.notification = "GOWA pronto, aguardando QR..."
            await ws_manager.broadcast("gowa_status", {"message": state.notification})
        else:
            state.notification = "Erro ao registrar device no GOWA"
            await ws_manager.broadcast("gowa_status", {"message": state.notification})
    except FileNotFoundError:
        state.notification = "GOWA não encontrado — modo sandbox disponível"
        logger.info("GOWA binary not found, sandbox-only mode.")
        await ws_manager.broadcast("gowa_status", {"message": state.notification})
    except Exception as e:
        state.notification = "GOWA indisponível — modo sandbox disponível"
        logger.info("GOWA failed to start (%s), sandbox-only mode.", e)
        await ws_manager.broadcast("gowa_status", {"message": state.notification})


async def status_poll_loop(deps):
    """Poll WhatsApp connection status every 5 seconds."""
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings
    last_connected: bool | None = None
    last_qr_required: bool | None = None
    while not state.stop_event.is_set():
        try:
            connected = await asyncio.to_thread(gowa_client.is_connected)
            state.connected = connected
            await ws_manager.broadcast("status", {
                "connected": state.connected,
                "msg_count": state.msg_count,
                "auto_reply_running": state.auto_reply_running,
                "bot_phone": state.bot_phone,
                "bot_name": state.bot_name,
            })
            # Plugin event: connection state transitions only (not every poll).
            qr_required = bool(state.qr_data is not None and not connected)
            if (last_connected, last_qr_required) != (connected, qr_required):
                await emit_with_filter("connection.changed", {
                    "is_connected": bool(connected),
                    "is_logged_in": bool(connected),  # GOWA conflates these
                    "qr_required": qr_required,
                    "bot_phone": state.bot_phone,
                    "bot_name": state.bot_name,
                    "ts": time.time(),
                })
                last_connected = connected
                last_qr_required = qr_required
            # Auto-reply is handled via GOWA webhook (POST /api/webhook)
            state.auto_reply_running = connected and settings.get("auto_reply", True)
            # Bot's display name = real WhatsApp profile name from GOWA (/devices
            # display_name), not a configured value. Fetched independently of the
            # phone capture below, which may happen first via the webhook echo path.
            if connected and not state.bot_name:
                name = await asyncio.to_thread(gowa_client.get_own_display_name)
                if name:
                    state.bot_name = name
                    if state.bot_phone:
                        group_mentions.set_bot_identity(state.bot_phone, state.bot_name)
            # Populate bot identity for @mention detection in groups
            if connected and not state.bot_phone:
                # Try config first
                state.bot_phone = settings.get("bot_phone", "").split(":")[0]
                # Auto-detect: fetch recent chat messages to find our own JID
                if not state.bot_phone:
                    try:
                        chats = await asyncio.to_thread(gowa_client.get_chats, 5)
                        for chat in chats:
                            cjid = chat.get("jid", "")
                            if "@s.whatsapp.net" in cjid:
                                msgs = await asyncio.to_thread(
                                    gowa_client.get_chat_messages, cjid, 5)
                                for m in msgs:
                                    if m.get("is_from_me") or m.get("from_me"):
                                        own_jid = (m.get("sender_jid", "")
                                                   or m.get("sender", "")
                                                   or m.get("from", ""))
                                        if own_jid and "@s.whatsapp.net" in own_jid:
                                            state.bot_phone = own_jid.split("@")[0].split(":")[0]
                                            logger.info("[Status] Bot phone auto-detected: %s",
                                                        state.bot_phone)
                                            break
                                if state.bot_phone:
                                    break
                    except Exception:
                        pass
                if state.bot_phone:
                    logger.info("[Status] Bot phone: %s, name: %s",
                                state.bot_phone, state.bot_name or "(empty)")
                    # Persist so it survives restarts and @mention detection in
                    # groups works immediately on the next boot (loaded above from
                    # config before the auto-detect path).
                    if settings.get("bot_phone", "") != state.bot_phone:
                        try:
                            settings.set("bot_phone", state.bot_phone)
                        except Exception as e:
                            logger.warning("[Status] Failed to persist bot_phone: %s", e)
                    # Register bot identity so mentions of the bot resolve to
                    # its configured panel name (settings > GOWA pushName).
                    group_mentions.set_bot_identity(state.bot_phone, state.bot_name)
        except Exception as e:
            logger.error("Status poll error: %s", e)
        await asyncio.sleep(5)


async def qr_poll_loop(deps):
    """Poll QR availability and cache QR image.

    QR codes from WhatsApp are valid for ~20s. We fetch a new one
    only when the cache is older than QR_CACHE_TTL, so the frontend
    always shows a stable image the user can actually scan.
    """
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    QR_CACHE_TTL = 120  # seconds — WhatsApp QRs valid ~160s; refresh conservatively
    while not state.stop_event.is_set():
        try:
            if not state.connected:
                age = time.time() - state.qr_fetched_at
                if state.qr_data is None or age >= QR_CACHE_TTL:
                    qr_data = await asyncio.to_thread(gowa_client.get_qr_code)
                    if qr_data and isinstance(qr_data, bytes):
                        state.qr_data = qr_data
                        state.qr_fetched_at = time.time()
                        state.qr_version += 1
                        await ws_manager.broadcast("qr_update", {
                            "available": True,
                            "version": state.qr_version,
                        })
                    elif state.qr_data is None:
                        await ws_manager.broadcast("qr_update", {"available": False})
            else:
                if state.qr_data is not None:
                    state.qr_data = None
                    state.qr_fetched_at = 0
                await ws_manager.broadcast("qr_update", {"available": False})
        except Exception as e:
            logger.error("QR poll error: %s", e)
        # Poll faster when we don't have a QR yet (waiting for first one)
        await asyncio.sleep(2 if state.qr_data is None and not state.connected else 5)


async def avatar_fetch_task(deps):
    """Keep WhatsApp profile photos fresh for all contacts.

    Runs an initial sweep once connected, then re-sweeps every
    ``AVATAR_REFRESH_INTERVAL`` seconds. Each pass re-fetches every contact's
    avatar and overwrites the cached file only when it actually changed (new
    photo, removed photo handled by keeping the last one), broadcasting
    ``avatar_updated`` so open clients update live.
    """
    state = deps.state
    settings = deps.settings
    avatars_dir = settings.data_dir / "statics" / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)

    # Wait until WhatsApp is connected
    while not state.stop_event.is_set():
        if state.connected:
            break
        await asyncio.sleep(3)

    if state.stop_event.is_set():
        return

    # Give GOWA a moment to stabilize after connection
    await asyncio.sleep(5)

    while not state.stop_event.is_set():
        try:
            contacts = await asyncio.to_thread(contact_repo.list_contacts, "", False)
            archived = await asyncio.to_thread(contact_repo.list_contacts, "", True)
            all_contacts = contacts + archived
        except Exception as e:
            logger.error("[Avatar] Failed to load contacts: %s", e)
            all_contacts = []

        changed = 0
        for c in all_contacts:
            if state.stop_event.is_set():
                return
            phone = c.get("phone", "")
            if not phone:
                continue
            try:
                if await refresh_and_broadcast(deps, phone):
                    changed += 1
            except Exception as e:
                logger.debug("[Avatar] refresh failed for %s: %s", phone, e)
            # Rate limit to avoid overwhelming GOWA
            await asyncio.sleep(0.5)

        logger.info("[Avatar] Sweep done: %d updated (of %d contacts)",
                    changed, len(all_contacts))

        # Sleep until the next sweep (interruptible by shutdown).
        slept = 0
        while slept < AVATAR_REFRESH_INTERVAL and not state.stop_event.is_set():
            await asyncio.sleep(3)
            slept += 3


async def ai_reactivation_loop(deps):
    """Turn the AI back on, per the rule in Painel → Agente → Automação.

    Wakes every ``AI_REACTIVATION_INTERVAL`` seconds. The rule itself (what is
    due, who qualifies) lives in :mod:`server.ai_reactivation`; this only paces
    it and keeps one failed pass from stopping the loop.
    """
    state = deps.state
    await asyncio.sleep(AI_REACTIVATION_DELAY)
    while not state.stop_event.is_set():
        try:
            await ai_reactivation.run_once(deps)
        except Exception as e:
            logger.error("[AI reactivation] pass failed: %s", e)
        slept = 0
        while slept < AI_REACTIVATION_INTERVAL and not state.stop_event.is_set():
            await asyncio.sleep(3)
            slept += 3

async def gowa_update_check_loop(deps):
    """Check once a day whether a newer GOWA was released.

    Only notifies when the release assessment finds a WhatsApp compatibility
    reason; installing always requires the user to approve. Versions outside
    the supported range are deliberately NOT broadcast (they show up flagged
    in the GOWA settings card instead), so we never push the user towards a
    build that could break the integration.
    """
    settings = deps.settings
    ws_manager = deps.ws_manager
    state = deps.state

    async def _sleep(total: int) -> bool:
        """Sleep in slices so shutdown stays responsive. False = stop."""
        slept = 0
        while slept < total:
            if state.stop_event.is_set():
                return False
            await asyncio.sleep(3)
            slept += 3
        return not state.stop_event.is_set()

    if not await _sleep(GOWA_UPDATE_CHECK_DELAY):
        return

    while not state.stop_event.is_set():
        try:
            if settings.get("gowa_auto_check_enabled", True):
                result = await asyncio.to_thread(gowa_updater.check, settings, True)
                latest = result.get("latest_version", "")
                skipped = settings.get("gowa_skipped_version", "")
                if (
                    result.get("update_available")
                    and result.get("latest_supported")
                    and result.get("whatsapp_update_recommended")
                    and latest
                    and latest != skipped
                ):
                    logger.info("New GOWA release available: %s", latest)
                    await ws_manager.broadcast("gowa_update_available", {
                        "latest_version": latest,
                        "installed_version": result.get("installed_version", ""),
                        "supported": True,
                        "release_url": result.get("release_url", ""),
                        "release_notes": result.get("release_notes", ""),
                        "published_at": result.get("published_at", ""),
                        "whatsapp_update_recommended": True,
                        "update_risk_level": result.get("update_risk_level", "medium"),
                        "update_reason": result.get("update_reason", ""),
                        "update_evidence": result.get("update_evidence", []),
                        "assessed_versions": result.get("assessed_versions", []),
                    })
        except Exception as e:
            logger.debug("GOWA update check failed: %s", e)

        if not await _sleep(GOWA_UPDATE_CHECK_INTERVAL):
            return
