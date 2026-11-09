"""On (re)connect the WebSocket sends the current status INCLUDING the connected number and name.

Without them the panel kept the previous account's number after a reconnect. Offline: only the /ws route
is mounted. Run: python tests/test_ws_initial_status.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.routes import websocket as ws_route  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))


state = SimpleNamespace(connected=True, msg_count=3, auto_reply_running=True, notification="ok",
                        qr_data=None, qr_version=0, bot_phone="554498510557", bot_name="Douglas")
ws_manager = MagicMock()
ws_manager.connect = lambda ws: _accept(ws)


async def _accept(ws):
    await ws.accept()


settings = {}  # no web password -> auth not required
deps = SimpleNamespace(state=state, ws_manager=ws_manager, settings=SimpleNamespace(get=lambda k, d=None: d))
app = FastAPI()
ws_route.register_routes(app, deps)
client = TestClient(app)

print("\ninitial status on connect")
with client.websocket_connect("/ws") as ws:
    first = ws.receive_json()
    check("first event is status", first["event"] == "status", str(first))
    data = first["data"]
    check("carries connected", data["connected"] is True)
    check("carries the connected number", data.get("bot_phone") == "554498510557", str(data))
    check("carries the account name", data.get("bot_name") == "Douglas", str(data))
    second = ws.receive_json()
    check("then gowa_status", second["event"] == "gowa_status", str(second))

print("\nheartbeat")
with client.websocket_connect("/ws") as ws:
    ws.receive_json(); ws.receive_json(); ws.receive_json()
    ws.send_json({"action": "ping"})
    pong = ws.receive_json()
    check("ping is answered with pong", pong["event"] == "pong", str(pong))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
