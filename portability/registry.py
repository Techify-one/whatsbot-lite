"""O que é exportável e como: classificação de segredos, allowlist de config, escopos.

Módulo PURO (sem banco). É a fonte única das regras que decidem o que sai no NDJSON e o que só
pode sair cifrado em ``secrets.enc``.

⚠️ Aqui a regra é NEGAR POR PADRÃO. A config do lite mistura preferência do usuário com estado da
instalação e com segredo (e o ``allowed_keys`` do ``PUT /api/config`` mistura os três), então a lista
de exportáveis é uma ALLOWLIST explícita (``EXPORTABLE_CONFIG``): chave nova no ``DEFAULT_CONFIG`` NÃO
viaja até alguém decidir, de propósito, que ela é preferência. Um teste (``test_portability_registry``)
falha se uma chave do ``DEFAULT_CONFIG`` não estiver classificada em exatamente uma das listas.

⚠️ O nome-exato não basta para achar segredo (``audit_repo`` do pro vazou um DSN por isso): a regra de
segredo é por SUBSTRING e erra para o lado de TRATAR COMO SEGREDO.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

# ── segredos ────────────────────────────────────────────────────────────────

#: Substring (case-insensitive) no NOME da chave ⇒ o valor é segredo.
SECRET_NAME_RE = re.compile(
    r"(token|secret|password|passwd|senha|api[_-]?key|apikey|private[_-]?key|dsn|pem|"
    r"credential|authorization|webhook[_-]?url|connection[_-]?string|database[_-]?url|key)",
    re.IGNORECASE)

#: Regra ESTREITA, para CAMPO de plugin (Fase 3): nome de credencial de verdade. A larga acima pega
#: ``agent_keys`` e até ``keyword_enabled`` — identificadores e opções que perderiam o valor.
SECRET_FIELD_RE = re.compile(
    r"(token|secret|password|passwd|senha|api[_-]?key|apikey|(private|access|auth|signing|"
    r"encryption|secret)[_-]?key|dsn|pem|credential|authorization|webhook[_-]?url|"
    r"connection[_-]?string|database[_-]?url)", re.IGNORECASE)

#: ``esquema://host`` no começo de um valor (http, https, ws, postgres…).
_URL_HOST_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://(?:[^/@\s]*@)?(\[[^\]]+\]|[^/:?#\s]+)", re.IGNORECASE)

def is_environment_value(value: Any) -> bool:
    """O valor aponta para a PRÓPRIA máquina (loopback)? Então só vale no ambiente de origem.

    Regra por VALOR (não por nome): ``http://127.0.0.1:8112`` num pacote vira, no destino, um serviço
    que não existe. Só pega loopback, nunca um host de verdade (``llm.techify.one``, ``10.8.200.13``)."""
    if not isinstance(value, str) or len(value) > 2048:
        return False
    m = _URL_HOST_RE.match(value.strip())
    if not m:
        return False
    host = m.group(1).strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    # Lido como IP, não por prefixo de texto: ``startswith("127.")`` tomaria ``127.0.0.1.evil.com``
    # (um host de verdade) por loopback e o valor sumiria do pacote em silêncio.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified

def is_secret_name(name: str) -> bool:
    """Verdadeiro se o NOME da chave indica segredo (substring, conservador)."""
    return bool(SECRET_NAME_RE.search(str(name or "")))

def is_secret_field(name: str) -> bool:
    """Verdadeiro se o NOME do campo de plugin indica credencial (regra estreita)."""
    return bool(SECRET_FIELD_RE.search(str(name or "")))

def is_secret_config_key(key: str) -> bool:
    """Chave de ``config`` cujo valor é segredo. Olha o último segmento E a chave inteira —
    ``plugin.pagamentos.client_secret`` e ``openrouter_api_key`` casam."""
    key = str(key or "")
    return is_secret_name(key) or is_secret_name(key.rsplit(".", 1)[-1])

# ── config: allowlist ───────────────────────────────────────────────────────

#: Preferências do usuário que viajam em claro no NDJSON.
EXPORTABLE_CONFIG = frozenset({
    # modelos e prompt
    "system_prompt", "model", "improvement_model", "audio_model", "image_model", "document_model",
    # comportamento da IA
    "auto_reply", "max_context_messages", "inactivity_timeout_min", "message_batch_delay",
    "response_delay_min", "response_delay_max", "split_messages", "split_message_delay",
    "group_reply_mode", "default_ai_enabled", "transfer_alert_enabled", "transfer_alert_duration",
    # transcrição
    "audio_transcription_mode", "audio_transcription_target", "audio_transcription_chat_prefix",
    "image_transcription_enabled", "document_transcription_enabled",
    # religar a IA (regra; ``last_run`` é do servidor)
    "ai_reactivation_enabled", "ai_reactivation_unit", "ai_reactivation_value",
    "ai_reactivation_weekdays", "ai_reactivation_time", "ai_reactivation_last_sender",
    "ai_reactivation_include_groups_archived", "ai_reactivation_timezone",
    # mídia e saldo
    "media_autodownload", "low_balance_enabled", "low_balance_threshold", "usd_brl_rate",
    "max_executions",
})

#: Chaves cujo valor é segredo: só viajam CIFRADAS em ``secrets.enc`` (e só com a senha do pacote).
SECRET_CONFIG = frozenset({"openrouter_api_key"})

#: Chaves que NUNCA entram no pacote nem são importadas — e o porquê de cada uma.
CONFIG_NEVER = frozenset({
    # Segredo que, por ser determinístico, dá sessão válida a quem o tiver: o token de sessão do painel
    # é ``sha256(hash + salt + "session")``. Levar o par entregaria o acesso ao painel de origem.
    "web_password_hash", "web_password_salt",
    # Identidade da instalação e vínculo com a conta Techify desta instalação.
    "installation_id", "llm_session_id", "access_token", "account_url", "setup_completed",
    # Identidade do WhatsApp conectado (reescrita pelo servidor a cada poll).
    "bot_phone", "bot_name", "provisioning_phones",
    # Estado do atualizador do GOWA e do hardware local; e a REDE da origem (proxy com senha).
    "gowa_skipped_version", "gowa_recommended_skipped_version", "gowa_latest_version",
    "gowa_last_check_at", "gowa_release_assessment_cache", "gowa_cpu_unsupported",
    "gowa_auto_check_enabled", "gowa_proxy_enabled", "gowa_proxy_mode", "gowa_proxy_scheme",
    "gowa_proxy_host", "gowa_proxy_port", "gowa_proxy_username", "gowa_proxy_password",
    "gowa_proxy_url",
    # Específico do host: portas e limite de disco (a hospedagem pode travá-lo por env).
    "gowa_port", "web_port", "disk_limit_gb",
    # Preferência de aviso de versão desta instalação e marcador do servidor.
    "whatsbot_skipped_version", "whatsbot_update_notifications_enabled", "ai_reactivation_last_run",
    # Um pacote (que pode ter sido adulterado) nunca liga o motor de IA sozinho: ele leva código.
    "ai_engine_enabled",
})

#: Prefixo da config de plugin (``plugin.<id>.<campo>``): só viaja na Fase 3, junto do plugin.
PLUGIN_CONFIG_PREFIX = "plugin."

def config_key_exportable(key: str) -> bool:
    """A chave entra no pacote EM CLARO? Só a que está na allowlist (e nunca a de segredo/estado)."""
    return key in EXPORTABLE_CONFIG and key not in CONFIG_NEVER and key not in SECRET_CONFIG

def config_key_secret(key: str) -> bool:
    """A chave viaja, mas SÓ cifrada?"""
    return key in SECRET_CONFIG and key not in CONFIG_NEVER

def config_key_importable(key: str) -> bool:
    """O import aceita gravar esta chave? É a mesma lista do export (em claro ou cifrada): um pacote
    adulterado que traga ``web_password_hash`` ou ``installation_id`` é ignorado, não gravado."""
    return config_key_exportable(key) or config_key_secret(key)

def classify_config_key(key: str) -> str:
    """``exportable`` | ``secret`` | ``never`` | ``unclassified`` — usada pelo teste de cobertura."""
    if key in CONFIG_NEVER:
        return "never"
    if key in SECRET_CONFIG:
        return "secret"
    if key in EXPORTABLE_CONFIG:
        return "exportable"
    return "unclassified"

#: Variável de ambiente que MASCARA o valor salvo no banco (``Settings.get`` dá prioridade a ela).
#: Importar essa chave grava no banco, mas a instalação continua usando a env: o import avisa.
ENV_MASKED_KEYS = {
    "openrouter_api_key": "OPENROUTER_API_KEY", "model": "WHATSBOT_MODEL",
    "improvement_model": "WHATSBOT_IMPROVEMENT_MODEL", "audio_model": "WHATSBOT_AUDIO_MODEL",
    "image_model": "WHATSBOT_IMAGE_MODEL", "document_model": "WHATSBOT_DOCUMENT_MODEL",
    "system_prompt": "WHATSBOT_SYSTEM_PROMPT", "auto_reply": "WHATSBOT_AUTO_REPLY",
    "max_context_messages": "WHATSBOT_MAX_CONTEXT", "message_batch_delay": "WHATSBOT_BATCH_DELAY",
}

# ── escopos ─────────────────────────────────────────────────────────────────

#: escopo -> entidades que ele leva (na ordem de import). Fase 1.
SCOPES: dict[str, tuple[str, ...]] = {
    "settings": ("config",),
    "catalog": ("tags",),
    # Contatos + observações + mensagens SEMPRE juntos: a mensagem aponta para o contato do pacote.
    "conversations": ("contacts", "messages"),
    # Motor de IA. ``ai_tools`` leva CÓDIGO PYTHON: entra sempre desligado e só com confirmação própria.
    "agents": ("ai_variables", "ai_prompts", "ai_tools", "ai_agents", "tool_overrides"),
}

#: Entidades que carregam segredo (para o relatório/UI saberem que a senha do pacote importa).
SECRET_BEARING_ENTITIES = frozenset({"config", "ai_variables"})

#: Entidades que carregam CÓDIGO executável: exigem a confirmação ``accept_code`` do operador.
CODE_BEARING_ENTITIES = frozenset({"ai_tools"})

def entities_for(scopes: list[str]) -> list[str]:
    out: list[str] = []
    for sc in scopes:
        for ent in SCOPES.get(sc, ()):
            if ent not in out:
                out.append(ent)
    return out

def validate_scopes(scopes: Any) -> list[str]:
    """Lista de escopos conhecidos, sem repetição, na ordem pedida. ``ValueError`` em escopo
    desconhecido (mensagem segura para ir ao usuário)."""
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("Escolha ao menos um escopo para exportar.")
    seen: list[str] = []
    for sc in scopes:
        if not isinstance(sc, str) or sc not in SCOPES:
            raise ValueError("Escopo desconhecido.")
        if sc not in seen:
            seen.append(sc)
    return seen

# ── estratégia de conflito ──────────────────────────────────────────────────

SKIP, MERGE, OVERWRITE = "skip", "merge", "overwrite"
STRATEGIES = (SKIP, MERGE, OVERWRITE)

#: Estratégias que FAZEM SENTIDO por entidade — o resto é recusado em vez de ser tratado em silêncio
#: como outra coisa (um "mesclar" que na prática pula engana). ``merge`` só existe onde há
#: granularidade de campo/chave. Mensagem é HISTÓRICO: nunca se reescreve, só se acrescenta.
SUPPORTED_STRATEGIES: dict[str, tuple[str, ...]] = {
    "config": (SKIP, MERGE, OVERWRITE),
    "tags": (SKIP, OVERWRITE),
    "contacts": (SKIP, MERGE, OVERWRITE),
    "messages": (SKIP,),
    "ai_variables": (SKIP, MERGE, OVERWRITE),
    "ai_prompts": (SKIP, MERGE, OVERWRITE),
    "ai_agents": (SKIP, MERGE, OVERWRITE),
    # Código nunca é "mesclado": ou se acrescenta a que falta, ou se troca a existente (e só com OVERWRITE).
    "ai_tools": (SKIP, OVERWRITE),
    "tool_overrides": (SKIP, MERGE, OVERWRITE),
}

def default_strategy(entity: str) -> str:
    """Padrão por entidade: ``merge`` se suportado, senão ``overwrite``, senão ``skip``.

    ⚠️ Entidade com CÓDIGO (``ai_tools``) é sempre ``skip``: trocar o código que já existe aqui é uma escolha
    explícita do operador (``overwrite``), nunca o padrão."""
    if entity in CODE_BEARING_ENTITIES:
        return SKIP
    sup = SUPPORTED_STRATEGIES.get(entity, (SKIP,))
    if MERGE in sup:
        return MERGE
    if OVERWRITE in sup:
        return OVERWRITE
    return SKIP

def validate_strategy(raw: Any, entities: list[str]) -> dict[str, str]:
    """``{entidade: estratégia}`` completo (padrão: ``default_strategy``). ``ValueError`` se inválido."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("A estratégia de conflito deve ser um objeto.")
    out = {e: default_strategy(e) for e in entities}
    for ent, strat in raw.items():
        if ent not in out:
            raise ValueError("Estratégia para uma entidade que não está no pacote.")
        if strat not in STRATEGIES:
            raise ValueError("Estratégia inválida (use skip, merge ou overwrite).")
        if strat not in SUPPORTED_STRATEGIES.get(ent, (SKIP,)):
            raise ValueError(f"A estratégia '{strat}' não se aplica a '{ent}'.")
        out[ent] = strat
    return out
