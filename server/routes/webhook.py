"""Webhook endpoint — receives real-time messages from GOWA."""

import asyncio
import json
import logging
import random
import re
import time
import uuid

from gowa.client import GOWASendError, extract_msg_id

from db.repositories import contact_repo, message_repo
from agent import group_mentions
from server import file_manager, media_policy
from server.execution import astart_execution, aend_execution, atrack_step, prune_executions
from server.helpers import _ok, parse_split_reply
from plugins.events import emit as emit_event, apply_filter, emit_with_filter

logger = logging.getLogger(__name__)


# Media types whose payload contains a downloadable ``path`` and can be
# rendered with a player/preview in the chat panel.
_PATHED_MEDIA: tuple[str, ...] = (
    "image", "audio", "video", "sticker", "document",
)


def _coerce_path(raw):
    """Accept either a string path or a dict ``{path, ...}`` from GOWA."""
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict):
        return (raw.get("path") or ""), dict(raw)
    return "", {}


def _media_name(media_type: str | None, extras: dict | None) -> str | None:
    """Name the sender gave the file, to keep in ``messages.media_name``.

    Only documents carry one: WhatsApp sends no file name for photos, voice
    notes or videos, and the ``filename`` GOWA stores for those is made up by
    GOWA itself (``image_<time>.jpg``), so it must not be shown as the sender's.
    """
    if media_type != "document":
        return None
    return file_manager.clean_name((extras or {}).get("file_name")) or None


def _extract_media(data: dict, *, is_from_me: bool, existing_text: str) -> dict:
    """Inspect a GOWA payload and resolve which media (if any) it carries.

    Returns a dict with:

    * ``media_type`` — one of ``image|audio|video|sticker|document|location|
      live_location|poll|interactive|order|product|contact|contacts`` or ``None``.
    * ``media_path`` — path on disk for playable media; ``"geo:lat,lng"`` for
      location/live_location; ``None`` for non-pathed types.
    * ``media_extras`` — type-specific metadata (caption, duration, lat/lng,
      name, options, button_id, …) or ``None`` if there's nothing extra.
    * ``text`` — final placeholder text (existing_text plus any extracted
      caption or auto-generated placeholder like ``"[Vídeo recebido]"``).
    * ``audio_path`` / ``image_path`` / ``document_path`` / ``document_name``
      — back-compat fields. Other call sites still read these individually
      to decide branches (transcription kind, batch path, etc.).

    Detection order matches the original implementation for ``image, audio,
    video_note, document`` then extends it with the new types.
    """
    text = existing_text or ""
    media_type: str | None = None
    media_path: str | None = None
    extras: dict | None = None
    audio_path = image_path = document_path = None
    document_name: str | None = None

    def _placeholder(noun: str) -> str:
        return f"[{noun} enviado]" if is_from_me else f"[{noun} recebido]"

    # — image ————————————————————————————————————————————
    raw = data.get("image")
    if raw:
        p, info = _coerce_path(raw)
        if p:
            image_path = p
            media_type = "image"
            media_path = p
            caption = (info.get("caption") or "").strip()
            if not text and caption:
                text = caption
            elif not text and is_from_me:
                text = "[Imagem enviada]"
            if caption or info.get("mimetype"):
                extras = {
                    k: v for k, v in {
                        "caption": caption or None,
                        "mimetype": info.get("mimetype"),
                    }.items() if v is not None
                } or None

    # — audio ————————————————————————————————————————————
    if media_type is None:
        raw = data.get("audio")
        if raw:
            p, info = _coerce_path(raw)
            if p:
                audio_path = p
                media_type = "audio"
                media_path = p
                if not text:
                    text = _placeholder("Áudio")
                if info.get("duration") or info.get("mimetype"):
                    extras = {
                        k: v for k, v in {
                            "duration_ms": info.get("duration"),
                            "mimetype": info.get("mimetype"),
                        }.items() if v is not None
                    } or None

    # — video_note (voice) — treated as audio ————————————————
    if media_type is None:
        raw = data.get("video_note")
        if raw:
            p, info = _coerce_path(raw)
            if p:
                audio_path = p
                media_type = "audio"
                media_path = p
                if not text:
                    text = _placeholder("Áudio")
                extras = {"is_voice_note": True}

    # — video ————————————————————————————————————————————
    if media_type is None:
        raw = data.get("video")
        if raw:
            p, info = _coerce_path(raw)
            if p:
                media_type = "video"
                media_path = p
                caption = (info.get("caption") or "").strip()
                if not text and caption:
                    text = caption
                elif not text:
                    text = _placeholder("Vídeo")
                extras = {
                    k: v for k, v in {
                        "caption": caption or None,
                        "duration_ms": info.get("duration"),
                        "mimetype": info.get("mimetype"),
                    }.items() if v is not None
                } or None

    # — sticker ——————————————————————————————————————————
    if media_type is None:
        raw = data.get("sticker")
        if raw:
            p, info = _coerce_path(raw)
            if p:
                media_type = "sticker"
                media_path = p
                if not text:
                    text = "[Sticker]"
                if info.get("is_animated") is not None or info.get("mimetype"):
                    extras = {
                        k: v for k, v in {
                            "is_animated": info.get("is_animated"),
                            "mimetype": info.get("mimetype"),
                        }.items() if v is not None
                    } or None

    # — document —————————————————————————————————————————
    if media_type is None:
        raw = data.get("document")
        if raw:
            p, info = _coerce_path(raw)
            if p:
                document_path = p
                media_type = "document"
                media_path = p
                document_name = (info.get("file_name")
                                 or info.get("filename") or "")
                # GOWA echoes the document caption into the top-level body,
                # so `existing_text` may already equal it.
                caption = (info.get("caption") or "").strip() or text
                # With auto-download ON, the GOWA webhook does NOT carry the
                # original filename and the on-disk path is UUID-based, so it
                # can't be recovered here. The webhook layer resolves the real
                # name via GOWA's chat-storage API and rebuilds `text`.
                label = document_name or "documento"
                verb = "enviado" if is_from_me else "recebido"
                text = (f"[Documento {verb}: {label}]"
                        + (f"\n{caption}" if caption else ""))
                extras = {
                    k: v for k, v in {
                        "file_name": document_name or None,
                        "mimetype": info.get("mimetype"),
                        "caption": caption or None,
                    }.items() if v is not None
                } or None

    # — location ——————————————————————————————————————————
    if media_type is None:
        loc = data.get("location")
        if isinstance(loc, dict):
            lat = loc.get("latitude") or loc.get("lat")
            lng = loc.get("longitude") or loc.get("lng")
            if lat is not None and lng is not None:
                name = (loc.get("name") or "").strip()
                address = (loc.get("address") or "").strip()
                media_type = "location"
                media_path = f"geo:{lat},{lng}"
                if not text:
                    if name:
                        text = f"[Localização: {name}]"
                    elif address:
                        text = f"[Localização: {address}]"
                    else:
                        text = f"[Localização: {lat},{lng}]"
                extras = {
                    "lat": lat, "lng": lng,
                    **({"name": name} if name else {}),
                    **({"address": address} if address else {}),
                }

    # — live_location —————————————————————————————————————
    if media_type is None:
        live = data.get("live_location")
        if isinstance(live, dict):
            lat = live.get("latitude") or live.get("lat")
            lng = live.get("longitude") or live.get("lng")
            if lat is not None and lng is not None:
                media_type = "live_location"
                media_path = f"geo:{lat},{lng}"
                if not text:
                    text = "[Localização ao vivo]"
                extras = {"lat": lat, "lng": lng}

    # — poll ——————————————————————————————————————————————
    if media_type is None:
        poll = data.get("poll")
        if isinstance(poll, dict):
            name = (poll.get("name") or "").strip()
            options = poll.get("options") or []
            opt_titles = [
                (o.get("name") or o.get("optionName") or "").strip()
                for o in options if isinstance(o, dict)
            ]
            if name or opt_titles:
                media_type = "poll"
                if not text:
                    text = f"[Enquete: {name or 'sem título'}]"
                extras = {"name": name, "options": opt_titles}

    # — interactive responses (buttons / list) ———————————————
    if media_type is None:
        br = data.get("buttons_response") or data.get("buttonsResponse")
        if isinstance(br, dict):
            title = (br.get("title") or br.get("display_text") or "").strip()
            button_id = (br.get("button_id") or br.get("selected_id") or "").strip()
            media_type = "interactive"
            if not text:
                text = f"[Resposta: {title or button_id or 'sem id'}]"
            extras = {"button_id": button_id, "title": title}
    if media_type is None:
        lr = data.get("list_response") or data.get("listResponse")
        if isinstance(lr, dict):
            title = (lr.get("title") or "").strip()
            row_id = (lr.get("row_id") or lr.get("selected_id") or "").strip()
            media_type = "interactive"
            if not text:
                text = f"[Seleção: {title or row_id or 'sem id'}]"
            extras = {"row_id": row_id, "title": title}

    # — order ————————————————————————————————————————————
    if media_type is None:
        order = data.get("order")
        if isinstance(order, dict):
            item_count = order.get("item_count") or order.get("itemCount")
            media_type = "order"
            if not text:
                if item_count is not None:
                    text = f"[Pedido: {item_count} item(ns)]"
                else:
                    text = "[Pedido recebido]"
            extras = {
                k: v for k, v in {
                    "item_count": item_count,
                    "total": order.get("total"),
                    "currency": order.get("currency"),
                }.items() if v is not None
            } or None

    # — product ——————————————————————————————————————————
    if media_type is None:
        prod = data.get("product")
        if isinstance(prod, dict):
            media_type = "product"
            if not text:
                text = "[Produto compartilhado]"
            extras = {
                "product_id": prod.get("product_id") or prod.get("id"),
                "title": prod.get("title"),
            }

    # — contact / vCard (single or array) ——————————————————
    if media_type is None and not text:
        shared: list[tuple[str, str]] = []
        single = data.get("contact")
        if isinstance(single, dict):
            n = (single.get("displayName") or single.get("display_name")
                 or single.get("name") or "").strip()
            ph = (single.get("phone_number") or single.get("phoneNumber") or "").strip()
            shared.append((n, ph))
        arr = data.get("contacts_array") or data.get("contactsArray")
        if isinstance(arr, list):
            for c in arr:
                if not isinstance(c, dict):
                    continue
                n = (c.get("displayName") or c.get("display_name")
                     or c.get("name") or "").strip()
                ph = (c.get("phone_number") or c.get("phoneNumber") or "").strip()
                shared.append((n, ph))
        if shared:
            media_type = "contact" if len(shared) == 1 else "contacts"
            if len(shared) == 1:
                n, ph = shared[0]
                label = n or ph or "sem nome"
                suffix = f" ({ph})" if ph and n else ""
                text = f"[Contato compartilhado: {label}{suffix}]"
            else:
                names = ", ".join(n or p or "?" for n, p in shared)
                text = f"[Contatos compartilhados ({len(shared)}): {names}]"
            extras = {"contacts": [
                {"name": n, "phone": ph} for n, ph in shared
            ]}

    return {
        "media_type": media_type,
        "media_path": media_path,
        "media_extras": extras,
        "text": text,
        "audio_path": audio_path,
        "image_path": image_path,
        "document_path": document_path,
        "document_name": document_name,
    }


# Keys that hold the id of the quoted message (the message being replied to).
_REPLY_ID_KEYS = (
    "reply_message_id", "quoted_message_id", "quotedMessageId",
    "replied_id", "repliedId", "reply_to", "reply_to_id", "in_reply_to",
    "quoted_id", "quotedId",
)
# Keys that hold the quoted message id *inside* a context-info object (whatsmeow's
# ContextInfo.StanzaID). Here a bare ``id`` IS the quoted id (unlike a `message`
# wrapper, where ``id`` is the current message's own id).
_REPLY_CTX_KEYS = ("stanza_id", "stanzaId", "StanzaID", "quoted_message_id", "id", "Id")


def _deep_find_reply_id(obj) -> str | None:
    """Recursively scan a payload for a key that clearly names a quoted/replied
    message id (e.g. ``replied_id``, ``reply_message_id``, ``stanza_id``). Requires
    the key to mention repl/quot/stanza AND id, so plain ``id`` or the quoted *text*
    (``quoted_message``) are never mistaken for it."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int)) and v != "" and isinstance(k, str):
                kl = k.lower()
                if (("repl" in kl or "quot" in kl or "stanza" in kl) and "id" in kl):
                    return str(v)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _deep_find_reply_id(v)
                if found:
                    return found
    elif isinstance(obj, list):
        for it in obj:
            found = _deep_find_reply_id(it)
            if found:
                return found
    return None


def _extract_reply_to(data: dict) -> str | None:
    """Best-effort extraction of the quoted message id from a GOWA webhook payload.

    GOWA is not consistent about exposing this for inbound messages (and nests it
    differently across versions), so we probe, in order: flat keys, a nested
    ``message`` object, a ``context_info`` object (flat or inside ``message``), and
    finally a guarded recursive scan. Returns None when nothing quoted-looking is
    present.
    """
    if not isinstance(data, dict):
        return None

    # 1) Flat well-known keys.
    for key in _REPLY_ID_KEYS:
        val = data.get(key)
        if val:
            return str(val)

    # 2) Some GOWA builds nest the text + reply id under a `message` object.
    msg = data.get("message")
    if isinstance(msg, dict):
        for key in _REPLY_ID_KEYS:
            val = msg.get(key)
            if val:
                return str(val)

    # 3) A container describing the quoted message (context info / quoted / reply).
    #    Inside one of these a bare ``id`` IS the quoted message id. Probe both at
    #    the top level and nested under ``message``.
    container_names = ("context_info", "contextInfo", "ContextInfo",
                       "quoted", "quoted_message", "quotedMessage", "reply")
    scopes = [data] + ([msg] if isinstance(msg, dict) else [])
    for scope in scopes:
        for name in container_names:
            ctx = scope.get(name)
            if isinstance(ctx, dict):
                for key in _REPLY_CTX_KEYS:
                    val = ctx.get(key)
                    if val:
                        return str(val)

    # 4) Last resort: scan the whole payload for a reply/quote *id* key.
    return _deep_find_reply_id(data)


def register_routes(app, deps):
    agent_handler = deps.agent_handler
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings
    statics_media_dir = deps.statics_media_dir
    data_dir = settings.data_dir

    # ── Media auto-download policy ─────────────────────────────────
    # GOWA downloads everything (it only has a global switch). The matrix in
    # ``media_autodownload`` decides what stays: a file the policy does not want
    # is kept on disk while the message is processed (so the AI can still see
    # it and transcription/description can read it) and dropped at the end of
    # the cycle by ``_finalize_discards``.

    def _should_discard(media_type: str | None, media_path: str | None,
                        is_group: bool) -> bool:
        if not media_path or statics_media_dir is None:
            return False
        if media_policy.policy_kind(media_type) is None:
            return False
        if media_policy.should_keep(settings.get("media_autodownload"), media_type, is_group):
            return False
        # Only ever discard a real file inside statics/media; anything else
        # (odd path from a plugin filter, file already gone) is simply kept.
        return media_policy.is_discardable_path(
            media_path, statics_media_dir, data_dir) is not None

    async def _finalize_discards(phone: str, jobs: list[tuple[str | None, str]]) -> None:
        """Delete the files of messages the policy did not keep and flag them."""
        for msg_id, path in jobs:
            try:
                target = media_policy.is_discardable_path(path, statics_media_dir, data_dir)
                if target is None:
                    continue
                await asyncio.to_thread(target.unlink)
                changed = await asyncio.to_thread(
                    message_repo.mark_media_state, [path], "discarded")
                if changed:
                    await ws_manager.broadcast("message_media_updated", {
                        "phone": phone, "msg_id": msg_id,
                        "media_state": "discarded", "media_path": None,
                    })
            except Exception as e:  # never let cleanup break message handling
                logger.warning("[Media] could not discard a file for %s: %s", phone, e)

    # ── Group Mention Helpers ──────────────────────────────────────

    def _is_bot_mentioned(text: str, data: dict) -> bool:
        """Check if the bot is mentioned in a group message."""
        if not text:
            return False
        text_lower = text.lower()
        # Check @phone mention
        bot_phone = state.bot_phone
        if bot_phone and f"@{bot_phone}" in text:
            return True
        # Check @name mention (case-insensitive)
        bot_name = state.bot_name
        if bot_name and f"@{bot_name.lower()}" in text_lower:
            return True
        # Check mentioned_jids from GOWA payload (if present)
        mentioned = data.get("mentioned_jids", data.get("mentioned", []))
        if mentioned and bot_phone:
            for jid in mentioned:
                if bot_phone in str(jid):
                    return True
        return False

    def _strip_bot_mention(text: str) -> str:
        """Remove bot @mention from message text."""
        bot_phone = state.bot_phone
        bot_name = state.bot_name
        if bot_phone:
            text = text.replace(f"@{bot_phone}", "").strip()
        if bot_name:
            text = re.sub(rf"@{re.escape(bot_name)}", "", text, flags=re.IGNORECASE).strip()
        return text

    # ── Reply Splitting & Sending ─────────────────────────────────

    async def _send_reply(phone: str, reply: str):
        """Send reply (possibly split into multiple parts) and broadcast."""
        # Plugin filter: full raw reply before split
        reply = await apply_filter("filter.reply.raw", reply, {"phone": phone})
        if reply is None:
            logger.info("[Batch] reply for %s aborted by filter.reply.raw", phone)
            return

        split_enabled = settings.get("split_messages", True)

        if split_enabled:
            parts = parse_split_reply(reply)
        else:
            parts = [reply]

        # Plugin filter: list of parts (can add/remove/reorder)
        parts = await apply_filter("filter.reply.parts", parts, {"phone": phone})
        if parts is None or not parts:
            logger.info("[Batch] reply for %s aborted by filter.reply.parts", phone)
            return

        # Initial response delay (simulates typing)
        delay_min = settings.get("response_delay_min", 1.0)
        delay_max = settings.get("response_delay_max", 3.0)
        await asyncio.sleep(random.uniform(delay_min, delay_max))

        sent_parts = []  # collect (part_text, msg_id) for saving after send
        for i, part in enumerate(parts):
            # Plugin filter: each part right before send (signature, formatting, redact)
            part = await apply_filter(
                "filter.reply.part", part,
                {"phone": phone, "index": i, "total": len(parts)},
            )
            if part is None:
                logger.info("[Batch] part %d for %s skipped by filter.reply.part", i + 1, phone)
                continue

            if i > 0:
                # Inter-message delay with ±0.5s variation
                base_delay = settings.get("split_message_delay", 2.0)
                if base_delay > 0:
                    await asyncio.sleep(base_delay + random.uniform(-0.5, 0.5))
                # Re-send typing indicator between parts
                try:
                    await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                except Exception:
                    pass

            # Resolve @Name / @todos -> real mentions for group targets. We keep
            # `part` (friendly @Name) for save/broadcast and send `send_text`
            # (inline @<number>) + mentions on the wire.
            send_text, mentions = part, None
            if "@g.us" in phone:
                send_text, mentions = await asyncio.to_thread(
                    group_mentions.resolve_outgoing, phone, part)

            # Track for echo-back filtering (key on the wire text we actually send)
            sent_key = f"{phone}:{send_text[:120]}"
            state.recently_sent[sent_key] = time.time()

            send_result = None
            try:
                send_result = await asyncio.to_thread(
                    gowa_client.send_message, phone, send_text, mentions)
                await atrack_step("gowa_send", {"phone": phone, "part": i + 1, "total_parts": len(parts)})
            except GOWASendError as e:
                logger.error("[Batch] Send failed for %s (part %d/%d): %s", phone, i + 1, len(parts), e)
                await atrack_step("gowa_send", {
                    "phone": phone, "part": i + 1, "error": str(e),
                }, status="error")
                await asyncio.to_thread(gowa_client.stop_chat_presence, phone)
                await ws_manager.broadcast("new_message", {
                    "phone": phone,
                    "message": {"role": "error", "content": f"Falha ao enviar: {e}", "ts": time.time()},
                })
                return

            part_msg_id = extract_msg_id(send_result)
            sent_parts.append((part, part_msg_id))

            # Broadcast each part to frontend individually
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {"role": "assistant", "content": part, "ts": time.time(),
                            "status": "sent", "msg_id": part_msg_id},
            })

            # Plugin event: AI reply leg
            await emit_with_filter("message.sent", {
                "phone": phone, "text": part, "msg_id": part_msg_id,
                "media_type": None, "media_path": None,
                "source": "ai", "status": "sent",
                "ts": time.time(),
            })

        # Save each part as a separate message to preserve split across page refresh
        for part, part_msg_id in sent_parts:
            try:
                await asyncio.to_thread(agent_handler.save_assistant_message, phone, part,
                                        msg_id=part_msg_id, status="sent")
                # Increment unread AI count (operator hasn't seen this reply yet)
                contact = agent_handler._contacts.get(phone)
                if contact:
                    await asyncio.to_thread(contact.increment_unread_ai)
            except Exception as e:
                logger.error("[Batch] Failed to save reply for %s: %s", phone, e)

        await asyncio.to_thread(gowa_client.stop_chat_presence, phone)
        state.msg_count += 1
        full_reply = "\n".join(parts)
        await atrack_step("response_sent", {
            "phone": phone,
            "parts": len(parts),
            "reply_preview": full_reply[:200],
        })
        logger.info("[Batch] Replied to %s (%d parts): %s", phone, len(parts), full_reply[:80])

        await ws_manager.broadcast("status", {
            "connected": state.connected,
            "msg_count": state.msg_count,
            "auto_reply_running": state.auto_reply_running,
            "bot_phone": state.bot_phone,
            "bot_name": state.bot_name,
        })

    async def _broadcast_tool_calls(phone: str, tool_calls: list[dict],
                                    contact_info: dict | None = None):
        """Broadcast private messages for each tool call executed by the LLM."""
        contact = agent_handler._get_contact(phone)
        for tc in tool_calls:
            tool_name = tc.get("tool", "unknown")
            args = tc.get("args", {})
            # Format: tool name + each arg on its own line
            lines = [f"\U0001f527 {tool_name}"]
            for key, value in args.items():
                lines.append(f"{key}: {value}")
            content = "\n".join(lines)

            contact.add_message("tool_call", content)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "tool_call",
                    "content": content,
                    "ts": time.time(),
                },
            })

        # Broadcast updated contact info so the frontend refreshes name/details
        if contact_info:
            logger.info("[ToolCall] Broadcasting contact_info_updated for %s: %s", phone, contact_info)
            await ws_manager.broadcast("contact_info_updated", {
                "phone": phone,
                "info": contact_info,
            })

        # If transfer_to_human was called, broadcast alert + state updates
        if any(tc.get("tool") == "transfer_to_human" for tc in tool_calls):
            await ws_manager.broadcast("human_transfer_alert", {"phone": phone})
            await ws_manager.broadcast("contact_ai_toggled", {
                "phone": phone,
                "ai_enabled": False,
            })
            await ws_manager.broadcast("tags_changed", agent_handler.tag_registry.all())
            await ws_manager.broadcast("contact_tags_updated", {
                "phone": phone,
                "tags": list(contact.tags),
            })

    # Expose broadcast_tool_calls for sandbox route
    deps.broadcast_tool_calls = _broadcast_tool_calls

    # ── Audio Transcription Delivery ──────────────────────────────

    async def _deliver_audio_transcription(phone: str, contact, transcription: str):
        """Deliver an audio transcription based on the configured target.

        target=private → save as 'transcription' role (operator-only card in the panel)
        target=chat    → send a new WhatsApp message with the configured prefix
        """
        target = settings.get("audio_transcription_target", "private")

        if target == "chat":
            chat_prefix = settings.get("audio_transcription_chat_prefix", "") or ""
            chat_message = f"{chat_prefix}{transcription}" if chat_prefix else transcription
            # Suppress GOWA echo-back for the message we're about to send
            sent_key = f"{phone}:{chat_message[:120]}"
            state.recently_sent[sent_key] = time.time()
            try:
                send_result = await asyncio.to_thread(
                    gowa_client.send_message, phone, chat_message)
                sent_msg_id = extract_msg_id(send_result)
                await asyncio.to_thread(
                    contact.add_message, "assistant", chat_message,
                    msg_id=sent_msg_id, status="operator")
                await ws_manager.broadcast("new_message", {
                    "phone": phone,
                    "message": {
                        "role": "assistant",
                        "content": chat_message,
                        "ts": time.time(),
                        "status": "operator",
                        "msg_id": sent_msg_id,
                    },
                })
                return
            except GOWASendError as e:
                logger.error("[Webhook] Failed to send transcription to chat for %s: %s", phone, e)
                state.recently_sent.pop(sent_key, None)
                # Fall through to private so the transcription is not lost.

        # private target (or fallback after a failed chat send)
        await asyncio.to_thread(contact.add_message, "transcription", transcription)
        await ws_manager.broadcast("new_message", {
            "phone": phone,
            "message": {
                "role": "transcription",
                "content": transcription,
                "ts": time.time(),
            },
        })

    async def _maybe_transcribe(
        media_kind: str,            # "audio" | "image" | "document"
        path: str,
        *,
        phone: str,
        source: str,                # "batch" | "echo" | "group_no_mention"
        is_group: bool = False,
        group_jid: str | None = None,
        file_name: str = "",        # document only — original filename
        mimetype: str = "",         # document only — best-effort mime hint
    ) -> str:
        """Run audio transcription / image description / document reading.

        Wraps the transcribe call sites in the codebase, exposing two plugin
        hooks: ``filter.transcription.should_run`` (bool, can pull the brake)
        and ``filter.transcription.result`` (str, can rewrite the transcript).
        Returns the final transcription string — empty when the action was
        skipped, failed, or yielded nothing.
        """
        # Core enabled-by-config gate — keep semantics identical to before
        # so plugins can only *narrow* the policy, never widen it.
        if media_kind == "audio":
            audio_mode = settings.get("audio_transcription_mode", "received")
            allow = (source == "echo" and audio_mode in ("sent", "both")) or (
                source != "echo" and audio_mode in ("received", "both")
            )
        elif media_kind == "document":
            allow = bool(settings.get("document_transcription_enabled", True))
        else:  # image
            allow = bool(settings.get("image_transcription_enabled", True))
        if not allow:
            return ""

        extras = {
            "phone": phone,
            "media_kind": media_kind,
            "media_path": path,
            "is_group": is_group,
            "group_jid": group_jid,
            "source": source,
        }
        should = await apply_filter(
            "filter.transcription.should_run", True, extras
        )
        if not should:
            return ""

        try:
            if media_kind == "audio":
                raw = await asyncio.to_thread(agent_handler.transcribe_audio, path, phone)
            elif media_kind == "document":
                raw = await asyncio.to_thread(
                    agent_handler.transcribe_document, path, phone, file_name, mimetype
                )
            else:
                raw = await asyncio.to_thread(agent_handler.describe_image, path, phone)
        except Exception as e:
            logger.error(
                "[Transcription] %s failed for %s: %s",
                media_kind, phone, e,
            )
            return ""

        extras["model"] = (
            getattr(agent_handler, "audio_model", None) if media_kind == "audio"
            else getattr(agent_handler, "document_model", None) if media_kind == "document"
            else getattr(agent_handler, "image_model", None)
        )
        final = await apply_filter(
            "filter.transcription.result", raw or "", extras
        )
        return final or ""

    # ── Batch Processing ──────────────────────────────────────────

    # ── Typing-Aware Orchestrator ─────────────────────────────────

    async def _wait_typing_paused(phone: str, max_wait: float = 30.0):
        """Block while the contact is typing/recording. Defensive timeout to avoid hangs.

        WhatsApp emits a single `composing` event when the user starts typing and a
        `paused` event when they stop — there is no heartbeat in between. The stale
        check below is a fallback for cases where `paused` never arrives (dropped
        connection, app killed, etc.) — set generously so genuine long typing isn't cut.
        """
        start = time.time()
        while True:
            ts = state.typing_state.get(phone)
            if not ts or not ts.get("active"):
                return
            # No event for 25s → assume paused (defensive)
            if time.time() - ts.get("last_ts", 0) > 25:
                logger.info("[Orchestrator] %s typing event stale, assuming paused", phone)
                state.typing_state[phone] = {**ts, "active": False}
                return
            if time.time() - start > max_wait:
                logger.warning("[Orchestrator] %s typing wait timeout %.1fs", phone, max_wait)
                state.typing_state[phone] = {**ts, "active": False}
                return
            await asyncio.sleep(0.3)

    async def _send_with_typing_guard(phone: str, reply: str):
        """Wait for contact to stop typing, mark sending=True, then send (uncancellable phase)."""
        await _wait_typing_paused(phone)
        state.sending[phone] = True
        try:
            await _send_reply(phone, reply)
        finally:
            state.sending[phone] = False

    def _schedule_orchestrator(phone: str):
        """Cancel existing orchestrator (unless mid-send) and spawn a new one."""
        existing = state.processing_tasks.get(phone)
        if existing and not existing.done():
            if state.sending.get(phone):
                # Mid-send — don't cancel. The current orchestrator will spawn the next
                # cycle automatically when it finishes sending (sees pending_messages).
                return
            existing.cancel()
        state.processing_tasks[phone] = asyncio.create_task(_orchestrate(phone))

    async def _run_one_cycle(phone: str, items: list[dict]):
        """One processing cycle: text batch (single LLM call) + each media item separately.

        Cancellable via task.cancel() up until the SEND phase, which is guarded by
        state.sending[phone]=True so the webhook does not interrupt mid-send.
        """
        exec_id = await astart_execution(phone, "webhook")
        # (msg_id, path) of received files the auto-download policy drops once
        # the AI is done with them. Collected up front and handled in
        # ``finally`` so every exit path (skipped reply, API error, a new
        # message cancelling the cycle half-way) still cleans up the disk.
        discard_jobs: list[tuple[str | None, str]] = [
            (it.get("msg_id"), it["media_path"])
            for it in items if it.get("discard") and it.get("media_path")
        ]
        try:
            await atrack_step("webhook_received", {
                "phone": phone,
                "items": [
                    {k: v for k, v in it.items() if k != "audio_path" or v}
                    for it in items
                ],
            })

            contact = agent_handler._get_contact(phone)

            text_parts: list[str] = []
            text_msg_ids: list[str] = []
            text_reply_to: str | None = None
            media_items: list[dict] = []
            for item in items:
                if (item.get("image_path") or item.get("audio_path")
                        or item.get("media_type")):
                    media_items.append(item)
                else:
                    text_parts.append(item.get("text", ""))
                    if item.get("msg_id"):
                        text_msg_ids.append(item["msg_id"])
                    # Best-effort: the combined batch quotes the last quoted item.
                    if item.get("reply_to_msg_id"):
                        text_reply_to = item["reply_to_msg_id"]

            await atrack_step("batch_accumulated", {
                "text_count": len(text_parts),
                "media_count": len(media_items),
                "combined_preview": "\n".join(t for t in text_parts if t)[:200],
            })

            if contact.ai_enabled and settings.get("auto_reply", True):
                msg_ids = await asyncio.to_thread(contact.mark_user_messages_as_read)
                if msg_ids:
                    for mid in msg_ids:
                        try:
                            await asyncio.to_thread(gowa_client.mark_as_read, mid, phone)
                        except Exception:
                            pass
                    await ws_manager.broadcast("messages_read", {"phone": phone, "only_user": True})

            # ── Text batch ──────────────────────────────────
            if text_parts:
                combined = "\n".join(t for t in text_parts if t)
                if combined:
                    logger.info("[Batch] Processing %d text messages from %s: %s",
                                len(text_parts), phone, combined[:80])
                    last_msg_id = text_msg_ids[-1] if text_msg_ids else None
                    contact.add_message("user", combined, msg_id=last_msg_id,
                                        reply_to_msg_id=text_reply_to)
                    await emit_with_filter("message.saved", {
                        "phone": phone, "text": combined, "msg_id": last_msg_id,
                        "media_type": None, "media_path": None,
                        "is_group": contact.is_group,
                        "source": "batch_text",
                        "ts": time.time(),
                    })
                    if contact.ai_enabled and settings.get("auto_reply", True):
                        if not agent_handler.api_key:
                            notice = "[WhatsBot-Lite] API key não configurada."
                            contact.add_message("system_notice", notice)
                            await ws_manager.broadcast("new_message", {
                                "phone": phone,
                                "message": {"role": "system_notice", "content": notice, "ts": time.time()},
                            })
                        else:
                            try:
                                await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                                # Cancellable LLM call
                                result = await agent_handler.aprocess_message(
                                    phone, combined,
                                    save_user_message=False, save_response=False)
                                if result.tool_calls:
                                    await _broadcast_tool_calls(phone, result.tool_calls, result.contact_info)
                                if result.reply:
                                    if result.reply.startswith("[WhatsBot-Lite]"):
                                        contact.add_message("system_notice", result.reply)
                                        await ws_manager.broadcast("new_message", {
                                            "phone": phone,
                                            "message": {"role": "system_notice", "content": result.reply, "ts": time.time()},
                                        })
                                    else:
                                        await _send_with_typing_guard(phone, result.reply)
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                logger.error("[Batch] Agent error for %s: %s", phone, e)
                                await atrack_step("error", {"error": str(e), "phase": "text_processing"}, status="error")

            # ── Media items (each handled individually) ─────
            for item in media_items:
                text = item.get("text", "")
                image_path = item.get("image_path")
                audio_path = item.get("audio_path")
                document_path = (item.get("media_path")
                                 if item.get("media_type") == "document" else None)
                doc_extras = item.get("media_extras") or {}

                # media_type/media_path resolved by _extract_media; fall back to
                # the image/audio paths for items predating the typed fields.
                media_label = item.get("media_type") or ("image" if image_path else "audio")
                logger.info("[Batch] Processing %s from %s", media_label, phone)

                _saved_text = text or ("[Áudio recebido]" if audio_path else "")
                _saved_media_type = item.get("media_type") or ("image" if image_path else "audio")
                _saved_media_path = item.get("media_path") or image_path or audio_path
                contact.add_message(
                    "user", _saved_text,
                    media_type=_saved_media_type,
                    media_path=_saved_media_path,
                    msg_id=item.get("msg_id"),
                    reply_to_msg_id=item.get("reply_to_msg_id"),
                    media_name=_media_name(_saved_media_type, item.get("media_extras")),
                )
                await emit_with_filter("message.saved", {
                    "phone": phone, "text": _saved_text,
                    "msg_id": item.get("msg_id"),
                    "media_type": _saved_media_type,
                    "media_path": _saved_media_path,
                    "media_extras": item.get("media_extras"),
                    "media_will_discard": bool(item.get("discard")),
                    "is_group": contact.is_group,
                    "source": "batch_media",
                    "ts": time.time(),
                })

                transcription = ""
                if audio_path:
                    transcription = await _maybe_transcribe(
                        "audio", audio_path,
                        phone=phone, source="batch",
                        is_group=contact.is_group,
                        group_jid=phone if contact.is_group else None,
                    )
                elif image_path:
                    transcription = await _maybe_transcribe(
                        "image", image_path,
                        phone=phone, source="batch",
                        is_group=contact.is_group,
                        group_jid=phone if contact.is_group else None,
                    )
                elif document_path:
                    transcription = await _maybe_transcribe(
                        "document", document_path,
                        phone=phone, source="batch",
                        is_group=contact.is_group,
                        group_jid=phone if contact.is_group else None,
                        file_name=doc_extras.get("file_name") or "",
                        mimetype=doc_extras.get("mimetype") or "",
                    )

                if transcription:
                    if audio_path:
                        new_content = f"[Transcrição do áudio]: {transcription}"
                    elif image_path:
                        desc_prefix = f"[Descrição da imagem]: {transcription}"
                        new_content = f"{desc_prefix}\n{text}" if text else desc_prefix
                    elif document_path:
                        doc_prefix = f"[Conteúdo do documento]: {transcription}"
                        new_content = f"{text}\n{doc_prefix}" if text else doc_prefix
                    else:
                        new_content = None
                    if new_content:
                        await asyncio.to_thread(
                            agent_handler.update_last_user_message_content, phone, new_content
                        )
                    if audio_path:
                        await _deliver_audio_transcription(phone, contact, transcription)
                    else:
                        # Image/document content — delivered as a private panel card.
                        contact.add_message("transcription", transcription)
                        await ws_manager.broadcast("new_message", {
                            "phone": phone,
                            "message": {
                                "role": "transcription",
                                "content": transcription,
                                "ts": time.time(),
                            },
                        })

                if not contact.ai_enabled or not settings.get("auto_reply", True):
                    continue

                if not agent_handler.api_key:
                    notice = "[WhatsBot-Lite] API key não configurada."
                    contact.add_message("system_notice", notice)
                    await ws_manager.broadcast("new_message", {
                        "phone": phone,
                        "message": {"role": "system_notice", "content": notice, "ts": time.time()},
                    })
                    continue

                llm_text = text or ""
                if audio_path:
                    if transcription:
                        llm_text = f"[Transcrição do áudio]: {transcription}"
                    else:
                        llm_text = llm_text or "[Áudio recebido]"
                elif image_path and transcription:
                    prefix = f"[Descrição da imagem]: {transcription}"
                    llm_text = f"{prefix}\n{text}" if text else prefix
                elif document_path and transcription:
                    doc_prefix = f"[Conteúdo do documento]: {transcription}"
                    llm_text = f"{text}\n{doc_prefix}" if text else doc_prefix

                try:
                    await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                    result = await agent_handler.aprocess_message(
                        phone,
                        llm_text,
                        save_user_message=False, save_response=False,
                        image_path=image_path if not transcription else None,
                    )
                    if result.tool_calls:
                        await _broadcast_tool_calls(phone, result.tool_calls, result.contact_info)
                    if result.reply:
                        if result.reply.startswith("[WhatsBot-Lite]"):
                            contact.add_message("system_notice", result.reply)
                            await ws_manager.broadcast("new_message", {
                                "phone": phone,
                                "message": {"role": "system_notice", "content": result.reply, "ts": time.time()},
                            })
                        else:
                            await _send_with_typing_guard(phone, result.reply)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("[Batch] Agent error for %s (%s): %s", phone, media_label, e)
                    await atrack_step("error", {"error": str(e), "phase": f"{media_label}_processing"}, status="error")

            await aend_execution(exec_id)
        except asyncio.CancelledError:
            await aend_execution(exec_id, error="cancelled")
            raise
        except Exception as exc:
            await aend_execution(exec_id, error=str(exc))
        finally:
            if discard_jobs:
                await _finalize_discards(phone, discard_jobs)

        max_exec = settings.get("max_executions", 200)
        try:
            await asyncio.to_thread(prune_executions, max_exec)
        except Exception:
            pass

    async def _orchestrate(phone: str):
        """Typing-aware batch orchestrator: wait → batch_delay → wait → cycle.

        Phases (each cancellable except the final SEND inside _run_one_cycle):
          1. Wait until contact stops typing (defensive 30s timeout)
          2. Sleep for the configured batch_delay
          3. Wait again (typing may have resumed during the sleep)
          4. Snapshot pending and run the LLM + send cycle

        Cancellation by the webhook (new message arrived) drops the current run; the
        webhook then schedules a fresh orchestrator that picks up the new pending list.
        """
        try:
            batch_delay = settings.get("message_batch_delay", 3.0)
            await _wait_typing_paused(phone)
            await asyncio.sleep(batch_delay)
            await _wait_typing_paused(phone)

            items = list(state.pending_messages.get(phone, []))
            if not items:
                return
            # Consume now: a NEW message arriving during _run_one_cycle goes into a fresh batch
            state.pending_messages.pop(phone, None)

            await _run_one_cycle(phone, items)

            # If new messages arrived during the SEND phase (when cancellation is blocked),
            # spawn another orchestrator so they get processed.
            if state.pending_messages.get(phone):
                state.processing_tasks[phone] = asyncio.create_task(_orchestrate(phone))
        except asyncio.CancelledError:
            return
        finally:
            cur = asyncio.current_task()
            if state.processing_tasks.get(phone) is cur:
                state.processing_tasks.pop(phone, None)

    # ── Webhook Endpoint ──────────────────────────────────────────

    @app.post("/api/webhook")
    async def webhook(body: dict):
        """Receive real-time message events from GOWA webhook."""
        # Plugin filter: full webhook payload before any parsing.
        # A plugin can rewrite media paths, drop messages or normalize fields.
        body = await apply_filter("filter.webhook.payload", body, {})
        if body is None:
            return _ok({"status": "filtered_out"})

        event = body.get("event", "")
        # GOWA wraps message data inside "payload"
        data = body.get("payload", body.get("data", body))

        # Store raw payload for debugging (last 50, in-memory fallback)
        state.webhook_payloads.append({
            "ts": time.time(),
            "event": event,
            "payload": data,
        })

        # Helper to extract a clean phone from a JID-ish string.
        def _phone_from_jid(jid: str) -> str:
            if not isinstance(jid, str) or not jid:
                return ""
            return jid.split("@")[0].split(":")[0]

        # ── Events GOWA emite que historicamente o WhatsBot-Lite ignorava ─────
        # Cada um vira um plugin event com payload tipado. O bot não age
        # localmente nestes (nenhum LLM, nenhum save), só fan-outa pros plugins.

        if event == "message.reaction":
            react_phone = _phone_from_jid(data.get("chat_id", "") or data.get("from", ""))
            react_from = _phone_from_jid(data.get("from", ""))
            reacted_id = data.get("reacted_message_id", "")
            emoji = data.get("reaction", "")
            is_from_me = bool(data.get("is_from_me", False))
            # Persist so reactions made on the phone / by the contact reflect in the
            # panel. Reactor "me" for the bot's own account, else the sender's phone.
            if reacted_id:
                reactor = "me" if is_from_me else (react_from or react_phone)
                reactions = await asyncio.to_thread(
                    message_repo.set_reaction, reacted_id, emoji, reactor)
                if reactions is not None:
                    await ws_manager.broadcast("message_reaction", {
                        "phone": react_phone, "msg_id": reacted_id, "reactions": reactions,
                    })
            await emit_with_filter("message.reaction", {
                "id": data.get("id", ""),
                "phone": react_phone,
                "from": react_from,
                "reaction": emoji,
                "reacted_message_id": reacted_id,
                "is_from_me": is_from_me,
                "ts": data.get("timestamp") or time.time(),
                "raw": data,
            })
            return _ok({"status": "reaction"})

        if event == "message.edited":
            await emit_with_filter("message.edited", {
                "id": data.get("id", ""),
                "phone": _phone_from_jid(data.get("chat_id", "") or data.get("from", "")),
                "from": _phone_from_jid(data.get("from", "")),
                "original_message_id": data.get("original_message_id", ""),
                "body": data.get("body", "") or data.get("content", ""),
                "is_from_me": bool(data.get("is_from_me", False)),
                "ts": data.get("timestamp") or time.time(),
                "raw": data,
            })
            return _ok({"status": "edited"})

        if event == "message.revoked":
            revoked_phone = _phone_from_jid(data.get("chat_id", "") or data.get("from", ""))
            revoked_id = data.get("revoked_message_id", "")
            # Flag as revoked (keeping content) so a revoke done on the phone/by the
            # contact reflects in the panel and survives reload, without losing the text.
            if revoked_id:
                matched = await asyncio.to_thread(message_repo.mark_revoked, revoked_id, "all")
                if matched:
                    await ws_manager.broadcast("message_revoked", {
                        "phone": revoked_phone, "msg_id": revoked_id,
                    })
            await emit_with_filter("message.revoked", {
                "id": data.get("id", ""),
                "phone": revoked_phone,
                "from": _phone_from_jid(data.get("from", "")),
                "revoked_message_id": revoked_id,
                "revoked_from_me": bool(data.get("revoked_from_me", False)),
                "revoked_chat": data.get("revoked_chat", ""),
                "ts": data.get("timestamp") or time.time(),
                "raw": data,
            })
            return _ok({"status": "revoked"})

        if event == "message.deleted":
            deleted_phone = _phone_from_jid(data.get("chat_id", "") or data.get("from", ""))
            deleted_id = data.get("deleted_message_id", "")
            # Keep the row (flag revoked) instead of hard-deleting, so a "delete for
            # me" done on the phone leaves the message visible in the panel.
            if deleted_id:
                matched = await asyncio.to_thread(message_repo.mark_revoked, deleted_id, "me")
                if matched:
                    await ws_manager.broadcast("message_deleted", {
                        "phone": deleted_phone, "msg_id": deleted_id,
                    })
            await emit_with_filter("message.deleted", {
                "deleted_message_id": deleted_id,
                "phone": deleted_phone,
                "from": _phone_from_jid(data.get("from", "")),
                "original_content": data.get("original_content", ""),
                "original_sender": data.get("original_sender", ""),
                "original_timestamp": data.get("original_timestamp"),
                "was_from_me": bool(data.get("was_from_me", False)),
                "ts": data.get("timestamp") or time.time(),
                "raw": data,
            })
            return _ok({"status": "deleted"})

        if event == "group.participants":
            chat_id = data.get("chat_id", "")
            ctype = data.get("type", "")
            jids = data.get("jids", []) or []
            # Apply the roster delta locally (join adds + resolves push name,
            # leave drops the member) and push the authoritative list to the open
            # panel so its @mention autocomplete updates immediately — a removed
            # member disappears and a just-joined one shows with its name, without
            # depending on a possibly-stale GOWA /group/info refetch.
            if chat_id:
                members = await asyncio.to_thread(
                    group_mentions.apply_participants_change, chat_id, ctype, jids)
                await ws_manager.broadcast("group_participants_changed",
                                           {"group_jid": chat_id, "members": members})
                # Surface a join/leave/promote notice in the chat timeline (only
                # for groups already tracked, to avoid materializing phantom
                # contacts). Saved as `system_notice`: rendered as a centered
                # bubble and excluded from the LLM context.
                existing = await asyncio.to_thread(contact_repo.get_by_phone, chat_id)
                if existing:
                    notice = await asyncio.to_thread(
                        group_mentions.describe_change, ctype, jids)
                    if notice:
                        contact_obj = agent_handler._get_contact(chat_id)
                        await asyncio.to_thread(
                            contact_obj.add_message, "system_notice", notice)
                        await ws_manager.broadcast("new_message", {
                            "phone": chat_id,
                            "message": {"role": "system_notice", "content": notice,
                                        "ts": time.time()},
                        })
            await emit_with_filter("group.participants_changed", {
                "chat_id": chat_id,
                "phone": _phone_from_jid(chat_id),
                "type": ctype,
                "jids": jids,
                "ts": time.time(),
                "raw": data,
            })
            return _ok({"status": "group_participants"})

        if event == "group.joined":
            await emit_with_filter("group.joined", {
                "chat_id": data.get("chat_id", "") or data.get("group_jid", ""),
                "phone": _phone_from_jid(data.get("chat_id", "") or data.get("group_jid", "")),
                "ts": time.time(),
                "raw": data,
            })
            return _ok({"status": "group_joined"})

        if event == "call.offer":
            await emit_with_filter("call.received", {
                "call_id": data.get("call_id", ""),
                "phone": _phone_from_jid(data.get("from", "")),
                "auto_rejected": bool(data.get("auto_rejected", False)),
                "remote_platform": data.get("remote_platform", ""),
                "remote_version": data.get("remote_version", ""),
                "group_jid": data.get("group_jid"),
                "ts": time.time(),
                "raw": data,
            })
            return _ok({"status": "call"})

        if isinstance(event, str) and event.startswith("newsletter."):
            await emit_with_filter("newsletter.event", {
                "subtype": event,
                "ts": time.time(),
                "raw": data,
            })
            return _ok({"status": "newsletter"})

        # Handle chat presence events (typing/recording indicators)
        if event == "chat_presence":
            from_jid = data.get("from", "")
            phone = from_jid.split("@")[0] if "@" in from_jid else from_jid
            presence_state = data.get("state", "")
            media = data.get("media", "") or "text"
            if phone and presence_state:
                logger.info("[Webhook] chat_presence %s from %s (media=%s)",
                            presence_state, phone, media)
                # Update orchestrator-visible typing state
                state.typing_state[phone] = {
                    "active": presence_state == "composing",
                    "media": media,
                    "last_ts": time.time(),
                }
                await ws_manager.broadcast("chat_presence", {
                    "phone": phone,
                    "state": presence_state,
                    "media": media,
                })
                await emit_with_filter("presence.changed", {
                    "phone": phone,
                    "state": presence_state,
                    "media": media,
                    "ts": time.time(),
                })
            return _ok({"status": "presence"})

        # Handle message.ack events (delivery + read receipts from WhatsApp)
        if event == "message.ack":
            receipt_type = data.get("receipt_type", "")
            msg_ids = data.get("ids", [])

            # Extract phone from ack payload (try multiple fields, GOWA is inconsistent)
            ack_phone = ""
            for field in ("chat_id", "from", "jid", "phone"):
                val = data.get(field, "")
                if val and "@" in val:
                    ack_phone = val.split("@")[0]
                    break
                elif val and not ack_phone:
                    ack_phone = val

            # Fallback: look up phone from the message in DB
            if not ack_phone and msg_ids:
                cid = await asyncio.to_thread(message_repo.get_contact_id_by_msg_id, msg_ids[0])
                if cid:
                    for phone_key, contact in agent_handler._contacts.items():
                        if contact.id == cid:
                            ack_phone = phone_key
                            break

            if receipt_type == "delivered" and msg_ids:
                # Update outgoing message status to "delivered" (with cascade to prior msgs)
                all_updated = []
                for mid in msg_ids:
                    updated = await asyncio.to_thread(message_repo.update_status_by_msg_id, mid, "delivered")
                    all_updated.extend(updated)
                # Deduplicate
                all_updated = list(dict.fromkeys(all_updated))
                logger.info("[Webhook] message.ack delivered for %s (ids=%s, cascaded=%d)",
                            ack_phone, msg_ids, len(all_updated))
                if ack_phone and all_updated:
                    await ws_manager.broadcast("message_status", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "delivered",
                    })
                    await emit_with_filter("receipt.changed", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "delivered",
                        "ts": time.time(),
                    })

            elif receipt_type in ("read", "read-self") and msg_ids:
                # Update outgoing message status to "read" (with cascade to prior msgs)
                all_updated = []
                for mid in msg_ids:
                    updated = await asyncio.to_thread(message_repo.update_status_by_msg_id, mid, "read")
                    all_updated.extend(updated)
                all_updated = list(dict.fromkeys(all_updated))
                logger.info("[Webhook] message.ack read for %s (ids=%s, cascaded=%d)",
                            ack_phone, msg_ids, len(all_updated))
                if ack_phone and all_updated:
                    await ws_manager.broadcast("message_status", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "read",
                    })
                    await emit_with_filter("receipt.changed", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "read",
                        "ts": time.time(),
                    })

                # Existing unread tracking logic (for incoming messages read by us)
                for phone_key, contact in agent_handler._contacts.items():
                    unread_ids = contact.get_unread_msg_ids()
                    matched = [mid for mid in msg_ids if mid in unread_ids]
                    if matched:
                        logger.info("[Webhook] message.ack unread cleared for %s (ids=%s)", phone_key, matched)
                        contact.mark_as_read()
                        await ws_manager.broadcast("messages_read", {"phone": phone_key})

            return _ok({"status": "ack"})

        # Only process incoming messages
        if event and event not in ("message", "message:received", ""):
            return _ok({"status": "ignored"})

        if not isinstance(data, dict):
            return _ok({"status": "ignored"})

        # Extract message fields (GOWA field names vary)
        is_from_me = data.get("is_from_me", data.get("from_me", data.get("FromMe", False)))

        # Capture bot's own phone from outgoing messages (for @mention detection)
        if is_from_me:
            own_jid = (data.get("sender_jid", "") or data.get("from", "")
                       or data.get("sender", ""))
            if own_jid and "@s.whatsapp.net" in own_jid:
                captured = own_jid.split("@")[0].split(":")[0]
                if captured and captured != state.bot_phone:
                    state.bot_phone = captured
                    logger.info("[Webhook] Bot phone captured from own message: %s", state.bot_phone)
                    # Persist + register so @mention detection in groups keeps
                    # working across restarts, even before the status poll runs.
                    try:
                        if settings.get("bot_phone", "") != state.bot_phone:
                            settings.set("bot_phone", state.bot_phone)
                        group_mentions.set_bot_identity(
                            state.bot_phone, state.bot_name)
                    except Exception as e:
                        logger.warning("[Webhook] Failed to persist bot_phone: %s", e)

        msg_id = data.get("id", data.get("Id", data.get("message_id", ""))
                         ) or str(uuid.uuid4())
        if msg_id in state.processed_messages:
            return _ok({"status": "duplicate"})

        # Best-effort: GOWA's webhook is inconsistent about exposing the quoted
        # message id. Probe a few known/likely keys (flat + nested context_info).
        reply_to_msg_id = _extract_reply_to(data)

        # Extract body — try multiple known field names
        text = (data.get("content", "")
                or data.get("body", "")
                or data.get("Body", "")
                or data.get("message", "")
                or data.get("text", "")).strip()

        # Extract media (image, audio, video_note→audio, video, sticker,
        # document, location, live_location, poll, interactive, order,
        # product, contact, contacts_array) — see _extract_media for the
        # detection order and the shape of the returned dict.
        extracted = _extract_media(
            data, is_from_me=is_from_me, existing_text=text
        )
        text = extracted["text"]
        media_type: str | None = extracted["media_type"]
        media_path: str | None = extracted["media_path"]
        media_extras: dict | None = extracted["media_extras"]
        # Back-compat locals used by downstream code that decides on
        # specific kinds (e.g. picks transcribe_audio vs describe_image,
        # branches on audio_path in the batch loop).
        audio_path: str | None = extracted["audio_path"]
        image_path: str | None = extracted["image_path"]
        document_path: str | None = extracted["document_path"]
        document_name: str | None = extracted["document_name"]

        # Extract chat and sender separately for group support.
        # GOWA v8.5.0 puts the chat in `chat_id` and the actual sender in `from`
        # (in private chats they're the same JID; in groups they differ — `from`
        # is the member who sent it, `chat_id` is the group). The legacy
        # `sender_jid`/`sender` fields aren't always present.
        chat_jid = (data.get("chat_jid", "") or data.get("chat_id", "")
                    or data.get("from", "") or data.get("jid", ""))
        sender_jid = (data.get("sender_jid", "") or data.get("sender", "")
                      or data.get("from", ""))

        is_group = "@g.us" in chat_jid

        # ── Resolve document filename ─────────────────────────────────
        # GOWA's webhook omits the original filename for documents when
        # auto-download is enabled (buildAutoDownloadPayload sends only
        # path+caption, and the on-disk path is UUID-based). GOWA *does*
        # persist `filename` in its chat storage BEFORE forwarding the
        # webhook, so we look it up via GET /chat/{jid}/messages.
        if media_type == "document":
            try:
                real_name = await asyncio.to_thread(
                    gowa_client.get_message_filename, chat_jid, msg_id)
            except Exception as e:
                logger.warning("[Webhook] document filename lookup failed: %s", e)
                real_name = ""
            doc_caption = (media_extras or {}).get("caption") or ""
            doc_label = real_name or document_name or "documento"
            verb = "enviado" if is_from_me else "recebido"
            text = (f"[Documento {verb}: {doc_label}]"
                    + (f"\n{doc_caption}" if doc_caption else ""))
            if real_name:
                media_extras = {**(media_extras or {}), "file_name": real_name}
                logger.info("[Webhook] document filename resolved: %s", real_name)

        if is_group:
            # For groups: route replies to the group, track individual sender
            phone = chat_jid  # keep full JID (e.g. 120363xxx@g.us)
            # Strip @domain AND :device suffix from sender JID (multi-device WhatsApp
            # sends "5511999999:25@s.whatsapp.net"; we want just the bare phone).
            individual_phone = (sender_jid.split("@")[0].split(":")[0]
                                if sender_jid else "")
            from_name = data.get("from_name", "") or data.get("pushName", "") or data.get("notify", "")
        else:
            # For private chats: the conversation key is `chat_id` (the other party in
            # both directions). For incoming msgs `sender_jid == chat_id`; for outgoing
            # (`is_from_me=true`) `sender_jid`/`from` is the bot itself, so using sender
            # would route the message to the bot's own contact thread.
            conv_jid = chat_jid or sender_jid
            phone = (conv_jid.split("@")[0].split(":")[0] if conv_jid else "")
            individual_phone = phone
            from_name = data.get("from_name", "") or data.get("pushName", "") or data.get("notify", "")

        if not phone or (not text and not media_type):
            # Last-chance plugin hook: a filter may detect a media kind the
            # core doesn't natively understand and turn it into a synthetic
            # parsed_msg. Returning ``None`` (the default when no plugin
            # subscribes) lets the legacy "ignored" path proceed.
            synthetic = await apply_filter(
                "filter.media.unknown",
                None,
                {"phone": phone, "raw": data},
            )
            if isinstance(synthetic, dict):
                media_type = synthetic.get("media_type") or media_type
                media_path = synthetic.get("media_path") or media_path
                media_extras = synthetic.get("media_extras") or media_extras
                if synthetic.get("text"):
                    text = synthetic["text"]
            if not phone or (not text and not media_type):
                logger.info(
                    "[Webhook] Skipping: text=%r phone=%r media=%s keys=%s payload=%s",
                    text[:50] if text else "", phone, media_type or "none",
                    list(data.keys()), str(data)[:1000],
                )
                return _ok({"status": "ignored"})

        state.processed_messages.add(msg_id)

        # Filter GOWA echo-backs: ignore messages we recently sent
        if text:
            sent_key = f"{phone}:{text[:120]}"
            sent_at = state.recently_sent.pop(sent_key, None)
            if sent_at and (time.time() - sent_at) < 30:
                logger.info("[Webhook] Ignoring echo-back for %s", phone)
                return _ok({"status": "echo"})

        # Sync outgoing messages sent from phone (not via our app)
        if is_from_me:
            # media_type/media_path already resolved by _extract_media above.

            # Plugin filter: mirror of `filter.message.before_save` but for
            # the echo branch — lets plugins rewrite/anonymize/drop messages
            # the user sent from their own phone before we persist them.
            outgoing_msg = {
                "phone": phone,
                "text": text,
                "msg_id": msg_id,
                "media_type": media_type,
                "media_path": media_path,
                "media_extras": media_extras,
                "reply_to_msg_id": reply_to_msg_id,
                "is_from_me": True,
                "source": "echo",
                "raw": data,
                "ts": time.time(),
            }
            outgoing_msg = await apply_filter(
                "filter.message.outgoing", outgoing_msg, {"phone": phone}
            )
            if outgoing_msg is None:
                logger.info(
                    "[Webhook] outgoing echo from %s filtered out", phone
                )
                return _ok({"status": "filtered_out"})
            text = outgoing_msg.get("text", text)
            msg_id = outgoing_msg.get("msg_id", msg_id)
            media_type = outgoing_msg.get("media_type", media_type)
            media_path = outgoing_msg.get("media_path", media_path)
            media_extras = outgoing_msg.get("media_extras", media_extras)
            reply_to_msg_id = outgoing_msg.get("reply_to_msg_id", reply_to_msg_id)

            logger.info("[Webhook] Syncing outgoing %s to %s: %s",
                        media_type or "message", phone,
                        text[:80] if text else f"[{media_type}]")

            # Save as "assistant" in contact memory (status="operator" to distinguish from AI)
            contact = agent_handler._get_contact(phone)
            await asyncio.to_thread(
                contact.add_message, "assistant", text,
                media_type=media_type, media_path=media_path, msg_id=msg_id,
                reply_to_msg_id=reply_to_msg_id,
                media_name=_media_name(media_type, media_extras),
                status="operator")

            # Broadcast to frontend
            broadcast_msg: dict = {"role": "assistant", "content": text,
                                   "ts": time.time(), "msg_id": msg_id,
                                   "status": "operator"}
            if reply_to_msg_id:
                broadcast_msg["reply_to_msg_id"] = reply_to_msg_id
            if media_type:
                broadcast_msg["media_type"] = media_type
                broadcast_msg["media_path"] = media_path
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": broadcast_msg,
            })

            # Plugin event: this message was sent from the user's phone outside the app
            await emit_with_filter("message.sent", {
                "phone": phone, "text": text, "msg_id": msg_id,
                "media_type": media_type, "media_path": media_path,
                "media_extras": media_extras,
                "source": "echo", "status": "operator",
                "ts": time.time(),
            })

            # Transcribe outgoing audio if the configured mode includes "sent"
            if audio_path:
                out_transcription = await _maybe_transcribe(
                    "audio", audio_path,
                    phone=phone, source="echo",
                    is_group=contact.is_group,
                    group_jid=phone if contact.is_group else None,
                )
                if out_transcription:
                    await _deliver_audio_transcription(
                        phone, contact, out_transcription
                    )

            return _ok({"status": "synced"})

        # media_type/media_path already resolved by _extract_media above.

        # For groups: prefix text with sender name and check @mention
        display_text = text
        skip_ai = False
        bot_mentioned = False  # set inside the is_group branch; kept bound for non-group
        if is_group:
            # Log full group payload for debugging field names
            logger.info("[Webhook] Group payload: %s", json.dumps(data, default=str, ensure_ascii=False)[:2000])

            # Ensure group metadata is stored
            contact = agent_handler._get_contact(phone)
            if not contact.is_group or not contact.group_name:
                contact.is_group = True
                # Try to get group name from payload fields (NOT from_name, that's the sender)
                group_name = (data.get("subject", "")
                              or data.get("group_name", "")
                              or data.get("group_subject", "")
                              or data.get("chat_name", ""))
                # Fallback: fetch group name from GOWA API
                if not group_name:
                    try:
                        group_name = await asyncio.to_thread(
                            gowa_client.get_group_name, phone)
                    except Exception as e:
                        logger.warning("[Webhook] Failed to fetch group name: %s", e)
                if group_name:
                    contact.group_name = group_name
                    logger.info("[Webhook] Group name resolved: %s -> %s", phone, group_name)
                else:
                    logger.warning("[Webhook] Could not resolve group name for %s", phone)
                contact.save()

            # Check if bot can send in this group
            if state.bot_phone:
                try:
                    can_send = await asyncio.to_thread(
                        gowa_client.can_bot_send_in_group, phone, state.bot_phone)
                    if contact.can_send != can_send:
                        contact.can_send = can_send
                        contact.save()
                        logger.info("[Webhook] Group %s can_send updated: %s", phone, can_send)
                except Exception as e:
                    logger.warning("[Webhook] Failed to check group send permission: %s", e)

            # Prefix message with sender name for group context.
            # Prefer the saved contact name (if the sender exists as a private
            # contact), so renames in the contact info panel propagate to new
            # group messages. Falls back to WhatsApp pushName, then phone.
            # Remember the sender's pushName so future name resolution works
            # (GOWA's participant list has no display names). Keyed by the
            # sender's digits (phone OR lid, depending on group addressing).
            if from_name:
                await asyncio.to_thread(
                    group_mentions.record_pushname, [individual_phone], from_name)

            # Resolve names for this group once (saved contact > pushName > number),
            # handling lid-addressed groups. `lookup` maps phone/lid digits -> name
            # and is reused to turn @<number> mentions in the body into @<Name>.
            try:
                lookup = await asyncio.to_thread(group_mentions.build_lookup, chat_jid)
            except Exception as e:
                logger.warning("[Webhook] member lookup failed for %s: %s", chat_jid, e)
                lookup = {}
            sender_label = lookup.get(individual_phone) or from_name or individual_phone
            if text:
                display_text = f"[{sender_label}]: {group_mentions.apply_incoming(lookup, text)}"

            # Check if bot is mentioned (use RAW text, before name resolution)
            group_mode = settings.get("group_reply_mode", "mention_only")
            bot_mentioned = _is_bot_mentioned(text, data)

            if group_mode == "never" or (group_mode == "mention_only" and not bot_mentioned):
                skip_ai = True
                logger.info("[Webhook] Group message (no mention) from %s in %s: %s",
                            sender_label, phone, text[:80] if text else "[media]")
            else:
                # Bot was mentioned — strip mention from text for LLM
                cleaned = _strip_bot_mention(text)
                display_text = (f"[{sender_label}]: {group_mentions.apply_incoming(lookup, cleaned)}"
                                if cleaned else display_text)
                logger.info("[Webhook] Group message (@mention) from %s in %s: %s",
                            sender_label, phone, text[:80] if text else "[media]")
        else:
            logger.info("[Webhook] %s from %s: %s",
                        media_type.capitalize() if media_type else "Message",
                        phone, text[:80] if text else f"[{media_type}]")

        # Check/update archive status from GOWA (skip if archived by app)
        try:
            contact = agent_handler._get_contact(phone)
            if not contact.archived_by_app:
                archived = await asyncio.to_thread(gowa_client.is_chat_archived, chat_jid)
                logger.info("[Webhook] Archive check: %s (jid=%s) -> archived=%s", phone, chat_jid, archived)
                if contact.is_archived != archived:
                    contact.is_archived = archived
                    contact.save()
                    logger.info("[Webhook] Archive status updated: %s -> %s", phone, archived)
                    await emit_with_filter("chat.archived", {
                        "phone": phone, "archived": bool(archived),
                        "ts": time.time(),
                    })
            else:
                logger.info("[Webhook] Skipping archive check for %s (archived by app)", phone)
        except Exception as e:
            logger.warning("[Webhook] Failed to check archive status for %s: %s", phone, e)

        # Auto-fill contact name from WhatsApp pushName (private chats only)
        if from_name and not is_group:
            await asyncio.to_thread(agent_handler._get_contact(phone).set_wa_name, from_name)

        # Increment unread count for incoming user messages
        await asyncio.to_thread(lambda: agent_handler._get_contact(phone).increment_unread(msg_id))

        # A group message that @mentions the bot raises the "mention" flag, shown as
        # an "@" next to the unread badge until the operator opens the conversation.
        if is_group and bot_mentioned:
            await asyncio.to_thread(lambda: agent_handler._get_contact(phone).mark_mention())

        # Build parsed message payload for plugins (filter + event). Includes
        # the full GOWA payload under `raw` so plugins that need an obscure
        # field can still get it. We emit BEFORE the skip_ai branch so group
        # messages without a mention still show up to event subscribers.
        parsed_msg = {
            "phone": phone,
            "name": from_name,
            "text": display_text,
            "raw_text": text,
            "msg_id": msg_id,
            "reply_to_msg_id": reply_to_msg_id,
            "media_type": media_type,
            "media_path": media_path,
            "media_extras": media_extras,
            "is_group": is_group,
            "group_jid": chat_jid if is_group else None,
            "individual_phone": individual_phone if is_group else None,
            "is_from_me": False,
            "raw": data,
            "ts": time.time(),
        }
        # Plugin filter: can rewrite/anonymize/translate or return None to drop
        parsed_msg = await apply_filter(
            "filter.message.before_save", parsed_msg, {"phone": phone}
        )
        if parsed_msg is None:
            logger.info("[Webhook] inbound from %s filtered out before save", phone)
            return _ok({"status": "filtered_out"})

        # Filter may have rewritten user-facing strings — propagate.
        display_text = parsed_msg.get("text", display_text)
        msg_id = parsed_msg.get("msg_id", msg_id)
        reply_to_msg_id = parsed_msg.get("reply_to_msg_id", reply_to_msg_id)
        media_type = parsed_msg.get("media_type", media_type)
        media_path = parsed_msg.get("media_path", media_path)
        media_extras = parsed_msg.get("media_extras", media_extras)

        # Auto-download policy: should the file survive this message?
        discard_media = _should_discard(media_type, media_path, is_group)

        # Broadcast incoming message to frontend in real-time
        broadcast_msg: dict = {"role": "user", "content": display_text, "ts": time.time(), "msg_id": msg_id}
        if reply_to_msg_id:
            broadcast_msg["reply_to_msg_id"] = reply_to_msg_id
        if media_type:
            broadcast_msg["media_type"] = media_type
            broadcast_msg["media_path"] = media_path
        if is_group and bot_mentioned:
            broadcast_msg["mentioned"] = True
        await ws_manager.broadcast("new_message", {
            "phone": phone,
            "message": broadcast_msg,
        })

        # Plugin event: fired for ALL inbound messages, including group msgs
        # without a mention. Plugins filter inside their handler on
        # `is_group`/`media_type` etc.
        await emit_with_filter("message.received", parsed_msg)

        # For group messages without mention: save to history but don't trigger AI.
        # Persist media_type/media_path so the chat panel can render the audio/image/document
        # player on reload, the same way it does for private chats.
        if skip_ai:
            # Transcribe audio/describe image even though the AI won't reply, so the
            # transcription card still appears in the panel — matches private-chat UX.
            transcription = ""
            doc_path_grp = media_path if media_type == "document" else None
            if audio_path:
                transcription = await _maybe_transcribe(
                    "audio", audio_path,
                    phone=phone, source="group_no_mention",
                    is_group=True, group_jid=phone,
                )
            elif image_path:
                transcription = await _maybe_transcribe(
                    "image", image_path,
                    phone=phone, source="group_no_mention",
                    is_group=True, group_jid=phone,
                )
            elif doc_path_grp:
                transcription = await _maybe_transcribe(
                    "document", doc_path_grp,
                    phone=phone, source="group_no_mention",
                    is_group=True, group_jid=phone,
                    file_name=(media_extras or {}).get("file_name") or "",
                    mimetype=(media_extras or {}).get("mimetype") or "",
                )

            saved_text = display_text
            if transcription and audio_path:
                saved_text = f"{display_text}\n[Transcrição do áudio]: {transcription}" if display_text else f"[Transcrição do áudio]: {transcription}"
            elif transcription and image_path:
                desc_prefix = f"[Descrição da imagem]: {transcription}"
                saved_text = f"{desc_prefix}\n{display_text}" if display_text else desc_prefix
            elif transcription and doc_path_grp:
                doc_prefix = f"[Conteúdo do documento]: {transcription}"
                saved_text = f"{display_text}\n{doc_prefix}" if display_text else doc_prefix

            contact_obj = agent_handler._get_contact(phone)
            await asyncio.to_thread(
                contact_obj.add_message,
                "user", saved_text,
                media_type=media_type, media_path=media_path,
                msg_id=msg_id, reply_to_msg_id=reply_to_msg_id,
                media_name=_media_name(media_type, media_extras))
            await emit_with_filter("message.saved", {
                "phone": phone, "text": saved_text, "msg_id": msg_id,
                "media_type": media_type, "media_path": media_path,
                "media_extras": media_extras,
                "media_will_discard": discard_media,
                "is_group": True, "group_jid": phone,
                "source": "group_no_mention",
                "ts": time.time(),
            })

            if transcription:
                if audio_path:
                    await _deliver_audio_transcription(phone, contact_obj, transcription)
                else:
                    await asyncio.to_thread(contact_obj.add_message, "transcription", transcription)
                    await ws_manager.broadcast("new_message", {
                        "phone": phone,
                        "message": {
                            "role": "transcription",
                            "content": transcription,
                            "ts": time.time(),
                        },
                    })
            if discard_media and media_path:
                await _finalize_discards(phone, [(msg_id, media_path)])
            return _ok({"status": "group_no_mention"})

        # Batch messages — accumulate and wait before responding
        if phone not in state.pending_messages:
            state.pending_messages[phone] = []
        state.pending_messages[phone].append({
            "text": display_text,
            "image_path": image_path,
            "audio_path": audio_path,
            "media_type": media_type,
            "media_path": media_path,
            "media_extras": media_extras,
            "discard": discard_media,
            "msg_id": msg_id,
            "reply_to_msg_id": reply_to_msg_id,
        })

        # A real message arriving from the contact proves they finished typing
        # *something*. WhatsApp doesn't reliably emit `paused` for linked devices,
        # so without this the orchestrator would block on a stale `composing` flag
        # until the 25s stale timeout. Clear here; a *new* `composing` event for
        # the next message will re-set `active=True` with a fresh last_ts.
        ts = state.typing_state.get(phone)
        if ts and ts.get("active"):
            state.typing_state[phone] = {**ts, "active": False}

        # Schedule (or restart) the typing-aware orchestrator. This cancels the current
        # cycle if it's not in the SEND phase yet, so a newly arrived message can be
        # bundled into the same batch (and any in-flight LLM call is aborted).
        _schedule_orchestrator(phone)

        # Prune processed set to avoid unbounded growth
        if len(state.processed_messages) > 5000:
            oldest = list(state.processed_messages)[:2500]
            for item in oldest:
                state.processed_messages.discard(item)

        # Prune stale recently_sent entries (older than 60s)
        now = time.time()
        stale = [k for k, v in state.recently_sent.items() if now - v > 60]
        for k in stale:
            del state.recently_sent[k]

        return _ok({"status": "batched"})
