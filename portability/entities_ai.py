"""Motor de IA: ``ai_variables``, ``ai_prompts``, ``ai_agents``, ``ai_tools`` e ``tool_overrides``.

Duas coisas diferem do resto do pacote:

* **``ai_tools.code`` é Python que ROTA NO BOOT sem nenhum interruptor** (``agent/ai_tool_installer.py``
  importa toda ferramenta com ``enabled=1``), e ``dependencies`` vira ``pip install`` no mesmo passo. Um
  pacote é dado não confiável, então a ferramenta que chega **entra SEMPRE desligada** (``enabled=0``,
  ``install_status='pending'``), qualquer que seja o valor no pacote, e o import só acontece com uma
  confirmação própria (``accept_code``) que lista cada ferramenta. Quem quiser usá-la a liga depois, na
  tela de ferramentas, depois de ler o código. A especificação de dependência passa por uma regex fechada:
  ``--index-url=http://…`` ou ``pkg @ http://…`` iriam direto para o argv do ``pip``.
* **Variáveis cujo NOME indica segredo** (``registry.is_secret_name``) viajam só em ``secrets.enc``, como a
  chave do proxy LLM. A decisão é pelo NOME no destino, nunca pela marca ``secret`` que o pacote traz.

``ai_credentials`` não existe no Lite, e ``ai_engine_enabled`` nunca viaja (``CONFIG_NEVER``): importar um
agente não liga o motor.

Escrita pelos repositórios (``agent_repo`` etc.), então cada alteração sobe a ``version`` e gera histórico.
Nada emite evento. Os agentes, prompts e variáveis valem na PRÓXIMA mensagem (a fábrica lê o banco a cada
pedido), sem reinício.

``merge`` aqui significa "não pisar no que o operador já personalizou": prompt e agente com ``version > 1``
foram editados neste servidor e ficam como estão; os de ``version == 1`` ainda são a semente do boot, então
o do pacote os substitui (sem isso importar numa instalação nova não traria o prompt).
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from portability import registry as reg
from portability.base import (CREATE, ERROR, MAX_PROMPT_BYTES, SKIP, UPDATE, Env, _is_text)

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")          # agent_key, prompt_key, tool_names[i]
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")               # o mesmo que o ``{placeholder}`` aceita
MAX_CODE_BYTES = 512 * 1024
MAX_DEPS = 30
MAX_DESC = 4000
MAX_LABEL = 200
MAX_TOOL_NAMES = 200
MAX_REPORTED_TOOLS = 100

_REL = r"(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_\-]+"
#: ``nome``, ``nome[extra]``, ``nome>=1,<2``. NUNCA começa com ``-`` (opção do pip), nem tem ``@``,
#: ``://``, espaço no meio do nome, ``;`` (marcador) ou caminho: só o que a PyPI entende por nome+versão.
_PIP_SPEC_RE = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9._\-]{{0,99}}(?:\[[A-Za-z0-9,._\-]{{1,100}}\])?"
    rf"(?:\s*{_REL}(?:\s*,\s*{_REL})*)?$")

class _Bad(ValueError):
    """Linha com campo inválido (a mensagem é só para quem lê o código)."""

def is_pip_spec(spec: Any) -> bool:
    return isinstance(spec, str) and len(spec) <= 200 and bool(_PIP_SPEC_RE.match(spec.strip()))

def _tool_name_ok(name: Any) -> bool:
    from agent.ai_tool_installer import NAME_RE         # a MESMA regra que o instalador exige
    return isinstance(name, str) and bool(NAME_RE.match(name))

def _text(v: Any, limit: int, *, allow_none: bool = False) -> str | None:
    if v is None and allow_none:
        return None
    if not _is_text(v, limit):
        raise _Bad("texto inválido")
    return v

def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise _Bad("booleano inválido")

def _num(v: Any, lo: float, hi: float) -> float | int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
        raise _Bad("número fora da faixa")
    return v

def _clean_model_config(v: Any) -> dict:
    """Só os 4 campos que o motor lê (``agno_engine.build_model``); o resto é descartado."""
    if not isinstance(v, dict):
        raise _Bad("model_config inválido")
    out: dict = {}
    model = v.get("model")
    if model not in (None, ""):
        out["model"] = _text(model, 200)
    if v.get("temperature") is not None:
        out["temperature"] = _num(v["temperature"], 0, 2)
    if v.get("top_p") is not None:
        out["top_p"] = _num(v["top_p"], 0, 1)
    if v.get("max_tokens") is not None:
        mt = _num(v["max_tokens"], 1, 1_000_000)
        if not isinstance(mt, int):
            raise _Bad("max_tokens deve ser inteiro")
        out["max_tokens"] = mt
    return out

def _clean_tool_names(v: Any) -> list[str] | None:
    """``None`` = todas as ferramentas. Qualquer outra coisa que não seja lista de nomes é inválida (o
    ``"all"`` que a tabela cita em comentário viraria ``set("all")`` no motor)."""
    if v is None:
        return None
    if not isinstance(v, list) or len(v) > MAX_TOOL_NAMES or not all(
            isinstance(i, str) and _KEY_RE.match(i) for i in v):
        raise _Bad("tool_names inválido")
    return list(dict.fromkeys(v))

def _invalid(env: Env, entity: str, key: Any) -> str:
    env.warn(entity, key if isinstance(key, str) else "?", "invalid_row")
    return ERROR

# ═══ variáveis ══════════════════════════════════════════════════════════════

def export_variables(env: Env) -> Iterator[dict]:
    from db.repositories import variable_repo
    for r in variable_repo.list_all():
        name, value = r["name"], r["value"] or ""
        if reg.is_secret_name(name):
            if env.include_secrets and value:
                env.secrets.setdefault("ai_variables", {})[name] = value
            yield {"name": name, "value": None, "category": r.get("category") or "", "secret": True}
        else:
            yield {"name": name, "value": value, "category": r.get("category") or "", "secret": False}

def apply_variable(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import variable_repo
    name = row.get("name")
    try:
        if not isinstance(name, str) or not _VAR_RE.match(name):
            raise _Bad("nome inválido")
        category = _text(row.get("category", ""), 100) or ""
        secret = reg.is_secret_name(name)
        if secret:
            value = (env.secrets.get("ai_variables") or {}).get(name)
            if not isinstance(value, str) or not value:
                env.warn("ai_variables", name, "secret_missing")
                return SKIP
        else:
            value = _text(row.get("value"), 256 * 1024)
    except _Bad:
        return _invalid(env, "ai_variables", name)
    existing = variable_repo.get(name)
    if existing is None:
        if not dry_run:
            variable_repo.save(name, value, category)
        env.mark("ai_variables", name)
        return CREATE
    if secret and existing["value"] and not env.overwrite_secrets:
        env.warn("ai_variables", name, "secret_kept")
        return SKIP
    if existing["value"] == value and (existing.get("category") or "") == category:
        return SKIP
    explicit = secret and env.overwrite_secrets
    if strategy == reg.SKIP and not explicit:
        return SKIP
    if strategy == reg.MERGE and existing["value"] and not explicit:
        return SKIP
    if not dry_run:
        variable_repo.save(name, value, category)
    return UPDATE

# ═══ prompts ════════════════════════════════════════════════════════════════

def export_prompts(env: Env) -> Iterator[dict]:
    from db.repositories import prompt_repo
    for r in prompt_repo.list_all():
        yield {"prompt_key": r["prompt_key"], "body": r["body"] or ""}

def _customized_here(existing: dict, *, has_content: bool) -> bool:
    """Já foi editado NESTE servidor (``version > 1``)? A semente do boot fica em ``version == 1``."""
    return int(existing.get("version") or 1) > 1 and has_content

def apply_prompt(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import prompt_repo
    key = row.get("prompt_key")
    try:
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise _Bad("chave inválida")
        body = _text(row.get("body", ""), MAX_PROMPT_BYTES)
    except _Bad:
        return _invalid(env, "ai_prompts", key)
    existing = prompt_repo.get(key)
    if existing is None:
        if not dry_run:
            prompt_repo.save(key, body)
        env.mark("ai_prompts", key)
        return CREATE
    if (existing["body"] or "") == body:
        return SKIP
    if strategy == reg.SKIP:
        return SKIP
    if strategy == reg.MERGE and _customized_here(existing, has_content=bool((existing["body"] or "").strip())):
        return SKIP
    if not dry_run:
        prompt_repo.save(key, body)
    return UPDATE

# ═══ agentes ════════════════════════════════════════════════════════════════

def export_agents(env: Env) -> Iterator[dict]:
    from db.repositories import agent_repo
    for r in agent_repo.list_all():
        tn = r.get("tool_names")
        yield {"agent_key": r["agent_key"], "display_name": r.get("display_name") or "",
               "prompt_key": r.get("prompt_key") or "", "model_config": r.get("model_config") or {},
               "tool_names": tn if isinstance(tn, list) else None, "enabled": bool(r.get("enabled"))}

def _known_tools(env: Env) -> set[str]:
    """Ferramentas que EXISTEM aqui: toda ferramenta registrada tem linha em ``tool_overrides`` (criada no
    boot), mais as de ``ai_tools`` e as que o próprio pacote cria."""
    base = env.cache.get("known_tools")
    if base is None:
        from db.repositories import tool_override_repo, tool_repo
        base = {r["name"] for r in tool_override_repo.list_all()} | {r["name"] for r in tool_repo.list_all()}
        env.cache["known_tools"] = base
    return base | env.virtual.get("ai_tools", set())

def apply_agent(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    from db.repositories import agent_repo, prompt_repo
    key = row.get("agent_key")
    try:
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise _Bad("chave inválida")
        prompt_key = row.get("prompt_key") or ""
        if prompt_key and (not isinstance(prompt_key, str) or not _KEY_RE.match(prompt_key)):
            raise _Bad("prompt_key inválido")
        new = {
            "display_name": _text(row.get("display_name", ""), 200) or "",
            "prompt_key": prompt_key,
            "model_config": _clean_model_config(row.get("model_config", {})),
            "tool_names": _clean_tool_names(row.get("tool_names")),
            "enabled": _bool(row.get("enabled", True)),
        }
    except _Bad:
        return _invalid(env, "ai_agents", key)
    # Referências soltas não impedem o import (o motor tem fallback), mas o operador precisa saber.
    if prompt_key and prompt_repo.get(prompt_key) is None and not env.known("ai_prompts", prompt_key):
        env.warn("ai_agents", key, "agent_prompt_missing")
    if new["tool_names"]:
        known = _known_tools(env)
        if any(t not in known for t in new["tool_names"]):
            env.warn("ai_agents", key, "agent_tool_missing")
    existing = agent_repo.get(key)
    if existing is None:
        if not dry_run:
            agent_repo.save(key, **new)
        env.mark("ai_agents", key)
        return CREATE
    if all(existing.get(f) == v for f, v in new.items()):
        return SKIP
    if strategy == reg.SKIP:
        return SKIP
    if strategy == reg.MERGE and _customized_here(existing, has_content=True):
        return SKIP
    if not dry_run:
        agent_repo.save(key, **new)
    return UPDATE

# ═══ ferramentas com código ═════════════════════════════════════════════════

def export_tools(env: Env) -> Iterator[dict]:
    """O CÓDIGO sai como está no banco. Só descrição, código e dependências: nunca o estado da instalação
    (``install_status``/``install_error``/``installed_deps``), que é desta máquina."""
    from db.repositories import tool_repo
    for r in tool_repo.list_all():
        yield {"name": r["name"], "description": r.get("description") or "", "code": r.get("code") or "",
               "dependencies": r.get("dependencies") or [], "enabled": bool(r.get("enabled"))}

def _clean_deps(v: Any) -> list[str]:
    if not isinstance(v, list) or len(v) > MAX_DEPS or not all(is_pip_spec(i) for i in v):
        raise _Bad("dependência inválida")
    return [i.strip() for i in v]

def apply_tool(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    """⚠️ A ferramenta SEMPRE entra desligada, e o ``enabled`` do pacote é ignorado.

    Toda ferramenta que o pacote TRAZ é registrada em ``env.cache["code_tools"]`` ANTES de decidir o que
    fazer com ela, qualquer que seja a estratégia: a confirmação do import é exigida pelo que o pacote
    carrega, não pelo que a estratégia escolhida na prévia faria (trocar para "substituir" sem recalcular
    não pode esconder código)."""
    from db.repositories import tool_repo
    name = row.get("name")
    try:
        if not _tool_name_ok(name):
            raise _Bad("nome inválido")
        description = _text(row.get("description", ""), MAX_DESC) or ""
        code = _text(row.get("code", ""), MAX_CODE_BYTES) or ""
        deps = _clean_deps(row.get("dependencies", []))
    except _Bad:
        return _invalid(env, "ai_tools", name)
    existing = tool_repo.get(name)
    if existing is None:
        state = "new"
    elif (existing["description"], existing["code"], existing["dependencies"]) == (description, code, deps):
        state = "same"
    else:
        state = "differs"
    env.cache.setdefault("code_tools", {})[name] = {"state": state, "dependencies": bool(deps)}
    if state == "same":
        return SKIP                                   # idêntica: não mexe nem desliga a que já roda
    if state == "differs" and strategy != reg.OVERWRITE:
        return SKIP
    env.warn("ai_tools", name, "tool_imported_disabled" if state == "new" else "tool_replaced_disabled")
    if deps:
        env.warn("ai_tools", name, "tool_has_dependencies")
    if not dry_run:
        if not env.accept_code:
            # Defesa em profundidade: a rota já exige ``accept_code``, mas o motor não grava código sem ele.
            env.warn("ai_tools", name, "tool_code_not_accepted")
            return SKIP
        tool_repo.save(name, description=description, code=code, dependencies=deps, enabled=False)
    env.mark("ai_tools", name)
    return CREATE if state == "new" else UPDATE

def code_tools_report(env: Env) -> dict:
    """``{"code_tools": [{name, state, dependencies}], "code_tools_total": N}`` para a tela e para o
    servidor (que exige ``accept_code`` quando ``code_tools_total > 0``)."""
    found = env.cache.get("code_tools") or {}
    rows = [{"name": n, **v} for n, v in sorted(found.items())]
    return {"code_tools": rows[:MAX_REPORTED_TOOLS], "code_tools_total": len(rows)}

# ═══ ajustes de ferramentas (tool_overrides) ════════════════════════════════

def export_overrides(env: Env) -> Iterator[dict]:
    """Só o que o operador de fato mudou (desligou, renomeou ou redescreveu): o resto é o padrão."""
    from db.repositories import tool_override_repo
    for r in tool_override_repo.list_all():
        if int(r.get("enabled", 1)) == 0 or r.get("description") or r.get("display_label"):
            yield {"name": r["name"], "enabled": bool(r.get("enabled", 1)),
                   "description": r.get("description"), "display_label": r.get("display_label")}

def apply_override(row: dict, strategy: str, env: Env, dry_run: bool) -> str:
    """Nunca CRIA linha: ela nasce quando a ferramenta é registrada no boot, e o boot apaga as órfãs.
    Ferramenta que não existe aqui (plugin não instalado, ferramenta importada ainda desligada) é pulada."""
    from db.repositories import tool_override_repo
    name = row.get("name")
    try:
        if not isinstance(name, str) or not _KEY_RE.match(name):
            raise _Bad("nome inválido")
        enabled = _bool(row.get("enabled", True))
        desc = _text(row.get("description"), MAX_DESC, allow_none=True) or None
        label = _text(row.get("display_label"), MAX_LABEL, allow_none=True) or None
    except _Bad:
        return _invalid(env, "tool_overrides", name)
    existing = tool_override_repo.get(name)
    if existing is None:
        env.warn("tool_overrides", name, "override_tool_unknown")
        return SKIP
    if strategy == reg.SKIP:
        return SKIP
    cur = {"enabled": bool(existing.get("enabled", 1)), "description": existing.get("description") or None,
           "display_label": existing.get("display_label") or None}
    if strategy == reg.OVERWRITE:
        want = {"enabled": enabled, "description": desc, "display_label": label}
    else:                                              # merge: completa o que está em branco
        want = {"enabled": cur["enabled"], "description": cur["description"] or desc,
                "display_label": cur["display_label"] or label}
    if want == cur:
        return SKIP
    if not dry_run:
        tool_override_repo.upsert_override(name, **want)
        env.cache["overrides_changed"] = True
    return UPDATE
