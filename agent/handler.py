import asyncio
import base64
import copy
import dataclasses
import json
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from openai import OpenAI, AsyncOpenAI

from agent.memory import ContactMemory, TagRegistry, _build_image_content
from agent.costing import estimate_cost
from agent.tools import CORE_TOOLS
from agent import group_mentions, agno_engine, agent_factory
from config.settings import LLM_API_BASE_URL
from db.repositories import message_repo, contact_repo, tool_override_repo, execution_repo
from agent.execution import track_step
from plugins.context import ToolContext, PromptContext
from plugins.events import (
    emit as emit_event,
    apply_filter,
    apply_filter_sync,
    emit_with_filter,
    emit_with_filter_sync,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Guard anti-vazamento de tool-call cru (rede de segurança)
# ──────────────────────────────────────────────────────────────────────────
# Alguns modelos (ex.: deepseek via proxy) ocasionalmente emitem a sintaxe
# interna de function-calling como TEXTO em vez de usar o campo estruturado
# `tool_calls` — ex.: `<｜DSML｜tool_calls>...<｜DSML｜invoke name="...">...`.
# O motor AGNO só lê o campo estruturado, então esse texto viraria a resposta
# final e iria pro cliente (e pro histórico, fazendo o modelo "ver" o próprio
# erro e repeti-lo). A causa raiz é do modelo/proxy; aqui só detectamos o
# padrão e nunca deixamos vazar pro WhatsApp nem persistir no histórico.
# ──────────────────────────────────────────────────────────────────────────

# O separador entre "<" e a palavra-chave pode ser o pipe ASCII "|", o pipe de
# largura-cheia "｜" (U+FF5C) e o "▁" (U+2581) que o DeepSeek usa nos seus
# tokens especiais de function-calling (ex.: `<｜tool▁calls▁begin｜>`,
# `<｜DSML｜tool_calls>`). A clínica só cobria o "|" ASCII e por isso NÃO pegava
# o vazamento real (que vem em largura-cheia) — aqui cobrimos as três formas.
_RAW_TOOL_CALL_RE = re.compile(
    r"<\s*[|｜▁]?\s*[\w\-]{0,40}\s*[|｜▁]?\s*(tool[_▁]?calls?|invoke|function[_▁]?calls?)\b",
    re.IGNORECASE,
)
_RAW_TOOL_CALL_MARKERS = (
    # Formas ASCII
    "<tool_call", "<|tool_calls|>", "[tool_calls]", "<|python_tag|>",
    "<function=", "</invoke>", "</tool_call>", "</function_calls>",
    # Formas de largura-cheia (DeepSeek/DSML) — "<｜" após "<" quase nunca é
    # texto legítimo; é delimitador de token especial vazado.
    "<｜", "｜tool", "｜invoke", "｜function", "｜python_tag", "tool▁calls",
)


def _looks_like_raw_tool_call(text: str) -> bool:
    """Heurística: o texto parece sintaxe crua de tool-call vazada, não uma resposta real."""
    if not text:
        return False
    if _RAW_TOOL_CALL_RE.search(text):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _RAW_TOOL_CALL_MARKERS)


_FALLBACK_REPLY_APOS_VAZAMENTO = (
    "Desculpa, tive um problema pra gerar a resposta agora. Pode repetir o "
    "que você precisa?"
)


@dataclasses.dataclass
class ProcessResult:
    """Result of process_message with optional tool call metadata."""
    reply: str
    tool_calls: list[dict] = dataclasses.field(default_factory=list)
    contact_info: dict | None = None


class AgentHandler:
    """Processes incoming WhatsApp messages using OpenRouter LLM."""

    def __init__(
        self,
        api_key: str,
        system_prompt: str,
        max_context_messages: int = 10,
        inactivity_timeout_min: int = 30,
        model: str = "deepseek/deepseek-v4.1-flash",
        improvement_model: str = "",
        audio_model: str = "google/gemini-2.5-flash",
        image_model: str = "google/gemini-2.5-flash",
        document_model: str = "google/gemini-2.5-flash",
        pricing_fn=None,
        default_ai_enabled: bool = True,
        ai_engine_enabled: bool = False,
    ):
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.max_context_messages = max_context_messages
        self.inactivity_timeout = inactivity_timeout_min * 60
        self.model = model
        # Model for the "sugerir melhoria" analysis; empty → use self.model.
        self.improvement_model = improvement_model
        self.audio_model = audio_model
        self.image_model = image_model
        self.document_model = document_model
        self.default_ai_enabled = default_ai_enabled
        # When True, prompt/model/tools are resolved per-request from the DB
        # (config-in-DB, see agent.agent_factory) instead of the in-code values.
        # Off → legacy behaviour (full parity).
        self.ai_engine_enabled = ai_engine_enabled
        self._contacts: dict[str, ContactMemory] = {}
        self._client: OpenAI | None = None
        self._async_client: AsyncOpenAI | None = None
        self.pricing_fn = pricing_fn
        # Full pricing dict (cache prices, time-of-day overrides); injected by routes/usage.
        self.pricing_details_fn = None
        self.split_messages: bool = True
        self.tag_registry = TagRegistry()

        # Tool registry — populated with core tools at construction; plugins
        # call ``register_plugin_tools`` after the loader runs.
        # ``_tool_originals`` keeps the canonical schema as defined in code
        # (already stripped of non-OpenAI fields like ``display_label``).
        # ``_tool_schemas`` is the *effective* list sent to the LLM, rebuilt
        # whenever overrides change.
        # ``_tool_default_labels`` holds the in-code ``display_label`` per tool
        # — UI default when the user hasn't customized it.
        self._tool_originals: dict[str, dict] = {}
        self._tool_default_labels: dict[str, str] = {}
        self._tool_schemas: list[dict] = []
        self._disabled_tools: set[str] = set()
        # name -> (executor_callable, plugin_id_or_None)
        self._tool_executors: dict[str, tuple[callable, str | None]] = {}
        for schema, executor in CORE_TOOLS:
            self._register_tool(schema, executor)

        # Prompt fragment registry — list of (fragment_fn, plugin_id_or_None).
        # Each fragment is ``Callable[[ContactMemory, PromptContext], str]``.
        self._prompt_fragments: list[tuple[callable, str | None]] = []

    def _register_tool(
        self,
        schema: dict,
        executor: callable,
        plugin_id: str | None = None,
    ) -> None:
        """Register a tool schema + executor. No-ops on name collision.

        Stores a clean deep-copy in ``_tool_originals`` (with WhatsBot-Lite-specific
        keys like ``display_label`` stripped, so it's safe to send to the LLM
        as-is) and eagerly inserts a default row into ``tool_overrides`` so the
        management UI sees every registered tool.
        """
        try:
            name = schema["function"]["name"]
        except (KeyError, TypeError):
            logger.warning("Invalid tool schema: %s", schema)
            return
        if name in self._tool_executors:
            existing_pid = self._tool_executors[name][1]
            logger.warning(
                "Tool name collision: '%s' already registered by %s; ignoring %s",
                name, existing_pid or "core", plugin_id or "core",
            )
            return
        # Pluck WhatsBot-Lite-only metadata so the schema we pass to OpenAI/OpenRouter
        # is a clean tool spec.
        clean = copy.deepcopy(schema)
        default_label = clean.pop("display_label", None)
        if default_label:
            self._tool_default_labels[name] = str(default_label)
        self._tool_originals[name] = clean
        self._tool_schemas.append(clean)
        self._tool_executors[name] = (executor, plugin_id)
        try:
            tool_override_repo.ensure(name, plugin_id)
        except Exception as e:
            logger.warning("tool_overrides.ensure failed for %s: %s", name, e)

    def register_plugin_tools(
        self,
        plugin_id: str,
        tools: list[tuple[dict, callable]],
    ) -> None:
        """Register tools from a plugin. Called by the plugin loader."""
        for schema, executor in tools:
            self._register_tool(schema, executor, plugin_id=plugin_id)

    def register_ai_tools(
        self,
        tools: list[tuple[dict, callable]],
    ) -> int:
        """Register code-in-DB tools (``ai_tools``). Returns the count registered.

        Called by ``agent.ai_tool_installer`` after core + plugin tools, so the
        registry's collision no-op gives code precedence over the DB. Tagged with
        ``plugin_id=None`` (same as core) — identity is the tool ``name``.
        """
        before = len(self._tool_executors)
        for schema, executor in tools:
            self._register_tool(schema, executor, plugin_id=None)
        return len(self._tool_executors) - before

    def register_plugin_prompts(
        self,
        plugin_id: str,
        fragments: list[callable],
    ) -> None:
        """Register prompt fragments from a plugin. Called by the plugin loader."""
        for fn in fragments:
            self._prompt_fragments.append((fn, plugin_id))

    def known_tool_names(self) -> set[str]:
        """Names of every tool currently registered (core + plugin)."""
        return set(self._tool_originals.keys())

    def refresh_tool_overrides(self) -> None:
        """Re-read ``tool_overrides`` and rebuild ``_tool_schemas``.

        Called after every PUT on /api/tools/{name} and once after plugin
        loading at startup. Atomically replaces ``_tool_schemas`` so requests
        already in flight keep their captured reference.

        Plugin tools registered after startup are not supported today — plugin
        enable/disable forces a server restart, and this method assumes the
        registry is stable when called.
        """
        try:
            overrides = {row["name"]: row for row in tool_override_repo.list_all()}
        except Exception as e:
            logger.warning("Failed to read tool_overrides: %s", e)
            overrides = {}
        new_schemas: list[dict] = []
        new_disabled: set[str] = set()
        for name, original in self._tool_originals.items():
            ov = overrides.get(name)
            if ov and not ov["enabled"]:
                new_disabled.add(name)
                continue
            if ov and ov.get("description"):
                schema = copy.deepcopy(original)
                schema["function"]["description"] = ov["description"]
                new_schemas.append(schema)
            else:
                new_schemas.append(original)
        self._tool_schemas = new_schemas
        self._disabled_tools = new_disabled

    def list_tools(self) -> list[dict]:
        """Return metadata for every registered tool, with override state merged."""
        try:
            overrides = {row["name"]: row for row in tool_override_repo.list_all()}
        except Exception:
            overrides = {}
        items: list[dict] = []
        for name, original in self._tool_originals.items():
            fn = original.get("function", {})
            default_description = fn.get("description", "")
            default_label = self._tool_default_labels.get(name)
            _, plugin_id = self._tool_executors.get(name, (None, None))
            ov = overrides.get(name) or {}
            current_description = ov.get("description") or default_description
            current_label = ov.get("display_label") or default_label
            items.append({
                "name": name,
                "plugin_id": plugin_id,
                "default_description": default_description,
                "current_description": current_description,
                "default_label": default_label,
                "display_label": ov.get("display_label"),
                "current_label": current_label,
                "enabled": bool(ov.get("enabled", 1)),
                "has_override": bool(ov.get("description")),
                "has_label_override": bool(ov.get("display_label")),
                "parameters_schema": fn.get("parameters", {}),
            })
        return items

    def _make_tool_ctx(
        self,
        contact: ContactMemory,
        plugin_id: str | None = None,
    ) -> ToolContext:
        return ToolContext(
            contact=contact,
            handler=self,
            tag_registry=self.tag_registry,
            plugin_id=plugin_id,
        )

    def _dispatch_tool(
        self,
        contact: ContactMemory,
        name: str,
        args: dict,
    ) -> str | None:
        """Run a tool by name and return an optional follow-up feedback string."""
        entry = self._tool_executors.get(name)
        if not entry:
            logger.warning("Unknown tool: %s", name)
            return None
        if name in self._disabled_tools:
            logger.info("Tool '%s' is disabled by user override; skipping", name)
            return None
        executor, plugin_id = entry
        ctx = self._make_tool_ctx(contact, plugin_id=plugin_id)
        try:
            return executor(ctx, args)
        except Exception as e:
            logger.warning("Tool '%s' execution failed: %s", name, e)
            return None

    def _sanitize_reply(self, sender: str, reply: str) -> str:
        """Substitui a resposta por um fallback se ela parecer tool-call cru vazado.

        Ver o comentário no topo do módulo (``_looks_like_raw_tool_call``) — isso
        é o modelo/proxy gerando texto malformado, não algo que dá pra evitar na
        origem. Aplicado ANTES de salvar no histórico e ANTES de enviar, pra não
        persistir o erro como contexto de turnos futuros. Rede de segurança: o
        motor AGNO já reduz muito esse vazamento (oferece tools em todo round),
        mas não recupera DSML emitido como texto.
        """
        if _looks_like_raw_tool_call(reply):
            logger.warning(
                "Reply for %s parece tool-call cru vazado (model=%s) — suprimindo: %r",
                sender, self.model, reply[:300],
            )
            return _FALLBACK_REPLY_APOS_VAZAMENTO
        return reply

    def _estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int,
                       cached_tokens: int = 0) -> tuple[float, float]:
        """Return ``(cost_usd, saved_usd)`` using the listed price (an estimate)."""
        details = None
        if self.pricing_details_fn:
            details = self.pricing_details_fn(model)
        if details:
            return estimate_cost(details, prompt_tokens, completion_tokens, cached_tokens)
        if self.pricing_fn:
            prompt_price, completion_price = self.pricing_fn(model)
            return (prompt_tokens * prompt_price) + (completion_tokens * completion_price), 0.0
        return 0.0, 0.0

    def _record_usage(self, phone: str, call_type: str, model: str, response) -> None:
        """Extract usage from an OpenAI-compatible response and record it."""
        try:
            usage = getattr(response, "usage", None)
            if not usage:
                return
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", 0) or 0
            cost_usd, saved_usd = self._estimate_cost(
                model, prompt_tokens, completion_tokens, cached_tokens)
            if phone:
                contact = self._get_contact(phone)
                contact.add_usage(call_type, model, prompt_tokens, completion_tokens, total_tokens,
                                  cost_usd, cached_tokens=cached_tokens, saved_usd=saved_usd)
            logger.debug("Usage recorded for %s: %s %s tokens=%d cached=%d cost=%.6f saved=%.6f",
                         phone, call_type, model, total_tokens, cached_tokens, cost_usd, saved_usd)
        except Exception as e:
            logger.warning("Failed to record usage: %s", e)
        # Trigger a low-balance check after every billable call. The monitor
        # rate-limits actual fetches so this is cheap on a hot path.
        try:
            from server import balance_monitor
            balance_monitor.trigger_check_async()
        except Exception:
            pass

    def _record_usage_tokens(self, phone: str, call_type: str, model: str,
                             prompt_tokens: int, completion_tokens: int,
                             total_tokens: int, cached_tokens: int = 0) -> None:
        """Record usage from explicit token counts (AGNO metrics path).

        Mirrors ``_record_usage`` but takes raw token numbers instead of an
        OpenAI response object, since the AGNO engine reports usage via
        ``RunMetrics`` rather than a ``response.usage`` attribute.
        """
        try:
            cost_usd, saved_usd = self._estimate_cost(
                model, prompt_tokens, completion_tokens, cached_tokens)
            contact = self._get_contact(phone)
            contact.add_usage(call_type, model, prompt_tokens, completion_tokens,
                              total_tokens, cost_usd, cached_tokens=cached_tokens,
                              saved_usd=saved_usd)
            ratio = (cached_tokens / prompt_tokens) if prompt_tokens else 0.0
            logger.debug("Usage recorded for %s: %s %s tokens=%d cached=%d ratio=%.2f cost=%.6f saved=%.6f",
                         phone, call_type, model, total_tokens, cached_tokens, ratio, cost_usd, saved_usd)
        except Exception as e:
            logger.warning("Failed to record usage: %s", e)
        try:
            from server import balance_monitor
            balance_monitor.trigger_check_async()
        except Exception:
            pass

    def _get_client(self) -> OpenAI:
        if self._client is None or self._client.api_key != self.api_key:
            self._client = OpenAI(
                base_url=LLM_API_BASE_URL,
                api_key=self.api_key,
            )
        return self._client

    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None or self._async_client.api_key != self.api_key:
            self._async_client = AsyncOpenAI(
                base_url=LLM_API_BASE_URL,
                api_key=self.api_key,
            )
        return self._async_client

    def update_config(
        self,
        api_key: str | None = None,
        system_prompt: str | None = None,
        max_context_messages: int | None = None,
        inactivity_timeout_min: int | None = None,
        model: str | None = None,
        improvement_model: str | None = None,
        audio_model: str | None = None,
        image_model: str | None = None,
        document_model: str | None = None,
        split_messages: bool | None = None,
        default_ai_enabled: bool | None = None,
        ai_engine_enabled: bool | None = None,
    ):
        if api_key is not None:
            self.api_key = api_key
            self._client = None
            self._async_client = None
        if system_prompt is not None:
            self.system_prompt = system_prompt
        if max_context_messages is not None:
            self.max_context_messages = max_context_messages
        if inactivity_timeout_min is not None:
            self.inactivity_timeout = inactivity_timeout_min * 60
        if model is not None:
            self.model = model
        if improvement_model is not None:
            self.improvement_model = improvement_model
        if audio_model is not None:
            self.audio_model = audio_model
        if image_model is not None:
            self.image_model = image_model
        if document_model is not None:
            self.document_model = document_model
        if split_messages is not None:
            self.split_messages = split_messages
        if default_ai_enabled is not None:
            self.default_ai_enabled = default_ai_enabled
        if ai_engine_enabled is not None:
            self.ai_engine_enabled = ai_engine_enabled

    def transcribe_audio(self, audio_path: str, phone: str = "") -> str:
        """Transcribe an audio file using the configured audio model."""
        if not self.api_key:
            return ""
        try:
            p = Path(audio_path)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            if not p.exists():
                logger.warning("Audio file not found for transcription: %s", audio_path)
                return ""
            data = p.read_bytes()
            b64 = base64.b64encode(data).decode()
            # Determine format from extension
            ext = p.suffix.lower().lstrip(".")
            if ext in ("oga", "ogg", "opus"):
                fmt = "ogg"
            elif ext == "mp3":
                fmt = "mp3"
            elif ext == "wav":
                fmt = "wav"
            else:
                fmt = "ogg"

            client = self._get_client()
            response = client.chat.completions.create(
                model=self.audio_model,
                timeout=60,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": b64, "format": fmt},
                        },
                        {
                            "type": "text",
                            "text": "Transcreva este áudio fielmente em português. Retorne apenas a transcrição, sem comentários adicionais.",
                        },
                    ],
                }],
                max_tokens=2048,
            )
            self._record_usage(phone, "audio", self.audio_model, response)
            result = response.choices[0].message.content.strip()
            track_step("media_processed", {
                "type": "audio",
                "model": self.audio_model,
                "transcription_length": len(result),
            })
            logger.info("Audio transcribed (%d chars): %s", len(result), result[:80])
            return result
        except Exception as e:
            logger.error("Audio transcription failed: %s", e)
            track_step("error", {"error": str(e), "phase": "audio_transcription"}, status="error")
            return ""

    def describe_image(self, image_path: str, phone: str = "") -> str:
        """Describe an image using the configured image model."""
        if not self.api_key:
            return ""
        try:
            p = Path(image_path)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            if not p.exists():
                logger.warning("Image file not found for description: %s", image_path)
                return ""
            data = p.read_bytes()
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            b64 = base64.b64encode(data).decode()

            client = self._get_client()
            response = client.chat.completions.create(
                model=self.image_model,
                timeout=60,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {
                            "type": "text",
                            "text": "Descreva detalhadamente o conteúdo desta imagem em português.",
                        },
                    ],
                }],
                max_tokens=1024,
            )
            self._record_usage(phone, "image", self.image_model, response)
            result = response.choices[0].message.content.strip()
            track_step("media_processed", {
                "type": "image",
                "model": self.image_model,
                "description_length": len(result),
            })
            logger.info("Image described (%d chars): %s", len(result), result[:80])
            return result
        except Exception as e:
            logger.error("Image description failed: %s", e)
            track_step("error", {"error": str(e), "phase": "image_description"}, status="error")
            return ""

    # Plain-text document extensions read directly from disk (no LLM needed).
    _TEXT_DOC_EXTS = {
        "txt", "text", "md", "markdown", "csv", "tsv", "log", "json", "xml",
        "html", "htm", "yaml", "yml", "ini", "cfg", "conf", "srt", "vtt", "rtf",
    }
    # Max characters of locally-extracted text fed back as the "transcription".
    _DOC_TEXT_LIMIT = 20000

    @staticmethod
    def _doc_kind(file_name: str, path: "Path", mimetype: str) -> str:
        """Classify a document into pdf | docx | text | unsupported.

        Extension (from the original filename first, then the on-disk path)
        wins; mimetype is the fallback since GOWA's auto-download path is often
        UUID-based without a usable suffix.
        """
        ext = ""
        for cand in (file_name, str(path)):
            if cand:
                e = Path(cand).suffix.lower().lstrip(".")
                if e:
                    ext = e
                    break
        mt = (mimetype or "").lower()
        if ext == "pdf" or "pdf" in mt:
            return "pdf"
        if ext == "docx" or "wordprocessingml" in mt:
            return "docx"
        if (ext in AgentHandler._TEXT_DOC_EXTS or mt.startswith("text/")
                or mt in ("application/json", "application/xml")):
            return "text"
        return "unsupported"

    @staticmethod
    def _extract_docx_text(p: "Path") -> str:
        """Extract visible text from a .docx (zip of XML) using stdlib only."""
        import zipfile
        import html as _html
        try:
            with zipfile.ZipFile(p) as z:
                xml = z.read("word/document.xml").decode("utf-8", "ignore")
        except Exception as e:
            logger.warning("docx extraction failed for %s: %s", p, e)
            return ""
        # Paragraph + line breaks → newlines, then strip every tag.
        xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n")
        text = re.sub(r"<[^>]+>", "", xml)
        return _html.unescape(text).strip()

    def transcribe_document(
        self,
        document_path: str,
        phone: str = "",
        file_name: str = "",
        mimetype: str = "",
    ) -> str:
        """Read/transcribe a document (PDF, DOCX, plain text) into text.

        PDFs go to the configured ``document_model`` via the OpenRouter-style
        ``file`` content part (the model handles both digital and scanned PDFs).
        DOCX and plain-text files are extracted locally with stdlib — no LLM
        call needed. Unsupported formats (legacy .doc, spreadsheets, …) return
        an empty string so the caller falls back to just the document label.
        """
        try:
            p = Path(document_path)
            if not p.is_absolute():
                p = Path(__file__).resolve().parent.parent / p
            if not p.exists():
                logger.warning("Document not found for transcription: %s", document_path)
                return ""

            kind = self._doc_kind(file_name, p, mimetype)

            if kind == "docx":
                result = self._extract_docx_text(p)[: self._DOC_TEXT_LIMIT].strip()
                if result:
                    track_step("media_processed", {
                        "type": "document", "model": "local-docx",
                        "transcription_length": len(result),
                    })
                    logger.info("Document (docx) extracted (%d chars)", len(result))
                return result

            if kind == "text":
                try:
                    result = p.read_text(encoding="utf-8", errors="ignore")
                except Exception as e:
                    logger.warning("Text document read failed for %s: %s", p, e)
                    return ""
                result = result[: self._DOC_TEXT_LIMIT].strip()
                if result:
                    track_step("media_processed", {
                        "type": "document", "model": "local-text",
                        "transcription_length": len(result),
                    })
                    logger.info("Document (text) read (%d chars)", len(result))
                return result

            if kind != "pdf":
                logger.info("Document type unsupported for transcription: %s (%s)",
                            file_name or document_path, mimetype)
                return ""

            # PDF → LLM file input.
            if not self.api_key:
                return ""
            data = p.read_bytes()
            b64 = base64.b64encode(data).decode()
            client = self._get_client()
            response = client.chat.completions.create(
                model=self.document_model,
                timeout=120,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "filename": file_name or p.name or "document.pdf",
                                "file_data": f"data:application/pdf;base64,{b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extraia e transcreva todo o conteúdo textual deste "
                                "documento em português brasileiro, incluindo tabelas e "
                                "dados relevantes de forma organizada. Retorne apenas o "
                                "conteúdo do documento, sem comentários adicionais."
                            ),
                        },
                    ],
                }],
                max_tokens=4096,
            )
            self._record_usage(phone, "document", self.document_model, response)
            result = (response.choices[0].message.content or "").strip()
            track_step("media_processed", {
                "type": "document",
                "model": self.document_model,
                "transcription_length": len(result),
            })
            logger.info("Document transcribed (%d chars): %s", len(result), result[:80])
            return result
        except Exception as e:
            logger.error("Document transcription failed: %s", e)
            track_step("error", {"error": str(e), "phase": "document_transcription"}, status="error")
            return ""

    def _get_contact(self, phone: str) -> ContactMemory:
        if phone not in self._contacts:
            self._contacts[phone] = ContactMemory(phone, default_ai_enabled=self.default_ai_enabled)
        return self._contacts[phone]

    def _select_active_tools(self, agent_spec) -> list[dict]:
        """Return the effective tool schemas, restricted to the agent's selection.

        When the DB-driven agent declares ``tool_names`` (a list), only those
        tools are exposed; ``None`` (or no spec) means every registered tool —
        identical to the legacy behaviour. Plugin ``filter.llm.tools`` runs
        afterwards on whatever subset this returns.
        """
        if agent_spec is None or agent_spec.tool_names is None:
            return list(self._tool_schemas)
        wanted = set(agent_spec.tool_names)
        return [
            s for s in self._tool_schemas
            if (s.get("function") or {}).get("name") in wanted
        ]

    @staticmethod
    def _encode_history_for_split(context_messages: list[dict]) -> list[dict]:
        """Re-encode assistant turns as JSON arrays for the split_messages format.

        When split_messages is on, the model is asked to answer with a JSON array
        of strings. But the assistant history is stored already split into clean
        plain text, so the model SEES its own past turns as plain text and mimics
        that pattern — drifting out of the JSON format. The presence of tools
        amplifies this drift dramatically (measured: 1/10 vs 15/15 success).

        Fix: present each assistant turn to the model in the SAME JSON-array shape
        it must produce. Consecutive assistant messages (one turn's split parts)
        are merged into a single array, mirroring one real response. Only the
        LLM-facing copy is changed — stored history and panel display are intact.
        """
        out: list[dict] = []
        buffer: list[str] = []

        def flush() -> None:
            if buffer:
                out.append({"role": "assistant",
                            "content": json.dumps(buffer, ensure_ascii=False)})
                buffer.clear()

        for m in context_messages:
            if m.get("role") == "assistant" and isinstance(m.get("content"), str):
                buffer.append(m.get("content") or "")
            else:
                flush()
                out.append(m)
        flush()
        return out

    def _build_system_prompt(self, contact: ContactMemory,
                             base_prompt: str | None = None) -> str:
        """Build system prompt with contact info and current date/time injected.

        ``base_prompt`` overrides ``self.system_prompt`` as the starting text
        (config-in-DB path); the dynamic sections (group context, contact info,
        tags, date, plugin fragments, split-messages format) layer on top
        unchanged in both paths.
        """
        prompt = base_prompt if base_prompt is not None else self.system_prompt
        if contact.is_group:
            gname = f" chamado '{contact.group_name}'" if contact.group_name else ""
            prompt += (
                f"\n\n--- Contexto de grupo ---\n"
                f"Esta é uma conversa de grupo do WhatsApp{gname}.\n"
                "As mensagens de usuários estão no formato '[Nome]: mensagem'.\n"
                "Quando responder, leve em conta quem fez a pergunta e responda "
                "de forma natural ao grupo.\n"
                "--- Fim do contexto de grupo ---"
            )
            # Inject participant names so the AI can @mention a specific member.
            try:
                members = group_mentions.get_members(contact.phone)
            except Exception:
                members = []
            named = [m["name"] for m in members if m.get("name")]
            if named:
                prompt += (
                    "\n\n--- Mencionar participantes ---\n"
                    "Para mencionar alguém do grupo, escreva @ seguido do nome "
                    "EXATAMENTE como aparece nesta lista — o sistema converte "
                    "automaticamente para a menção real do WhatsApp:\n"
                    + ", ".join(f"@{n}" for n in named)
                    + "\nMencione apenas quando fizer sentido se dirigir a uma "
                    "pessoa específica; não mencione todo mundo sem necessidade.\n"
                    "--- Fim mencionar participantes ---"
                )
        info_summary = contact.get_info_summary()
        if info_summary:
            prompt += (
                f"\n\n--- Informações já conhecidas sobre este contato ({contact.phone}) ---\n"
                f"{info_summary}\n"
                "IMPORTANTE: Use estas informações na conversa. "
                "NÃO pergunte dados que já estão listados acima (ex: nome, email, etc). "
                "NÃO chame save_contact_info para dados que já aparecem nesta seção — "
                "eles já estão salvos. Só use save_contact_info quando o usuário revelar "
                "informação NOVA na mensagem mais recente.\n"
                "--- Fim das informações ---"
            )
        tags_summary = contact.get_tags_summary()
        if tags_summary:
            prompt += (
                f"\n\n--- Tags do contato ---\n"
                f"{tags_summary}\n"
                "Estas tags foram aplicadas por operadores humanos para classificar este contato. "
                "Use-as como sinal de contexto sobre o histórico/perfil do contato, mas não as "
                "mencione diretamente na conversa nem peça confirmação sobre elas.\n"
                "--- Fim das tags ---"
            )
        prompt += (
            "\n\nMensagens marcadas com '[Mensagem do operador humano]' no histórico "
            "foram enviadas por um atendente real, não por você. Considere o contexto "
            "mas não imite o estilo do operador."
            "\n\nMensagens marcadas com '[Nota privada do operador]' são notas "
            "internas do painel — o contato NUNCA as viu. Quando o operador "
            "acionar você sobre uma nota (via toggle 'IA lê' no painel), trate-a "
            "como instrução direta para executar: use as tools disponíveis (ex.: "
            "criar lembrete, salvar informações do contato, etc.) se a instrução "
            "pedir, e redija a resposta ao contato. Caso contrário, use as notas "
            "apenas como contexto, sem citar, mencionar ou parafrasear em "
            "respostas ao contato."
        )

        # Plugin-contributed prompt fragments. Each fragment is a callable that
        # receives (contact, PromptContext) and returns a string (or empty).
        # Errors are isolated so a buggy plugin can't kill the request.
        for fragment_fn, plugin_id in self._prompt_fragments:
            try:
                ctx = PromptContext(handler=self, plugin_id=plugin_id)
                chunk = fragment_fn(contact, ctx)
                if chunk:
                    prompt += chunk
            except Exception as e:
                logger.warning(
                    "Plugin %s prompt fragment failed: %s",
                    plugin_id or "?", e,
                )
        _BRT = timezone(timedelta(hours=-3))
        now = datetime.now(_BRT)
        dias = ["segunda-feira", "terça-feira", "quarta-feira",
                "quinta-feira", "sexta-feira", "sábado", "domingo"]
        prompt += (
            f"\n\n--- Data e hora atual ---\n"
            f"Data: {now.strftime('%d/%m/%Y')} ({dias[now.weekday()]})\n"
            f"Hora: {now.strftime('%H:%M')}\n"
            "--- Fim ---"
        )
        if self.split_messages:
            prompt += (
                "\n\n--- FORMATO DE SAÍDA (OBRIGATÓRIO) ---\n"
                "Sua resposta INTEIRA deve ser UM array JSON de strings — nada mais.\n"
                "O PRIMEIRO caractere da sua saída é '[' e o ÚLTIMO é ']'.\n"
                "Cada string do array vira uma mensagem separada no WhatsApp.\n"
                "\n"
                "PROIBIDO (quebra o sistema):\n"
                "- Escrever qualquer coisa fora do array (sem texto antes do '[' nem depois do ']').\n"
                "- Usar separadores entre mensagens: nada de '---', 'response', '\\n\\n', bullets, números.\n"
                "  A ÚNICA forma de separar mensagens é criar outro elemento (string) no MESMO array.\n"
                "- Retornar vários arrays. É sempre UM único array, com um único '[' e um único ']'.\n"
                "- Markdown, títulos ou formatação especial dentro das strings.\n"
                "\n"
                "Como dividir:\n"
                "- Cada mensagem deve ser CURTA (1 a 3 linhas). Mensagens grandes ficam horríveis.\n"
                "- Resposta simples = array com 1 elemento só. Não separe a saudação do conteúdo curto.\n"
                "- Dados distintos (link, chave PIX, valor, instrução) = cada um em seu próprio elemento.\n"
                "\n"
                "EXEMPLO de como a sua resposta DEVE ser.\n"
                "Se você fosse mandar isto (NÃO faça assim, é uma só mensagem gigante):\n"
                "  Ótimo! Já vou te mandar o link do e-book.\n"
                "  https://exemplo.com/ebook\n"
                "  Faça o PIX de R$27 pra liberar o acesso.\n"
                "  Chave: f765ce68-49ba-4c08-9624-8b1fd63779b2\n"
                "  Aparece como: Techify\n"
                "  Quando pagar, me manda o print que eu confiro.\n"
                "Você DEVE responder assim (array JSON, cada parte uma mensagem curta):\n"
                '[\"Ótimo! Já vou te mandar o link do e-book.\", '
                '\"https://exemplo.com/ebook\", '
                '\"Faça o PIX de R$27 pra liberar o acesso.\", '
                '\"Chave: f765ce68-49ba-4c08-9624-8b1fd63779b2\", '
                '\"Aparece como: Techify\", '
                '\"Quando pagar, me manda o print que eu confiro.\"]\n'
                "\n"
                "Mais exemplos:\n"
                'Resposta curta: [\"Ok, só um minuto!\"]\n'
                'Resposta média: [\"Sobre o plano mensal:\", \"São R$99 e inclui X, Y e Z.\", \"Quer o link?\"]\n'
                "--- FIM DO FORMATO ---"
            )
        return prompt

    def _find_tools_used_around(self, phone: str, ts: float | None) -> list[dict]:
        """Best-effort: return the tools the AI called for the reply at ``ts``.

        Messages have no execution_id, so we match by phone + the execution
        time window that contains the reply timestamp (with a small tolerance).
        Returns a list of {"tool", "args"} dicts (possibly empty).
        """
        if not ts:
            return []
        used: list[dict] = []
        try:
            execs = execution_repo.list_executions(limit=50, phone=phone)
            match = None
            for e in execs:
                started = e.get("started_at") or 0
                completed = e.get("completed_at") or started
                if started - 5 <= ts <= completed + 15:
                    match = e
                    break
            if match:
                full = execution_repo.get_by_id(match["id"]) or {}
                for step in full.get("steps", []):
                    if step.get("step_type") == "tool_executed":
                        data = step.get("data") or {}
                        used.append({"tool": data.get("tool"), "args": data.get("args")})
        except Exception as e:
            logger.warning("Failed to recover tool calls for improvement (%s): %s", phone, e)
        return used

    def generate_improvement(self, phone: str, target_message: dict | None,
                             feedback: str) -> str:
        """Analyze an AI reply the operator flagged as wrong and suggest fixes.

        One-off, non-agentic LLM call. Gathers the contact's recent
        conversation, the full system prompt, the registered tools and the
        tools the AI actually used around the flagged reply, then asks the
        model to (1) diagnose what went wrong and (2) recommend concrete
        edits to the main prompt. Returns the analysis text — the caller
        saves it as a private note.
        """
        if not self.api_key:
            return "[WhatsBot-Lite] API key não configurada — não foi possível gerar a análise."

        contact = self._get_contact(phone)

        # Faithful main prompt for this contact (config-in-DB aware).
        agent_spec = agent_factory.build_for_contact(self, contact)
        base_prompt = agent_spec.base_prompt if agent_spec else None
        system_prompt_str = self._build_system_prompt(contact, base_prompt=base_prompt)

        # Available tools (enabled only), name + effective description.
        try:
            avail = [t for t in self.list_tools() if t.get("enabled")]
        except Exception:
            avail = []
        avail_text = "\n".join(
            f"- {t['name']}: {t.get('current_description') or ''}".rstrip(": ")
            for t in avail
        ) or "(nenhuma ferramenta registrada)"

        target = target_message or {}
        target_ts = target.get("ts")
        target_content = (target.get("content") or "").strip() or "(sem texto)"

        used = self._find_tools_used_around(phone, target_ts)
        if used:
            used_lines = []
            for u in used:
                args = u.get("args")
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args_str = str(args)
                used_lines.append(f"- {u.get('tool')}({args_str})")
            used_text = "\n".join(used_lines)
        else:
            used_text = ("Nenhuma ferramenta foi usada nesta resposta "
                         "(ou não foi possível rastrear a execução).")

        # Plain-text transcript of the recent conversation. Mark the flagged
        # reply inline so the model can pinpoint it.
        limit = self.max_context_messages
        rows = message_repo.get_context(contact.id, limit)
        role_label = {"user": "Cliente", "assistant": "IA",
                      "private_note": "Nota privada do operador"}
        lines = []
        for m in rows:
            role = m.get("role")
            if role == "assistant" and m.get("status") == "operator":
                label = "Operador (humano)"
            else:
                label = role_label.get(role, role)
            text = (m.get("content") or "").strip()
            if not text and m.get("media_type"):
                text = f"[{m['media_type']}]"
            line = f"{label}: {text}"
            if target_ts and abs((m.get("ts") or 0) - target_ts) < 1.0 \
                    and role == "assistant":
                line += "   ⟵ RESPOSTA MARCADA COMO INCORRETA"
            lines.append(line)
        transcript = "\n".join(lines) or "(sem histórico)"

        analysis_system = (
            "Você é um analista de qualidade de agentes de IA de atendimento no "
            "WhatsApp. Um operador humano marcou uma resposta da IA como incorreta "
            "ou problemática. Com base no prompt principal do agente, no histórico "
            "da conversa, nas ferramentas disponíveis, nas ferramentas que a IA "
            "efetivamente usou e no feedback do operador, produza uma análise "
            "OBJETIVA e ACIONÁVEL, em português do Brasil, com duas seções:\n"
            "1. Diagnóstico — o que a IA fez de errado nessa resposta e por quê "
            "(cite o trecho relevante do prompt, ou a ferramenta que deveria / não "
            "deveria ter sido usada).\n"
            "2. Como ajustar o prompt principal — recomendações concretas: trechos "
            "de texto para adicionar, remover ou reescrever no prompt principal "
            "para evitar o erro. Quando útil, mostre a frase sugerida entre aspas, "
            "pronta para colar.\n\n"
            "Regras:\n"
            "- Não invente ferramentas que não existem na lista fornecida.\n"
            "- Seja conciso e direto; foque no que muda o comportamento da IA.\n"
            "- Você fala apenas com o operador; não redija uma resposta ao cliente."
        )
        analysis_user = (
            f"## PROMPT PRINCIPAL DO AGENTE (atual)\n{system_prompt_str}\n\n"
            f"## FERRAMENTAS DISPONÍVEIS\n{avail_text}\n\n"
            f"## FERRAMENTAS USADAS NESTA RESPOSTA\n{used_text}\n\n"
            f"## HISTÓRICO RECENTE DA CONVERSA\n{transcript}\n\n"
            f"## RESPOSTA MARCADA COMO INCORRETA\n{target_content}\n\n"
            f"## FEEDBACK DO OPERADOR\n"
            f"{feedback.strip() or '(o operador não escreveu um comentário; diagnostique pelo contexto)'}"
        )

        analysis_model = self.improvement_model or self.model
        client = self._get_client()
        response = client.chat.completions.create(
            model=analysis_model,
            timeout=120,
            temperature=0.4,
            max_tokens=1600,
            messages=[
                {"role": "system", "content": analysis_system},
                {"role": "user", "content": analysis_user},
            ],
        )
        self._record_usage(phone, "improvement", analysis_model, response)
        return (response.choices[0].message.content or "").strip()

    async def aprocess_message(self, sender: str, text: str, *,
                               save_user_message: bool = True,
                               save_response: bool = True,
                               image_path: str | None = None,
                               audio_path: str | None = None,
                               disable_tools: bool = False) -> ProcessResult:
        """Async cancellable equivalent of process_message.

        Uses AsyncOpenAI so that cancelling the surrounding asyncio task aborts the
        in-flight HTTP request instead of letting it complete in the background.
        """
        if not self.api_key:
            return ProcessResult(reply="[WhatsBot-Lite] API key não configurada.")

        contact = self._get_contact(sender)

        media_type: str | None = None
        media_path: str | None = None
        if image_path:
            media_type = "image"
            media_path = image_path
        elif audio_path:
            media_type = "audio"
            media_path = audio_path

        if save_user_message:
            contact.add_message("user", text or "", media_type=media_type, media_path=media_path)

        context_messages = contact.get_context_messages(self.max_context_messages)
        if self.split_messages:
            context_messages = self._encode_history_for_split(context_messages)

        # Config-in-DB: resolve the DB-driven agent for this contact (or None to
        # use the in-code prompt/model/tools — full parity when the flag is off).
        agent_spec = agent_factory.build_for_contact(self, contact)
        model = (agent_spec.model_config.get("model") if agent_spec else None) or self.model
        model_config = agent_spec.model_config if agent_spec else None
        base_prompt = agent_spec.base_prompt if agent_spec else None

        system_prompt_str = self._build_system_prompt(contact, base_prompt=base_prompt)
        system_prompt_str = await apply_filter(
            "filter.system_prompt", system_prompt_str, {"phone": sender}
        )
        if system_prompt_str is None:
            return ProcessResult(reply="")

        messages = [
            {"role": "system", "content": system_prompt_str},
            *context_messages,
        ]
        messages = await apply_filter(
            "filter.llm.messages", messages, {"phone": sender}
        )
        if messages is None:
            return ProcessResult(reply="")

        active_tools = [] if disable_tools else self._select_active_tools(agent_spec)
        active_tools = await apply_filter(
            "filter.llm.tools", active_tools, {"phone": sender}
        )
        if active_tools is None:
            active_tools = []

        try:
            _llm_t0 = time.monotonic()
            await emit_with_filter("llm.before", {
                "phone": sender,
                "model": model,
                "message_count": len(messages),
                "has_tools": bool(active_tools),
                "tool_count": len(active_tools),
                "image_path": image_path,
                "audio_path": audio_path,
                "ts": time.time(),
            })

            # Delegate the reasoning + tool-calling loop to the AGNO engine.
            # Tool filters/events (filter.tool.args/result, tool.before/after)
            # are applied inside the wrapped tool entrypoints; usage is reported
            # via AGNO RunMetrics rather than an OpenAI response object.
            result = await agno_engine.run_async(
                self, contact, sender, messages, active_tools,
                model_config=model_config,
            )
            reply = self._sanitize_reply(sender, result.reply)
            executed_tools = result.executed_tools
            usage_dict = result.usage

            if usage_dict:
                self._record_usage_tokens(
                    sender, "text", model,
                    usage_dict.get("prompt_tokens", 0),
                    usage_dict.get("completion_tokens", 0),
                    usage_dict.get("total_tokens", 0),
                    usage_dict.get("cached_tokens", 0),
                )

            if save_response:
                contact.add_message("assistant", reply)
            logger.info("Processed message from %s", sender)

            updated_info = None
            if any(tc.get("tool") == "save_contact_info" for tc in executed_tools):
                updated_info = dict(contact.info)
                updated_info["observations"] = list(updated_info.get("observations", []))

            await emit_with_filter("llm.after", {
                "phone": sender,
                "model": model,
                "reply": reply,
                "tool_calls": executed_tools,
                "usage": usage_dict,
                "latency_ms": int((time.monotonic() - _llm_t0) * 1000),
                "ts": time.time(),
            })

            return ProcessResult(reply=reply, tool_calls=executed_tools, contact_info=updated_info)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("LLM error for %s: %s", sender, e)
            track_step("error", {"error": str(e), "phase": "llm_call"}, status="error")
            await emit_with_filter("llm.after", {
                "phone": sender, "model": model,
                "reply": "", "tool_calls": [], "usage": None,
                "error": str(e),
                "latency_ms": int((time.monotonic() - locals().get("_llm_t0", time.monotonic())) * 1000),
                "ts": time.time(),
            })
            error_msg = str(e)
            if "401" in error_msg or "unauthorized" in error_msg.lower():
                return ProcessResult(reply="[WhatsBot-Lite] API key inválida. Verifique sua chave OpenRouter.")
            if "429" in error_msg or "rate" in error_msg.lower():
                return ProcessResult(reply="[WhatsBot-Lite] Limite de requisições atingido. Tente novamente em instantes.")
            return ProcessResult(reply="[WhatsBot-Lite] Erro ao processar mensagem. Tente novamente.")

    def process_message(self, sender: str, text: str, *,
                        save_user_message: bool = True,
                        save_response: bool = True,
                        image_path: str | None = None,
                        audio_path: str | None = None,
                        disable_tools: bool = False) -> ProcessResult:
        """Process an incoming message and return the AI response."""
        if not self.api_key:
            return ProcessResult(reply="[WhatsBot-Lite] API key não configurada.")

        contact = self._get_contact(sender)

        # Determine media metadata for storage
        media_type: str | None = None
        media_path: str | None = None
        if image_path:
            media_type = "image"
            media_path = image_path
        elif audio_path:
            media_type = "audio"
            media_path = audio_path

        if save_user_message:
            contact.add_message("user", text or "", media_type=media_type, media_path=media_path)

        context_messages = contact.get_context_messages(self.max_context_messages)
        if self.split_messages:
            context_messages = self._encode_history_for_split(context_messages)

        # Config-in-DB: resolve the DB-driven agent for this contact (or None to
        # use the in-code prompt/model/tools — full parity when the flag is off).
        agent_spec = agent_factory.build_for_contact(self, contact)
        model = (agent_spec.model_config.get("model") if agent_spec else None) or self.model
        model_config = agent_spec.model_config if agent_spec else None
        base_prompt = agent_spec.base_prompt if agent_spec else None

        system_prompt_str = self._build_system_prompt(contact, base_prompt=base_prompt)
        system_prompt_str = apply_filter_sync(
            "filter.system_prompt", system_prompt_str, {"phone": sender}
        )
        if system_prompt_str is None:
            return ProcessResult(reply="")

        messages = [
            {"role": "system", "content": system_prompt_str},
            *context_messages,
        ]
        messages = apply_filter_sync(
            "filter.llm.messages", messages, {"phone": sender}
        )
        if messages is None:
            return ProcessResult(reply="")

        active_tools = [] if disable_tools else self._select_active_tools(agent_spec)
        active_tools = apply_filter_sync(
            "filter.llm.tools", active_tools, {"phone": sender}
        )
        if active_tools is None:
            active_tools = []

        try:
            _llm_t0 = time.monotonic()
            emit_with_filter_sync("llm.before", {
                "phone": sender, "model": model,
                "message_count": len(messages),
                "has_tools": bool(active_tools),
                "tool_count": len(active_tools),
                "image_path": image_path, "audio_path": audio_path,
                "ts": time.time(),
            })

            # Delegate the reasoning + tool-calling loop to the AGNO engine.
            # Tool filters/events run inside the wrapped tool entrypoints; usage
            # is reported via AGNO RunMetrics.
            result = agno_engine.run_sync(
                self, contact, sender, messages, active_tools,
                model_config=model_config,
            )
            reply = self._sanitize_reply(sender, result.reply)
            executed_tools = result.executed_tools
            usage_dict = result.usage

            if usage_dict:
                self._record_usage_tokens(
                    sender, "text", model,
                    usage_dict.get("prompt_tokens", 0),
                    usage_dict.get("completion_tokens", 0),
                    usage_dict.get("total_tokens", 0),
                    usage_dict.get("cached_tokens", 0),
                )

            if save_response:
                contact.add_message("assistant", reply)
            logger.info("Processed message from %s", sender)

            # Snapshot contact info if any tool modified it
            updated_info = None
            if any(tc.get("tool") == "save_contact_info" for tc in executed_tools):
                updated_info = dict(contact.info)
                # Deep copy observations list
                updated_info["observations"] = list(updated_info.get("observations", []))

            emit_with_filter_sync("llm.after", {
                "phone": sender, "model": model, "reply": reply,
                "tool_calls": executed_tools, "usage": usage_dict,
                "latency_ms": int((time.monotonic() - _llm_t0) * 1000),
                "ts": time.time(),
            })

            return ProcessResult(reply=reply, tool_calls=executed_tools, contact_info=updated_info)

        except Exception as e:
            logger.error("LLM error for %s: %s", sender, e)
            track_step("error", {"error": str(e), "phase": "llm_call"}, status="error")
            emit_with_filter_sync("llm.after", {
                "phone": sender, "model": model,
                "reply": "", "tool_calls": [], "usage": None,
                "error": str(e),
                "latency_ms": int((time.monotonic() - locals().get("_llm_t0", time.monotonic())) * 1000),
                "ts": time.time(),
            })
            error_msg = str(e)
            if "401" in error_msg or "unauthorized" in error_msg.lower():
                return ProcessResult(reply="[WhatsBot-Lite] API key inválida. Verifique sua chave OpenRouter.")
            if "429" in error_msg or "rate" in error_msg.lower():
                return ProcessResult(reply="[WhatsBot-Lite] Limite de requisições atingido. Tente novamente em instantes.")
            return ProcessResult(reply="[WhatsBot-Lite] Erro ao processar mensagem. Tente novamente.")

    def test_api_key(self, api_key: str) -> tuple[bool, str]:
        """Test if an API key is valid."""
        try:
            client = OpenAI(
                base_url=LLM_API_BASE_URL,
                api_key=api_key,
            )
            client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=5,
            )
            return True, "API key válida!"
        except Exception as e:
            return False, f"Erro: {e}"

    def save_assistant_message(self, phone: str, text: str, *,
                               msg_id: str | None = None,
                               status: str = "sent") -> dict:
        """Save an assistant (bot) message to contact memory after successful send."""
        contact = self._get_contact(phone)
        contact.add_message("assistant", text, msg_id=msg_id, status=status)
        return message_repo.get_last(contact.id) or {"role": "assistant", "content": text, "ts": time.time()}

    def save_operator_message(self, phone: str, text: str, *,
                              status: str | None = None,
                              msg_id: str | None = None,
                              reply_to_msg_id: str | None = None) -> dict:
        """Save a manually sent message (from the operator) without LLM processing."""
        contact = self._get_contact(phone)
        contact.add_message("assistant", text, status=status, msg_id=msg_id,
                            reply_to_msg_id=reply_to_msg_id)
        return message_repo.get_last(contact.id) or {"role": "assistant", "content": text, "ts": time.time()}

    def mark_message_sent(self, phone: str, content: str,
                          msg_id: str | None = None) -> dict | None:
        """Find the most recent failed message with matching content and mark as sent."""
        contact = self._get_contact(phone)
        message_repo.update_status(contact.id, content, "sent", msg_id=msg_id)
        return {"content": content}

    def update_last_user_message_content(self, phone: str, new_content: str,
                                         msg_id: str | None = None) -> None:
        """Update a user message's content (e.g., with a transcription).

        With ``msg_id`` the exact media row is updated; without it (or when no
        row carries that id) it falls back to the newest user message.
        """
        contact = self._get_contact(phone)
        msg = message_repo.get_user_message_by_msg_id(contact.id, msg_id) if msg_id else None
        if not msg:
            msg = message_repo.get_last_user_message(contact.id)
        if msg and msg.get("_id"):
            message_repo.update_content(msg["_id"], new_content)

    def clear_conversation(self, sender: str):
        contact = self._get_contact(sender)
        message_repo.delete_all(contact.id)

    def clear_all_conversations(self):
        for contact in self._contacts.values():
            message_repo.delete_all(contact.id)
        self._contacts.clear()
