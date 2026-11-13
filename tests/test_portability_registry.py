"""Importar/Exportar — regras de segredo, allowlist de config, escopos e estratégias.

Módulo PURO (``portability.registry``). O teste mais importante aqui é o de COBERTURA: toda chave do
``DEFAULT_CONFIG`` tem de estar classificada em exatamente uma lista. Sem isso, uma chave nova (um
token, uma senha) nasceria "não classificada" e o export decidiria por omissão.

Roda direto (``python tests/test_portability_registry.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from config.settings import DEFAULT_CONFIG, _ENV_OVERRIDES  # noqa: E402
from portability import registry as reg  # noqa: E402

# ── cobertura da config ─────────────────────────────────────────────────────

def test_every_default_config_key_is_classified():
    unclassified = sorted(k for k in DEFAULT_CONFIG if reg.classify_config_key(k) == "unclassified")
    assert not unclassified, (
        f"Chave(s) nova(s) no DEFAULT_CONFIG sem classificação em portability/registry.py: "
        f"{unclassified}. Decida: EXPORTABLE_CONFIG (preferência), SECRET_CONFIG (segredo, só cifrada) "
        f"ou CONFIG_NEVER (estado/host/rede).")

def test_lists_are_disjoint():
    a, b, c = reg.EXPORTABLE_CONFIG, reg.SECRET_CONFIG, reg.CONFIG_NEVER
    assert not (a & b) and not (a & c) and not (b & c)

def test_allowlist_has_no_ghost_keys():
    """Chave na allowlist que não existe no DEFAULT_CONFIG é typo (ou chave lida mas não semeada).
    ``max_executions`` é a única conhecida: o app a lê com default próprio, fora do DEFAULT_CONFIG."""
    known_outside_defaults = {"max_executions"}
    ghosts = {k for k in (reg.EXPORTABLE_CONFIG | reg.SECRET_CONFIG) if k not in DEFAULT_CONFIG}
    assert ghosts <= known_outside_defaults, sorted(ghosts - known_outside_defaults)

def test_nothing_secret_looking_is_exported_in_clear():
    """Nenhuma chave com nome de segredo entra em claro."""
    leaked = sorted(k for k in reg.EXPORTABLE_CONFIG if reg.is_secret_config_key(k))
    assert not leaked, leaked

def test_the_api_key_travels_only_encrypted():
    k = "openrouter_api_key"
    assert reg.config_key_secret(k) and not reg.config_key_exportable(k) and reg.config_key_importable(k)

@pytest.mark.parametrize("key", [
    "web_password_hash", "web_password_salt",       # token de sessão é determinístico: daria acesso
    "installation_id", "llm_session_id", "access_token", "account_url", "setup_completed",
    "bot_phone", "bot_name", "provisioning_phones",
    "gowa_proxy_password", "gowa_proxy_url", "gowa_proxy_host", "gowa_proxy_enabled",
    "gowa_cpu_unsupported", "gowa_latest_version", "gowa_skipped_version",
    "gowa_port", "web_port", "disk_limit_gb",
    "ai_engine_enabled",                             # um pacote não liga o motor que executa código
    "whatsbot_skipped_version", "ai_reactivation_last_run",
])
def test_never_keys_neither_export_nor_import(key):
    assert not reg.config_key_exportable(key)
    assert not reg.config_key_secret(key)
    assert not reg.config_key_importable(key)        # pacote adulterado com essa chave é ignorado

def test_unknown_and_plugin_keys_are_not_importable():
    for k in ("chave_inventada", "plugin.lembretes.x", "", "installation_id "):
        assert not reg.config_key_importable(k)

def test_password_hash_pair_is_in_never_even_if_name_matches_secret_rule():
    # o nome casa com a regra de segredo, mas a decisão é NUNCA (não cifrar e levar)
    assert reg.is_secret_config_key("web_password_hash")
    assert "web_password_hash" in reg.CONFIG_NEVER and "web_password_hash" not in reg.SECRET_CONFIG

def test_env_masked_keys_exist_in_env_overrides():
    """Mapa 'chave → variável' do aviso env_override_active tem de bater com o que o app realmente lê."""
    actual = {cfg: env for env, (cfg, _cast) in _ENV_OVERRIDES.items()}
    for key, env in reg.ENV_MASKED_KEYS.items():
        assert actual.get(key) == env, (key, env, actual.get(key))

def test_env_masked_keys_are_all_importable():
    """Aviso de máscara de env só faz sentido para o que o import grava."""
    for key in reg.ENV_MASKED_KEYS:
        assert reg.config_key_importable(key), key

# ── detecção de segredo por nome ────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "openrouter_api_key", "access_token", "client_secret", "db_password", "senha_admin",
    "plugin.pagamentos.client_secret", "x.y.api-key", "TELEGRAM_BOT_TOKEN", "webhook_url",
    "database_url", "my_dsn", "cert_pem", "private_key", "Authorization"])
def test_secret_names_detected(name):
    assert reg.is_secret_config_key(name)

@pytest.mark.parametrize("name", ["model", "system_prompt", "auto_reply", "group_reply_mode", ""])
def test_ordinary_names_not_secret(name):
    assert not reg.is_secret_config_key(name)

def test_field_rule_is_narrower_than_name_rule():
    """Campo de plugin usa regra estreita: ``agent_keys``/``keyword_enabled`` não são credencial."""
    for name in ("agent_keys", "group_attr_key", "keyword_enabled", "monkey"):
        assert reg.is_secret_name(name)               # a larga pega (e perderia o valor)
        assert not reg.is_secret_field(name)          # a estreita não
    for name in ("api_key", "client_secret", "access_key", "bot_token", "db_password", "dsn"):
        assert reg.is_secret_field(name)

# ── valor de ambiente ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "http://127.0.0.1:8112", "http://localhost:3000/x", "https://LOCALHOST", "http://0.0.0.0:1",
    "http://[::1]:9", "ws://127.5.5.5/ws", "postgresql://u:p@127.0.0.1/db", "http://api.localhost"])
def test_loopback_values_are_environment(value):
    assert reg.is_environment_value(value)

@pytest.mark.parametrize("value", [
    "https://llm.techify.one/api/v1", "http://10.8.200.13:8090", "https://example.com",
    "texto qualquer", "", None, 42, "x" * 5000, "http://127.0.0.1.evil.com"])
def test_real_hosts_and_non_urls_are_not_environment(value):
    assert not reg.is_environment_value(value)

# ── escopos ─────────────────────────────────────────────────────────────────

def test_scopes_validation():
    assert reg.validate_scopes(["settings", "catalog", "settings"]) == ["settings", "catalog"]
    for bad in ([], None, "settings", ["nope"], ["settings", 3], [None]):
        with pytest.raises(ValueError):
            reg.validate_scopes(bad)

def test_entities_for_keeps_order_without_duplicates():
    assert reg.entities_for(["conversations", "settings"]) == ["contacts", "messages", "config"]
    assert reg.entities_for(["catalog", "catalog"]) == ["tags"]

def test_conversations_scope_always_carries_contacts_and_messages_together():
    """A mensagem aponta para o contato DO PACOTE: escopo sem um dos dois quebraria a referência."""
    assert set(reg.SCOPES["conversations"]) == {"contacts", "messages"}

# ── estratégias ─────────────────────────────────────────────────────────────

def test_default_strategy_per_entity():
    assert reg.default_strategy("config") == reg.MERGE
    assert reg.default_strategy("contacts") == reg.MERGE
    assert reg.default_strategy("tags") == reg.OVERWRITE
    assert reg.default_strategy("messages") == reg.SKIP          # histórico: só acrescenta
    assert reg.default_strategy("desconhecida") == reg.SKIP

def test_validate_strategy_defaults_and_overrides():
    out = reg.validate_strategy({"tags": "skip"}, ["config", "tags", "messages"])
    assert out == {"config": "merge", "tags": "skip", "messages": "skip"}
    assert reg.validate_strategy(None, ["tags"]) == {"tags": "overwrite"}

@pytest.mark.parametrize("raw, ents", [
    ({"messages": "overwrite"}, ["messages"]),       # histórico nunca é reescrito
    ({"messages": "merge"}, ["messages"]),
    ({"tags": "merge"}, ["tags"]),                   # tag não tem granularidade de campo
    ({"tags": "apagar"}, ["tags"]),                  # valor inválido
    ({"config": "skip"}, ["tags"]),                  # entidade fora do pacote
    ("overwrite", ["tags"]),                         # formato errado
    ([], ["tags"]) if False else ({"x": 1}, ["tags"]),
])
def test_validate_strategy_rejects(raw, ents):
    with pytest.raises(ValueError):
        reg.validate_strategy(raw, ents)

def test_every_supported_strategy_is_a_known_one():
    for ent, sup in reg.SUPPORTED_STRATEGIES.items():
        assert set(sup) <= set(reg.STRATEGIES), ent
        assert reg.default_strategy(ent) in sup, ent

def test_every_scope_entity_has_a_strategy_entry():
    for sc, ents in reg.SCOPES.items():
        for e in ents:
            assert e in reg.SUPPORTED_STRATEGIES, (sc, e)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
