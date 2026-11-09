"""WhatsApp connection endpoints (QR, reconnect, logout)."""

import asyncio
import logging

from fastapi.responses import Response

from server.helpers import _ok

logger = logging.getLogger(__name__)


def register_routes(app, deps):
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state

    @app.get("/api/qr")
    async def get_qr():
        if state.connected or not state.qr_data:
            return Response(status_code=204)
        return Response(
            content=state.qr_data,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/qr/refresh")
    async def refresh_qr():
        """Force a new QR code fetch on next poll cycle."""
        state.qr_data = None
        state.qr_fetched_at = 0
        return _ok({"message": "QR refresh solicitado."})

    @app.post("/api/whatsapp/reconnect")
    async def reconnect():
        await asyncio.to_thread(gowa_client.reconnect)
        state.qr_data = None
        state.qr_fetched_at = 0
        state.connected = False
        state.bot_phone = ""
        state.bot_name = ""
        state.notification = "Reconectando..."
        await ws_manager.broadcast("gowa_status", {"message": state.notification})
        return _ok({"message": "Reconectando..."})

    @app.post("/api/whatsapp/logout")
    async def logout():
        await asyncio.to_thread(gowa_client.logout)
        state.qr_data = None
        state.qr_fetched_at = 0
        state.connected = False
        state.bot_phone = ""
        state.bot_name = ""
        # The saved number belongs to the account that just logged out; keeping it would
        # show it for whichever account is paired next.
        try:
            deps.settings.set("bot_phone", "")
        except Exception as e:
            logger.warning("[Logout] Failed to clear saved bot_phone: %s", e)
        state.notification = "Desconectado."
        await ws_manager.broadcast("status", {
            "connected": False,
            "msg_count": state.msg_count,
            "auto_reply_running": state.auto_reply_running,
            "bot_phone": state.bot_phone,
            "bot_name": state.bot_name,
        })
        await ws_manager.broadcast("gowa_status", {"message": state.notification})
        return _ok({"message": "Desconectado."})
