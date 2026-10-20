"""Offline tests for server/ai_reactivation.py (automatic AI re-enabling).

The decision is pure, so most checks need no server. The query and ``run_once``
use a temporary SQLite database. Run: python tests/test_ai_reactivation.py
"""

import asyncio
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import init_db  # noqa: E402

init_db(Path(tempfile.mkdtemp(prefix="whatsbot_react_")) / "whatsbot.db")

from agent.memory import TagRegistry  # noqa: E402
from agent.handler import AgentHandler  # noqa: E402
from agent.tools.transfer_to_human import TRANSFER_TAG  # noqa: E402
from config.settings import Settings, TECHIFY_PROVISION_NUMBER  # noqa: E402
from db.repositories import contact_repo, message_repo  # noqa: E402
from server import ai_reactivation as ar  # noqa: E402

passed = 0
failed = 0

def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f" -- {detail}" if detail else ""))

def section(title: str):
    print(f"\n{'-'*60}\n  {title}\n{'-'*60}")

SP = ZoneInfo("America/Sao_Paulo")
HOUR = 3600
NOW = 1_800_000_000.0

def cfg(**over) -> dict:
    base = {
        "enabled": True, "unit": "hours", "value": 1, "weekdays": [0, 1, 2, 3, 4],
        "time": "08:00", "last_sender": "both", "include_groups_archived": False,
        "timezone": "",
    }
    base.update(over)
    return base

def row(last_role, last_ts, **over) -> dict:
    r = {"phone": "5511999990000", "last_role": last_role, "last_ts": last_ts,
         "ai_disabled_at": None, "created_at": 1.0}
    r.update(over)
    return r

def at(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=SP).timestamp()

# ── clean / read_config ─────────────────────────────────────────────────
section("Validation of what the panel sends")
check("bool flags only take bool", ar.clean("ai_reactivation_enabled", True) is True
      and ar.clean("ai_reactivation_enabled", "yes") is ar.INVALID
      and ar.clean("ai_reactivation_include_groups_archived", 1) is ar.INVALID)
check("unit is one of the four", ar.clean("ai_reactivation_unit", "weeks") is ar.INVALID
      and ar.clean("ai_reactivation_unit", "schedule") == "schedule")
check("last sender is one of three", ar.clean("ai_reactivation_last_sender", "bot") is ar.INVALID
      and ar.clean("ai_reactivation_last_sender", "attendant") == "attendant")
check("value is clamped to >= 1", ar.clean("ai_reactivation_value", 0) == 1
      and ar.clean("ai_reactivation_value", "-5") == 1 and ar.clean("ai_reactivation_value", "7") == 7)
check("value rejects junk and booleans", ar.clean("ai_reactivation_value", "abc") is ar.INVALID
      and ar.clean("ai_reactivation_value", True) is ar.INVALID)
check("value has an upper bound", ar.clean("ai_reactivation_value", 10**12) == ar.MAX_VALUE)
check("weekdays are deduplicated, sorted and bounded",
      ar.clean("ai_reactivation_weekdays", [4, 0, 0, 9, -1, "1", True]) == [0, 4])
check("weekdays must be a list", ar.clean("ai_reactivation_weekdays", "0,1") is ar.INVALID)
check("an empty weekday list is kept (the panel blocks saving it)",
      ar.clean("ai_reactivation_weekdays", []) == [])
check("time must be HH:MM", ar.clean("ai_reactivation_time", "8:00") is ar.INVALID
      and ar.clean("ai_reactivation_time", "24:00") is ar.INVALID
      and ar.clean("ai_reactivation_time", "08:60") is ar.INVALID
      and ar.clean("ai_reactivation_time", "23:59") == "23:59")
check("timezone accepts IANA names and empty only",
      ar.clean("ai_reactivation_timezone", "America/Sao_Paulo") == "America/Sao_Paulo"
      and ar.clean("ai_reactivation_timezone", "") == ""
      and ar.clean("ai_reactivation_timezone", "../etc/passwd") is ar.INVALID
      and ar.clean("ai_reactivation_timezone", 3) is ar.INVALID)
check("unknown keys are rejected", ar.clean("ai_reactivation_last_run", 5.0) is ar.INVALID)

class FakeSettings(dict):
    def set(self, k, v):
        self[k] = v

check("read_config defaults: off, groups/archived off",
      ar.read_config(FakeSettings()) ["enabled"] is False
      and ar.read_config(FakeSettings())["include_groups_archived"] is False)
bad = ar.read_config(FakeSettings({"ai_reactivation_unit": "x", "ai_reactivation_value": "?"}))
check("read_config falls back per key on junk", bad["unit"] == "hours" and bad["value"] == 24)

# ── who spoke last ──────────────────────────────────────────────────────
section("Who spoke last")
check("both accepts customer, attendant and nobody",
      all(ar.owner_allows("both", r) for r in ("user", "assistant", None)))
check("customer accepts only the customer",
      ar.owner_allows("customer", "user") and not ar.owner_allows("customer", "assistant")
      and not ar.owner_allows("customer", None))
check("attendant accepts only the attendant",
      ar.owner_allows("attendant", "assistant") and not ar.owner_allows("attendant", "user")
      and not ar.owner_allows("attendant", None))

# ── wait in minutes / hours / days ──────────────────────────────────────
section("Quiet time (minutes, hours, days)")
check("not enough time has passed", ar.pick_rows([row("user", NOW - 30 * 60)], cfg(unit="hours", value=1), NOW) == [])
check("exactly the wait is enough", len(ar.pick_rows([row("user", NOW - HOUR)], cfg(unit="hours", value=1), NOW)) == 1)
check("minutes", len(ar.pick_rows([row("user", NOW - 11 * 60)], cfg(unit="minutes", value=10), NOW)) == 1
      and ar.pick_rows([row("user", NOW - 9 * 60)], cfg(unit="minutes", value=10), NOW) == [])
check("days", len(ar.pick_rows([row("user", NOW - 3 * 86400)], cfg(unit="days", value=2), NOW)) == 1
      and ar.pick_rows([row("user", NOW - 86400)], cfg(unit="days", value=2), NOW) == [])
check("the sender filter is applied on top",
      ar.pick_rows([row("user", NOW - 9 * HOUR)], cfg(unit="hours", value=1, last_sender="attendant"), NOW) == []
      and len(ar.pick_rows([row("assistant", NOW - 9 * HOUR)], cfg(unit="hours", value=1, last_sender="attendant"), NOW)) == 1)

section("Where the wait starts counting")
fresh_off = row("user", NOW - 10 * HOUR, ai_disabled_at=NOW - 60)
check("switched off a minute ago is NOT turned on for an old last message",
      ar.pick_rows([fresh_off], cfg(unit="hours", value=1), NOW) == [])
check("...but is, once the wait has passed since it was switched off",
      len(ar.pick_rows([fresh_off], cfg(unit="hours", value=1), NOW + 2 * HOUR)) == 1)
check("no message and no marker falls back to creation",
      ar.reference_ts({"created_at": 42.0}) == 42.0)
check("a newer message wins over an older marker",
      ar.reference_ts(row("user", 500.0, ai_disabled_at=100.0)) == 500.0)

# ── schedule ────────────────────────────────────────────────────────────
section("Weekday + time of day")
WORK = [0, 1, 2, 3, 4]
mon = at(2026, 10, 19, 8, 0)  # a Monday
check("2026-10-19 is a Monday", datetime(2026, 10, 19).weekday() == 0)
check("slot is today at HH:MM once it has passed",
      ar.latest_slot(WORK, "08:00", mon + 60, SP) == mon)
check("before the time, the previous working day is the latest slot",
      ar.latest_slot(WORK, "08:00", mon - 60, SP) == at(2026, 10, 16, 8, 0))
check("on a weekend the latest slot is Friday",
      ar.latest_slot(WORK, "08:00", at(2026, 10, 25, 12, 0), SP) == at(2026, 10, 23, 8, 0))
check("no weekdays -> no slot", ar.latest_slot([], "08:00", mon, SP) is None)
check("a single weekday looks back up to a week",
      ar.latest_slot([2], "08:00", at(2026, 10, 19, 12, 0), SP) == at(2026, 10, 14, 8, 0))
c = cfg(unit="schedule", weekdays=WORK, time="08:00")
check("due right after the time", ar.schedule_due(c, mon + 30, 0.0, SP))
check("not due before the time", not ar.schedule_due(c, mon - 30, 0.0, SP))
check("not due on a day that was not chosen", not ar.schedule_due(c, at(2026, 10, 25, 8, 5), 0.0, SP))
check("fires once: not due again after it already ran", not ar.schedule_due(c, mon + 90, mon + 30, SP))
check("fires again the next chosen day", ar.schedule_due(c, at(2026, 10, 20, 8, 1), mon + 30, SP))
check("a late start inside the grace still fires", ar.schedule_due(c, mon + 30 * 60, 0.0, SP))
check("...but not once the grace is over", not ar.schedule_due(c, mon + 3 * HOUR, 0.0, SP))
check("schedule ignores quiet time and applies the sender filter",
      len(ar.pick_rows([row("user", NOW - 5)], cfg(unit="schedule"), NOW)) == 1
      and ar.pick_rows([row("user", NOW - 5)], cfg(unit="schedule", last_sender="attendant"), NOW) == [])
check("the zone changes the instant of the slot",
      ar.latest_slot(WORK, "08:00", mon + 60, ZoneInfo("UTC")) != mon)
check("resolve_tz falls back for an unknown name", ar.resolve_tz("Nowhere/Land") is not None)

# ── the query ───────────────────────────────────────────────────────────
section("Query: conversations with the AI off")
def mk(phone, *, ai=False, group=False, archived=False, msgs=()):
    data = contact_repo.get_or_create(phone, default_ai_enabled=ai)
    contact_repo.update(data["id"], is_group=1 if group else 0, is_archived=1 if archived else 0,
                        ai_enabled=1 if ai else 0)
    for i, (role, status, ts) in enumerate(msgs):
        message_repo.add(data["id"], role, f"m{i}", status=status, ts=ts)
    return data["id"]

mk("5511100000001", msgs=[("user", None, 100.0), ("assistant", None, 200.0)])           # AI reply ignored
mk("5511100000002", msgs=[("user", None, 100.0), ("assistant", "operator", 200.0)])     # attendant last
mk("5511100000003", msgs=[("assistant", "operator", 100.0), ("user", None, 300.0), ("private_note", None, 400.0)])
mk("5511100000004", ai=True, msgs=[("user", None, 100.0)])                                # AI on: not a candidate
mk("5511100000005", group=True, msgs=[("user", None, 100.0)])
mk("5511100000006", archived=True, msgs=[("user", None, 100.0)])
mk("5511100000007")                                                                       # no messages at all
# The fake clock (NOW) is months ahead of the real one, and a contact born with the
# AI off gets ai_disabled_at = creation. Pin both to the fake clock right away, before
# any pass runs, or the first pass would see a contact created "months ago".
contact_repo.update(contact_repo.get_by_phone("5511100000007")["id"],
                    created_at=NOW - 60, ai_disabled_at=NOW - 60)
mk(TECHIFY_PROVISION_NUMBER, msgs=[("user", None, 100.0)])

by = {r["phone"]: r for r in contact_repo.ai_disabled_for_reactivation(False)}
check("only conversations with the AI off", "5511100000004" not in by)
check("the AI's own reply is ignored", by["5511100000001"]["last_role"] == "user" and by["5511100000001"]["last_ts"] == 100.0)
check("the attendant counts (status=operator)", by["5511100000002"]["last_role"] == "assistant")
check("notes and system rows are ignored", by["5511100000003"]["last_role"] == "user" and by["5511100000003"]["last_ts"] == 300.0)
check("groups and archived stay out by default", "5511100000005" not in by and "5511100000006" not in by)
check("a conversation with no messages comes back with none", by["5511100000007"]["last_role"] is None)
with_all = {r["phone"] for r in contact_repo.ai_disabled_for_reactivation(True)}
check("the checkbox brings groups and archived in", {"5511100000005", "5511100000006"} <= with_all)
skipped = {r["phone"] for r in contact_repo.ai_disabled_for_reactivation(True, [TECHIFY_PROVISION_NUMBER])}
check("excluded numbers never come back", TECHIFY_PROVISION_NUMBER not in skipped and "5511100000001" in skipped)
check("(the provisioning number is in the table for this to mean something)",
      TECHIFY_PROVISION_NUMBER in {r["phone"] for r in contact_repo.ai_disabled_for_reactivation(True)})

# ── run_once ────────────────────────────────────────────────────────────
section("run_once: effects")
class FakeWS:
    def __init__(self):
        self.events = []
    async def broadcast(self, event, data):
        self.events.append((event, data))

settings = Settings()
handler = AgentHandler(api_key="x", system_prompt="", max_context_messages=10, model="m")
ws = FakeWS()
deps = SimpleNamespace(settings=settings, agent_handler=handler, ws_manager=ws)

def run(now=None):
    ws.events.clear()
    return asyncio.run(ar.run_once(deps, now))

def ai_on(phone) -> bool:
    return bool(contact_repo.get_by_phone(phone)["ai_enabled"])

settings.set("ai_reactivation_enabled", False)
check("disabled -> does nothing", run(NOW) == 0 and not ai_on("5511100000001"))

settings.set("ai_reactivation_enabled", True)
settings.set("ai_reactivation_unit", "hours")
settings.set("ai_reactivation_value", 1)
settings.set("ai_reactivation_last_sender", "attendant")
n = run(NOW)
check("only the attendant's conversation turned on", ai_on("5511100000002") and not ai_on("5511100000001")
      and not ai_on("5511100000003"), f"n={n}")
check("the AI-toggled event goes to the panel", ("contact_ai_toggled", {"phone": "5511100000002", "ai_enabled": True}) in ws.events)

# transferred conversation: tag is removed when the AI comes back
TagRegistry().create(TRANSFER_TAG, "#ef4444")
mem = handler._get_contact("5511100000003")
mem.add_tag(TRANSFER_TAG)
settings.set("ai_reactivation_last_sender", "customer")
run(NOW)
check("the customer's conversation turned on", ai_on("5511100000003"))
mem = handler._get_contact("5511100000003")
check("the transfer tag was removed", TRANSFER_TAG not in mem.tags
      and TRANSFER_TAG not in contact_repo.get_full_contact("5511100000003")["tags"])
check("the tag change reaches the panel", any(e == "contact_tags_updated" for e, _ in ws.events))
check("the marker is cleared when the AI is on again",
      contact_repo.get_by_phone("5511100000003").get("ai_disabled_at") in (None, 0))

settings.set("ai_reactivation_last_sender", "both")
run(NOW)
check("groups/archived untouched by default", not ai_on("5511100000005") and not ai_on("5511100000006"))
check("the provisioning number is never turned on", not ai_on(TECHIFY_PROVISION_NUMBER))
run(NOW)
check("a conversation with no messages and a recent creation waits",
      not ai_on("5511100000007"))

settings.set("ai_reactivation_include_groups_archived", True)
run(NOW)
check("with the checkbox, groups and archived are turned on", ai_on("5511100000005") and ai_on("5511100000006"))
check("the provisioning number is STILL never turned on", not ai_on(TECHIFY_PROVISION_NUMBER))

section("run_once: schedule fires once")
for p in ("5511100000008",):
    mk(p, msgs=[("user", None, 100.0)])
settings.set("ai_reactivation_unit", "schedule")
settings.set("ai_reactivation_weekdays", [0, 1, 2, 3, 4])
settings.set("ai_reactivation_time", "08:00")
settings.set("ai_reactivation_timezone", "America/Sao_Paulo")
settings.set("ai_reactivation_last_run", 0.0)
check("before the time nothing happens", run(mon - 60) == 0 and not ai_on("5511100000008"))
check("at the time it runs", run(mon + 60) >= 1 and ai_on("5511100000008"))
check("and records the run", settings.get("ai_reactivation_last_run") == mon + 60)
contact_repo.update(contact_repo.get_by_phone("5511100000008")["id"], ai_enabled=0)
check("a second pass in the same minute window does not re-fire", run(mon + 120) == 0 and not ai_on("5511100000008"))

print(f"\n{'='*60}\n  {passed} passed, {failed} failed\n{'='*60}")
sys.exit(1 if failed else 0)
