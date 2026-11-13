"""Entidades de configuração e catálogo: ``config`` e ``tags``.

Regras comuns a toda entidade:

* ``apply(row, strategy, env, dry_run)`` é o MESMO código no dry-run e no import real — só a gravação
  muda — então as contagens da prévia batem com o resultado. O que passaria a existir depois do
  import é marcado em ``env.mark`` para o dry-run resolver referência a algo que o pacote cria.
* O pacote é dado NÃO CONFIÁVEL: entrada validada com o mesmo rigor das rotas do painel (reaproveita
  ``ai_reactivation.clean`` e ``media_policy.normalize``). Lixo é descartado com aviso, nunca gravado.
* Escrita pelos repositórios. Nada dispara evento (``tag.created``, ``config.changed``…): plugin
  nenhum reage e a IA não responde a um import.
* Aviso é CÓDIGO (a tela traduz); nunca carrega valor de config nem segredo.
"""

from __future__ import annotations

from typing import Any, Iterator

from portability import registry as reg
from portability.base import CREATE, ERROR, OUTCOMES, SKIP, UPDATE, Entity, Env, valid_color

_MISSING = object()
_MAX_TAG = 30                      # o mesmo limite de POST /api/tags

class _Invalid:
    def __repr__(self) -> str:  # pragma: no cover
        return "INVALID"

INVALID = _Invalid()

# ═══ config ═════════════════════════════════════════════════════════════════

def _clean_config_value(key: str, value: Any) -> Any:
    """Valida UM valor contra o tipo do default da chave. Devolve o valor a gravar ou ``INVALID``."""
    from config.settings import DEFAULT_CONFIG
    if key.startswith("ai_reactivation_"):
        from server import ai_reactivation
        out = ai_reactivation.clean(key, value)
        return INVALID if out is ai_reactivation.INVALID else out
    if key == "media_autodownload":
        if not isinstance(value, dict):
            return INVALID
        from server import media_policy
        return media_policy.normalize(value)
    default = DEFAULT_CONFIG.get(key)
    if default is None:      # chave lida pelo app mas fora do DEFAULT_CONFIG (``max_executions``)
        if isinstance(value, bool):
            return INVALID
        if isinstance(value, (int, float)):
            return value
        return value if isinstance(value, str) and len(value) <= 1000 else INVALID
    if isinstance(default, bool):
        return value if isinstance(value, bool) else INVALID
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return INVALID
        return float(value) if isinstance(default, float) else int(value)
    if isinstance(default, str):
        return value if isinstance(value, str) and len(value) <= 100_000 else INVALID
    if isinstance(default, list):
        return value if isinstance(value, list) and len(value) <= 5000 else INVALID
    if isinstance(default, dict):
        return value if isinstance(value, dict) else INVALID
    return INVALID

def _export_config(env: Env) -> Iterator[dict]:
    """Só o que está na allowlist. A chave de segredo sai com ``value: null`` e, quando o operador
    pediu segredos, o valor vai para ``env.secrets`` (que o handler cifra em ``secrets.enc``)."""
    from db.repositories import config_repo
    current = config_repo.get_all()          # cru: ignora a variável de ambiente, que mascara o banco
    for key in sorted(current):
        if reg.config_key_secret(key):
            val = current[key]
            if env.include_secrets and isinstance(val, str) and val:
                env.secrets.setdefault("config", {})[key] = val
            yield {"key": key, "value": None, "secret": True}
        elif reg.config_key_exportable(key):
            yield {"key": key, "value": current[key], "secret": False}

def _apply_config(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from config.settings import DEFAULT_CONFIG
    from db.repositories import config_repo
    key = row.get("key")
    # Uma única porta: o que não está na allowlist do export não entra, mesmo que o pacote traga
    # (``web_password_hash``, ``installation_id``, ``ai_engine_enabled``… de um pacote adulterado).
    if not isinstance(key, str) or not reg.config_key_importable(key):
        env.warn("config", key if isinstance(key, str) else "?", "key_not_importable")
        return SKIP
    current = config_repo.get(key, _MISSING)
    is_secret = reg.config_key_secret(key)
    if is_secret:
        value = (env.secrets.get("config") or {}).get(key)
        if not isinstance(value, str) or not value:
            env.warn("config", key, "secret_missing")      # pacote sem senha / sem a chave
            return SKIP
        if current not in (_MISSING, None, "") and not env.overwrite_secrets:
            env.warn("config", key, "secret_kept")
            return SKIP
        new: Any = value
    else:
        new = _clean_config_value(key, row.get("value"))
        if new is INVALID:
            env.warn("config", key, "invalid_value")
            return ERROR
    if current is not _MISSING and current == new:
        return SKIP
    if strategy == reg.SKIP and current is not _MISSING:
        return SKIP
    # "Sobrescrever segredos" é ordem explícita do operador: vale também sob ``merge`` (o padrão), que
    # senão pularia todo segredo já preenchido e a opção deixaria de ter efeito.
    explicit_secret = env.overwrite_secrets and is_secret
    if (strategy == reg.MERGE and current is not _MISSING and current != DEFAULT_CONFIG.get(key)
            and not explicit_secret):
        return SKIP          # o operador já personalizou: mesclar não pisa
    if not dry_run:
        config_repo.set(key, new)
        env.config_changed.append(key)
    _warn_if_env_masks(env, key)
    return UPDATE

def _warn_if_env_masks(env: Env, key: str) -> None:
    """``Settings.get`` dá prioridade à variável de ambiente: o valor importado fica no banco, mas a
    instalação segue usando a env. Sem o aviso o operador acharia que o import não funcionou."""
    import os
    var = reg.ENV_MASKED_KEYS.get(key)
    if var and os.environ.get(var):
        env.warn("config", key, "env_override_active")

# ═══ tags ═══════════════════════════════════════════════════════════════════

def _export_tags(env: Env) -> Iterator[dict]:
    from db.repositories import tag_repo
    for name, data in tag_repo.get_all().items():
        yield {"name": name, "color": data.get("color") or ""}

def _apply_tag(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import tag_repo
    name, color = row.get("name"), row.get("color")
    if (not isinstance(name, str) or not name or name != name.strip()
            or len(name) > _MAX_TAG or not valid_color(color)):
        env.warn("tags", name if isinstance(name, str) else "?", "invalid_row")
        return ERROR
    color = color.strip()
    existing = tag_repo.get_by_name(name)
    if existing is None:
        if not dry_run and not tag_repo.create(name, color):
            return SKIP          # corrida: outro processo criou entre o get e o create
        env.mark("tags", name)
        if not dry_run:
            env.cache["tags_changed"] = True
        return CREATE
    if strategy != reg.OVERWRITE or (existing.get("color") or "") == color:
        return SKIP
    if not dry_run:
        tag_repo.update(name, color=color)
        env.cache["tags_changed"] = True
    return UPDATE

# ═══ registro ═══════════════════════════════════════════════════════════════

ENTITIES: dict[str, Entity] = {e.name: e for e in (
    Entity("config", _export_config, _apply_config),
    Entity("tags", _export_tags, _apply_tag),
)}

def entity(name: str) -> Entity:
    return ENTITIES[name]

__all__ = ["ENTITIES", "entity", "INVALID", "OUTCOMES", "CREATE", "UPDATE", "SKIP", "ERROR",
           "Entity", "Env"]
