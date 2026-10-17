"""Contact CRUD and messaging endpoints."""

import asyncio
import logging
import time
from pathlib import Path

from fastapi import File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from gowa.client import GOWASendError, extract_msg_id

from db.repositories import contact_repo, message_repo, config_repo
from agent import group_mentions
from server import file_manager, media_policy
from server.avatars import avatar_version, refresh_and_broadcast
from server.helpers import _ok, _err, parse_split_reply
from plugins.events import emit as emit_event, apply_filter, emit_with_filter
from server.routes.sandbox import SANDBOX_CONTACT_PREFIX

logger = logging.getLogger(__name__)

# Group send permission is looked up in GOWA when a group is opened. It rarely
# changes (the webhook also refreshes it on every group message), so a short
# cache keeps re-opening a group from paying the round trip each time.
_CAN_SEND_TTL = 60.0
_CAN_SEND_TIMEOUT = 4.0
_CAN_SEND_BACKOFF = 15.0
_can_send_cache: dict[str, tuple[float, bool]] = {}
# When a lookup fails or times out its worker thread keeps running until GOWA's
# own HTTP timeout. Without a pause, every re-open of a group would start
# another blocked thread, so failures are remembered briefly.
_can_send_failed_at: dict[str, float] = {}

# History paging: the newest page is what opening a conversation needs; older
# and newer pages stream in on demand (scroll, jump to a search hit or quote).
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 300


def _is_sandbox_contact(phone: str) -> bool:
    """True when the contact is a sandbox/test number — operator sends to it
    must stay local (a real GOWA send would fail: the number isn't on WhatsApp)."""
    return bool(config_repo.get(f"{SANDBOX_CONTACT_PREFIX}{phone}"))


def _normalize_br_phone(raw: str) -> str | None:
    """Digits-only phone with country code, or None when it's too short.

    Prefixes `55` when missing, so a foreign number typed without its country
    code would be corrupted — callers must only pass numbers that are BR or
    already carry the country code."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) < 10:
        return None
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


def register_routes(app, deps):
    agent_handler = deps.agent_handler
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings
    statics_senditems_dir = deps.statics_senditems_dir
    statics_media_dir = deps.statics_media_dir
    _download_lock = asyncio.Lock()  # serialises the Baixar button, see download_message_media

    async def _send_read_receipts(phone: str, msg_ids: list[str]):
        """Send read receipts to GOWA in background (best-effort)."""
        for mid in msg_ids:
            try:
                await asyncio.to_thread(gowa_client.mark_as_read, mid, phone)
                logger.info("[ReadReceipt] Sent for %s msg %s", phone, mid)
            except Exception as e:
                logger.warning("[ReadReceipt] Failed for %s msg %s: %s", phone, mid, e)

    @app.get("/api/contacts")
    async def list_contacts(q: str = "", archived: bool = False):
        """List all contacts with summary info."""
        results = await asyncio.to_thread(contact_repo.list_contacts, q, archived)
        # Cache-busting version for each avatar (file mtime) so updated photos
        # are picked up by the browser instead of the stale cached image.
        for c in results:
            c["avatar_v"] = avatar_version(settings, c.get("phone", ""))
        return _ok(results)

    @app.post("/api/contacts/check-phone")
    async def check_phone(request: Request):
        """Check if a phone number is registered on WhatsApp."""
        body = await request.json()
        phone = (body.get("phone") or "").strip()
        if not phone:
            return _err("Campo 'phone' é obrigatório.")

        digits = _normalize_br_phone(phone)
        if digits is None:
            return _err("Número inválido. Informe DDD + número.")

        try:
            result = await asyncio.to_thread(gowa_client.check_phone, digits)
        except GOWASendError as e:
            return _err(f"Erro ao verificar número: {e}")

        registered = result.get("registered", False)
        name = result.get("name", "")
        # Use canonical phone from WhatsApp (avoids BR 12/13 digit duplicates)
        canonical = result.get("canonical_phone", digits) if registered else digits

        # If registered, pre-create contact with WhatsApp name and AI setting
        if registered:
            ai_default = settings.get("default_ai_enabled", True)
            def _save():
                contact_repo.get_or_create(canonical, default_ai_enabled=ai_default)
                if name:
                    c = contact_repo.get_by_phone(canonical)
                    if c and not c["name"]:
                        contact_repo.update(c["id"], name=f"~{name}")
            await asyncio.to_thread(_save)

        return _ok({
            "phone": canonical,
            "registered": registered,
            "jid": result.get("jid", ""),
            "name": name,
        })

    @app.post("/api/contacts/send-self-link")
    async def send_self_link(request: Request):
        """Send a wa.me link for `phone` to the connected account's own chat.

        Used by the "start conversation" warning: instead of opening the chat
        from here (unofficial API, risk of ban), the operator gets the link on
        their own phone and starts the conversation from the official app.
        """
        body = await request.json()
        phone = (body.get("phone") or "").strip()
        digits = _normalize_br_phone(phone)
        if digits is None:
            return _err("Número inválido. Informe DDD + número.")

        own =(await asyncio.to_thread(gowa_client.get_own_number) or "").strip()
        if not own:
            return _err("Não foi possível identificar o número conectado. "
                        "Verifique a conexão do WhatsApp.", status=503)

        link = f"https://wa.me/{digits}"
        message = (f"Iniciar conversa com +{digits}\n\n{link}\n\n"
                   f"Toque no link para abrir a conversa direto no WhatsApp.")

        try:
            send_result = await asyncio.to_thread(gowa_client.send_message, own, message)
        except Exception as e:
            logger.error("[SelfLink] Failed to send link for %s to %s: %s", digits, own, e)
            return _err(f"Falha ao enviar o link: {e}", status=500)

        msg_id = extract_msg_id(send_result)
        state.recently_sent[f"{own}:{message[:120]}"] = time.time()

        try:
            msg_data = await asyncio.to_thread(
                agent_handler.save_operator_message, own, message,
                status="operator", msg_id=msg_id,
            )
        except Exception as e:
            logger.error("[SelfLink] Failed to save message for %s: %s", own, e)
            msg_data = None

        if msg_data:
            await ws_manager.broadcast("new_message", {"phone": own, "message": msg_data})

        await emit_with_filter("message.sent", {
            "phone": own, "text": message, "msg_id": msg_id,
            "media_type": None, "media_path": None,
            "source": "operator", "status": "operator",
            "reply_to_msg_id": None,
            "ts": time.time(),
        })

        logger.info("[SelfLink] Sent %s to own number %s", link, own)
        return _ok({"link": link, "phone": digits, "own_phone": own, "msg_id": msg_id})

    @app.get("/api/contacts/unread-count")
    async def unread_count():
        """Number of conversations with unread messages (for the browser-tab badge).

        Declared before /api/contacts/{phone} so the static path wins over the
        path parameter."""
        count = await asyncio.to_thread(contact_repo.unread_conversation_count)
        return _ok({"count": count})

    @app.get("/api/contacts/lookup")
    async def lookup_contact(phone: str = ""):
        """Tell whether `phone` is already a contact, without creating anything.

        Read-only twin of `check-phone` (which pre-creates the contact) and of
        `GET /api/contacts/{phone}` (which materializes a missing one and marks
        messages as read). The group sender label uses it to decide between
        opening the existing conversation and going through the start-chat
        warning. Declared before /api/contacts/{phone} so the static path wins."""
        canonical = _normalize_br_phone(phone)
        if canonical is None:
            return _err("Número inválido. Informe DDD + número.")
        contact = await asyncio.to_thread(contact_repo.get_by_phone, canonical)
        return _ok({"exists": contact is not None, "contact": contact})

    async def _group_can_send(phone: str) -> bool | None:
        """Whether the bot may send in group ``phone`` (None when unknown).

        Cached briefly and bounded by a timeout: a slow GOWA must never hold
        the conversation hostage, the stored value is used instead."""
        cached = _can_send_cache.get(phone)
        if cached and (time.time() - cached[0]) < _CAN_SEND_TTL:
            return cached[1]
        if (time.time() - _can_send_failed_at.get(phone, 0.0)) < _CAN_SEND_BACKOFF:
            return None
        try:
            can_send = await asyncio.wait_for(
                asyncio.to_thread(gowa_client.can_bot_send_in_group, phone, state.bot_phone),
                timeout=_CAN_SEND_TIMEOUT)
        except Exception as e:
            _can_send_failed_at[phone] = time.time()
            logger.warning("[Contact] Failed to check group send permission (%s): %s",
                           type(e).__name__, e)
            return None
        _can_send_failed_at.pop(phone, None)
        _can_send_cache[phone] = (time.time(), bool(can_send))
        return bool(can_send)

    def _page_payload(contact_id: int, page: dict) -> dict:
        """Client-facing shape of one history page (+ quoted originals)."""
        return {
            "messages": page["messages"],
            "has_more_before": page["has_more_before"],
            "has_more_after": page["has_more_after"],
            "quoted": message_repo.get_quoted(contact_id, page["messages"]),
        }

    @app.get("/api/contacts/{phone}")
    async def get_contact(phone: str, mark_read: bool = True,
                          limit: int | None = None, around: int | None = None):
        """Return full contact data including conversation history.

        Without ``limit`` the whole history is returned (legacy shape). With
        ``limit`` only the newest page comes back (or the page around message
        id ``around``), plus ``has_more_before``/``has_more_after`` so the
        client can fetch the rest from ``/messages``."""
        if limit is not None:
            limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
        # Start the group permission lookup right away so it overlaps the DB work.
        send_task = None
        if state.bot_phone and "@g.us" in phone:
            send_task = asyncio.create_task(_group_can_send(phone))

        def _load():
            data = contact_repo.get_full_contact(phone)
            if data is None:
                # Auto-create contact for verified phone numbers
                ai_default = settings.get("default_ai_enabled", True)
                contact_repo.get_or_create(phone, default_ai_enabled=ai_default)
                data = contact_repo.get_full_contact(phone)
            if data is None:
                return None, []
            contact_id = data["id"]
            # Mark as read when viewing contact (skip if mark_read=false)
            msg_ids = []
            if mark_read and (data.get("unread_count", 0) > 0 or data.get("unread_ai_count", 0) > 0):
                msg_ids = contact_repo.mark_as_read(contact_id)
                data["unread_count"] = 0
                data["unread_ai_count"] = 0
                # Update in-memory cache
                if phone in agent_handler._contacts:
                    agent_handler._contacts[phone].unread_count = 0
                    agent_handler._contacts[phone].unread_ai_count = 0
            # Load messages
            if limit is None:
                data["messages"] = message_repo.get_all(contact_id)
                data["has_more_before"] = False
                data["has_more_after"] = False
                data["quoted"] = {}
            else:
                page = message_repo.get_page(contact_id, limit, around=around)
                if around is not None and not page["found"]:
                    # Unknown/foreign message id: fall back to the newest page.
                    page = message_repo.get_page(contact_id, limit)
                data.update(_page_payload(contact_id, page))
            # Load usage for the full response
            data["usage"] = []
            return data, msg_ids
        try:
            data, msg_ids = await asyncio.to_thread(_load)
        except BaseException:
            if send_task is not None:
                send_task.cancel()
            raise
        if data is None:
            if send_task is not None:
                send_task.cancel()
            return _err("Contato não encontrado.", status=404)
        if msg_ids:
            asyncio.create_task(_send_read_receipts(phone, msg_ids))
        # Check group send permissions (fresh check on every contact load)
        if data.get("is_group") and state.bot_phone:
            can_send = await (send_task if send_task is not None else _group_can_send(phone))
            if can_send is not None:
                try:
                    if data.get("can_send", True) != can_send:
                        await asyncio.to_thread(
                            contact_repo.update, data["id"], can_send=1 if can_send else 0)
                        data["can_send"] = can_send
                        if phone in agent_handler._contacts:
                            agent_handler._contacts[phone].can_send = can_send
                except Exception as e:
                    logger.warning("[Contact] Failed to store group send permission: %s", e)
        elif send_task is not None:
            send_task.cancel()
        # Opening a conversation triggers a best-effort avatar refresh in the
        # background; if the photo changed, an `avatar_updated` WS event updates
        # it live. Include the current version for immediate cache-busting.
        data["avatar_v"] = avatar_version(settings, phone)
        asyncio.create_task(refresh_and_broadcast(deps, phone))
        return _ok(data)

    @app.get("/api/contacts/{phone}/messages")
    async def get_contact_messages(phone: str, before: int | None = None,
                                   after: int | None = None, around: int | None = None,
                                   limit: int = DEFAULT_PAGE_LIMIT):
        """One page of a conversation's history, ascending by (ts, id).

        Pass at most one of ``before`` (older than that message id), ``after``
        (newer than it) or ``around`` (that message with context on both
        sides). No cursor returns the newest page. Read-only: it never marks
        anything as read and never creates a contact."""
        if sum(v is not None for v in (before, after, around)) > 1:
            return _err("Use apenas um entre 'before', 'after' e 'around'.")
        limit = max(1, min(int(limit), MAX_PAGE_LIMIT))

        def _load():
            contact = contact_repo.get_by_phone(phone)
            if contact is None:
                return None, None
            page = message_repo.get_page(
                contact["id"], limit, before=before, after=after, around=around)
            if not page["found"]:
                return contact, None
            return contact, _page_payload(contact["id"], page)
        contact, payload = await asyncio.to_thread(_load)
        if contact is None:
            return _err("Contato não encontrado.", status=404)
        if payload is None:
            return _err("Mensagem não encontrada.", status=404)
        return _ok(payload)

    @app.delete("/api/contacts/{phone}")
    async def delete_contact(phone: str):
        """Permanently delete a contact and all associated data."""
        def _delete():
            data = contact_repo.get_by_phone(phone)
            if data is None:
                return False
            contact_repo.delete(data["id"])
            # Clear in-memory cache
            agent_handler._contacts.pop(phone, None)
            return True
        found = await asyncio.to_thread(_delete)
        if not found:
            return _err("Contato não encontrado.", status=404)
        logger.info("[Contact] Deleted contact %s", phone)
        await ws_manager.broadcast("contact_deleted", {"phone": phone})
        return _ok({"message": "Contato apagado."})

    @app.post("/api/contacts/{phone}/archive")
    async def archive_contact(phone: str, body: dict):
        """Archive or unarchive a contact (by app)."""
        archived = body.get("archived")
        if archived is None:
            return _err("Campo 'archived' é obrigatório.")
        def _archive():
            data = contact_repo.get_by_phone(phone)
            if data is None:
                return None
            contact_repo.set_archived(data["id"], bool(archived), by_app=True)
            # Update in-memory cache
            if phone in agent_handler._contacts:
                agent_handler._contacts[phone].is_archived = bool(archived)
                agent_handler._contacts[phone].archived_by_app = bool(archived)
            return bool(archived)
        result = await asyncio.to_thread(_archive)
        if result is None:
            return _err("Contato não encontrado.", status=404)
        logger.info("[Contact] %s contact %s", "Archived" if result else "Unarchived", phone)
        await ws_manager.broadcast("contact_archived", {"phone": phone, "archived": result})
        return _ok({"archived": result})

    @app.post("/api/contacts/{phone}/pin")
    async def pin_contact(phone: str, body: dict):
        """Pin or unpin a conversation (pinned ones sort to the top of the list)."""
        pinned = body.get("pinned")
        if pinned is None:
            return _err("Campo 'pinned' é obrigatório.")
        def _pin():
            data = contact_repo.get_by_phone(phone)
            if data is None:
                return None
            contact_repo.set_pinned(data["id"], bool(pinned))
            return bool(pinned)
        result = await asyncio.to_thread(_pin)
        if result is None:
            return _err("Contato não encontrado.", status=404)
        logger.info("[Contact] %s contact %s", "Pinned" if result else "Unpinned", phone)
        await ws_manager.broadcast("contact_pinned", {"phone": phone, "pinned": result})
        return _ok({"pinned": result})

    @app.post("/api/contacts/{phone}/send")
    async def send_to_contact(phone: str, body: dict):
        """Send a manual message to a contact (operator-initiated, no LLM)."""
        message = (body.get("message") or "").strip()
        if not message:
            return _err("Campo 'message' é obrigatório.")
        # Optional: quote/reply to an existing message (GOWA msg_id).
        reply_to = (body.get("reply_to") or "").strip() or None

        # Plugin filter: allow plugins to add signature/formatting/redact to operator sends
        filtered = await apply_filter(
            "filter.reply.part", message,
            {"phone": phone, "index": 0, "total": 1, "source": "operator"},
        )
        if filtered is None:
            return _err("Mensagem bloqueada por plugin.", status=400)
        message = filtered

        # Sandbox/test contact — never goes over GOWA (the number isn't real).
        # Persist the operator message locally and broadcast it, no error.
        if await asyncio.to_thread(_is_sandbox_contact, phone):
            msg_data = await asyncio.to_thread(
                agent_handler.save_operator_message, phone, message, status="operator",
                reply_to_msg_id=reply_to,
            )
            await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
            await emit_with_filter("message.sent", {
                "phone": phone, "text": message, "msg_id": None,
                "media_type": None, "media_path": None,
                "source": "operator", "status": "operator",
                "reply_to_msg_id": reply_to,
                "ts": time.time(),
            })
            logger.info("[Send] Sandbox contact %s — message saved locally (no GOWA)", phone)
            return _ok({"message": "Mensagem enviada.", "msg_id": None})

        # Resolve @Name / @todos -> real mentions for group targets. `message`
        # (friendly @Name) is saved/shown; `send_text` (inline @<number>) +
        # mentions go on the wire.
        send_text, mentions = message, None
        if "@g.us" in phone:
            send_text, mentions = await asyncio.to_thread(
                group_mentions.resolve_outgoing, phone, message)

        # Track sent message to filter GOWA echo-backs (key on the wire text)
        state.recently_sent[f"{phone}:{send_text[:120]}"] = time.time()

        # Try to send via GOWA — always save message (with status on failure)
        send_failed = False
        error_msg = ""
        send_result = None
        try:
            send_result = await asyncio.to_thread(
                gowa_client.send_message, phone, send_text, mentions, reply_to)
        except GOWASendError as e:
            logger.error("[Send] Failed to send message to %s: %s", phone, e)
            send_failed = True
            error_msg = str(e)
        except Exception as e:
            logger.error("[Send] Failed to send message to %s: %s", phone, e)
            send_failed = True
            error_msg = str(e)

        msg_id = extract_msg_id(send_result) if not send_failed else None

        # Always save to contact memory (with status="failed" if send failed)
        try:
            msg_data = await asyncio.to_thread(
                agent_handler.save_operator_message, phone, message,
                status="failed" if send_failed else "operator",
                msg_id=msg_id, reply_to_msg_id=reply_to,
            )
        except Exception as e:
            logger.error("[Send] Failed to save message for %s: %s", phone, e)
            return _err(f"Erro ao salvar mensagem: {e}", status=500)

        if send_failed:
            # Broadcast error event for frontend toast/error bubble
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar mensagem: {error_msg}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar mensagem: {error_msg}", status=500)

        logger.info("[Send] Manual message to %s: %s", phone, message[:80])

        # Broadcast to all WS clients
        await ws_manager.broadcast("new_message", {
            "phone": phone,
            "message": msg_data,
        })

        # Plugin event: manual operator send
        await emit_with_filter("message.sent", {
            "phone": phone, "text": message, "msg_id": msg_id,
            "media_type": None, "media_path": None,
            "source": "operator", "status": "operator",
            "reply_to_msg_id": reply_to,
            "ts": time.time(),
        })

        return _ok({"message": "Mensagem enviada.", "msg_id": msg_id})

    @app.post("/api/contacts/{phone}/messages/delete")
    async def delete_message(phone: str, body: dict):
        """Delete a message. scope='me' (local) or scope='all' (revoke for everyone).

        Identifies the message by GOWA ``msg_id`` (preferred) or, for local-only
        messages without one, by DB ``db_id``. ``scope='all'`` is only allowed for
        the operator's own (outgoing) messages and requires a msg_id.

        In every case the message is deleted on WhatsApp (via GOWA) but KEPT in our
        DB — it is only flagged as revoked, never hard-deleted — so the panel keeps
        showing it with a 'deleted' indicator.
        """
        scope = (body.get("scope") or "me").strip()
        msg_id = (body.get("msg_id") or "").strip()
        db_id = body.get("db_id")
        if scope not in ("me", "all"):
            return _err("scope inválido (use 'me' ou 'all').")
        if not msg_id and not db_id:
            return _err("É necessário msg_id ou db_id.")

        is_sandbox = await asyncio.to_thread(_is_sandbox_contact, phone)

        if scope == "all":
            if not msg_id:
                return _err("Apagar para todos exige uma mensagem já enviada ao WhatsApp.", status=400)
            # Only outgoing (own) messages can be revoked for everyone.
            msg = await asyncio.to_thread(message_repo.get_by_msg_id, msg_id)
            if msg and msg.get("role") == "user":
                return _err("Só é possível apagar para todos as suas próprias mensagens.", status=400)
            if not is_sandbox:
                await asyncio.to_thread(gowa_client.revoke_message, msg_id, phone)
            await asyncio.to_thread(message_repo.mark_revoked, msg_id, "all")
            await ws_manager.broadcast("message_revoked", {"phone": phone, "msg_id": msg_id})
            await emit_with_filter("message.revoked", {
                "id": msg_id, "phone": phone,
                "revoked_message_id": msg_id, "revoked_from_me": True,
                "ts": time.time(),
            })
            logger.info("[Delete] Revoked (all) msg %s for %s", msg_id, phone)
            return _ok({"message": "Mensagem apagada para todos.", "msg_id": msg_id})

        # scope == "me": delete on the linked device via GOWA, but keep the row in
        # our DB (flagged revoked) so the panel still shows it.
        was_from_me = True
        if msg_id:
            msg = await asyncio.to_thread(message_repo.get_by_msg_id, msg_id)
            was_from_me = (msg or {}).get("role") != "user"
            if not is_sandbox:
                await asyncio.to_thread(gowa_client.delete_message, msg_id, phone)
            await asyncio.to_thread(message_repo.mark_revoked, msg_id, "me")
        if db_id:
            await asyncio.to_thread(message_repo.mark_revoked_by_id, int(db_id), "me")
        await ws_manager.broadcast("message_deleted", {
            "phone": phone, "msg_id": msg_id or None, "db_id": db_id,
        })
        await emit_with_filter("message.deleted", {
            "phone": phone, "deleted_message_id": msg_id or "",
            "was_from_me": was_from_me, "ts": time.time(),
        })
        logger.info("[Delete] Revoked (me, kept in DB) msg %s/db %s for %s", msg_id, db_id, phone)
        return _ok({"message": "Mensagem apagada para você.", "msg_id": msg_id or None})

    @app.post("/api/contacts/{phone}/messages/{msg_id}/download-media")
    async def download_message_media(phone: str, msg_id: str):
        """Fetch again, from WhatsApp, a file the auto-download policy dropped.

        Best effort: WhatsApp keeps the file only for a while and GOWA has no
        media-retry, so an old message can answer "not available anymore". Never
        retried automatically; one click is one attempt.
        """
        if await asyncio.to_thread(_is_sandbox_contact, phone):
            return _err("Esta é uma conversa de teste: não há arquivo no WhatsApp.", status=400)
        contact_row = await asyncio.to_thread(contact_repo.get_by_phone, phone)
        if not contact_row:
            return _err("Conversa não encontrada.", status=404)
        target = await asyncio.to_thread(message_repo.get_media_target, contact_row["id"], msg_id)
        if not target or not media_policy.policy_kind(target.get("media_type")):
            return _err("Mensagem não encontrada.", status=404)
        if target.get("role") != "user":
            return _err("Só dá para baixar arquivos recebidos.", status=400)
        if target.get("media_path"):
            existing = media_policy.is_discardable_path(
                target["media_path"], statics_media_dir, settings.data_dir)
            if existing is not None:
                return _ok({"media_path": target["media_path"], "already": True})

        # One at a time: GOWA creates <phone>/<date>/ before it downloads and
        # writes the file seconds later, and settle_download prunes empty
        # folders. Overlapping clicks could otherwise remove a folder that a
        # sibling download is about to write into.
        async with _download_lock:
            try:
                info = await asyncio.to_thread(gowa_client.download_media, msg_id, phone)
            except GOWASendError as e:
                if e.error_type == "expired":
                    return _err("Esse arquivo não está mais disponível no WhatsApp.", status=410)
                if e.error_type == "network":
                    return _err("O WhatsApp não está acessível agora. Tente de novo em instantes.", status=502)
                logger.warning("[DownloadMedia] %s msg %s: %s", phone, msg_id, e)
                return _err("Não foi possível baixar o arquivo. Tente de novo mais tarde.", status=502)

            # GOWA answers with a path relative to its own working directory (the
            # project root). Accept it only if it lands inside statics/media.
            resolved = media_policy.is_discardable_path(
                info["file_path"], statics_media_dir, settings.data_dir)
            if resolved is None:
                logger.warning("[DownloadMedia] GOWA returned a path outside statics/media")
                return _err("Não foi possível baixar o arquivo. Tente de novo mais tarde.", status=502)
            # GOWA files an on-demand download under <phone>/<date>/ with no
            # extension; bring it to the media root like every auto-downloaded file.
            resolved = await asyncio.to_thread(
                media_policy.settle_download, resolved, statics_media_dir)
        rel = "statics/media/" + resolved.relative_to(statics_media_dir.resolve()).as_posix()

        await asyncio.to_thread(message_repo.set_media, target["id"], rel, None)
        # The Files screen caches its listing; drop it so the file shows up loose.
        await asyncio.to_thread(deps.file_manager.invalidate)
        await ws_manager.broadcast("message_media_updated", {
            "phone": phone, "msg_id": msg_id, "media_state": None, "media_path": rel,
        })
        await ws_manager.broadcast("storage_changed", {"files": 1, "bytes": 0, "messages": 1})
        logger.info("[DownloadMedia] restored a %s for %s", target.get("media_type"), phone)
        return _ok({"media_path": rel})

    @app.post("/api/contacts/{phone}/messages/react")
    async def react_to_message(phone: str, body: dict):
        """React to a message with an emoji. Empty emoji removes the operator's reaction.

        The operator's own reaction is stored under the sentinel reactor "me".
        """
        msg_id = (body.get("msg_id") or "").strip()
        emoji = (body.get("emoji") or "").strip()
        if not msg_id:
            return _err("msg_id é obrigatório.")

        if not await asyncio.to_thread(_is_sandbox_contact, phone):
            await asyncio.to_thread(gowa_client.react_to_message, msg_id, phone, emoji)
        reactions = await asyncio.to_thread(message_repo.set_reaction, msg_id, emoji, "me")
        if reactions is None:
            return _err("Mensagem não encontrada.", status=404)
        await ws_manager.broadcast("message_reaction", {
            "phone": phone, "msg_id": msg_id, "reactions": reactions,
        })
        await emit_with_filter("message.reaction", {
            "id": msg_id, "phone": phone,
            "reaction": emoji, "reacted_message_id": msg_id,
            "is_from_me": True, "ts": time.time(),
        })
        logger.info("[React] %s reacted %r to msg %s", phone, emoji, msg_id)
        return _ok({"message": "Reação registrada.", "reactions": reactions})

    async def _run_private_ai(phone: str, text: str, reply_in_chat: bool = True):
        """Process a private message via the LLM.

        Triggered by the operator's "IA lê" toggle on the private-message panel.
        Tool calls run normally and their cards are broadcast to the panel.

        If `reply_in_chat` is True, the LLM reply is sent to the contact as a
        regular assistant message. If False, each reply part is saved as a
        private note (stays only in the panel).
        """
        try:
            result = await agent_handler.aprocess_message(
                phone, text,
                save_user_message=False,
                save_response=False,
            )
            reply_text = (result.reply or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("[PrivateAI] aprocess_message failed for %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {"role": "error",
                            "content": f"Erro ao processar IA: {e}",
                            "ts": time.time()},
            })
            return

        if result.tool_calls:
            try:
                await deps.broadcast_tool_calls(
                    phone, result.tool_calls, result.contact_info)
            except Exception as e:
                logger.warning("[PrivateAI] broadcast_tool_calls failed for %s: %s",
                               phone, e)

        if not reply_text:
            return

        # The LLM may return a JSON array of strings when split_messages is on.
        parts = (parse_split_reply(reply_text)
                 if settings.get("split_messages", True) else [reply_text])
        filter_source = "private_ai" if reply_in_chat else "private_ai_note"
        # Plugin filter on the part list (can reorder/add/remove).
        parts = await apply_filter("filter.reply.parts", parts, {"phone": phone, "source": filter_source})
        if parts is None or not parts:
            return

        for i, part in enumerate(parts):
            part = await apply_filter(
                "filter.reply.part", part,
                {"phone": phone, "index": i, "total": len(parts), "source": filter_source},
            )
            if part is None:
                continue

            if not reply_in_chat:
                # AI reply stays in the panel as a private note.
                saved_note = None
                try:
                    def _save_note(p=part):
                        contact = agent_handler._get_contact(phone)
                        contact.add_message("private_note", p)
                        return message_repo.get_last(contact.id)
                    saved_note = await asyncio.to_thread(_save_note)
                except Exception as e:
                    logger.error("[PrivateAI] failed to save private note: %s", e)
                note_msg = {
                    "role": "private_note",
                    "content": part,
                    "ts": (saved_note or {}).get("ts", time.time()),
                    "status": None,
                }
                if saved_note and saved_note.get("_id"):
                    note_msg["_id"] = saved_note["_id"]
                await ws_manager.broadcast("new_message", {"phone": phone, "message": note_msg})
                continue

            state.recently_sent[f"{phone}:{part[:120]}"] = time.time()
            send_failed = False
            send_error = ""
            send_result = None
            try:
                send_result = await asyncio.to_thread(
                    gowa_client.send_message, phone, part)
            except GOWASendError as e:
                logger.error("[PrivateAI] send failed for %s: %s", phone, e)
                send_failed = True
                send_error = str(e)
            except Exception as e:
                logger.error("[PrivateAI] send failed for %s: %s", phone, e)
                send_failed = True
                send_error = str(e)

            msg_id = extract_msg_id(send_result) if not send_failed else None
            if msg_id:
                state.processed_messages.add(msg_id)

            try:
                msg_data = await asyncio.to_thread(
                    agent_handler.save_assistant_message, phone, part,
                    msg_id=msg_id,
                    status="failed" if send_failed else "sent",
                )
            except Exception as e:
                logger.error("[PrivateAI] failed to save assistant message: %s", e)
                msg_data = {
                    "role": "assistant", "content": part, "ts": time.time(),
                    "status": "failed" if send_failed else "sent", "msg_id": msg_id,
                }

            if send_failed:
                await ws_manager.broadcast("new_message", {
                    "phone": phone,
                    "message": {"role": "error",
                                "content": f"Falha ao enviar resposta da IA: {send_error}",
                                "ts": time.time()},
                })
                return
            await ws_manager.broadcast("new_message", {
                "phone": phone, "message": msg_data,
            })
            await emit_with_filter("message.sent", {
                "phone": phone, "text": part, "msg_id": msg_id,
                "media_type": None, "media_path": None,
                "source": "private_ai", "status": "sent",
                "ts": time.time(),
            })

    @app.post("/api/contacts/{phone}/private-message")
    async def send_private_message(phone: str, body: dict):
        """Save a message that stays in the panel — never delivered to the contact.

        AI processing is triggered when the operator sets `ai_read=true` (UI
        toggle "IA lê"). `ai_reply` (default true) controls whether the AI
        reply goes to the WhatsApp chat or stays as a private note.
        """
        text = (body.get("text") or "").strip()
        if not text:
            return _err("Campo 'text' é obrigatório.")

        ai_read = bool(body.get("ai_read", False))
        ai_reply = bool(body.get("ai_reply", True))

        try:
            def _save():
                contact = agent_handler._get_contact(phone)
                contact.add_message("private_note", text)
                return message_repo.get_last(contact.id)
            saved = await asyncio.to_thread(_save)
        except Exception as e:
            logger.error("[Private] Failed to save private message for %s: %s", phone, e)
            return _err(f"Erro ao salvar mensagem privada: {e}", status=500)

        # Carry the DB row id so the panel can delete the note without a reload.
        note_msg = {
            "role": "private_note",
            "content": text,
            "ts": (saved or {}).get("ts", time.time()),
            "status": None,
        }
        if saved and saved.get("_id"):
            note_msg["_id"] = saved["_id"]
        await ws_manager.broadcast("new_message", {"phone": phone, "message": note_msg})

        if ai_read:
            asyncio.create_task(_run_private_ai(phone, text, reply_in_chat=ai_reply))

        logger.info("[Private] Saved private note for %s (ai_read=%s, ai_reply=%s): %s",
                    phone, ai_read, ai_reply, text[:80])
        return _ok(note_msg)

    @app.post("/api/contacts/{phone}/improve")
    async def generate_improvement(phone: str, body: dict):
        """Analyze an AI reply flagged as wrong and save the analysis as a note.

        The operator right-clicks a wrong AI reply, writes an optional
        suggestion, and the LLM reviews the conversation, the main prompt and
        the tools (available + used) to diagnose the issue and recommend prompt
        edits. The conclusion is stored as a private note (panel-only).
        """
        feedback = (body.get("feedback") or "").strip()
        target = body.get("message") or {}

        try:
            analysis = await asyncio.to_thread(
                agent_handler.generate_improvement, phone, target, feedback)
        except Exception as e:
            logger.exception("[Improve] Failed to generate improvement for %s: %s", phone, e)
            return _err(f"Erro ao gerar análise de melhoria: {e}", status=500)

        analysis = (analysis or "").strip()
        if not analysis:
            return _err("A análise voltou vazia. Tente novamente.", status=500)

        note_text = f"🔧 Análise de melhoria\n\n{analysis}"

        # Saved as a `system` event message: shown in the panel but NOT read by
        # the AI (excluded from LLM context in message_repo.get_context).
        try:
            def _save():
                contact = agent_handler._get_contact(phone)
                contact.add_message("system", note_text)
                return message_repo.get_last(contact.id)
            saved = await asyncio.to_thread(_save)
        except Exception as e:
            logger.error("[Improve] Failed to save improvement note for %s: %s", phone, e)
            return _err(f"Erro ao salvar a análise: {e}", status=500)

        note_msg = {
            "role": "system",
            "content": note_text,
            "ts": (saved or {}).get("ts", time.time()),
            "status": None,
        }
        if saved and saved.get("_id"):
            note_msg["_id"] = saved["_id"]
        await ws_manager.broadcast("new_message", {"phone": phone, "message": note_msg})

        logger.info("[Improve] Generated improvement note for %s (feedback=%r)",
                    phone, feedback[:80])
        return _ok(note_msg)

    @app.post("/api/contacts/{phone}/retry-send")
    async def retry_send_to_contact(phone: str, body: dict):
        """Retry sending a message that previously failed."""
        message = (body.get("message") or "").strip()
        if not message:
            return _err("Campo 'message' é obrigatório.")

        # Track for echo-back filtering
        state.recently_sent[f"{phone}:{message[:120]}"] = time.time()

        send_result = None
        try:
            send_result = await asyncio.to_thread(gowa_client.send_message, phone, message)
        except GOWASendError as e:
            logger.error("[Retry] Failed to resend to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao reenviar mensagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao reenviar: {e}", status=500)
        except Exception as e:
            logger.error("[Retry] Failed to resend to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao reenviar: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao reenviar: {e}", status=500)

        msg_id = extract_msg_id(send_result)

        # Mark the existing failed message as sent
        try:
            await asyncio.to_thread(agent_handler.mark_message_sent, phone, message, msg_id)
        except Exception as e:
            logger.error("[Retry] Failed to update message status for %s: %s", phone, e)

        state.msg_count += 1
        await emit_with_filter("message.sent", {
            "phone": phone, "text": message, "msg_id": msg_id,
            "media_type": None, "media_path": None,
            "source": "retry", "status": "sent",
            "ts": time.time(),
        })
        logger.info("[Retry] Resent to %s: %s", phone, message[:80])
        return _ok({"message": "Mensagem reenviada."})

    @app.post("/api/contacts/{phone}/send-image")
    async def send_image_to_contact(
        phone: str,
        image: UploadFile = File(...),
        caption: str = Form(""),
    ):
        """Send an image to a contact (operator-initiated)."""
        suffix = Path(image.filename or "img.png").suffix or ".png"
        dest = statics_senditems_dir / f"{int(time.time() * 1000)}{suffix}"
        content = await image.read()
        dest.write_bytes(content)

        send_result = None
        # Sandbox/test contact — keep the image local, never hit GOWA.
        is_sandbox = await asyncio.to_thread(_is_sandbox_contact, phone)
        try:
            if not is_sandbox:
                send_result = await asyncio.to_thread(gowa_client.send_image, phone, str(dest), caption)
        except GOWASendError as e:
            logger.error("[Send] Failed to send image to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar imagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar imagem: {e}", status=500)
        except Exception as e:
            logger.error("[Send] Failed to send image to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao enviar imagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao enviar imagem: {e}", status=500)

        msg_id = extract_msg_id(send_result)
        # Filter GOWA echo-back: mark this msg_id as already processed so the
        # webhook ignores it when the WhatsApp roundtrip echoes the message back.
        if msg_id:
            state.processed_messages.add(msg_id)

        # Relative path for storage and frontend
        rel_path = f"statics/operator/{dest.name}"
        msg_data = {
            "role": "assistant",
            "content": caption,
            "ts": time.time(),
            "media_type": "image",
            "media_path": rel_path,
            "status": "operator",
            "msg_id": msg_id,
        }
        contact = agent_handler._get_contact(phone)
        contact.add_message("assistant", caption, media_type="image", media_path=rel_path,
                            status="operator", msg_id=msg_id,
                            media_name=file_manager.upload_name(image.filename) or None)

        await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
        await emit_with_filter("message.sent", {
            "phone": phone, "text": caption, "msg_id": msg_id,
            "media_type": "image", "media_path": rel_path,
            "source": "operator", "status": "operator",
            "ts": time.time(),
        })
        logger.info("[Send] Image sent to %s", phone)
        return _ok({"message": "Imagem enviada."})

    @app.post("/api/contacts/{phone}/send-audio")
    async def send_audio_to_contact(
        phone: str,
        audio: UploadFile = File(...),
    ):
        """Send an audio file to a contact (operator-initiated)."""
        suffix = Path(audio.filename or "voice.ogg").suffix or ".ogg"
        dest = statics_senditems_dir / f"{int(time.time() * 1000)}{suffix}"
        content = await audio.read()
        dest.write_bytes(content)

        send_result = None
        # Sandbox/test contact — keep the audio local, never hit GOWA.
        is_sandbox = await asyncio.to_thread(_is_sandbox_contact, phone)
        try:
            if not is_sandbox:
                send_result = await asyncio.to_thread(gowa_client.send_audio, phone, str(dest))
        except GOWASendError as e:
            logger.error("[Send] Failed to send audio to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar áudio: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar áudio: {e}", status=500)
        except Exception as e:
            logger.error("[Send] Failed to send audio to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao enviar áudio: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao enviar áudio: {e}", status=500)

        msg_id = extract_msg_id(send_result)
        # Filter GOWA echo-back: mark this msg_id as already processed so the
        # webhook ignores it when the WhatsApp roundtrip echoes the message back.
        if msg_id:
            state.processed_messages.add(msg_id)

        rel_path = f"statics/operator/{dest.name}"
        msg_data = {
            "role": "assistant",
            "content": "[Áudio]",
            "ts": time.time(),
            "media_type": "audio",
            "media_path": rel_path,
            "status": "operator",
            "msg_id": msg_id,
        }
        contact = agent_handler._get_contact(phone)
        contact.add_message("assistant", "[Áudio]", media_type="audio", media_path=rel_path,
                            status="operator", msg_id=msg_id,
                            media_name=file_manager.upload_name(audio.filename) or None)

        await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
        await emit_with_filter("message.sent", {
            "phone": phone, "text": "", "msg_id": msg_id,
            "media_type": "audio", "media_path": rel_path,
            "source": "operator", "status": "operator",
            "ts": time.time(),
        })
        logger.info("[Send] Audio sent to %s", phone)
        return _ok({"message": "Áudio enviado."})

    @app.post("/api/contacts/{phone}/send-document")
    async def send_document_to_contact(
        phone: str,
        document: UploadFile = File(...),
        caption: str = Form(""),
    ):
        """Send an arbitrary file (document) to a contact (operator-initiated)."""
        filename = document.filename or "arquivo"
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix
        stem = Path(safe_name).stem or "arquivo"
        dest = statics_senditems_dir / f"{int(time.time() * 1000)}_{stem}{suffix}"
        content = await document.read()
        dest.write_bytes(content)

        send_result = None
        is_sandbox = await asyncio.to_thread(_is_sandbox_contact, phone)
        try:
            if not is_sandbox:
                send_result = await asyncio.to_thread(
                    gowa_client.send_file, phone, str(dest), caption, safe_name
                )
        except GOWASendError as e:
            logger.error("[Send] Failed to send document to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar documento: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar documento: {e}", status=500)
        except Exception as e:
            logger.error("[Send] Failed to send document to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao enviar documento: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao enviar documento: {e}", status=500)

        msg_id = extract_msg_id(send_result)
        if msg_id:
            state.processed_messages.add(msg_id)

        rel_path = f"statics/operator/{dest.name}"
        text_content = f"[Documento enviado: {safe_name}]"
        if caption.strip():
            text_content = f"{text_content}\n{caption.strip()}"

        msg_data = {
            "role": "assistant",
            "content": text_content,
            "ts": time.time(),
            "media_type": "document",
            "media_path": rel_path,
            "status": "operator",
            "msg_id": msg_id,
        }
        contact = agent_handler._get_contact(phone)
        contact.add_message("assistant", text_content, media_type="document",
                            media_path=rel_path, status="operator", msg_id=msg_id,
                            media_name=file_manager.clean_name(safe_name) or None)

        await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
        await emit_with_filter("message.sent", {
            "phone": phone, "text": caption, "msg_id": msg_id,
            "media_type": "document", "media_path": rel_path,
            "source": "operator", "status": "operator",
            "ts": time.time(),
        })
        logger.info("[Send] Document sent to %s: %s", phone, safe_name)
        return _ok({"message": "Documento enviado."})

    @app.post("/api/contacts/{phone}/presence")
    async def send_presence_to_contact(phone: str, body: dict):
        """Send typing/stop presence indicator to a contact (operator-initiated)."""
        action = body.get("action", "start")
        await asyncio.to_thread(gowa_client.send_chat_presence, phone, action)
        return _ok({"status": "ok"})

    @app.post("/api/contacts/{phone}/read")
    async def mark_contact_read(phone: str):
        """Mark all messages from this contact as read (reset unread_count)."""
        def _mark():
            contact = agent_handler._get_contact(phone)
            return contact.mark_as_read()
        msg_ids = await asyncio.to_thread(_mark)
        if msg_ids:
            asyncio.create_task(_send_read_receipts(phone, msg_ids))
        return _ok({"message": "Marcado como lido."})

    @app.post("/api/contacts/mark-all-unread")
    async def mark_all_contacts_unread():
        """Mark every conversation as unread (re-light the in-app green badge)."""
        def _mark():
            count = contact_repo.mark_all_as_unread()
            # Keep already-loaded ContactMemory caches consistent.
            for contact in agent_handler._contacts.values():
                if contact.unread_count < 1:
                    contact.unread_count = 1
            return count
        count = await asyncio.to_thread(_mark)
        return _ok({"count": count, "message": "Todas as conversas marcadas como não lidas."})

    @app.post("/api/contacts/mark-all-read")
    async def mark_all_contacts_read():
        """Mark every conversation as read (clear all in-app unread badges)."""
        def _mark():
            count = contact_repo.mark_all_as_read()
            # Keep already-loaded ContactMemory caches consistent.
            for contact in agent_handler._contacts.values():
                contact.unread_count = 0
                contact.unread_ai_count = 0
            return count
        count = await asyncio.to_thread(_mark)
        return _ok({"count": count, "message": "Todas as conversas marcadas como lidas."})

    @app.post("/api/contacts/{phone}/unread")
    async def mark_contact_unread(phone: str):
        """Mark a single conversation as unread (re-light the in-app green badge)."""
        def _mark():
            contact = agent_handler._get_contact(phone)
            contact.mark_as_unread()
            return contact.unread_count
        unread_count = await asyncio.to_thread(_mark)
        return _ok({"unread_count": unread_count, "message": "Marcado como não lida."})

    @app.get("/api/contacts/{phone}/members")
    async def get_group_members(phone: str, force: bool = False):
        """List group participants with resolved names (for @mention autocomplete).

        ``force=true`` bypasses the TTL cache (used after a participant change to
        pick up a just-joined member immediately).
        """
        if "@g.us" not in phone:
            return _ok({"members": []})
        members = await asyncio.to_thread(
            group_mentions.get_members, phone, force, True)
        return _ok({"members": members})

    @app.post("/api/contacts/{phone}/toggle-ai")
    async def toggle_contact_ai(phone: str, body: dict):
        """Enable or disable AI auto-reply for a specific contact."""
        enabled = body.get("enabled")
        if enabled is None:
            return _err("Campo 'enabled' é obrigatório.")
        def _toggle():
            contact = agent_handler._get_contact(phone)
            contact.set_ai_enabled(bool(enabled))
            return contact.ai_enabled
        result = await asyncio.to_thread(_toggle)
        await ws_manager.broadcast("contact_ai_toggled", {
            "phone": phone,
            "ai_enabled": result,
        })
        await emit_with_filter("contact.ai_toggled", {
            "phone": phone, "ai_enabled": result, "ts": time.time(),
        })
        return _ok({"ai_enabled": result})

    @app.get("/api/contacts/{phone}/avatar")
    async def get_contact_avatar(phone: str):
        """Return contact's WhatsApp profile photo (cached on disk)."""
        avatars_dir = statics_senditems_dir.parent / "avatars"
        avatars_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = avatars_dir / f"{phone}.jpg"

        if avatar_path.exists():
            return FileResponse(str(avatar_path), media_type="image/jpeg")

        # Fetch from GOWA on-demand
        try:
            data = await asyncio.to_thread(gowa_client.get_avatar, phone)
        except Exception:
            data = None

        if data and isinstance(data, bytes):
            avatar_path.write_bytes(data)
            return FileResponse(str(avatar_path), media_type="image/jpeg")

        return Response(status_code=204)

    @app.put("/api/contacts/{phone}/info")
    async def update_contact_info(phone: str, body: dict):
        """Update contact info fields (name, email, profession, company, observations)."""
        def _update():
            contact = agent_handler._get_contact(phone)
            # Update scalar fields via update_info
            contact.update_info(
                name=body.get("name", ""),
                email=body.get("email", ""),
                profession=body.get("profession", ""),
                company=body.get("company", ""),
            )
            # Observations: replace entire list (update_info only appends)
            if "observations" in body:
                new_obs = [
                    o for o in body["observations"] if isinstance(o, str) and o.strip()
                ]
                contact.info["observations"] = new_obs
                contact_repo.set_observations(contact.id, new_obs)
            return contact.info
        info = await asyncio.to_thread(_update)
        await emit_with_filter("contact.updated", {
            "phone": phone, "info": info, "ts": time.time(),
        })
        return _ok(info)
