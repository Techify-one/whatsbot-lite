"""Automatic re-enabling of the AI on conversations it was switched off for.

The AI goes off for a conversation when it transfers to a human
(``transfer_to_human``), when the attendant switches it off in the panel, or
when it is born off (``default_ai_enabled``). This module turns it back on
later, by one of two rules chosen in Painel → Agente → Automação:

* ``minutes`` / ``hours`` / ``days``: the conversation has been quiet for that
  long, counted from the last message;
* ``schedule``: on the chosen weekdays, at the chosen time of day.

Either way only conversations whose last message came from the chosen side
(customer, attendant or both) are touched. The deciding part is pure (no I/O,
no clock of its own) so ``tests/test_ai_reactivation.py`` runs offline; the
effects (database, WebSocket, plugin events) live in :func:`run_once`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from agent.tools.transfer_to_human import TRANSFER_TAG
from config.settings import TECHIFY_PROVISION_NUMBER
from db.repositories import contact_repo
from plugins.events import emit_with_filter

logger = logging.getLogger(__name__)

UNITS = ("minutes", "hours", "days", "schedule")
OWNERS = ("both", "customer", "attendant")
UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}
MAX_VALUE = 100_000

# A scheduled run is still honoured this long after its time. It covers a server
# that was down or restarting at 08:00, without turning the AI on at 23:00 for a
# slot that passed in the morning.
SCHEDULE_GRACE = 3600  # seconds

KEY_PREFIX = "ai_reactivation_"
INVALID = object()
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}$")

def clean(key: str, value):
    """Validate one ``ai_reactivation_*`` value from the panel.

    Returns the value to store, or :data:`INVALID` when it must be ignored, so
    junk never reaches the config and the loop never has to defend against it.
    """
    if key in ("ai_reactivation_enabled", "ai_reactivation_include_groups_archived"):
        return value if isinstance(value, bool) else INVALID
    if key == "ai_reactivation_unit":
        return value if value in UNITS else INVALID
    if key == "ai_reactivation_last_sender":
        return value if value in OWNERS else INVALID
    if key == "ai_reactivation_value":
        if isinstance(value, bool):
            return INVALID
        try:
            number = int(value)
        except (TypeError, ValueError):
            return INVALID
        return min(max(number, 1), MAX_VALUE)
    if key == "ai_reactivation_weekdays":
        if not isinstance(value, list):
            return INVALID
        days = {d for d in value if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}
        return sorted(days)
    if key == "ai_reactivation_time":
        return value if isinstance(value, str) and _HHMM.match(value) else INVALID
    if key == "ai_reactivation_timezone":
        # The browser's IANA name. Not checked against the tz database on
        # purpose: whether the server can resolve it is decided at run time.
        if value == "" or (isinstance(value, str) and len(value) <= 64 and _TZ_NAME.match(value)):
            return value
        return INVALID
    return INVALID

def read_config(settings) -> dict:
    """The effective rule, always well-formed (falls back per key)."""
    def pick(key: str, default):
        value = clean(key, settings.get(key, default))
        return default if value is INVALID else value

    return {
        "enabled": pick("ai_reactivation_enabled", False),
        "unit": pick("ai_reactivation_unit", "hours"),
        "value": pick("ai_reactivation_value", 24),
        "weekdays": pick("ai_reactivation_weekdays", [0, 1, 2, 3, 4]),
        "time": pick("ai_reactivation_time", "08:00"),
        "last_sender": pick("ai_reactivation_last_sender", "both"),
        "include_groups_archived": pick("ai_reactivation_include_groups_archived", False),
        "timezone": pick("ai_reactivation_timezone", ""),
    }

def resolve_tz(name: str) -> tzinfo:
    """The zone the schedule is read in: the browser's, else the server's own."""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown name, or no tz database on this host
            logger.warning("[AI reactivation] timezone %r unavailable; using the server's", name)
    return datetime.now().astimezone().tzinfo

def owner_allows(owner: str, last_role: str | None) -> bool:
    """Does the side that spoke last match the rule?

    ``last_role`` is ``'user'`` (customer), ``'assistant'`` (attendant) or
    ``None`` (no human message yet — only "both" accepts that).
    """
    if owner == "both":
        return True
    if last_role == "user":
        return owner == "customer"
    if last_role == "assistant":
        return owner == "attendant"
    return False

def reference_ts(row: dict) -> float:
    """Where the quiet period is counted from.

    The later of the last human message and the moment the AI was switched off:
    a conversation switched off by hand a minute ago is not turned back on just
    because its last message is old. Rows from before ``ai_disabled_at`` existed
    fall back to the last message, and conversations with neither to creation.
    """
    ts = max(row.get("last_ts") or 0.0, row.get("ai_disabled_at") or 0.0)
    return ts or row.get("created_at") or 0.0

def latest_slot(weekdays: list[int], hhmm: str, now: float, tz: tzinfo) -> float | None:
    """Epoch of the most recent scheduled time at or before ``now`` (Mon = 0)."""
    if not weekdays:
        return None
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    local_now = datetime.fromtimestamp(now, tz)
    for back in range(8):
        day = local_now.date() - timedelta(days=back)
        if day.weekday() not in weekdays:
            continue
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if slot <= local_now:
            return slot.timestamp()
    return None

def schedule_due(cfg: dict, now: float, last_run: float, tz: tzinfo) -> bool:
    """A scheduled time passed since the last run and is still within the grace."""
    slot = latest_slot(cfg["weekdays"], cfg["time"], now, tz)
    return slot is not None and slot > last_run and now - slot <= SCHEDULE_GRACE

def pick_rows(rows: list[dict], cfg: dict, now: float) -> list[dict]:
    """Of the conversations with the AI off, the ones to turn back on now.

    For ``schedule`` the caller has already established that a slot is due, so
    every row matching the sender filter qualifies; for minutes/hours/days it
    must also have been quiet long enough.
    """
    matching = [r for r in rows if owner_allows(cfg["last_sender"], r.get("last_role"))]
    if cfg["unit"] == "schedule":
        return matching
    wait = cfg["value"] * UNIT_SECONDS[cfg["unit"]]
    return [r for r in matching if now - reference_ts(r) >= wait]

def _excluded_phones(settings) -> list[str]:
    """Contacts whose AI is off on purpose and must never come back."""
    return [TECHIFY_PROVISION_NUMBER, *(settings.get("provisioning_phones", []) or [])]

def _reactivate(agent_handler, phone: str) -> dict:
    """Turn the AI on for one conversation and drop the transfer tag (worker thread)."""
    contact = agent_handler._get_contact(phone)
    contact.set_ai_enabled(True)
    tag_removed = TRANSFER_TAG in contact.tags
    if tag_removed:
        contact.remove_tag(TRANSFER_TAG)
    return {"phone": phone, "tags": list(contact.tags), "tag_removed": tag_removed}

async def run_once(deps, now: float | None = None) -> int:
    """One pass of the rule. Returns how many conversations were turned back on."""
    settings = deps.settings
    cfg = read_config(settings)
    if not cfg["enabled"]:
        return 0

    now = time.time() if now is None else now
    scheduled = cfg["unit"] == "schedule"
    if scheduled:
        last_run = float(settings.get("ai_reactivation_last_run", 0.0) or 0.0)
        if not schedule_due(cfg, now, last_run, resolve_tz(cfg["timezone"])):
            return 0
        # Recorded before acting: a failure below must not re-fire every minute.
        settings.set("ai_reactivation_last_run", now)

    rows = await asyncio.to_thread(
        contact_repo.ai_disabled_for_reactivation,
        cfg["include_groups_archived"], _excluded_phones(settings),
    )
    due = pick_rows(rows, cfg, now)

    for row in due:
        phone = row["phone"]
        try:
            result = await asyncio.to_thread(_reactivate, deps.agent_handler, phone)
        except Exception as e:
            logger.error("[AI reactivation] failed for a conversation: %s", e)
            continue
        await deps.ws_manager.broadcast("contact_ai_toggled", {"phone": phone, "ai_enabled": True})
        await emit_with_filter("contact.ai_toggled", {
            "phone": phone, "ai_enabled": True, "source": "auto_reactivation", "ts": time.time(),
        })
        if result["tag_removed"]:
            await deps.ws_manager.broadcast(
                "contact_tags_updated", {"phone": phone, "tags": result["tags"]})
            await emit_with_filter("contact.untagged", {
                "phone": phone, "tag": TRANSFER_TAG, "ts": time.time(),
            })

    if due:
        logger.info("[AI reactivation] AI turned back on for %d conversation(s)", len(due))
    return len(due)
