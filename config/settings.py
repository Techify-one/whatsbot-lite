import os
import uuid
from pathlib import Path
from typing import Any, Callable

from db.repositories import config_repo


def get_data_dir() -> Path:
    """Return the application data directory (project root)."""
    data_dir = Path(__file__).resolve().parent.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# LLM API base URL — OpenRouter-compatible proxy. Override via the
# LLM_API_BASE_URL env var to point back at OpenRouter or another proxy.
LLM_API_BASE_URL = os.environ.get(
    "LLM_API_BASE_URL", "https://llm.techify.one/api/v1"
).rstrip("/")

# Techify account provisioning — used by the first-run setup wizard. The WhatsBot-Lite
# fetches the current provisioning TARGET (destination number *and* the phrase to
# send) from TECHIFY_SERVICE_NUMBER_URL, sends that WhatsApp message to that
# number, Techify creates an account + API key, and the wizard polls
# TECHIFY_REQUEST_APIKEY_URL (keyed by the connected WhatsApp number) until the
# key is ready. Once the account is created the key stays downloadable for
# ~1 minute.
#
# The endpoint answers ``{"ok": true, "phone": "...", "message": "..."}`` and is
# the SOURCE OF TRUTH for both fields: rotating the number or the phrase is a
# matter of editing that response — no client release, no env anywhere. Both
# fields are resolved INDEPENDENTLY (endpoint -> env -> literal below), so a
# response that does not carry ``message`` yet still dictates ``phone``, and the
# phrase falls back on its own.
#
# ⚠️ The literals below are the LAST safety net, for when the endpoint is down —
# they are NOT the lever for changing either value (editing them means shipping a
# release to every install). "No destination" is still a possible outcome — when
# someone blanks the env or a plugin aborts the seam — and in that case the send
# is REFUSED with an actionable error instead of going to a number nobody chose.
# The full precedence, plugin seams included, lives in
# ``server/routes/setup.fetch_provision_target``.
TECHIFY_SERVICE_NUMBER_URL = os.environ.get(
    "TECHIFY_SERVICE_NUMBER_URL", "https://llm.techify.one/service_number"
).rstrip("/")
TECHIFY_PROVISION_NUMBER = os.environ.get(
    "TECHIFY_PROVISION_NUMBER", "5513981744038"
).strip()
TECHIFY_REQUEST_APIKEY_URL = os.environ.get(
    "TECHIFY_REQUEST_APIKEY_URL", "https://llm.techify.one/request-apikey"
).rstrip("/")
TECHIFY_PROVISION_MESSAGE = os.environ.get(
    "TECHIFY_PROVISION_MESSAGE", "Quero Criar conta e receber minha Chave de API"
).strip()


_ENV_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "OPENROUTER_API_KEY": ("openrouter_api_key", str),
    "WHATSBOT_MODEL": ("model", str),
    "WHATSBOT_IMPROVEMENT_MODEL": ("improvement_model", str),
    "WHATSBOT_AUDIO_MODEL": ("audio_model", str),
    "WHATSBOT_IMAGE_MODEL": ("image_model", str),
    "WHATSBOT_DOCUMENT_MODEL": ("document_model", str),
    "WHATSBOT_SYSTEM_PROMPT": ("system_prompt", str),
    "WHATSBOT_WEB_PORT": ("web_port", int),
    "WHATSBOT_GOWA_PORT": ("gowa_port", int),
    "WHATSBOT_AUTO_REPLY": ("auto_reply", lambda v: v.lower() in ("1", "true", "yes")),
    "WHATSBOT_MAX_CONTEXT": ("max_context_messages", int),
    "WHATSBOT_BATCH_DELAY": ("message_batch_delay", float),
    "WHATSBOT_AI_ENGINE": ("ai_engine_enabled", lambda v: v.lower() in ("1", "true", "yes")),
    "WHATSBOT_GOWA_AUTO_CHECK": ("gowa_auto_check_enabled", lambda v: v.lower() in ("1", "true", "yes")),
    # Hosting sets this to cap an instance; it wins over the value saved in the panel.
    "WHATSBOT_DISK_LIMIT_GB": ("disk_limit_gb", float),
}

# Reverse lookup: config_key -> (env_key, cast). Used by get() to apply env overrides on-demand.
_ENV_OVERRIDES_BY_KEY: dict[str, tuple[str, Callable[[str], Any]]] = {
    cfg_key: (env_key, cast) for env_key, (cfg_key, cast) in _ENV_OVERRIDES.items()
}

DEFAULT_CONFIG = {
    "openrouter_api_key": "",
    # Stable per-installation id sent to the LLM proxy so OpenRouter keeps routing
    # to the same provider (prompt cache hits). Server-only: generated on first use.
    "llm_session_id": "",
    "model": "deepseek/deepseek-v4.1-flash",
    # Model used by the "sugerir melhoria" analysis (non-agentic). Empty string
    # → falls back to the chat ``model``.
    "improvement_model": "",
    "audio_model": "google/gemini-2.5-flash",
    "image_model": "google/gemini-2.5-flash",
    "document_model": "google/gemini-2.5-flash",
    "system_prompt": (
        "Você é um assistente útil e amigável. Responda de forma clara e concisa. "
        "Use português brasileiro."
    ),
    "auto_reply": False,
    "max_context_messages": 10,
    "inactivity_timeout_min": 30,
    "message_batch_delay": 3.0,
    "response_delay_min": 1.0,
    "response_delay_max": 3.0,
    "gowa_port": 64999,
    "web_port": 8080,
    "usd_brl_rate": 5.50,
    "split_messages": True,
    "split_message_delay": 2.0,
    "audio_transcription_mode": "received",
    "audio_transcription_target": "private",
    "audio_transcription_chat_prefix": "",
    "image_transcription_enabled": True,
    "document_transcription_enabled": True,
    "transfer_alert_enabled": True,
    "transfer_alert_duration": 5,
    "group_reply_mode": "mention_only",
    # --- Religar a IA automaticamente ------------------------------------------
    # Liga de novo a IA em conversas em que ela foi desligada (transferência,
    # atendente ou padrão de novos contatos). Regra em server/ai_reactivation.py.
    # ``unit``: minutes | hours | days (tempo parado, contado da última mensagem)
    # ou schedule (dias da semana + hora; 0 = segunda). ``last_sender``: both |
    # customer | attendant. ``timezone`` é o fuso do navegador de quem salvou
    # (vazio = fuso do servidor). ``last_run`` e ``provisioning_phones`` são
    # escritos pelo servidor.
    "ai_reactivation_enabled": False,
    "ai_reactivation_unit": "hours",
    "ai_reactivation_value": 24,
    "ai_reactivation_weekdays": [0, 1, 2, 3, 4],
    "ai_reactivation_time": "08:00",
    "ai_reactivation_last_sender": "both",
    "ai_reactivation_include_groups_archived": False,
    "ai_reactivation_timezone": "",
    "ai_reactivation_last_run": 0.0,
    "provisioning_phones": [],
    # --- Download automático de mídia recebida ---------------------------------
    # O GOWA continua baixando tudo (só tem liga/desliga geral); o app descarta o
    # arquivo que esta matriz não permite. Figurinha segue a linha "image".
    # Padrão enxuto para todos: só áudio fica. Ver server/media_policy.py.
    "media_autodownload": {
        "audio": {"individual": True, "group": True},
        "image": {"individual": False, "group": False},
        "video": {"individual": False, "group": False},
        "document": {"individual": False, "group": False},
    },
    # Limite de espaço para a barra de uso do gerenciador de arquivos. 0 = sem
    # limite (a tela mostra só o total usado). Override por env
    # ``WHATSBOT_DISK_LIMIT_GB`` (hospedagem), que trava o campo na tela.
    "disk_limit_gb": 0.0,
    # --- Motor de agente dirigido pelo banco (config-in-DB + code-in-DB) -----
    # Quando ``ai_engine_enabled`` é True, prompt/modelo/tools do agente são
    # lidos do banco (tabelas ``ai_*``) em vez das constantes do AgentHandler,
    # e tools podem ser criadas/editadas como código Python no próprio banco.
    # Off (default) → caminho legado intacto (paridade total). Override por env
    # ``WHATSBOT_AI_ENGINE``.
    "ai_engine_enabled": False,
    "bot_phone": "",
    "bot_name": "",
    "default_ai_enabled": True,
    "web_password_hash": "",
    "web_password_salt": "",
    "setup_completed": False,
    # Techify account — returned by /request-apikey alongside the API key.
    # account_url is the customer's account/recharge page; access_token is
    # the credential for that account (kept server-side only).
    "account_url": "",
    "access_token": "",
    # Low-balance notification — broadcast a "low_balance" WS event when the
    # remaining OpenRouter credit drops below the threshold (USD). The frontend
    # opens a modal pointing to ``account_url`` for the user to recharge.
    "low_balance_enabled": True,
    "low_balance_threshold": 0.50,
    # --- Atualização do binário GOWA ----------------------------------------
    # ``gowa_auto_check_enabled`` liga APENAS a verificação diária de novas
    # releases; a instalação sempre exige aprovação explícita do usuário.
    # ``gowa_skipped_version`` guarda a versão que o usuário mandou pular (não
    # avisa mais nela, mas volta a avisar na próxima). Versão consultada,
    # horário e avaliação são escritos pelo servidor para sobreviver a restart.
    "gowa_auto_check_enabled": True,
    "gowa_skipped_version": "",
    # Versão do GOWA recomendada pela release do WhatsBot-Lite (arquivo
    # GOWA_VERSION) que a pessoa respondeu "Agora não". Chave própria: não é
    # a ``gowa_skipped_version``, que trata da última release do GitHub.
    "gowa_recommended_skipped_version": "",
    "gowa_latest_version": "",
    "gowa_last_check_at": 0.0,
    # Cached structured verdict for the installed→latest release interval.
    # It contains public release evidence only; no API key or user content.
    "gowa_release_assessment_cache": {},
    # --- Avisos de atualização do WhatsBot-Lite --------------------------------
    # Ignorar vale somente para uma release; o próximo número volta a avisar.
    # O toggle desliga todos os avisos até ser reativado no painel.
    "whatsbot_update_notifications_enabled": True,
    "whatsbot_skipped_version": "",
    # --- Proxy de saída do GOWA ---------------------------------------------
    # Roteia a conexão do WhatsApp (WebSocket do whatsmeow) por um proxy.
    # Requer GOWA >= 8.11.0. ``gowa_proxy_mode`` escolhe entre preencher os
    # campos separados ("fields") ou colar uma URL única ("url"). Detalhes e
    # validação em gowa/proxy.py; escritas passam por /api/gowa/proxy.
    "gowa_proxy_enabled": False,
    "gowa_proxy_mode": "fields",
    "gowa_proxy_scheme": "socks5",
    "gowa_proxy_host": "",
    "gowa_proxy_port": 0,
    "gowa_proxy_username": "",
    "gowa_proxy_password": "",
    "gowa_proxy_url": "",
}


_MISSING = object()


_llm_session_id_cache: str = ""

def get_llm_session_id() -> str:
    """Return the stable per-installation LLM session id, creating it on first use.

    Random on purpose (``wb-<uuid4 hex>``): it is sent to the provider, so it must
    never be derived from a phone number, name or API key.
    """
    global _llm_session_id_cache
    if _llm_session_id_cache:
        return _llm_session_id_cache
    current = config_repo.get("llm_session_id", "")
    if not (isinstance(current, str) and current.startswith("wb-")):
        current = f"wb-{uuid.uuid4().hex}"
        config_repo.set("llm_session_id", current)
    _llm_session_id_cache = current
    return current


INSTALLATION_ID_ENV = "WHATSBOT_INSTALLATION_ID"


def _valid_uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        return ""


def get_installation_id() -> str:
    """Return the unique, persistent id of this installation (config key ``installation_id``).

    Stored in the installation's own database, so it survives restarts and
    updates (self-update in place or a new Docker image on WhatsBot Cloud).
    WhatsBot Cloud passes the id it registered at provisioning time in
    ``WHATSBOT_INSTALLATION_ID``; that value wins and is written to the
    database. Elsewhere a UUID v4 is generated once and reused afterwards.
    """
    stored = _valid_uuid(config_repo.get("installation_id", ""))
    from_env = _valid_uuid(os.environ.get(INSTALLATION_ID_ENV, ""))
    if from_env:
        if stored != from_env:
            config_repo.set("installation_id", from_env)
        return from_env
    if not stored:
        stored = str(uuid.uuid4())
        config_repo.set("installation_id", stored)
    return stored


class Settings:
    def __init__(self):
        self.data_dir = get_data_dir()
        self.logs_dir = self.data_dir / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        self.load()

    def load(self):
        """Seed missing defaults into the DB. No in-memory cache is kept — reads are write-through to config_repo."""
        current = config_repo.get_all()
        # Migrate legacy audio_transcription_enabled → audio_transcription_mode
        if "audio_transcription_enabled" in current:
            legacy_enabled = current.pop("audio_transcription_enabled")
            if "audio_transcription_mode" not in current:
                migrated = "received" if legacy_enabled else "off"
                config_repo.set("audio_transcription_mode", migrated)
                current["audio_transcription_mode"] = migrated
            config_repo.delete_prefix("audio_transcription_enabled")
        # Persist defaults for any key missing in the DB
        missing = {k: v for k, v in DEFAULT_CONFIG.items() if k not in current}
        if missing:
            # An install that already has an API key configured is NOT a
            # first run — seed setup_completed=True so the setup wizard does
            # not ambush existing users after an update.
            if "setup_completed" in missing:
                env_key = os.environ.get("OPENROUTER_API_KEY", "")
                missing["setup_completed"] = bool(
                    current.get("openrouter_api_key") or env_key
                )
            config_repo.set_many(missing)

    @staticmethod
    def _env_override(key: str):
        """Return env-overridden value for ``key`` if present, else ``_MISSING``."""
        mapping = _ENV_OVERRIDES_BY_KEY.get(key)
        if mapping is None:
            return _MISSING
        env_key, cast = mapping
        raw = os.environ.get(env_key)
        if not raw:
            return _MISSING
        try:
            return cast(raw)
        except (ValueError, TypeError):
            return _MISSING

    def save(self):
        """No-op. Kept for backward compatibility — writes are write-through via set()/__setitem__."""
        return

    def get(self, key: str, default=None):
        override = self._env_override(key)
        if override is not _MISSING:
            return override
        value = config_repo.get(key, _MISSING)
        if value is _MISSING:
            return DEFAULT_CONFIG.get(key, default)
        return value

    def set(self, key: str, value):
        config_repo.set(key, value)

    def __getitem__(self, key):
        override = self._env_override(key)
        if override is not _MISSING:
            return override
        value = config_repo.get(key, _MISSING)
        if value is _MISSING:
            if key in DEFAULT_CONFIG:
                return DEFAULT_CONFIG[key]
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        config_repo.set(key, value)
