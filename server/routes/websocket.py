"""WebSocket endpoint."""

import json

from fastapi import WebSocket, WebSocketDisconnect

from server.auth import auth_required, verify_token


def register_routes(app, deps):
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # Check auth token from query param if password is set
        if auth_required(settings):
            token = websocket.query_params.get("token", "")
            if not token or not verify_token(token, settings):
                await websocket.accept()
                await websocket.close(code=4401, reason="Unauthorized")
                return

        await ws_manager.connect(websocket)
        # Send initial state
        try:
            await websocket.send_text(json.dumps({"event": "status", "data": {
                "connected": state.connected,
                "msg_count": state.msg_count,
                "auto_reply_running": state.auto_reply_running,
                # Without these the panel kept the previous number/name after a reconnect,
                # until the next 5 s status broadcast (which a dropped socket never received).
                "bot_phone": state.bot_phone,
                "bot_name": state.bot_name,
            }}))
            await websocket.send_text(json.dumps({"event": "gowa_status", "data": {
                "message": state.notification,
            }}))
            # Send current QR state so page refreshes show QR immediately
            if not state.connected and state.qr_data:
                await websocket.send_text(json.dumps({"event": "qr_update", "data": {
                    "available": True,
                    "version": state.qr_version,
                }}))
            else:
                await websocket.send_text(json.dumps({"event": "qr_update", "data": {
                    "available": False,
                }}))
        except Exception:
            pass
        # Keep alive
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)
        except Exception:
            ws_manager.disconnect(websocket)
