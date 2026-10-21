"""Comprehensive endpoint tests for WhatsBot-Lite API.

Uses FastAPI TestClient with a real temporary SQLite database.
No external services needed (GOWA/OpenRouter are mocked).
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Initialize the engine in a temp directory before importing anything else.
# Default: SQLite. Override with ``WHATSBOT_TEST_DB_URL`` to run the same
# assertions against Postgres (e.g. via testcontainers).
_tmpdir = tempfile.mkdtemp(prefix="whatsbot_test_")
_db_path = Path(_tmpdir) / "whatsbot.db"

from db import init_db, init_engine

_test_url = os.environ.get("WHATSBOT_TEST_DB_URL", "").strip()
if _test_url:
    init_engine(_test_url)
    from db.connection import _run_alembic_upgrade  # noqa: E402
    _run_alembic_upgrade()
else:
    init_db(_db_path)

# Seed some test data
from db.repositories import contact_repo, message_repo, usage_repo, tag_repo, config_repo

def _seed_data():
    """Insert test contacts, messages, tags, usage into the test DB."""
    now = time.time()

    # Create contacts
    c1 = contact_repo.get_or_create("5511999990001")
    contact_repo.update(c1["id"], name="Alice Test", email="alice@test.com",
                        profession="Engineer", company="TestCo")

    c2 = contact_repo.get_or_create("5511999990002")
    contact_repo.update(c2["id"], name="Bob Test", is_archived=True)

    # Add messages
    message_repo.add(c1["id"], "user", "Olá, tudo bem?", ts=now - 100)
    message_repo.add(c1["id"], "assistant", "Tudo sim! Como posso ajudar?", ts=now - 90)
    message_repo.add(c1["id"], "user", "Qual o horário de funcionamento?", ts=now - 50)
    message_repo.add(c1["id"], "assistant", "Nosso horário é de 9h às 18h.", ts=now - 40)

    message_repo.add(c2["id"], "user", "Oi", ts=now - 200)

    # Add observations
    contact_repo.add_observation(c1["id"], "Cliente VIP")

    # Add tags
    tag_repo.create("vip", "#ff0000")
    tag_repo.create("lead", "#00ff00")
    tag_repo.add_contact_tag(c1["id"], "vip")

    # Add usage
    usage_repo.add(c1["id"], "text", "openai/gpt-4o-mini", 100, 50, 150, 0.001)
    usage_repo.add(c1["id"], "text", "openai/gpt-4o-mini", 200, 80, 280, 0.002)

    # Increment unread for c2
    contact_repo.increment_unread(c2["id"], "msg_001")

_seed_data()

# Now import app components
from config.settings import Settings
from agent.handler import AgentHandler
from server.app import create_app

# Create mocks for GOWA
mock_gowa_manager = MagicMock()
mock_gowa_client = MagicMock()
mock_gowa_client.send_message = MagicMock(return_value=None)
mock_gowa_client.send_image = MagicMock(return_value=None)
mock_gowa_client.send_audio = MagicMock(return_value=None)
mock_gowa_client.send_file = MagicMock(return_value=None)
mock_gowa_client.send_chat_presence = MagicMock(return_value=None)
mock_gowa_client.mark_as_read = MagicMock(return_value=None)
mock_gowa_client.revoke_message = MagicMock(return_value=None)
mock_gowa_client.delete_message = MagicMock(return_value=None)
mock_gowa_client.react_to_message = MagicMock(return_value=None)
mock_gowa_client.reconnect = MagicMock(return_value=None)
mock_gowa_client.logout = MagicMock(return_value=None)
mock_gowa_client.get_own_number = MagicMock(return_value="5511999990001")

# Create real Settings and AgentHandler (backed by test DB)
settings = Settings()
agent_handler = AgentHandler(
    api_key="test-key-fake",
    system_prompt="Você é um assistente de teste.",
    max_context_messages=10,
    model="openai/gpt-4o-mini",
)

# Create the app (skip lifespan to avoid background tasks)
app = create_app(
    settings=settings,
    gowa_manager=mock_gowa_manager,
    gowa_client=mock_gowa_client,
    agent_handler=agent_handler,
)

# Patch lifespan to be a no-op for testing
from contextlib import asynccontextmanager

@asynccontextmanager
async def _noop_lifespan(app):
    yield

app.router.lifespan_context = _noop_lifespan

from starlette.testclient import TestClient
client = TestClient(app, raise_server_exceptions=False)


# ═══════════════════════════════════════════════════════════════════
#  Test runner
# ═══════════════════════════════════════════════════════════════════

passed = 0
failed = 0
errors = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK {name}")
    else:
        failed += 1
        msg = f"  FAIL {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        errors.append(msg)


def section(title: str):
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


# ═══════════════════════════════════════════════════════════════════
#  1. Health
# ═══════════════════════════════════════════════════════════════════
section("Health")
r = client.get("/health")
check("GET /health -> 200", r.status_code == 200)
check("GET /health -> ok=true", r.json().get("ok") is True)

# ═══════════════════════════════════════════════════════════════════
#  2. Auth
# ═══════════════════════════════════════════════════════════════════
section("Auth")

r = client.get("/api/auth/check")
check("GET /api/auth/check (no password) -> authenticated", r.json()["data"]["authenticated"] is True)
check("GET /api/auth/check -> has_password=false", r.json()["data"]["has_password"] is False)

r = client.post("/api/auth/login", json={"password": "test"})
check("POST /api/auth/login (no pw set) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  3. Config
# ═══════════════════════════════════════════════════════════════════
section("Config")

r = client.get("/api/config")
check("GET /api/config -> 200", r.status_code == 200)
data = r.json()["data"]
check("GET /api/config -> has model field", "model" in data)
check("GET /api/config -> has API key field", "openrouter_api_key" in data)
check("GET /api/config -> has system_prompt", "system_prompt" in data)
check("GET /api/config -> has split_messages", "split_messages" in data)
check("GET /api/config -> has has_password", "has_password" in data)
check("GET /api/config -> has setup_completed", "setup_completed" in data)

r = client.put("/api/config", json={"auto_reply": False})
check("PUT /api/config -> 200", r.status_code == 200)
check("PUT /api/config -> saved", r.json()["data"]["message"] == "Configurações salvas!")

r = client.get("/api/config")
check("PUT /api/config -> auto_reply persisted", r.json()["data"]["auto_reply"] is False)

# Restore
client.put("/api/config", json={"auto_reply": True})

# group_reply_mode is exposed and round-trips (controls AI replies in groups)
check("GET /api/config -> has group_reply_mode", "group_reply_mode" in data)
r = client.put("/api/config", json={"group_reply_mode": "always"})
check("PUT /api/config (group_reply_mode) -> 200", r.status_code == 200)
r = client.get("/api/config")
check("PUT /api/config -> group_reply_mode persisted",
      r.json()["data"]["group_reply_mode"] == "always")
client.put("/api/config", json={"group_reply_mode": "mention_only"})

# Automatic AI re-enabling: off by default, validated on the way in, and the
# server-owned keys cannot be written from the panel.
_react = client.get("/api/config").json()["data"]
check("AI reactivation: off by default", _react["ai_reactivation_enabled"] is False)
check("AI reactivation: groups/archived off by default",
      _react["ai_reactivation_include_groups_archived"] is False)
check("AI reactivation: exposes the rule's fields",
      all(f"ai_reactivation_{k}" in _react for k in
          ("unit", "value", "weekdays", "time", "last_sender", "timezone")))
r = client.put("/api/config", json={
    "ai_reactivation_enabled": True, "ai_reactivation_unit": "schedule",
    "ai_reactivation_weekdays": [4, 0, 0, 9], "ai_reactivation_time": "18:30",
    "ai_reactivation_last_sender": "customer", "ai_reactivation_value": "0",
    "ai_reactivation_include_groups_archived": True,
    "ai_reactivation_timezone": "America/Sao_Paulo",
})
check("PUT /api/config (AI reactivation) -> 200", r.status_code == 200)
_react = client.get("/api/config").json()["data"]
check("AI reactivation: values persisted, weekdays cleaned",
      _react["ai_reactivation_enabled"] is True and _react["ai_reactivation_unit"] == "schedule"
      and _react["ai_reactivation_weekdays"] == [0, 4] and _react["ai_reactivation_time"] == "18:30"
      and _react["ai_reactivation_last_sender"] == "customer"
      and _react["ai_reactivation_include_groups_archived"] is True)
check("AI reactivation: value is clamped to at least 1", _react["ai_reactivation_value"] == 1)
client.put("/api/config", json={
    "ai_reactivation_unit": "weeks", "ai_reactivation_time": "25:99",
    "ai_reactivation_last_sender": "robot", "ai_reactivation_enabled": "yes",
    "ai_reactivation_last_run": 123.0,
})
_react = client.get("/api/config").json()["data"]
check("AI reactivation: invalid values are ignored, not stored",
      _react["ai_reactivation_unit"] == "schedule" and _react["ai_reactivation_time"] == "18:30"
      and _react["ai_reactivation_last_sender"] == "customer" and _react["ai_reactivation_enabled"] is True)
check("AI reactivation: last_run is not writable from the panel",
      settings.get("ai_reactivation_last_run") != 123.0)
client.put("/api/config", json={
    "ai_reactivation_enabled": False, "ai_reactivation_unit": "hours",
    "ai_reactivation_include_groups_archived": False,
    "ai_reactivation_weekdays": [0, 1, 2, 3, 4], "ai_reactivation_time": "08:00",
    "ai_reactivation_last_sender": "both", "ai_reactivation_value": 24,
})

# setup_completed flag round-trips through PUT
r = client.put("/api/config", json={"setup_completed": True})
check("PUT /api/config (setup_completed) -> 200", r.status_code == 200)
r = client.get("/api/config")
check("PUT /api/config -> setup_completed persisted", r.json()["data"]["setup_completed"] is True)
client.put("/api/config", json={"setup_completed": False})  # restore

# Test key (will fail since no real API)
r = client.post("/api/config/test-key", json={"api_key": ""})
check("POST /api/config/test-key (empty) -> error", r.json()["ok"] is False)

# ═══════════════════════════════════════════════════════════════════
#  4. Status
# ═══════════════════════════════════════════════════════════════════
section("Status")

r = client.get("/api/status")
check("GET /api/status -> 200", r.status_code == 200)
data = r.json()["data"]
check("GET /api/status -> has connected", "connected" in data)
check("GET /api/status -> has msg_count", "msg_count" in data)
check("GET /api/status -> has auto_reply_running", "auto_reply_running" in data)

# ═══════════════════════════════════════════════════════════════════
#  5. Contacts list
# ═══════════════════════════════════════════════════════════════════
section("Contacts — List")

r = client.get("/api/contacts")
check("GET /api/contacts -> 200", r.status_code == 200)
contacts_data = r.json()["data"]
check("GET /api/contacts -> is list", isinstance(contacts_data, list))
non_archived = [c for c in contacts_data if not c.get("is_archived")]
check("GET /api/contacts -> has non-archived contacts", len(non_archived) >= 1)
check("GET /api/contacts -> exposes avatar_v (cache-busting)", all("avatar_v" in c for c in contacts_data))

# Search
r = client.get("/api/contacts?q=Alice")
check("GET /api/contacts?q=Alice -> finds Alice", len(r.json()["data"]) >= 1)

r = client.get("/api/contacts?q=xyznotexist")
check("GET /api/contacts?q=xyz -> empty", len(r.json()["data"]) == 0)

# Accent-insensitive search: an unaccented query matches accented names (both ways)
_acc = contact_repo.get_or_create("5511999990009")
contact_repo.update(_acc["id"], name="Ótavio Açaí")
r = client.get("/api/contacts?q=otavio")
check("GET /api/contacts?q=otavio -> matches 'Ótavio' (no accent)",
      any(c["phone"] == "5511999990009" for c in r.json()["data"]))
r = client.get("/api/contacts?q=AÇAÍ")
check("GET /api/contacts?q=AÇAÍ -> matches 'Açaí' (accented, any case)",
      any(c["phone"] == "5511999990009" for c in r.json()["data"]))

# Search by message content (normal, private note, transcription)
_msc = contact_repo.get_or_create("5511999990010")
contact_repo.update(_msc["id"], name="Sem Termo No Nome")
_orc = message_repo.add(_msc["id"], "user", "preciso do orçamento atualizado")
message_repo.add(_msc["id"], "private_note", "lembrete: cobrar pagamento amanhã")
message_repo.add(_msc["id"], "transcription", "transcrição: reunião remarcada para quinta")
r = client.get("/api/contacts?q=orcamento")
_hit = next((c for c in r.json()["data"] if c["phone"] == "5511999990010"), None)
check("GET /api/contacts?q=orcamento -> matches by normal message (accent-insensitive)", _hit is not None)
check("GET /api/contacts?q=orcamento -> returns match snippet (original accents)",
      bool(_hit) and "orçamento" in _hit.get("match_snippet", ""))
check("GET /api/contacts?q=orcamento -> returns matched message id",
      bool(_hit) and _hit.get("match_msg_id") == _orc["id"])
r = client.get("/api/contacts?q=cobrar pagamento")
check("GET /api/contacts?q=cobrar pagamento -> matches by private note",
      any(c["phone"] == "5511999990010" for c in r.json()["data"]))
r = client.get("/api/contacts?q=remarcada")
check("GET /api/contacts?q=remarcada -> matches by transcription",
      any(c["phone"] == "5511999990010" for c in r.json()["data"]))
r = client.get("/api/contacts?q=conteudoinexistente123")
check("GET /api/contacts?q=conteudoinexistente123 -> empty",
      not any(c["phone"] == "5511999990010" for c in r.json()["data"]))

# Bot @mention flag: surfaces in the list and clears when the chat is read
_mc = contact_repo.get_or_create("5511999990011")
contact_repo.update(_mc["id"], is_group=1, group_name="Grupo Menção")
contact_repo.set_mention(_mc["id"])
r = client.get("/api/contacts")
_mhit = next((c for c in r.json()["data"] if c["phone"] == "5511999990011"), None)
check("GET /api/contacts -> has_unread_mention exposed", bool(_mhit) and _mhit.get("has_unread_mention") is True)
contact_repo.mark_as_read(_mc["id"])
r = client.get("/api/contacts")
_mhit2 = next((c for c in r.json()["data"] if c["phone"] == "5511999990011"), None)
check("mark_as_read -> mention flag cleared", bool(_mhit2) and _mhit2.get("has_unread_mention") is False)

# Unread-count endpoint (browser-tab badge). Static path must win over /{phone}.
r = client.get("/api/contacts/unread-count")
check("GET /api/contacts/unread-count -> 200", r.status_code == 200)
check("GET /api/contacts/unread-count -> int count", isinstance(r.json()["data"]["count"], int))
_uc = contact_repo.get_or_create("5511999990012")
contact_repo.mark_as_read(_uc["id"])
_before = client.get("/api/contacts/unread-count").json()["data"]["count"]
contact_repo.increment_unread(_uc["id"], "WAMID_UC_1")
_after = client.get("/api/contacts/unread-count").json()["data"]["count"]
check("unread-count -> increments with an unread conversation", _after == _before + 1)
contact_repo.mark_as_read(_uc["id"])
_cleared = client.get("/api/contacts/unread-count").json()["data"]["count"]
check("unread-count -> drops after read", _cleared == _before)

# Archived
r = client.get("/api/contacts?archived=true")
check("GET /api/contacts?archived=true -> has archived", len(r.json()["data"]) >= 1)
archived_names = [c.get("name", "") for c in r.json()["data"]]
check("GET /api/contacts?archived=true -> Bob is archived", any("Bob" in n for n in archived_names))

# Pin / unpin: pinned conversations sort to the top of the list
r = client.post("/api/contacts/5511999990001/pin", json={"pinned": True})
check("POST /pin -> 200", r.status_code == 200)
check("POST /pin -> pinned true", r.json()["data"]["pinned"] is True)
r = client.get("/api/contacts")
_list = r.json()["data"]
check("GET /api/contacts -> pinned contact is first", _list and _list[0]["phone"] == "5511999990001")
check("GET /api/contacts -> exposes is_pinned", _list[0].get("is_pinned") is True)
r = client.post("/api/contacts/5511999990001/pin", json={"pinned": False})
check("POST /pin (unpin) -> 200", r.status_code == 200 and r.json()["data"]["pinned"] is False)
r = client.post("/api/contacts/5511999990001/pin", json={})
check("POST /pin (no field) -> 400", r.status_code == 400)
r = client.post("/api/contacts/5511999999999/pin", json={"pinned": True})
check("POST /pin (unknown contact) -> 404", r.status_code == 404)

# ═══════════════════════════════════════════════════════════════════
#  6. Contact detail
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Detail")

r = client.get("/api/contacts/5511999990001")
check("GET /api/contacts/{phone} -> 200", r.status_code == 200)
data = r.json()["data"]
check("GET /api/contacts/{phone} -> has phone", data.get("phone") == "5511999990001")
check("GET /api/contacts/{phone} -> has name", data.get("name") == "Alice Test")
check("GET /api/contacts/{phone} -> has email", data.get("email") == "alice@test.com")
check("GET /api/contacts/{phone} -> has messages", isinstance(data.get("messages"), list))
check("GET /api/contacts/{phone} -> messages count", len(data["messages"]) == 4)
check("GET /api/contacts/{phone} -> has tags", isinstance(data.get("tags"), list))
check("GET /api/contacts/{phone} -> has observations", isinstance(data.get("info", {}).get("observations"), list))

# Non-existent contact — auto-creates on GET
r = client.get("/api/contacts/0000000000")
check("GET /api/contacts/0000 -> auto-create 200", r.status_code == 200 and r.json()["ok"])

# ═══════════════════════════════════════════════════════════════════
#  7. Contact send message
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Send Message")

r = client.post("/api/contacts/5511999990001/send", json={"message": "Teste manual"})
check("POST /send -> 200", r.status_code == 200)
check("POST /send -> message sent", "enviada" in r.json()["data"]["message"].lower())
check("POST /send -> gowa called", mock_gowa_client.send_message.called)

# Empty message
r = client.post("/api/contacts/5511999990001/send", json={"message": ""})
check("POST /send (empty) -> 400", r.status_code == 400)

# Reply (quote an existing message): reply_to is forwarded to GOWA and persisted
mock_gowa_client.send_message.reset_mock()
r = client.post("/api/contacts/5511999990001/send",
                json={"message": "Respondendo", "reply_to": "WAMID_QUOTE_1"})
check("POST /send (reply_to) -> 200", r.status_code == 200)
_args, _kwargs = mock_gowa_client.send_message.call_args
check("POST /send (reply_to) -> gowa got reply id",
      "WAMID_QUOTE_1" in (list(_args) + list(_kwargs.values())))
_reply_cid = contact_repo.get_full_contact("5511999990001")["id"]
_last = message_repo.get_last(_reply_cid)
check("POST /send (reply_to) -> persisted reply_to_msg_id",
      _last.get("reply_to_msg_id") == "WAMID_QUOTE_1")

# ═══════════════════════════════════════════════════════════════════
#  7a. Send wa.me link to the connected account's own chat
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Send Self Link")

mock_gowa_client.send_message.reset_mock()
r = client.post("/api/contacts/send-self-link", json={"phone": "85973605591"})
check("POST /send-self-link -> 200", r.status_code == 200)
_sl = r.json().get("data") or {}
check("POST /send-self-link -> wa.me link",
      _sl.get("link") == "https://wa.me/5585973605591", _sl.get("link"))
check("POST /send-self-link -> own phone", _sl.get("own_phone") == "5511999990001")
_args, _kwargs = mock_gowa_client.send_message.call_args
check("POST /send-self-link -> sent to own number",
      "5511999990001" in (list(_args) + list(_kwargs.values())))
check("POST /send-self-link -> link in message text",
      any("https://wa.me/5585973605591" in str(a) for a in (list(_args) + list(_kwargs.values()))))
_self_contact = contact_repo.get_full_contact("5511999990001")
check("POST /send-self-link -> saved on own chat",
      "https://wa.me/5585973605591" in (message_repo.get_last(_self_contact["id"]) or {}).get("content", ""))

r = client.post("/api/contacts/send-self-link", json={"phone": "123"})
check("POST /send-self-link (invalid) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  7b. Phone normalization shared by check-phone / send-self-link / lookup
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Check Phone & Lookup")

# check-phone: same normalization as before the shared helper was extracted
mock_gowa_client.check_phone = MagicMock(return_value={"registered": False})
r = client.post("/api/contacts/check-phone", json={"phone": "123"})
check("POST /check-phone (short) -> 400", r.status_code == 400)
check("POST /check-phone (short) -> same error text",
      "Número inválido" in (r.json().get("error") or ""), r.text)
r = client.post("/api/contacts/check-phone", json={"phone": "(11) 99999-0009"})
check("POST /check-phone -> 200", r.status_code == 200)
check("POST /check-phone -> prefixes 55 and strips punctuation",
      mock_gowa_client.check_phone.call_args[0][0] == "5511999990009",
      str(mock_gowa_client.check_phone.call_args))

# lookup: read-only, so it must never materialize a contact
def _contact_total() -> int:
    return (len(contact_repo.list_contacts("", False))
            + len(contact_repo.list_contacts("", True)))

_lk_before = _contact_total()
r = client.get("/api/contacts/lookup", params={"phone": "5521988887777"})
check("GET /lookup (unknown) -> 200", r.status_code == 200)
check("GET /lookup (unknown) -> exists false, contact null",
      r.json().get("data") == {"exists": False, "contact": None}, r.text)
check("GET /lookup (unknown) -> did not create a contact",
      contact_repo.get_by_phone("5521988887777") is None
      and _contact_total() == _lk_before)

_alice_id = contact_repo.get_by_phone("5511999990001")["id"]
r = client.get("/api/contacts/lookup", params={"phone": "5511999990001"})
_lk = r.json().get("data") or {}
check("GET /lookup (known) -> exists true", _lk.get("exists") is True, r.text)
check("GET /lookup (known) -> returns the seeded contact",
      (_lk.get("contact") or {}).get("id") == _alice_id)
# Route order guard: /{phone} would answer with messages/usage in the payload
check("GET /lookup -> static route wins over /{phone}",
      set(_lk) == {"exists", "contact"}, str(sorted(_lk)))

r = client.get("/api/contacts/lookup", params={"phone": "(11) 99999-0001"})
check("GET /lookup (no country code) -> same contact",
      ((r.json().get("data") or {}).get("contact") or {}).get("id") == _alice_id, r.text)

r = client.get("/api/contacts/lookup", params={"phone": "551199990001"})
check("GET /lookup (BR 12-digit variant) -> same contact",
      ((r.json().get("data") or {}).get("contact") or {}).get("id") == _alice_id, r.text)

r = client.get("/api/contacts/lookup", params={"phone": "123"})
check("GET /lookup (short) -> 400", r.status_code == 400)
check("GET /lookup (short) -> ok false", r.json().get("ok") is False)
r = client.get("/api/contacts/lookup")
check("GET /lookup (missing phone) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  8. Contact retry send
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Retry Send")

r = client.post("/api/contacts/5511999990001/retry-send", json={"message": "Retry msg"})
check("POST /retry-send -> 200", r.status_code == 200)

r = client.post("/api/contacts/5511999990001/retry-send", json={"message": ""})
check("POST /retry-send (empty) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  8a. Delete message (scope me / all)
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Delete Message")

_del_cid = contact_repo.get_full_contact("5511999990001")["id"]

# scope=all on own (outgoing) message -> revoke for everyone
message_repo.add(_del_cid, "assistant", "Msg para apagar", msg_id="WAMID_DEL_1", status="operator")
mock_gowa_client.revoke_message.reset_mock()
r = client.post("/api/contacts/5511999990001/messages/delete",
                json={"msg_id": "WAMID_DEL_1", "scope": "all"})
check("POST /messages/delete (all) -> 200", r.status_code == 200)
check("POST /messages/delete (all) -> gowa revoke called", mock_gowa_client.revoke_message.called)
_m = message_repo.get_by_msg_id("WAMID_DEL_1")
check("POST /messages/delete (all) -> flagged revoked", bool(_m) and bool(_m.get("revoked")))
check("POST /messages/delete (all) -> content preserved", bool(_m) and bool(_m.get("content")))
check("POST /messages/delete (all) -> scope all", bool(_m) and _m.get("revoke_scope") == "all")

# scope=all on a contact (user) message -> rejected
message_repo.add(_del_cid, "user", "Msg do contato", msg_id="WAMID_USER_1")
r = client.post("/api/contacts/5511999990001/messages/delete",
                json={"msg_id": "WAMID_USER_1", "scope": "all"})
check("POST /messages/delete (all, user msg) -> 400", r.status_code == 400)

# scope=me by db_id (local message without msg_id) -> row kept, flagged revoked
_local = message_repo.add(_del_cid, "assistant", "Local sem msg_id")
r = client.post("/api/contacts/5511999990001/messages/delete",
                json={"db_id": _local["id"], "scope": "me"})
check("POST /messages/delete (me, db_id) -> 200", r.status_code == 200)
_kept = [m for m in message_repo.get_all(_del_cid) if m.get("_id") == _local["id"]]
check("POST /messages/delete (me) -> row kept", len(_kept) == 1)
check("POST /messages/delete (me) -> flagged revoked", bool(_kept and _kept[0].get("revoked")))
check("POST /messages/delete (me) -> content preserved", bool(_kept and _kept[0].get("content")))
check("POST /messages/delete (me) -> scope me", bool(_kept) and _kept[0].get("revoke_scope") == "me")

# missing identifiers -> 400
r = client.post("/api/contacts/5511999990001/messages/delete", json={"scope": "me"})
check("POST /messages/delete (no id) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  8b. React to message
# ═══════════════════════════════════════════════════════════════════
section("Contacts — React Message")

message_repo.add(_del_cid, "user", "Msg para reagir", msg_id="WAMID_REACT_1")

# Add a reaction
mock_gowa_client.react_to_message.reset_mock()
r = client.post("/api/contacts/5511999990001/messages/react",
                json={"msg_id": "WAMID_REACT_1", "emoji": "👍"})
check("POST /messages/react -> 200", r.status_code == 200)
check("POST /messages/react -> gowa called", mock_gowa_client.react_to_message.called)
check("POST /messages/react -> reactions in response", r.json()["data"]["reactions"].get("👍") == ["me"])
_m = message_repo.get_by_msg_id("WAMID_REACT_1")
check("POST /messages/react -> persisted", _m.get("reactions", {}).get("👍") == ["me"])

# Change reaction (replaces, one per reactor)
r = client.post("/api/contacts/5511999990001/messages/react",
                json={"msg_id": "WAMID_REACT_1", "emoji": "❤️"})
_m = message_repo.get_by_msg_id("WAMID_REACT_1")
check("POST /messages/react (change) -> replaced", "👍" not in _m.get("reactions", {}) and _m["reactions"].get("❤️") == ["me"])

# Remove reaction (empty emoji)
r = client.post("/api/contacts/5511999990001/messages/react",
                json={"msg_id": "WAMID_REACT_1", "emoji": ""})
_m = message_repo.get_by_msg_id("WAMID_REACT_1")
check("POST /messages/react (remove) -> cleared", not _m.get("reactions"))

# Missing msg_id -> 400
r = client.post("/api/contacts/5511999990001/messages/react", json={"emoji": "👍"})
check("POST /messages/react (no msg_id) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  8b. Private message (panel-only) — no AI trigger
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Private Message")

_gowa_calls_before = mock_gowa_client.send_message.call_count
r = client.post(
    "/api/contacts/5511999990001/private-message",
    json={"text": "Cliente VIP, atender com prioridade"},
)
check("POST /private-message -> 200", r.status_code == 200)
_pn = r.json()["data"]
check("POST /private-message -> saved note returned",
      _pn.get("role") == "private_note" and "prioridade" in (_pn.get("content") or ""))
check("POST /private-message -> returns DB id (for delete)", bool(_pn.get("_id")))
check(
    "POST /private-message -> no GOWA send",
    mock_gowa_client.send_message.call_count == _gowa_calls_before,
)

# Confirm the message is in the contact's history with role=private_note
r = client.get("/api/contacts/5511999990001")
msgs = r.json()["data"]["messages"]
private_notes = [m for m in msgs if m.get("role") == "private_note"]
check("GET /api/contacts -> private_note present", len(private_notes) >= 1)
check(
    "GET /api/contacts -> private_note status is null",
    private_notes and private_notes[-1].get("status") in (None, ""),
)

# Private notes are deletable by DB id (scope=me) without a msg_id -> kept, flagged revoked
r = client.post("/api/contacts/5511999990001/messages/delete",
                json={"db_id": _pn["_id"], "scope": "me"})
check("POST /messages/delete (private note, db_id) -> 200", r.status_code == 200)
r = client.get("/api/contacts/5511999990001")
_remaining = [m for m in r.json()["data"]["messages"] if m.get("_id") == _pn["_id"]]
check("POST /messages/delete (private note) -> row kept", len(_remaining) == 1)
check("POST /messages/delete (private note) -> flagged revoked", bool(_remaining and _remaining[0].get("revoked")))
check(
    "GET /api/contacts -> private_note content matches",
    private_notes and "VIP" in private_notes[-1]["content"],
)

# Empty payload
r = client.post("/api/contacts/5511999990001/private-message", json={"text": ""})
check("POST /private-message (empty) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  9. Contact send image
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Send Image")

# Create a fake PNG (1x1 pixel)
fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
r = client.post(
    "/api/contacts/5511999990001/send-image",
    files={"image": ("test.png", io.BytesIO(fake_png), "image/png")},
    data={"caption": "Test caption"},
)
check("POST /send-image -> 200", r.status_code == 200)
check("POST /send-image -> gowa called", mock_gowa_client.send_image.called)

# ═══════════════════════════════════════════════════════════════════
#  10. Contact send audio
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Send Audio")

fake_ogg = b"OggS" + b"\x00" * 100
r = client.post(
    "/api/contacts/5511999990001/send-audio",
    files={"audio": ("voice.ogg", io.BytesIO(fake_ogg), "audio/ogg")},
)
check("POST /send-audio -> 200", r.status_code == 200)
check("POST /send-audio -> gowa called", mock_gowa_client.send_audio.called)

# ═══════════════════════════════════════════════════════════════════
#  10b. Contact send document
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Send Document")

fake_pdf = b"%PDF-1.4\n" + b"\x00" * 100
r = client.post(
    "/api/contacts/5511999990001/send-document",
    files={"document": ("relatorio.pdf", io.BytesIO(fake_pdf), "application/pdf")},
    data={"caption": "Doc caption"},
)
check("POST /send-document -> 200", r.status_code == 200)
check("POST /send-document -> gowa called", mock_gowa_client.send_file.called)

# ═══════════════════════════════════════════════════════════════════
#  11. Contact presence
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Presence")

r = client.post("/api/contacts/5511999990001/presence", json={"action": "start"})
check("POST /presence -> 200", r.status_code == 200)
check("POST /presence -> gowa called", mock_gowa_client.send_chat_presence.called)

# ═══════════════════════════════════════════════════════════════════
#  12. Contact mark read
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Mark Read")

r = client.post("/api/contacts/5511999990002/read")
check("POST /read -> 200", r.status_code == 200)

# ═══════════════════════════════════════════════════════════════════
#  13. Contact toggle AI
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Toggle AI")

r = client.post("/api/contacts/5511999990001/toggle-ai", json={"enabled": False})
check("POST /toggle-ai -> 200", r.status_code == 200)
check("POST /toggle-ai -> disabled", r.json()["data"]["ai_enabled"] is False)

r = client.post("/api/contacts/5511999990001/toggle-ai", json={"enabled": True})
check("POST /toggle-ai -> re-enabled", r.json()["data"]["ai_enabled"] is True)

r = client.post("/api/contacts/5511999990001/toggle-ai", json={})
check("POST /toggle-ai (no field) -> 400", r.status_code == 400)

# ═══════════════════════════════════════════════════════════════════
#  14. Contact update info
# ═══════════════════════════════════════════════════════════════════
section("Contacts — Update Info")

r = client.put("/api/contacts/5511999990001/info", json={
    "name": "Alice Updated",
    "email": "alice_new@test.com",
    "profession": "Senior Engineer",
    "company": "NewCo",
    "observations": ["VIP client", "Prefers morning calls"],
})
check("PUT /info -> 200", r.status_code == 200)
info = r.json()["data"]
check("PUT /info -> name updated", info.get("name") == "Alice Updated")
check("PUT /info -> email updated", info.get("email") == "alice_new@test.com")

# Verify persistence
r = client.get("/api/contacts/5511999990001")
data = r.json()["data"]
check("PUT /info -> persisted name", data["name"] == "Alice Updated")
check("PUT /info -> persisted observations", len(data.get("info", {}).get("observations", [])) == 2)

# ═══════════════════════════════════════════════════════════════════
#  15. Tags
# ═══════════════════════════════════════════════════════════════════
section("Tags")

r = client.get("/api/tags")
check("GET /api/tags -> 200", r.status_code == 200)
tags = r.json()["data"]
check("GET /api/tags -> is dict", isinstance(tags, dict))
check("GET /api/tags -> has vip", "vip" in tags)
check("GET /api/tags -> has lead", "lead" in tags)

# Create tag
r = client.post("/api/tags", json={"name": "hot", "color": "#ff6600"})
check("POST /api/tags -> 200", r.status_code == 200)
check("POST /api/tags -> created", r.json()["data"]["name"] == "hot")

# Duplicate tag
r = client.post("/api/tags", json={"name": "hot", "color": "#ff6600"})
check("POST /api/tags (dup) -> 400", r.status_code == 400)

# Update tag
r = client.put("/api/tags/hot", json={"name": "super_hot", "color": "#ff0066"})
check("PUT /api/tags/{name} -> 200", r.status_code == 200)
check("PUT /api/tags -> renamed", r.json()["data"]["name"] == "super_hot")

# Update non-existent
r = client.put("/api/tags/nonexist", json={"color": "#000"})
check("PUT /api/tags (404) -> 404", r.status_code == 404)

# Delete tag
r = client.delete("/api/tags/super_hot")
check("DELETE /api/tags -> 200", r.status_code == 200)

r = client.delete("/api/tags/super_hot")
check("DELETE /api/tags (404) -> 404", r.status_code == 404)

# Set contact tags
r = client.put("/api/contacts/5511999990001/tags", json={"tags": ["vip", "lead"]})
check("PUT /contacts/{phone}/tags -> 200", r.status_code == 200)
check("PUT /contacts/{phone}/tags -> set", set(r.json()["data"]["tags"]) == {"vip", "lead"})

# Non-existent contact (use different number not auto-created elsewhere)
r = client.put("/api/contacts/9999999999/tags", json={"tags": ["vip"]})
check("PUT /contacts/9999/tags -> 404", r.status_code == 404)

# ═══════════════════════════════════════════════════════════════════
#  16. Usage
# ═══════════════════════════════════════════════════════════════════
section("Usage")

r = client.get("/api/usage/summary")
check("GET /usage/summary -> 200", r.status_code == 200)
data = r.json()["data"]
check("GET /usage/summary -> has total_tokens", "total_tokens" in data)
check("GET /usage/summary -> tokens > 0", data.get("total_tokens", 0) > 0)

r = client.get("/api/usage/summary?period=24h")
check("GET /usage/summary?period=24h -> 200", r.status_code == 200)
check("GET /usage/summary -> has period_start", r.json()["data"].get("period_start") is not None)

r = client.get("/api/usage/by-contact")
check("GET /usage/by-contact -> 200", r.status_code == 200)
by_contact = r.json()["data"]
check("GET /usage/by-contact -> is list", isinstance(by_contact, list))
check("GET /usage/by-contact -> has entries", len(by_contact) >= 1)

r = client.get("/api/usage/contact/5511999990001")
check("GET /usage/contact/{phone} -> 200", r.status_code == 200)
detail = r.json()["data"]
check("GET /usage/contact -> is list", isinstance(detail, list))
check("GET /usage/contact -> has records", len(detail) >= 2)

r = client.get("/api/usage/contact/0000000000")
check("GET /usage/contact/0000 -> empty", r.json()["data"] == [])

# ═══════════════════════════════════════════════════════════════════
#  17. Logs
# ═══════════════════════════════════════════════════════════════════
section("Logs")

r = client.get("/api/logs")
check("GET /api/logs -> 200", r.status_code == 200)
check("GET /api/logs -> is list", isinstance(r.json()["data"], list))

r = client.get("/api/logs?limit=5")
check("GET /api/logs?limit=5 -> 200", r.status_code == 200)

r = client.delete("/api/logs")
check("DELETE /api/logs -> 200", r.status_code == 200)

# ═══════════════════════════════════════════════════════════════════
#  18. Webhook Payloads
# ═══════════════════════════════════════════════════════════════════
section("Webhook Payloads")

r = client.get("/api/webhook-payloads")
check("GET /api/webhook-payloads -> 200", r.status_code == 200)
check("GET /api/webhook-payloads -> is list", isinstance(r.json()["data"], list))

# ═══════════════════════════════════════════════════════════════════
#  19. Webhook (incoming message simulation)
# ═══════════════════════════════════════════════════════════════════
section("Webhook")

# Presence event
r = client.post("/api/webhook", json={
    "type": "chat_presence",
    "data": [{"from": "5511999990001@s.whatsapp.net", "state": "composing"}],
})
check("POST /webhook (presence) -> 200", r.status_code == 200)

# is_from_me echo (should be ignored)
r = client.post("/api/webhook", json={
    "body": "echo test",
    "from": "5511999990001@s.whatsapp.net",
    "id": "echo_001",
    "is_from_me": True,
})
check("POST /webhook (echo) -> 200", r.status_code == 200)

# message.ack event
r = client.post("/api/webhook", json={
    "type": "message.ack",
    "data": [{"id": "msg_001", "chat_jid": "5511999990002@s.whatsapp.net", "ack": 3}],
})
check("POST /webhook (ack) -> 200", r.status_code == 200)

# Reply-quote extraction from inbound payloads (GOWA nests this inconsistently).
from server.routes.webhook import _extract_reply_to as _ext_reply
check("reply extract: flat replied_id", _ext_reply({"replied_id": "Q1"}) == "Q1")
check("reply extract: nested message.replied_id",
      _ext_reply({"message": {"text": "oi", "id": "SELF", "replied_id": "Q2"}}) == "Q2")
check("reply extract: context_info.stanza_id",
      _ext_reply({"context_info": {"stanza_id": "Q3"}}) == "Q3")
check("reply extract: quoted_message object id",
      _ext_reply({"quoted_message": {"id": "Q4"}}) == "Q4")
check("reply extract: own id is NOT a reply", _ext_reply({"id": "SELF", "body": "oi"}) is None)
check("reply extract: quoted text is NOT a reply id",
      _ext_reply({"quoted_message": "texto", "body": "oi"}) is None)

# ═══════════════════════════════════════════════════════════════════
#  20. QR / WhatsApp
# ═══════════════════════════════════════════════════════════════════
section("WhatsApp / QR")

r = client.get("/api/qr")
check("GET /api/qr -> 204 (no qr)", r.status_code == 204)

r = client.post("/api/qr/refresh")
check("POST /api/qr/refresh -> 200", r.status_code == 200)

r = client.post("/api/whatsapp/reconnect")
check("POST /whatsapp/reconnect -> 200", r.status_code == 200)
check("POST /whatsapp/reconnect -> gowa called", mock_gowa_client.reconnect.called)

r = client.post("/api/whatsapp/logout")
check("POST /whatsapp/logout -> 200", r.status_code == 200)
check("POST /whatsapp/logout -> gowa called", mock_gowa_client.logout.called)

# ═══════════════════════════════════════════════════════════════════
#  20b. Setup Wizard
# ═══════════════════════════════════════════════════════════════════
section("Setup Wizard")


class _FakeTechifyResp:
    """Stands in for an httpx Response from the Techify provisioning route."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_async_client(resp):
    """Build a patch target mimicking httpx.AsyncClient as an async CM.

    Answers both verbs with the same body: ``GET /service_number`` (the
    provisioning target) and ``POST /request-apikey`` (the key polling).
    """
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=resp)
    fake_client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


# The provisioning target (number + phrase) comes from Techify's
# /service_number. Pinning it keeps this block off the network — otherwise the
# assertions below would depend on the endpoint being reachable from wherever
# the suite runs.
_service_number = _fake_async_client(_FakeTechifyResp(200, {
    "ok": True, "phone": "5599000000001", "message": "Frase publicada no endpoint"}))

# request-key: sends the provisioning WhatsApp message and arms polling
_send_calls_before = mock_gowa_client.send_message.call_count
with patch("server.routes.setup.httpx.AsyncClient", _service_number):
    r = client.post("/api/setup/request-key")
check("POST /api/setup/request-key -> 200", r.status_code == 200)
check("POST /api/setup/request-key -> returns number",
      r.json()["data"].get("number") == "5511999990001")
check("POST /api/setup/request-key -> WhatsApp message sent",
      mock_gowa_client.send_message.call_count == _send_calls_before + 1)
check("POST /api/setup/request-key -> sends the endpoint's pair (number + phrase)",
      mock_gowa_client.send_message.call_args[0][:2]
      == ("5599000000001", "Frase publicada no endpoint"),
      detail=str(mock_gowa_client.send_message.call_args))

# Both provisioning seams abort the send: an empty destination would fire the
# phrase at a number nobody chose, and an empty phrase would burn the single
# conversation opening WhatsApp grants with a brand-new contact.
from plugins import events as _bus  # noqa: E402

for _seam, _label in (("filter.provisioning.number", "sem destino"),
                      ("filter.provisioning.message", "sem frase")):
    _bus.register_filter("test_setup_abort", _seam, lambda ctx, value: None)
    _send_calls_before = mock_gowa_client.send_message.call_count
    with patch("server.routes.setup.httpx.AsyncClient", _service_number):
        r = client.post("/api/setup/request-key")
    _bus._filters.pop(_seam, None)
    check(f"POST /api/setup/request-key ({_label}) -> erro acionável",
          r.status_code == 400 and not r.json()["ok"] and r.json().get("error"),
          detail=f"{r.status_code} {r.json()}")
    check(f"POST /api/setup/request-key ({_label}) -> nada é enviado",
          mock_gowa_client.send_message.call_count == _send_calls_before)

# Re-arm the polling the abort cases refused to arm, so key-status can run.
with patch("server.routes.setup.httpx.AsyncClient", _service_number):
    client.post("/api/setup/request-key")

# key-status: account not ready yet
with patch("server.routes.setup.httpx.AsyncClient",
           _fake_async_client(_FakeTechifyResp(200, {"status": "pending"}))):
    r = client.get("/api/setup/key-status")
check("GET /api/setup/key-status (pending) -> 200", r.status_code == 200)
check("GET /api/setup/key-status -> pending", r.json()["data"]["status"] == "pending")

# key-status: key ready -> persisted to config
_provisioned_key = "sk-techify-provisioned-abcdef123456"
with patch("server.routes.setup.httpx.AsyncClient",
           _fake_async_client(_FakeTechifyResp(200, {
               "status": "ready", "api_key": _provisioned_key}))):
    r = client.get("/api/setup/key-status")
check("GET /api/setup/key-status (ready) -> 200", r.status_code == 200)
check("GET /api/setup/key-status -> ready", r.json()["data"]["status"] == "ready")
check("GET /api/setup/key-status -> key saved to config",
      config_repo.get("openrouter_api_key") == _provisioned_key)

# ═══════════════════════════════════════════════════════════════════
#  21. Sandbox
# ═══════════════════════════════════════════════════════════════════
section("Sandbox")

# sandbox/send requires a working LLM — mock it
with patch.object(agent_handler, "process_message") as mock_process:
    from agent.handler import ProcessResult
    mock_process.return_value = ProcessResult(reply="Resposta de teste", tool_calls=[])

    r = client.post("/api/sandbox/send", json={"phone": "sandbox_test", "message": "Oi"})
    check("POST /sandbox/send -> 200", r.status_code == 200)
    check("POST /sandbox/send -> has reply", "reply" in r.json().get("data", {}))
    check("POST /sandbox/send -> reply text", r.json()["data"]["reply"] == "Resposta de teste")

r = client.post("/api/sandbox/send", json={"phone": "", "message": "Oi"})
check("POST /sandbox/send (no phone) -> 400", r.status_code == 400)

r = client.post("/api/sandbox/send", json={"phone": "test", "message": ""})
check("POST /sandbox/send (no msg) -> 400", r.status_code == 400)

r = client.post("/api/sandbox/clear", json={"phone": "sandbox_test"})
check("POST /sandbox/clear -> 200", r.status_code == 200)

r = client.post("/api/sandbox/clear", json={})
check("POST /sandbox/clear (all) -> 200", r.status_code == 200)

# ═══════════════════════════════════════════════════════════════════
#  22. Frontend routes (SPA)
# ═══════════════════════════════════════════════════════════════════
section("Frontend SPA Routes")

for path in ["/", "/painel", "/sandbox", "/costs", "/arquivos"]:
    r = client.get(path)
    check(f"GET {path} -> 200", r.status_code == 200)

# ═══════════════════════════════════════════════════════════════════
#  23. GOWA version / update
# ═══════════════════════════════════════════════════════════════════
section("GOWA — Version & Update")

from gowa import binary as _gowa_binary  # noqa: E402
from gowa import updater as _gowa_updater  # noqa: E402

# Sandbox the binary resolution: without this, POST /gowa/rollback below would
# uninstall the GOWA the developer actually has in storages/bin.
_gowa_fake_root = Path(tempfile.mkdtemp(prefix="whatsbot_test_gowa_"))
(_gowa_fake_root / "bin").mkdir()
(_gowa_fake_root / "storages").mkdir()
# GOWA_VERSION = recommended by the release; the installed version is the file
# next to the binary (they differ after a self-update, which preserves bin/).
(_gowa_fake_root / "GOWA_VERSION").write_text("9.6.0\n")
(_gowa_fake_root / "bin" / (_gowa_binary.binary_name() + ".version")).write_text("8.8.0\n")
_fake_binary = _gowa_fake_root / "bin" / _gowa_binary.binary_name()
_fake_binary.write_bytes(b"#!/bin/sh\nexit 0\n")
_fake_binary.chmod(0o755)
_orig_repo_root = _gowa_binary.repo_root
_gowa_binary.repo_root = lambda: _gowa_fake_root

# Never hit GitHub from the tests: stub the release lookup.
_FAKE_RELEASE = {
    "version": "9.1.0",
    "published_at": "2026-08-15T19:35:00Z",
    "notes": "fake",
    "url": "https://example.invalid/release",
    "cached": False,
    "rate_limited": False,
}
_orig_fetch = _gowa_updater.fetch_latest_release
_gowa_updater.fetch_latest_release = lambda force=False: dict(_FAKE_RELEASE)
_orig_interval = _gowa_updater.fetch_release_interval
_orig_compare = _gowa_updater.fetch_compare_commits
_orig_whatsmeow_compare = _gowa_updater.fetch_whatsmeow_commits
_gowa_updater.fetch_release_interval = lambda current, latest, force=False: [{
    "version": "9.1.0",
    "published_at": _FAKE_RELEASE["published_at"],
    "notes": "Latest WhatsApp Web protocol support.",
    "url": _FAKE_RELEASE["url"],
}]
_gowa_updater.fetch_compare_commits = lambda current, latest, force=False: []
_gowa_updater.fetch_whatsmeow_commits = lambda current, latest, force=False: []

from gowa import release_analyzer as _gowa_release_analyzer  # noqa: E402
_orig_release_llm = _gowa_release_analyzer._call_llm
_gowa_release_analyzer._call_llm = lambda *_args, **_kwargs: None

r = client.get("/api/gowa/version")
check("GET /gowa/version -> 200", r.status_code == 200)
_gv = r.json().get("data", {})
check("GET /gowa/version -> installed_version", bool(_gv.get("installed_version")))
check("GET /gowa/version -> source", _gv.get("source") in ("bundled", "managed", "env"))
check("GET /gowa/version -> supported_range", _gv.get("supported_range") == _gowa_updater.GOWA_SUPPORTED_RANGE)
check("GET /gowa/version -> instalada vem do arquivo ao lado do binário (8.8.0)",
      _gv.get("installed_version") == "8.8.0", str(_gv.get("installed_version")))
check("GET /gowa/version -> recommended_version (GOWA_VERSION)",
      _gv.get("recommended_version") == "9.6.0")
check("GET /gowa/version -> installed_version_known", _gv.get("installed_version_known") is True)
check("GET /gowa/version -> recommended_update_available",
      _gv.get("recommended_update_available") is True)
check("GET /gowa/version -> recommended_supported", _gv.get("recommended_supported") is True)
check("GET /gowa/version -> recommended_skipped inicia falso",
      _gv.get("recommended_skipped") is False)

# The recommendation must come from local files only: break GitHub and the
# release analyzer and the route has to answer the same, instantly.
def _boom(*_a, **_k):
    raise AssertionError("a rota /gowa/version não pode usar rede nem o analisador")

_saved_net = (_gowa_updater.fetch_latest_release, _gowa_updater.fetch_release_interval,
              _gowa_updater.fetch_compare_commits, _gowa_updater.fetch_whatsmeow_commits,
              _gowa_release_analyzer._call_llm, _gowa_updater.urllib.request.urlopen)
_gowa_updater.fetch_latest_release = _boom
_gowa_updater.fetch_release_interval = _boom
_gowa_updater.fetch_compare_commits = _boom
_gowa_updater.fetch_whatsmeow_commits = _boom
_gowa_release_analyzer._call_llm = _boom
_gowa_updater.urllib.request.urlopen = _boom
try:
    _t0 = time.monotonic()
    r = client.get("/api/gowa/version")
    _elapsed = time.monotonic() - _t0
finally:
    (_gowa_updater.fetch_latest_release, _gowa_updater.fetch_release_interval,
     _gowa_updater.fetch_compare_commits, _gowa_updater.fetch_whatsmeow_commits,
     _gowa_release_analyzer._call_llm, _gowa_updater.urllib.request.urlopen) = _saved_net
check("GET /gowa/version sem GitHub e sem LLM -> 200", r.status_code == 200, r.text[:200])
check("GET /gowa/version sem GitHub e sem LLM -> recomendada disponível",
      r.json().get("data", {}).get("recommended_update_available") is True)
check("GET /gowa/version responde rápido (sem esperar rede/LLM)", _elapsed < 2.0, f"{_elapsed:.2f}s")

# "Agora não": persisted in the database, and it only mutes THAT version.
r = client.post("/api/gowa/skip-recommended", json={"version": ""})
check("POST /gowa/skip-recommended (vazio) usa a recomendada -> 200", r.status_code == 200)
check("POST /gowa/skip-recommended -> persistido no banco",
      config_repo.get("gowa_recommended_skipped_version") == "9.6.0")
_gv = client.get("/api/gowa/version").json()["data"]
check("GET /gowa/version -> recommended_skipped após 'Agora não'", _gv.get("recommended_skipped") is True)
check("GET /gowa/version -> adiar não esconde a atualização",
      _gv.get("recommended_update_available") is True)
check("'Agora não' não toca em gowa_skipped_version (GitHub)",
      not config_repo.get("gowa_skipped_version", ""))

(_gowa_fake_root / "GOWA_VERSION").write_text("9.7.0\n")
_gv = client.get("/api/gowa/version").json()["data"]
check("GOWA_VERSION muda -> volta a perguntar",
      _gv.get("recommended_version") == "9.7.0" and _gv.get("recommended_skipped") is False)
(_gowa_fake_root / "GOWA_VERSION").write_text("9.6.0\n")

r = client.post("/api/gowa/skip-recommended", json={"version": "../../etc/passwd"})
check("POST /gowa/skip-recommended (versão inválida) -> 400", r.status_code == 400)
client.post("/api/gowa/skip-recommended", json={"version": "9.6.0"})
client.put("/api/config", json={"gowa_recommended_skipped_version": "0.0.1"})
check("gowa_recommended_skipped_version não é gravável por PUT /config",
      config_repo.get("gowa_recommended_skipped_version") == "9.6.0")

# Binary nobody can identify: the panel says so instead of inventing a number.
_sidecar = _gowa_fake_root / "bin" / (_gowa_binary.binary_name() + ".version")
_sidecar.unlink()
_gv = client.get("/api/gowa/version").json()["data"]
check("binário sem identificação -> installed_version vazia", _gv.get("installed_version") == "")
check("binário sem identificação -> installed_version_known falso",
      _gv.get("installed_version_known") is False)
check("binário sem identificação -> recomendação disponível",
      _gv.get("recommended_update_available") is True)
_gp = client.get("/api/gowa/proxy").json()["data"]
check("proxy com versão desconhecida -> supported falso", _gp.get("supported") is False)
check("proxy expõe installed_version_known", _gp.get("installed_version_known") is False)
_sidecar.write_text("8.8.0\n")

r = client.get("/api/gowa/update/check")
check("GET /gowa/update/check -> 200", r.status_code == 200)
_gc = r.json().get("data", {})
check("GET /gowa/update/check -> latest_version", _gc.get("latest_version") == "9.1.0")
check("GET /gowa/update/check -> update_available", _gc.get("update_available") is True)
check("GET /gowa/update/check -> latest_supported", _gc.get("latest_supported") is True)
check("GET /gowa/update/check -> compatibilidade recomendada",
      _gc.get("whatsapp_update_recommended") is True)

# Out-of-range version must ask for confirmation instead of installing.
r = client.post("/api/gowa/update", json={"version": "10.5.0"})
check("POST /gowa/update (unsupported) -> 200", r.status_code == 200)
_gu = r.json().get("data", {})
check("POST /gowa/update (unsupported) -> requires_confirmation",
      _gu.get("requires_confirmation") is True)

# A malformed version must never reach the download URL builder.
r = client.post("/api/gowa/update", json={"version": "../../etc/passwd"})
check("POST /gowa/update (versão inválida) -> 400", r.status_code == 400)

# Rollback with nothing to restore.
r = client.post("/api/gowa/rollback")
check("POST /gowa/rollback (sem backup) -> 400", r.status_code == 400)

r = client.post("/api/gowa/skip-version", json={"version": "9.1.0"})
check("POST /gowa/skip-version -> 200", r.status_code == 200)
check("POST /gowa/skip-version -> persisted",
      client.get("/api/config").json()["data"].get("gowa_skipped_version") == "9.1.0")

r = client.post("/api/gowa/skip-version", json={"version": ""})
check("POST /gowa/skip-version (vazio) -> 400", r.status_code == 400)

# The auto-check toggle must survive PUT /api/config (allowlist regression).
client.put("/api/config", json={"gowa_auto_check_enabled": False})
check("PUT /config gowa_auto_check_enabled -> persisted",
      client.get("/api/config").json()["data"].get("gowa_auto_check_enabled") is False)
client.put("/api/config", json={"gowa_auto_check_enabled": True})

_gowa_updater.fetch_latest_release = _orig_fetch
_gowa_updater.fetch_release_interval = _orig_interval
_gowa_updater.fetch_compare_commits = _orig_compare
_gowa_updater.fetch_whatsmeow_commits = _orig_whatsmeow_compare
_gowa_release_analyzer._call_llm = _orig_release_llm


# ═══════════════════════════════════════════════════════════════════
#  24. GOWA proxy
# ═══════════════════════════════════════════════════════════════════
section("GOWA — Proxy")

import socket as _socket  # noqa: E402
import threading as _threading  # noqa: E402

r = client.get("/api/gowa/proxy")
check("GET /gowa/proxy -> 200", r.status_code == 200)
_pp = r.json().get("data", {})
check("GET /gowa/proxy -> desativado por padrão", _pp.get("enabled") is False)
check("GET /gowa/proxy -> min_version", _pp.get("min_version") == "8.11.0")
check("GET /gowa/proxy -> schemes", "socks5" in (_pp.get("schemes") or []))
check("GET /gowa/proxy -> GOWA 8.8.0 não suporta proxy", _pp.get("supported") is False)

# Config inválida não é salva.
r = client.put("/api/gowa/proxy", json={"enabled": True, "mode": "fields", "host": ""})
check("PUT /gowa/proxy (host vazio) -> 400", r.status_code == 400)
r = client.put("/api/gowa/proxy", json={"enabled": True, "mode": "url", "url": "1.2.3.4:1080"})
check("PUT /gowa/proxy (URL sem esquema) -> 400", r.status_code == 400)

mock_gowa_manager.restart.reset_mock()
r = client.put("/api/gowa/proxy", json={
    "enabled": True, "mode": "fields", "scheme": "socks5",
    "host": "10.0.0.9", "port": 1080, "username": "user", "password": "segredo",
})
check("PUT /gowa/proxy -> 200", r.status_code == 200)
_pp = r.json().get("data", {})
check("PUT /gowa/proxy -> salvo", _pp.get("enabled") is True and _pp.get("host") == "10.0.0.9")
check("PUT /gowa/proxy -> senha nunca volta em claro", _pp.get("password") == "***")
check("PUT /gowa/proxy -> has_password", _pp.get("has_password") is True)
check("PUT /gowa/proxy -> URL efetiva mascarada",
      _pp.get("effective_url") == "socks5://user:***@10.0.0.9:1080", str(_pp.get("effective_url")))
check("PUT /gowa/proxy -> reiniciou o GOWA", mock_gowa_manager.restart.called)

from gowa import proxy as _gowa_proxy  # noqa: E402

check("proxy aplicado ao subprocesso",
      _gowa_proxy.current_url() == "socks5://user:segredo@10.0.0.9:1080", _gowa_proxy.current_url())

# Reenviar a senha mascarada preserva a senha salva e não reinicia à toa.
mock_gowa_manager.restart.reset_mock()
r = client.put("/api/gowa/proxy", json={
    "enabled": True, "mode": "fields", "scheme": "socks5",
    "host": "10.0.0.9", "port": 1080, "username": "user", "password": "***",
})
check("PUT /gowa/proxy (senha mascarada) -> senha preservada",
      _gowa_proxy.current_url() == "socks5://user:segredo@10.0.0.9:1080", _gowa_proxy.current_url())
check("PUT /gowa/proxy sem mudança -> não reinicia", not mock_gowa_manager.restart.called)

# Teste de conectividade contra um SOCKS5 falso local.
def _fake_socks5():
    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            _, count = conn.recv(2)
            conn.recv(count)
            conn.sendall(b"\x05\x00")
            head = conn.recv(4)
            if head and head[3] == 0x03:
                length = conn.recv(1)[0]
                conn.recv(length + 2)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass
        finally:
            conn.close()

    _threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


_srv, _port = _fake_socks5()
r = client.post("/api/gowa/proxy/test", json={
    "enabled": True, "mode": "fields", "scheme": "socks5",
    "host": "127.0.0.1", "port": _port, "username": "", "password": "",
})
check("POST /gowa/proxy/test -> 200", r.status_code == 200)
_pt = r.json().get("data", {})
check("POST /gowa/proxy/test -> ok", _pt.get("ok") is True, str(_pt.get("message")))
_srv.close()

r = client.post("/api/gowa/proxy/test", json={"enabled": True, "mode": "url", "url": "ftp://x:1"})
check("POST /gowa/proxy/test (URL inválida) -> 400", r.status_code == 400)

# Desativar limpa o proxy do subprocesso.
mock_gowa_manager.restart.reset_mock()
r = client.put("/api/gowa/proxy", json={"enabled": False})
check("PUT /gowa/proxy (desativar) -> 200", r.status_code == 200)
check("desativado -> sem proxy no subprocesso", _gowa_proxy.current_url() == "",
      _gowa_proxy.current_url())
check("desativar reinicia o GOWA", mock_gowa_manager.restart.called)

# As chaves do proxy não são graváveis pelo PUT /api/config genérico.
client.put("/api/config", json={"gowa_proxy_enabled": True, "gowa_proxy_host": "6.6.6.6"})
check("PUT /config não escreve config de proxy",
      client.get("/api/gowa/proxy").json()["data"].get("enabled") is False)

_gowa_binary.repo_root = _orig_repo_root
shutil.rmtree(_gowa_fake_root, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
#  25. Auth with password
# ═══════════════════════════════════════════════════════════════════
section("Auth — With Password")

# Set a password
r = client.put("/api/config", json={"web_password": "mysecret123"})
check("SET password -> 200", r.status_code == 200)

# Now auth should be required
r = client.get("/api/auth/check")
check("GET /auth/check (no token) -> 401", r.status_code == 401)

# Login
r = client.post("/api/auth/login", json={"password": "mysecret123"})
check("POST /auth/login -> 200", r.status_code == 200)
token = r.json()["data"]["token"]
check("POST /auth/login -> has token", len(token) > 0)

# Check with token
r = client.get("/api/auth/check", headers={"Authorization": f"Bearer {token}"})
check("GET /auth/check (valid token) -> 200", r.status_code == 200)
check("GET /auth/check -> authenticated", r.json()["data"]["authenticated"] is True)

# Wrong password
r = client.post("/api/auth/login", json={"password": "wrong"})
check("POST /auth/login (wrong) -> 401", r.status_code == 401)

# API endpoint without auth (should be blocked)
r = client.get("/api/config")
check("GET /api/config (no auth) -> 401", r.status_code == 401)

# API endpoint with auth
r = client.get("/api/config", headers={"Authorization": f"Bearer {token}"})
check("GET /api/config (with auth) -> 200", r.status_code == 200)

# Webhook should be exempt from auth
r = client.post("/api/webhook", json={"type": "unknown"})
check("POST /webhook (auth exempt) -> 200", r.status_code == 200)

# Health should be exempt
r = client.get("/health")
check("GET /health (auth exempt) -> 200", r.status_code == 200)

# Remove password to not affect other tests
r = client.put("/api/config", json={"web_password": ""}, headers={"Authorization": f"Bearer {token}"})
check("REMOVE password -> 200", r.status_code == 200)

# ═══════════════════════════════════════════════════════════════════
#  Media auto-download policy + file manager
# ═══════════════════════════════════════════════════════════════════
section("Media policy (config)")
r = client.get("/api/config")
_pol = r.json()["data"]["media_autodownload"]
check("config exposes the lean default matrix",
      _pol["audio"] == {"individual": True, "group": True}
      and _pol["image"] == {"individual": False, "group": False}
      and _pol["video"]["group"] is False and _pol["document"]["individual"] is False)
check("sticker has no row of its own", "sticker" not in _pol)
r = client.put("/api/config", json={"media_autodownload": {"image": {"group": True}, "zzz": 1, "audio": "x"}})
check("PUT media_autodownload -> 200", r.status_code == 200)
_pol = client.get("/api/config").json()["data"]["media_autodownload"]
check("PUT merges over defaults and drops junk",
      _pol["image"] == {"individual": False, "group": True}
      and _pol["audio"] == {"individual": True, "group": True} and "zzz" not in _pol)
client.put("/api/config", json={"media_autodownload": {}})
check("PUT {} resets to defaults", client.get("/api/config").json()["data"]["media_autodownload"]["image"]["group"] is False)
client.put("/api/config", json={"disk_limit_gb": 5})
check("disk_limit_gb is saved", client.get("/api/config").json()["data"]["disk_limit_gb"] == 5)
client.put("/api/config", json={"disk_limit_gb": "lixo"})
check("disk_limit_gb ignores junk", client.get("/api/config").json()["data"]["disk_limit_gb"] == 5)
client.put("/api/config", json={"disk_limit_gb": -3})
check("disk_limit_gb never goes negative", client.get("/api/config").json()["data"]["disk_limit_gb"] == 0)

section("Media policy (webhook discard)")
_media_root = PROJECT_ROOT / "statics" / "media"
_test_dir = _media_root / "_policy_test"
_test_dir.mkdir(parents=True, exist_ok=True)
_saved_cfg = {k: settings.get(k) for k in (
    "image_transcription_enabled", "document_transcription_enabled",
    "audio_transcription_mode", "message_batch_delay", "auto_reply")}
settings["image_transcription_enabled"] = False
settings["document_transcription_enabled"] = False
settings["audio_transcription_mode"] = "off"
settings["message_batch_delay"] = 0.2
mock_gowa_client.get_message_filename = MagicMock(return_value="")

def _media_file(name: str, size: int = 64) -> str:
    (_test_dir / name).write_bytes(b"x" * size)
    return f"statics/media/_policy_test/{name}"

def _msg_row(phone_key: str, msg_id: str):
    c = contact_repo.get_by_phone(phone_key)
    for m in message_repo.get_all(c["id"]):
        if m.get("msg_id") == msg_id:
            return m
    return None

def _group_media(msg_id: str, kind: str, path: str):
    return client.post("/api/webhook", json={"event": "message", "payload": {
        "id": msg_id, "chat_id": "120363000000000001@g.us",
        "from": "5511988880001:5@s.whatsapp.net", "from_name": "Membro",
        "subject": "Grupo Teste", "is_from_me": False, "timestamp": str(int(time.time())),
        kind: {"path": path, "caption": ""},
    }})

_grp = "120363000000000001@g.us"
_p = _media_file("g_img.jpg")
r = _group_media("pol_g_img", "image", _p)
check("group image -> 200", r.status_code == 200)
_row = _msg_row(_grp, "pol_g_img")
check("group image is saved", _row is not None and _row.get("media_type") == "image")
check("group image: file dropped by the default policy", not (PROJECT_ROOT / _p).exists())
check("group image: media_path cleared", _row is not None and not _row.get("media_path"))
check("group image: state is discarded", _row is not None and _row.get("media_state") == "discarded")

_p = _media_file("g_aud.ogg")
_group_media("pol_g_aud", "audio", _p)
_row = _msg_row(_grp, "pol_g_aud")
check("group audio: file kept (audio is on by default)", (PROJECT_ROOT / _p).exists())
check("group audio: media_path kept, no state",
      _row is not None and _row.get("media_path") == _p and not _row.get("media_state"))

_p = _media_file("g_vid.mp4")
_group_media("pol_g_vid", "video", _p)
check("group video: dropped", not (PROJECT_ROOT / _p).exists())
_p = _media_file("g_doc.pdf")
_group_media("pol_g_doc", "document", _p)
check("group document: dropped", not (PROJECT_ROOT / _p).exists())
_p = _media_file("g_stk.webp")
_group_media("pol_g_stk", "sticker", _p)
check("group sticker: follows the image row (dropped)", not (PROJECT_ROOT / _p).exists())
_row = _msg_row(_grp, "pol_g_stk")
check("group sticker: message kept as sticker/discarded",
      _row is not None and _row.get("media_type") == "sticker" and _row.get("media_state") == "discarded")

client.put("/api/config", json={"media_autodownload": {"image": {"group": True}}})
_p = _media_file("g_img_keep.jpg")
_group_media("pol_g_img_keep", "image", _p)
check("group image kept when the group toggle is on", (PROJECT_ROOT / _p).exists())
_p = _media_file("g_img_ind.jpg")
client.put("/api/config", json={"media_autodownload": {"image": {"group": True, "individual": False}}})
_row = _msg_row(_grp, "pol_g_img_keep")
check("kept image keeps its path", _row is not None and bool(_row.get("media_path")))
client.put("/api/config", json={"media_autodownload": {}})

# Outside statics/media nothing is ever deleted, whatever the payload says.
_outside = Path(_tmpdir) / "not_media.jpg"
_outside.write_bytes(b"keep me")
_group_media("pol_g_outside", "image", str(_outside))
check("a path outside statics/media is never deleted", _outside.exists())
_p = "statics/operator/policy_guard.png"
(PROJECT_ROOT / "statics" / "operator").mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / _p).write_bytes(b"operator file")
_group_media("pol_g_oper", "image", _p)
check("a path in statics/operator is never deleted by the policy", (PROJECT_ROOT / _p).exists())
(PROJECT_ROOT / _p).unlink()

# Private chat goes through the batch cycle (needs a live event loop).
_ind = "5511977770001"
_c = contact_repo.get_or_create(_ind)
contact_repo.update(_c["id"], ai_enabled=False)
_p_img = _media_file("i_img.jpg")
_p_aud = _media_file("i_aud.ogg")

def _wait(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False

with TestClient(app, raise_server_exceptions=False) as live:
    for _mid, _kind, _path in (("pol_i_img", "image", _p_img), ("pol_i_aud", "audio", _p_aud)):
        live.post("/api/webhook", json={"event": "message", "payload": {
            "id": _mid, "chat_id": f"{_ind}@s.whatsapp.net", "from": f"{_ind}@s.whatsapp.net",
            "from_name": "Cliente", "is_from_me": False, "timestamp": str(int(time.time())),
            _kind: {"path": _path, "caption": ""},
        }})
    check("private image: dropped after the cycle", _wait(lambda: not (PROJECT_ROOT / _p_img).exists()))
    _row = _msg_row(_ind, "pol_i_img")
    check("private image: saved with discarded state",
          _row is not None and _row.get("media_state") == "discarded" and not _row.get("media_path"))
    check("private audio: kept", (PROJECT_ROOT / _p_aud).exists())
    _row = _msg_row(_ind, "pol_i_aud")
    check("private audio: path kept", _row is not None and _row.get("media_path") == _p_aud)

r = client.get(f"/api/contacts/{_ind}")
_api_msgs = r.json()["data"]["messages"]
_api_img = next((m for m in _api_msgs if m.get("msg_id") == "pol_i_img"), {})
check("API exposes media_state on a discarded message", _api_img.get("media_state") == "discarded")
check("API keeps media_type for the placeholder", _api_img.get("media_type") == "image")

section("Media download button (download-media)")
from gowa.client import GOWASendError
_GOWA_DL_DIR = "_dl_test_phone/2026-10-16"
def _fresh_download(rel_name: str) -> dict:
    """What GOWA's on-demand download really does: <phone>/<date>/<name>, no extension."""
    d = _media_root / _GOWA_DL_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / rel_name).write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 124)
    return {"file_path": f"statics/media/{_GOWA_DL_DIR}/{rel_name}", "filename": rel_name, "file_size": 128}

mock_gowa_client.download_media = MagicMock(return_value=_fresh_download("restored"))
r = client.post(f"/api/contacts/{_ind}/messages/pol_i_img/download-media")
check("download-media -> 200", r.status_code == 200, r.text[:200])
_row = _msg_row(_ind, "pol_i_img")
check("download-media restores the path and clears the state",
      _row is not None and _row.get("media_path") == "statics/media/restored.jpg"
      and not _row.get("media_state"))
check("the file sits loose in the media root, with the extension its content calls for",
      (_media_root / "restored.jpg").is_file() and not (_media_root / "_dl_test_phone").exists())
mock_gowa_client.download_media.reset_mock()
r = client.post(f"/api/contacts/{_ind}/messages/pol_i_img/download-media")
check("download-media on a message that already has its file does not call GOWA",
      r.status_code == 200 and not mock_gowa_client.download_media.called)

mock_gowa_client.download_media = MagicMock(side_effect=GOWASendError("gone", error_type="expired"))
r = client.post(f"/api/contacts/{_grp}/messages/pol_g_vid/download-media")
check("expired media -> 410 with a friendly message",
      r.status_code == 410 and "não está mais disponível" in r.json()["error"], r.text[:200])
mock_gowa_client.download_media = MagicMock(side_effect=GOWASendError("down", error_type="network"))
r = client.post(f"/api/contacts/{_grp}/messages/pol_g_vid/download-media")
check("GOWA unreachable -> 502", r.status_code == 502)
r = client.post(f"/api/contacts/{_grp}/messages/nope/download-media")
check("unknown message -> 404", r.status_code == 404)
r = client.post("/api/contacts/5500000000000/messages/x/download-media")
check("unknown conversation -> 404", r.status_code == 404)
_text_c = contact_repo.get_by_phone("5511999990001")
r = client.post("/api/contacts/5511999990001/messages/nope/download-media")
check("a text message is not downloadable", r.status_code == 404)
mock_gowa_client.download_media = MagicMock(return_value={
    "file_path": "storages/whatsbot.db", "filename": "x", "file_size": 1})
r = client.post(f"/api/contacts/{_grp}/messages/pol_g_vid/download-media")
check("a path outside statics/media returned by GOWA is refused", r.status_code == 502)
_row = _msg_row(_grp, "pol_g_vid")
check("...and the message is left untouched", _row is not None and _row.get("media_state") == "discarded")
mock_gowa_client.download_media = MagicMock(return_value=_fresh_download("restored_grp"))
r = client.post(f"/api/contacts/{_grp}/messages/pol_g_vid/download-media")
check("group media can be downloaded too", r.status_code == 200)

section("File manager (API)")
r = client.get("/api/files/usage")
check("GET /api/files/usage -> 200", r.status_code == 200)
_u = r.json()["data"]
check("usage has the received root", _u["roots"]["received"]["files"] >= 3)
check("usage separates what cannot be deleted", _u["system_bytes"] > 0 and _u["total_bytes"] >= _u["deletable_bytes"])
check("usage without limit has no percent", _u["limit_bytes"] == 0 and _u["percent"] is None)
client.put("/api/config", json={"disk_limit_gb": 1})
_u = client.get("/api/files/usage?refresh=true").json()["data"]
check("usage with a limit reports percent", _u["limit_bytes"] == 1024 ** 3 and _u["percent"] is not None)
client.put("/api/config", json={"disk_limit_gb": 0})

r = client.get("/api/files?root=received&flat=true&sort=size&order=desc&limit=500")
check("GET /api/files flat -> 200", r.status_code == 200)
_items = r.json()["data"]["items"]
_mine = [i for i in _items if i["rel"].startswith("_policy_test/")]
check("lists the test files", len(_mine) >= 3)
check("sorted heaviest first", [i["size"] for i in _items] == sorted([i["size"] for i in _items], reverse=True))
check("rows carry opaque ids, never paths", all("/" not in i["id"] and "statics" not in i["id"] for i in _items))
_restored = next((i for i in _items if i["rel"] == "restored.jpg"), None)
check("a file fetched by Baixar is listed loose in the root, not inside a folder",
      _restored is not None and "/" not in _restored["rel"])
check("a file linked to a message shows its origin",
      _restored is not None and _restored["orphan"] is False and _restored["origin"]["phone"] == _ind)
_orph = next((i for i in _mine if i["name"] == "g_aud.ogg"), None)
check("a kept group file is linked to its group",
      _orph is not None and _orph["origin"]["is_group"] is True)
# The "Conversa" column links to the exact message: contacts.id + messages.id.
_grp_row = _msg_row(_grp, "pol_g_aud")
check("origin carries the ids the conversation view opens by",
      _orph is not None and _orph["origin"]["contact_id"] == contact_repo.get_by_phone(_grp)["id"]
      and _orph["origin"]["message_id"] == _grp_row["_id"])
_ind_row = _msg_row(_ind, "pol_i_img")
check("origin of a restored file points at the message that brought it",
      _restored is not None and _restored["origin"]["message_id"] == _ind_row["_id"]
      and _restored["origin"]["contact_id"] == contact_repo.get_by_phone(_ind)["id"])
_page = client.get(f"/api/contacts/{_ind}/messages?around={_ind_row['_id']}&limit=20").json()["data"]
check("the linked message id opens a page that contains it",
      any(m["_id"] == _ind_row["_id"] for m in _page["messages"]))
r = client.get("/api/files?root=received&flat=true&orphans=true&limit=500")
_orph_names = {i["name"] for i in r.json()["data"]["items"]}
(_test_dir / "orphan_one.bin").write_bytes(b"o" * 10)
client.get("/api/files/usage?refresh=true")
_orph_names = {i["name"] for i in client.get("/api/files?root=received&flat=true&orphans=true&limit=500").json()["data"]["items"]}
check("orphans filter lists files without a message", "orphan_one.bin" in _orph_names)
check("orphans filter excludes linked files", "restored.jpg" not in _orph_names and "g_aud.ogg" not in _orph_names)
check("bad root -> 400", client.get("/api/files?root=storages").status_code == 400)
check("traversal in path -> 400", client.get("/api/files?root=received&path=../../storages").status_code == 400)
check("received root lists folders by default",
      any(i["type"] == "dir" for i in client.get("/api/files?root=received").json()["data"]["items"]))
(_media_root / "5511999990001").mkdir(parents=True, exist_ok=True)
(_media_root / "5511999990001" / "label_probe.jpg").write_bytes(b"p")
(_media_root / "120363000000000001").mkdir(parents=True, exist_ok=True)
(_media_root / "120363000000000001" / "label_probe.jpg").write_bytes(b"p")
client.get("/api/files/usage?refresh=true")
_dirs = {i["name"]: i for i in client.get("/api/files?root=received").json()["data"]["items"] if i["type"] == "dir"}
check("a contact folder shows the contact name", _dirs["5511999990001"].get("label") == (contact_repo.get_by_phone("5511999990001")["name"].lstrip("~")))
check("a group folder (phone without @g.us) shows the group name and flag",
      _dirs["120363000000000001"].get("label") and _dirs["120363000000000001"].get("is_group") is True)
check("a folder with no contact keeps no label", "label" not in _dirs.get("_policy_test", {}))
shutil.rmtree(_media_root / "5511999990001", ignore_errors=True)
shutil.rmtree(_media_root / "120363000000000001", ignore_errors=True)
client.get("/api/files/usage?refresh=true")

r = client.get(f"/api/files/{_restored['id']}/preview")
check("preview serves the file", r.status_code == 200 and r.content == b"\xff\xd8\xff\xe0" + b"x" * 124)
check("preview is inline for images", "inline" in r.headers.get("content-disposition", ""))
check("preview sets nosniff", r.headers.get("x-content-type-options") == "nosniff")
r = client.get(f"/api/files/{_restored['id']}/preview?download=true")
check("download=true forces attachment", "attachment" in r.headers.get("content-disposition", ""))
(_test_dir / "evil.html").write_text("<script>alert(1)</script>")
client.get("/api/files/usage?refresh=true")
_evil = next(i for i in client.get("/api/files?root=received&flat=true&limit=500").json()["data"]["items"] if i["name"] == "evil.html")
r = client.get(f"/api/files/{_evil['id']}/preview")
check("html is never rendered inline", "attachment" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type", "").startswith("application/octet-stream"))
(_test_dir / "evil.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
(_test_dir / "photo.jfif").write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 40)
(_test_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n1 0 obj")
(_test_dir / "no_extension").write_bytes(b"\xff\xd8\xff\xe0" + b"n" * 40)
(_test_dir / "report.docx").write_bytes(b"PK\x03\x04docx")
client.get("/api/files/usage?refresh=true")
_by_name = {i["name"]: i for i in client.get("/api/files?root=received&flat=true&limit=500").json()["data"]["items"]}
def _preview(name, suffix=""):
    return client.get(f"/api/files/{_by_name[name]['id']}/preview{suffix}")
r = _preview("evil.svg")
check("svg is never rendered inline", "attachment" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type", "").startswith("application/octet-stream"))
r = _preview("photo.jfif")
check("jfif is served as an inline jpeg", "inline" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type") == "image/jpeg")
check("jfif is listed as an image", _by_name["photo.jfif"]["kind"] == "image")
r = _preview("doc.pdf")
check("pdf is served inline", "inline" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type") == "application/pdf")
r = _preview("no_extension")
check("a jpeg without extension is shown by its content", "inline" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type") == "image/jpeg")
r = _preview("report.docx")
check("an office file stays a download", "attachment" in r.headers.get("content-disposition", "")
      and r.headers.get("content-type", "").startswith("application/octet-stream"))
check("download=true still forces attachment on a viewable file",
      "attachment" in _preview("photo.jfif", "?download=true").headers.get("content-disposition", ""))
check("every preview keeps nosniff", all(
    _preview(n).headers.get("x-content-type-options") == "nosniff"
    for n in ("evil.svg", "photo.jfif", "doc.pdf", "report.docx")))
import base64 as _b64
def _forge(root, rel):
    return _b64.urlsafe_b64encode(f"{root}:{rel}".encode()).decode().rstrip("=")
check("preview of a forged traversal id -> 404",
      client.get(f"/api/files/{_forge('received', '../../storages/whatsbot.db')}/preview").status_code == 404)
check("preview of garbage id -> 404", client.get("/api/files/!!!/preview").status_code == 404)

section("File manager (sender file names)")
from urllib.parse import unquote as _unquote

def _disposition_name(resp) -> str:
    """Filename a browser would save the response under."""
    cd = resp.headers.get("content-disposition", "")
    if "filename*=utf-8''" in cd:
        return _unquote(cd.split("filename*=utf-8''", 1)[1])
    return cd.split('filename="', 1)[1].rstrip('"') if 'filename="' in cd else ""

def _listed(name_or_rel_prefix: str):
    client.get("/api/files/usage?refresh=true")
    items = client.get("/api/files?root=received&flat=true&limit=500").json()["data"]["items"]
    return next((i for i in items if i["rel"].startswith(name_or_rel_prefix)), None)

# 1) Customer sends "Teste.mp4": GOWA stores it under a uuid name; the webhook
#    carries the real one in the document payload.
settings["document_transcription_enabled"] = False
client.put("/api/config", json={"media_autodownload": {"document": {"individual": True, "group": True},
                                                          "video": {"individual": True, "group": True}}})
_nm_doc = _media_file("1792000001-aaaa-uuid-doc.mp4")
r = client.post("/api/webhook", json={"event": "message", "payload": {
    "id": "nm_doc_1", "chat_id": "120363000000000001@g.us",
    "from": "5511988880001:5@s.whatsapp.net", "from_name": "Membro", "subject": "Grupo Teste",
    "is_from_me": False, "timestamp": str(int(time.time())),
    "document": {"path": _nm_doc, "file_name": "Teste.mp4", "mimetype": "video/mp4", "caption": ""},
}})
check("document with a name -> 200", r.status_code == 200)
_row = _msg_row(_grp, "nm_doc_1")
check("the sender's file name is stored on the message", _row is not None and _row.get("media_path") == _nm_doc)
_it = _listed("_policy_test/1792000001-aaaa-uuid-doc.mp4")
check("the file manager shows the name the customer used", _it is not None and _it["name"] == "Teste.mp4",
      str(_it and _it["name"]))
check("...while the id and rel still point at the real file",
      _it is not None and _it["rel"] == "_policy_test/1792000001-aaaa-uuid-doc.mp4")
r = client.get(f"/api/files/{_it['id']}/preview?download=true")
check("downloading it saves it under the same name", _disposition_name(r) == "Teste.mp4", r.headers.get("content-disposition", ""))
r = client.get(f"/api/files/{_it['id']}/preview")
check("opening it in the viewer also carries the name", _disposition_name(r) == "Teste.mp4")

# 2) A name with accents, spaces and a hostile path part is kept readable and safe.
_nm_hostile = _media_file("1792000002-bbbb-uuid-doc.pdf")
client.post("/api/webhook", json={"event": "message", "payload": {
    "id": "nm_doc_2", "chat_id": "120363000000000001@g.us",
    "from": "5511988880001:5@s.whatsapp.net", "from_name": "Membro", "subject": "Grupo Teste",
    "is_from_me": False, "timestamp": str(int(time.time())),
    "document": {"path": _nm_hostile, "file_name": "..\\..\\Relatório final: 2026?.pdf", "mimetype": "application/pdf", "caption": ""},
}})
_it = _listed("_policy_test/1792000002-bbbb-uuid-doc.pdf")
check("a hostile or odd name is cleaned, accents kept", _it is not None and _it["name"] == "Relatório final 2026.pdf",
      str(_it and _it["name"]))
r = client.get(f"/api/files/{_it['id']}/preview?download=true")
check("the download name has no path and no forbidden characters",
      _disposition_name(r) == "Relatório final 2026.pdf", r.headers.get("content-disposition", ""))

# 3) Photos, videos and voice notes carry no name from WhatsApp: GOWA's own
#    invented name (image_<time>.jpg) must not be passed off as the customer's.
_nm_img = _media_file("1792000003-cccc-uuid-img.jpg")
client.post("/api/webhook", json={"event": "message", "payload": {
    "id": "nm_img_1", "chat_id": "120363000000000001@g.us",
    "from": "5511988880001:5@s.whatsapp.net", "from_name": "Membro", "subject": "Grupo Teste",
    "is_from_me": False, "timestamp": str(int(time.time())),
    "image": {"path": _nm_img, "caption": "", "file_name": "image_20261016_114440.jpg"},
}})
client.put("/api/config", json={"media_autodownload": {"image": {"individual": True, "group": True}}})
_nm_img2 = _media_file("1792000004-dddd-uuid-img.jpg")
client.post("/api/webhook", json={"event": "message", "payload": {
    "id": "nm_img_2", "chat_id": "120363000000000001@g.us",
    "from": "5511988880001:5@s.whatsapp.net", "from_name": "Membro", "subject": "Grupo Teste",
    "is_from_me": False, "timestamp": str(int(time.time())),
    "image": {"path": _nm_img2, "caption": "", "file_name": "image_20261016_114440.jpg"},
}})
_it = _listed("_policy_test/1792000004-dddd-uuid-img.jpg")
check("a photo keeps its on-disk name (WhatsApp sends none)",
      _it is not None and _it["name"] == "1792000004-dddd-uuid-img.jpg", str(_it and _it["name"]))
check("a photo is not named after GOWA's made-up filename", _it is not None and "image_2026" not in _it["name"])

# 4) Documents saved BEFORE the column existed keep their name only in the text.
_nm_old = _media_file("1792000005-eeee-uuid-old.pdf")
_old_c = contact_repo.get_by_phone(_ind)
message_repo.add(_old_c["id"], "user", "[Documento recebido: Proposta antiga.pdf]\nsegue", media_type="document",
                 media_path=_nm_old, msg_id="nm_old_1")
_it = _listed("_policy_test/1792000005-eeee-uuid-old.pdf")
check("an old document is named from its message text", _it is not None and _it["name"] == "Proposta antiga.pdf",
      str(_it and _it["name"]))
_nm_gen = _media_file("1792000006-ffff-uuid-gen.pdf")
message_repo.add(_old_c["id"], "user", "[Documento recebido: documento]", media_type="document",
                 media_path=_nm_gen, msg_id="nm_old_2")
_it = _listed("_policy_test/1792000006-ffff-uuid-gen.pdf")
check("the generic 'documento' placeholder is never shown as a name",
      _it is not None and _it["name"] == "1792000006-ffff-uuid-gen.pdf", str(_it and _it["name"]))

# 4b) A document the operator sent from their own phone (echo) keeps its name too.
_nm_echo = _media_file("1792000007-gggg-uuid-echo.pdf")
r = client.post("/api/webhook", json={"event": "message", "payload": {
    "id": "nm_echo_1", "chat_id": f"{_ind}@s.whatsapp.net", "from": "5511900000000@s.whatsapp.net",
    "is_from_me": True, "timestamp": str(int(time.time())),
    "document": {"path": _nm_echo, "file_name": "Tabela de preços.xlsx", "caption": ""},
}})
check("echo of a document sent from the phone -> 200", r.status_code == 200)
_it = _listed("_policy_test/1792000007-gggg-uuid-echo.xlsx") or _listed("_policy_test/1792000007-gggg-uuid-echo.pdf")
check("a document sent from the phone is listed under its own name",
      _it is not None and _it["name"] == "Tabela de preços.xlsx", str(_it and _it["name"]))

# 5) What the operator sends keeps the name picked in the file dialog.
_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
r = client.post("/api/contacts/5511999990001/send-document",
                files={"document": ("Orçamento 2026.pdf", io.BytesIO(b"%PDF-1.4\n" + b"\x00" * 40), "application/pdf")},
                data={"caption": ""})
check("operator sends a document -> 200", r.status_code == 200)
client.get("/api/files/usage?refresh=true")
_sent = [i for i in client.get("/api/files?root=sent&flat=true&limit=500").json()["data"]["items"]
         if i["name"] == "Orçamento 2026.pdf"]
check("a sent document is listed under the name the operator chose", len(_sent) == 1)
r = client.get(f"/api/files/{_sent[0]['id']}/preview?download=true")
check("...and downloads under it", _disposition_name(r) == "Orçamento 2026.pdf", r.headers.get("content-disposition", ""))
r = client.post("/api/contacts/5511999990001/send-image", files={"image": ("foto da obra.png", io.BytesIO(_png), "image/png")})
client.get("/api/files/usage?refresh=true")
_sent_img = [i for i in client.get("/api/files?root=sent&flat=true&limit=500").json()["data"]["items"]
             if i["name"] == "foto da obra.png"]
check("a sent image keeps the file's own name", len(_sent_img) == 1)
r = client.post("/api/contacts/5511999990001/send-audio", files={"audio": ("voice.ogg", io.BytesIO(b"OggS" + b"\x00" * 60), "audio/ogg")})
client.get("/api/files/usage?refresh=true")
_voice = [i for i in client.get("/api/files?root=sent&flat=true&limit=500").json()["data"]["items"]
          if i["name"] == "voice.ogg"]
check("a mic recording is not given a fake name", len(_voice) == 0)

# 6) Sorting by name orders by what the user sees.
_by_name = client.get("/api/files?root=received&flat=true&sort=name&order=asc&limit=500").json()["data"]["items"]
_mine_sorted = [i["name"] for i in _by_name if i["rel"].startswith("_policy_test/")]
check("sort by name orders by the shown names",
      _mine_sorted == sorted(_mine_sorted, key=str.lower), str(_mine_sorted))

# 7) Deleting a file by its displayed (renamed) row still works through the id.
_it = _listed("_policy_test/1792000001-aaaa-uuid-doc.mp4")
r = client.post("/api/files/delete", json={"ids": [_it["id"]]})
check("a renamed file is deleted through its id", r.status_code == 200 and r.json()["data"]["files"] == 1)
check("...and the right file on disk is gone", not (PROJECT_ROOT / _nm_doc).exists())
client.put("/api/config", json={"media_autodownload": {}})

section("File manager (filters)")
# Own contacts, so the sections above cannot change what is expected here.
_fc_ind = contact_repo.get_or_create("5511966660001")
contact_repo.update(_fc_ind["id"], name="Marcos Pereira")
_fc_other = contact_repo.get_or_create("5511966660002")
contact_repo.update(_fc_other["id"], name="Joana Silva")
_fc_grp = contact_repo.get_or_create("120363000000000777@g.us")
contact_repo.update(_fc_grp["id"], is_group=1, group_name="Obra Rua das Flores")

def _fc_file(name: str, age_days: float = 0.0, size: int = 32) -> str:
    path = _test_dir / name
    path.write_bytes(b"x" * size)
    if age_days:
        _t = time.time() - age_days * 86400
        os.utime(path, (_t, _t))
    return f"statics/media/_policy_test/{name}"

_fc_a = _fc_file("fc_audio.ogg")
_fc_b = _fc_file("fc_photo.jpg", age_days=20)
_fc_c = _fc_file("fc_doc.pdf", age_days=3)
_fc_d = _fc_file("fc_grp_img.jpg", age_days=1)
_fc_e = _fc_file("fc_plain_audio.ogg")
message_repo.add(_fc_ind["id"], "user", "[Transcrição do áudio]: preciso do boleto de outubro",
                 media_type="audio", media_path=_fc_a, msg_id="fc_1")
message_repo.add(_fc_ind["id"], "user", "[Descrição da imagem]: um recibo de pagamento\nfoto da obra",
                 media_type="image", media_path=_fc_b, msg_id="fc_2")
message_repo.add(_fc_other["id"], "user", "[Documento recebido: Contrato Social.pdf]",
                 media_type="document", media_path=_fc_c, media_name="Contrato Social.pdf", msg_id="fc_3")
message_repo.add(_fc_grp["id"], "user", "[Fulano]: segue a planta da construção", media_type="image", media_path=_fc_d, msg_id="fc_4")
message_repo.add(_fc_other["id"], "user", "[Áudio recebido]", media_type="audio", media_path=_fc_e, msg_id="fc_5")
client.get("/api/files/usage?refresh=true")

def _fc_list(**params) -> list[dict]:
    qs = "&".join(f"{k}={v}" for k, v in {"root": "received", "flat": "true", "limit": 500, **params}.items())
    r = client.get(f"/api/files?{qs}")
    assert r.status_code == 200, r.text[:200]
    return [i for i in r.json()["data"]["items"] if i["rel"].startswith("_policy_test/fc_")]

def _fc_names(**params) -> set[str]:
    return {i["rel"].split("/")[-1] for i in _fc_list(**params)}

check("without filters every test file is listed", len(_fc_list()) == 5)

# search
check("search finds a document by the name the sender gave", _fc_names(q="contrato") == {"fc_doc.pdf"})
check("search ignores case", _fc_names(q="CONTRATO social") == {"fc_doc.pdf"})
check("search ignores accents, typed with or without them",
      _fc_names(q="construcao") == {"fc_grp_img.jpg"} and _fc_names(q="CONSTRUÇÃO") == {"fc_grp_img.jpg"})
_hit = _fc_list(q="boleto")
check("search finds an audio by its transcription", {i["rel"].split("/")[-1] for i in _hit} == {"fc_audio.ogg"})
check("a text hit says it came from the message and shows the excerpt",
      _hit and _hit[0].get("match") == "text" and "boleto" in _hit[0].get("snippet", ""))
check("a name hit says it came from the name", _fc_list(q="contrato")[0].get("match") == "name")
check("search finds a photo by its description", _fc_names(q="recibo") == {"fc_photo.jpg"})
check("search finds a photo by the caption under the description", _fc_names(q="foto da obra") == {"fc_photo.jpg"})
check("search ignores the placeholders the app writes",
      _fc_names(q="recebido") == set() and _fc_names(q="Transcrição") == set() and _fc_names(q="Descrição") == set())
check("search can match the on-disk name of a file with no name of its own", _fc_names(q="fc_plain") == {"fc_plain_audio.ogg"})
check("an unmatched search lists nothing", _fc_names(q="zzzxxyy") == set())
check("a blank search is no filter", len(_fc_list(q="%20%20")) == 5)

# conversation
check("conversation by contact name", _fc_names(conversation="marcos") == {"fc_audio.ogg", "fc_photo.jpg"})
check("conversation name ignores accents and case", _fc_names(conversation="JOANA") == {"fc_doc.pdf", "fc_plain_audio.ogg"})
check("conversation by group name", _fc_names(conversation="flores") == {"fc_grp_img.jpg"})
check("conversation by phone number", _fc_names(conversation="5511966660001") == {"fc_audio.ogg", "fc_photo.jpg"})
check("conversation by a number typed with formatting",
      _fc_names(conversation="(11)%2096666-0001") == {"fc_audio.ogg", "fc_photo.jpg"})
check("conversation by the number without the country code",
      _fc_names(conversation="11966660002") == {"fc_doc.pdf", "fc_plain_audio.ogg"})
check("conversation that nobody has lists nothing", _fc_names(conversation="ninguem%20assim") == set())
check("a file with no message belongs to no conversation", "orphan_one.bin" not in {i["name"] for i in _fc_list(conversation="marcos")} )

# private / group
check("only individual chats", _fc_names(chat_type="individual") == {"fc_audio.ogg", "fc_photo.jpg", "fc_doc.pdf", "fc_plain_audio.ogg"})
check("only groups", _fc_names(chat_type="group") == {"fc_grp_img.jpg"})
check("an unknown chat_type is ignored", len(_fc_list(chat_type="qualquer")) == 5)
check("chat_type and conversation combine", _fc_names(chat_type="group", conversation="marcos") == set())

# period (the browser sends midnights; the server takes epoch seconds)
_now = time.time()
check("date_from keeps the recent ones", _fc_names(date_from=_now - 2 * 86400) == {"fc_audio.ogg", "fc_grp_img.jpg", "fc_plain_audio.ogg"})
check("date_to keeps the old ones", _fc_names(date_to=_now - 2 * 86400) == {"fc_photo.jpg", "fc_doc.pdf"})
check("a range", _fc_names(date_from=_now - 5 * 86400, date_to=_now - 2 * 86400) == {"fc_doc.pdf"})

# filters stack
check("search + period + chat type stack",
      _fc_names(q="obra", date_to=_now - 10 * 86400, chat_type="individual") == {"fc_photo.jpg"})
check("search + file kind stack", _fc_names(q="marcos", kind="audio") == set() and _fc_names(conversation="marcos", kind="audio") == {"fc_audio.ogg"})
check("the total reflects the filter",
      client.get("/api/files?root=received&flat=true&conversation=marcos").json()["data"]["total_bytes"] == 64)
check("orphans still works next to a filter", "fc_audio.ogg" not in _fc_names(orphans="true", conversation="marcos"))
check("paging works with a filter",
      len(client.get("/api/files?root=received&flat=true&conversation=joana&limit=1").json()["data"]["items"]) == 1
      and client.get("/api/files?root=received&flat=true&conversation=joana&limit=1").json()["data"]["total"] == 2)
check("filters never widen access (bad path still 400)",
      client.get("/api/files?root=received&path=../../storages&q=x").status_code == 400)
check("a search with LIKE wildcards is a plain search", _fc_names(q="%25") == set() and _fc_names(conversation="%25") == set())

# picking a conversation from the suggestions (two clients can share a name)
_fc_twin = contact_repo.get_or_create("5511966660003")
contact_repo.update(_fc_twin["id"], name="Marcos Pereira")
_fc_f = _fc_file("fc_twin_doc.pdf", age_days=2)
message_repo.add(_fc_twin["id"], "user", "[Documento recebido: Planta.pdf]",
                 media_type="document", media_path=_fc_f, media_name="Planta.pdf", msg_id="fc_6")
client.get("/api/files/usage?refresh=true")

def _fc_suggest(q: str, **params) -> list[dict]:
    qs = "&".join(f"{k}={v}" for k, v in {"q": q, **params}.items())
    r = client.get(f"/api/files/conversations?{qs}")
    assert r.status_code == 200, r.text[:200]
    return r.json()["data"]

_sug = _fc_suggest("marcos")
check("suggestions list both clients that share a name",
      {s["phone"] for s in _sug} == {"5511966660001", "5511966660003"}, str(_sug))
check("a suggestion carries what tells them apart and what draws the row",
      all({"id", "phone", "name", "is_group", "avatar_v"} <= set(s) for s in _sug) and all(s["name"] == "Marcos Pereira" for s in _sug))
check("suggestions find a person by the number typed with formatting",
      [s["phone"] for s in _fc_suggest("(11)%2096666-0003")] == ["5511966660003"])
check("suggestions find a group by name and flag it", [(s["name"], s["is_group"]) for s in _fc_suggest("FLORES")] == [("Obra Rua das Flores", True)])
check("suggestions ignore accents and case", {s["phone"] for s in _fc_suggest("JOANA")} == {"5511966660002"})
check("a blank or unmatched text suggests nothing", _fc_suggest("%20%20") == [] and _fc_suggest("ninguem%20assim") == [])
check("the suggestion list honors the limit", len(_fc_suggest("marcos", limit=1)) == 1)
check("suggestions never create a contact", contact_repo.get_by_phone("5511966660099") is None and _fc_suggest("5511966660099") == [])

_by_id = {s["phone"]: s["id"] for s in _sug}
check("picking one client lists only that client's files",
      _fc_names(contact_id=_by_id["5511966660003"]) == {"fc_twin_doc.pdf"}
      and _fc_names(contact_id=_by_id["5511966660001"]) == {"fc_audio.ogg", "fc_photo.jpg"})
check("typing the name instead still lists both clients",
      _fc_names(conversation="marcos") == {"fc_audio.ogg", "fc_photo.jpg", "fc_twin_doc.pdf"})
check("a picked conversation wins over typed text",
      _fc_names(contact_id=_by_id["5511966660003"], conversation="joana") == {"fc_twin_doc.pdf"})
check("a picked conversation stacks with the other filters",
      _fc_names(contact_id=_by_id["5511966660001"], kind="audio") == {"fc_audio.ogg"}
      and _fc_names(contact_id=_by_id["5511966660001"], chat_type="group") == set())
check("an id nobody has lists nothing", _fc_names(contact_id=987654) == set())
check("a non-positive contact_id is no filter", len(_fc_list(contact_id=0)) == 6)
check("a picked conversation keeps the list free of orphans", "orphan_one.bin" not in {i["name"] for i in _fc_list(contact_id=_by_id["5511966660001"])})

for _f in (_fc_a, _fc_b, _fc_c, _fc_d, _fc_e, _fc_f):
    (PROJECT_ROOT / _f).unlink(missing_ok=True)
client.get("/api/files/usage?refresh=true")

section("File manager (delete)")
check("delete without ids -> 400", client.post("/api/files/delete", json={"ids": []}).status_code == 400)
check("delete with non-string ids -> 400", client.post("/api/files/delete", json={"ids": [1]}).status_code == 400)
_db_before = (PROJECT_ROOT / "storages" / "whatsbot.db").exists()
r = client.post("/api/files/delete", json={"ids": [
    _forge("received", "../../storages/whatsbot.db"), _forge("received", ".."), _forge("storages", "x"), "lixo"]})
check("hostile ids delete nothing", r.status_code == 200 and r.json()["data"]["files"] == 0 and r.json()["data"]["failed"] == 4)
check("the database survived", (PROJECT_ROOT / "storages" / "whatsbot.db").exists() == _db_before)

r = client.post("/api/files/delete", json={"ids": [_restored["id"]]})
check("delete a linked file", r.status_code == 200 and r.json()["data"]["files"] == 1 and r.json()["data"]["messages"] == 1, r.text[:200])
check("the file is gone", not (PROJECT_ROOT / "statics/media/restored.jpg").exists())
_row = _msg_row(_ind, "pol_i_img")
check("the message becomes removed", _row is not None and _row.get("media_state") == "removed" and not _row.get("media_path"))
r = client.post(f"/api/contacts/{_ind}/messages/pol_i_img/download-media")
check("a removed file can be requested again (button still offered)", r.status_code in (200, 410, 502))

_dir_id = next(i["id"] for i in client.get("/api/files?root=received").json()["data"]["items"] if i["name"] == "_policy_test")
r = client.post("/api/files/delete", json={"ids": [_dir_id]})
check("delete a whole folder", r.status_code == 200 and r.json()["data"]["files"] >= 3)
check("the folder is gone", not _test_dir.exists())
check("the media root remains", _media_root.is_dir())
_row = _msg_row(_ind, "pol_i_aud")
check("messages of a deleted folder are flagged", _row is not None and _row.get("media_state") == "removed")

# restore what this section changed
for _k, _v in _saved_cfg.items():
    settings[_k] = _v
shutil.rmtree(_test_dir, ignore_errors=True)
# Baixar now settles files loose in the media root (real project folder): sweep them.
shutil.rmtree(_media_root / "_dl_test_phone", ignore_errors=True)
for _leftover in ("restored.jpg", "restored_grp.jpg"):
    (_media_root / _leftover).unlink(missing_ok=True)

# ═══════════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  RESULTS: {passed} passed, {failed} failed")
print(f"{'='*60}")

if errors:
    print("\nFailed tests:")
    for e in errors:
        print(e)

# Cleanup temp dir
import shutil
try:
    shutil.rmtree(_tmpdir, ignore_errors=True)
except Exception:
    pass

sys.exit(1 if failed else 0)
