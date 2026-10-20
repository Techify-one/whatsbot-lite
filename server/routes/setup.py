"""First-run setup wizard endpoints — Techify API key provisioning.

The setup wizard (frontend) connects WhatsApp, then triggers
``POST /api/setup/request-key`` which makes the WhatsBot-Lite send a WhatsApp
message to the Techify provisioning number. The target — destination number
**and** the phrase to send — is fetched at request time from Techify's
``/service_number`` endpoint, which is the source of truth: either field can
be rotated without a client release, falling back to env and then to a literal
in ``config/settings.py`` when the endpoint is down (see
:func:`fetch_provision_target`). Techify creates an account +
API key keyed by the sender's number. The wizard then polls
``GET /api/setup/key-status``, which in turn POSTs to Techify's
``/request-apikey`` endpoint (body ``{"number": ...}``) server-side and
saves the key to the config once ready. The ``/request-apikey`` response
also carries ``account_url`` (the customer's account/recharge page) and
``access_token`` (credential for that account), both persisted to config.
Techify keeps the key downloadable for ~1 minute after the account is
created.

Manual fallback: WhatsApp's reach-out timelock (error 463) can block the bot
from sending the provisioning message to a brand-new contact — it hits
newly-linked / low-activity numbers hardest, which is exactly this wizard's
scenario. When that happens we still arm the key polling and return the data
the frontend needs to ask the user to send the very same message *by hand*
from their own phone (a ``wa.me`` deep link + a scannable QR pointing to it).
A human-initiated first message is the documented way past the timelock, and
because it goes out from the same WhatsApp number, the key polling resolves
exactly as in the automatic path — the key still lands in the config on its
own, no copy-paste required.
"""

import asyncio
import dataclasses
import logging
import time
from urllib.parse import quote

import httpx
import segno

from config.settings import (
    TECHIFY_PROVISION_MESSAGE,
    TECHIFY_PROVISION_NUMBER,
    TECHIFY_REQUEST_APIKEY_URL,
    TECHIFY_SERVICE_NUMBER_URL,
)
from gowa.client import GOWASendError
from plugins.events import apply_filter
from server.helpers import _ok, _err

logger = logging.getLogger(__name__)


def _wa_deep_link(number: str, message: str) -> str:
    """Build a wa.me click-to-chat link that pre-fills ``message`` to ``number``."""
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(message)}"


def _qr_data_uri(url: str) -> str:
    """Render ``url`` as a PNG data URI QR code (scanning it opens WhatsApp)."""
    try:
        return segno.make(url, error="m").png_data_uri(scale=5, border=2)
    except Exception as e:  # never let QR rendering break provisioning
        logger.warning("Setup: failed to render provisioning QR: %s", e)
        return ""


@dataclasses.dataclass(frozen=True)
class ProvisionTarget:
    """Provisioning destination: WHO to message and WITH WHICH phrase.

    Both fields travel together because they only make sense together — the
    phrase is the trigger THAT number recognizes. An empty value on either side
    means "there is no destination" and the send is refused.
    """

    number: str
    message: str


async def _fetch_service_number() -> dict | None:
    """``GET /service_number``. ``None`` = could not ask (network/HTTP/JSON).

    A dict (even one missing the fields) means the endpoint answered: from there
    on, a missing field is information, not a transport failure.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(TECHIFY_SERVICE_NUMBER_URL)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Setup: failed to fetch service number (%s), using fallback", e)
        return None
    return data if isinstance(data, dict) else {}


def _pick(remote: dict, key: str, fallback: str) -> tuple[str, str]:
    """``(value, source)`` — the remote field wins; missing/empty falls back.

    Pure. Each field is resolved ON ITS OWN: an endpoint that does not return
    ``message`` yet still dictates ``phone``, and vice versa.
    """
    value = str(remote.get(key) or "").strip()
    return (value, "service_number") if value else (fallback, "fallback")


async def fetch_provision_target() -> ProvisionTarget:
    """Resolve who the account request is sent to, and with which phrase.

    Precedence, per field and in this order:

    1. ``GET /service_number`` — the SOURCE OF TRUTH. It answers
       ``{"ok": true, "phone": "...", "message": "..."}``; rotating the number or
       the phrase means editing that response, with no release and no env on any
       client.
    2. env ``TECHIFY_PROVISION_NUMBER`` / ``TECHIFY_PROVISION_MESSAGE`` — the
       per-install override.
    3. the literal in ``config/settings.py`` — the last safety net, for when the
       endpoint is down. It is what keeps provisioning alive through a Cloudflare
       outage.
    4. ``filter.provisioning.number`` and ``filter.provisioning.message`` — the
       plugin seams, which get the LAST word over whatever the core resolved.

    The two seams are symmetric on purpose: whoever points the send at another
    number must be able to send along the phrase THAT destination recognizes —
    otherwise overriding the number delivers a text the other side silently
    ignores.

    ``None``/``""`` on either one ABORTS: the core returns an empty target and
    ``request_key`` refuses to send, rather than firing the phrase at a number
    nobody chose (or firing an empty message at it).

    The core does not validate the SHAPE of what comes back — normalizing the
    phone belongs to whoever answers, exactly as it always did for the value
    coming out of ``/service_number``.
    """
    remote = await _fetch_service_number()
    body = remote or {}
    number, number_source = _pick(body, "phone", TECHIFY_PROVISION_NUMBER)
    message, message_source = _pick(body, "message", TECHIFY_PROVISION_MESSAGE)
    if remote is not None:
        if number_source == "fallback":
            logger.warning("Setup: /service_number returned no phone, using fallback")
        if message_source == "fallback":
            logger.info("Setup: /service_number returned no message, using fallback")

    chosen = await apply_filter(
        "filter.provisioning.number", number,
        {"source": number_source, "message": message},
    )
    number = str(chosen or "").strip()
    if not number:
        # With no destination there is nothing to ask about the phrase: the send
        # already died here.
        return ProvisionTarget(number="", message="")

    chosen = await apply_filter(
        "filter.provisioning.message", message,
        {"source": message_source, "number": number},
    )
    return ProvisionTarget(number=number, message=str(chosen or "").strip())


async def fetch_provision_number() -> str:
    """Just the destination, for callers that do not need the phrase.

    ``""`` = none. See :func:`fetch_provision_target`.
    """
    return (await fetch_provision_target()).number


def register_routes(app, deps):
    settings = deps.settings
    gowa_client = deps.gowa_client
    agent_handler = deps.agent_handler
    state = deps.state

    @app.post("/api/setup/request-key")
    async def request_key():
        """Send the Techify provisioning message and arm the key polling."""
        # Resolve the connected WhatsApp number (digits only).
        number = (state.bot_phone or "").split(":")[0].strip()
        if not number:
            number = (await asyncio.to_thread(gowa_client.get_own_number) or "").strip()
            if number:
                state.bot_phone = number
        if not number:
            return _err(
                "Não foi possível identificar seu número. "
                "Aguarde a conexão concluir e tente de novo."
            )

        target = await fetch_provision_target()
        provision_number = target.number
        if not provision_number:
            # No destination = no send. Nothing below this line may run:
            # materializing the contact and pausing its AI would create a ghost
            # contact keyed by the empty phone, and arming the polling would spin
            # the wizard until the TTL over a message that never went out.
            logger.warning("Setup: no provisioning destination configured; "
                           "refusing to send the provisioning message")
            return _err(
                "Nenhum número de destino configurado para o provisionamento. "
                "Configure o número que deve receber o pedido de conta e "
                "tente de novo."
            )
        if not target.message:
            # Destination without a phrase: sending an empty message would burn
            # the single conversation opening WhatsApp grants with a brand-new
            # contact (the reach-out timelock is per contact) and the other side
            # would have no trigger to recognize.
            logger.warning("Setup: no provisioning message resolved; "
                           "refusing to send an empty provisioning message")
            return _err(
                "Nenhuma mensagem de provisionamento configurada. "
                "Configure a frase que deve ser enviada e tente de novo."
            )

        # The Techify provisioning number is a support/automation contact — the
        # bot must never auto-reply to it. Force AI off for that contact before
        # the message goes out, so it stays disabled even after Techify replies.
        try:
            def _disable_provision_ai():
                contact = agent_handler._get_contact(provision_number)
                contact.set_ai_enabled(False)
                # The number can be rotated through /service_number, so remember
                # the one actually used: the automatic AI re-enabling skips these
                # (server/ai_reactivation.py).
                known = list(settings.get("provisioning_phones", []) or [])
                if provision_number not in known:
                    settings.set("provisioning_phones", [*known, provision_number])

            await asyncio.to_thread(_disable_provision_ai)
        except Exception as e:
            logger.warning(
                "Setup: could not disable AI for provisioning contact %s: %s",
                provision_number, e,
            )

        def _arm_polling():
            state.setup_key_number = number
            state.setup_key_requested_at = time.time()

        try:
            await asyncio.to_thread(
                gowa_client.send_message, provision_number, target.message
            )
        except GOWASendError as e:
            logger.error("Setup: failed to send provisioning message: %s", e)
            if getattr(e, "error_type", "") == "reachout_timelock":
                # WhatsApp's anti-spam restriction on first messages to new
                # contacts (error 463): the bot can't open the chat, but the
                # *user* can, by hand, from the phone linked to this session.
                # Arm the key polling anyway and hand the frontend everything it
                # needs to guide that manual send — once it goes out, the key
                # lands in the config automatically, same as the auto path.
                _arm_polling()
                wa_link = _wa_deep_link(provision_number, target.message)
                logger.info(
                    "Setup: reach-out timelock; falling back to manual send, "
                    "polling key for %s", number,
                )
                return _ok({
                    "status": "manual",
                    "number": number,
                    "provision_number": provision_number,
                    "provision_message": target.message,
                    "wa_link": wa_link,
                    "qr_data_uri": _qr_data_uri(wa_link),
                })
            return _err(f"Não foi possível enviar a mensagem: {e}")

        _arm_polling()
        logger.info("Setup: provisioning message sent, polling key for %s", number)
        return _ok({"status": "sent", "number": number})

    @app.get("/api/setup/key-status")
    async def key_status():
        """Poll Techify for the provisioned API key; save it once ready."""
        number = state.setup_key_number
        if not number:
            return _ok({"status": "pending"})

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    TECHIFY_REQUEST_APIKEY_URL, json={"number": number}
                )
            if resp.status_code != 200:
                logger.warning("Setup: Techify returned HTTP %s", resp.status_code)
                return _ok({"status": "error"})
            data = resp.json()
        except Exception as e:
            logger.warning("Setup: key-status poll failed: %s", e)
            return _ok({"status": "error"})

        if not isinstance(data, dict):
            logger.warning("Setup: Techify returned a non-object body")
            return _ok({"status": "error"})

        status = data.get("status", "pending")
        api_key = data.get("api_key", "")
        account_url = data.get("account_url", "")
        access_token = data.get("access_token", "")

        if status == "ready" and api_key:
            settings["openrouter_api_key"] = api_key
            if account_url:
                settings["account_url"] = account_url
            if access_token:
                settings["access_token"] = access_token
            settings.save()
            agent_handler.update_config(
                api_key=api_key,
                system_prompt=settings.get("system_prompt", ""),
                model=settings.get("model", "deepseek/deepseek-v4.1-flash"),
                audio_model=settings.get("audio_model", "google/gemini-2.5-flash"),
                image_model=settings.get("image_model", "google/gemini-2.5-flash"),
                max_context_messages=settings.get("max_context_messages", 10),
            )
            logger.info("Setup: API key provisioned and saved.")
            return _ok({"status": "ready"})

        return _ok({"status": status})
