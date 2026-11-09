"""The connected number shown in the panel follows GOWA, not a stale saved value.

Offline: fake GOWA client / settings / state. Run: python tests/test_bot_phone_live.py
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server import background  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))


class FakeSettings(dict):
    def set(self, k, v):
        self[k] = v


def run_one_poll(*, saved, live, state_phone="", connected=True):
    """Run exactly one iteration of status_poll_loop and return (state, settings)."""
    state = SimpleNamespace(
        stop_event=asyncio.Event(), connected=False, msg_count=0, auto_reply_running=False,
        bot_phone=state_phone, bot_name="Douglas", qr_data=None, notification="")
    settings = FakeSettings(bot_phone=saved, auto_reply=True)
    gowa = MagicMock()
    gowa.is_connected.return_value = connected
    gowa.get_own_number.return_value = live
    gowa.get_own_display_name.return_value = "Douglas"
    gowa.get_chats.return_value = []
    ws = SimpleNamespace(broadcast=AsyncMock())
    deps = SimpleNamespace(gowa_client=gowa, ws_manager=ws, state=state, settings=settings)

    real_sleep = background.asyncio.sleep

    async def stop_after_first(_):
        state.stop_event.set()
        await real_sleep(0)

    background.asyncio.sleep = stop_after_first
    try:
        asyncio.run(background.status_poll_loop(deps))
    finally:
        background.asyncio.sleep = real_sleep
    return state, settings


print("\nswitching accounts: old number saved, new account connected")
st, cfg = run_one_poll(saved="556492973092", live="554498510557:7")
check("panel shows the LIVE number", st.bot_phone == "554498510557", st.bot_phone)
check("device suffix (:7) is dropped", ":" not in st.bot_phone)
check("saved number is corrected", cfg["bot_phone"] == "554498510557", cfg["bot_phone"])

print("\nstale value already in memory is replaced")
st, cfg = run_one_poll(saved="556492973092", live="554498510557", state_phone="556492973092")
check("in-memory number follows GOWA", st.bot_phone == "554498510557", st.bot_phone)

print("\nGOWA cannot tell the number: keep the old fallback")
st, cfg = run_one_poll(saved="556492973092", live="")
check("falls back to the saved number", st.bot_phone == "556492973092", st.bot_phone)
check("saved number untouched", cfg["bot_phone"] == "556492973092")

print("\nnot connected: nothing is read or changed")
st, cfg = run_one_poll(saved="556492973092", live="554498510557", connected=False)
check("no number while disconnected", st.bot_phone == "", st.bot_phone)
check("saved number untouched", cfg["bot_phone"] == "556492973092")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
