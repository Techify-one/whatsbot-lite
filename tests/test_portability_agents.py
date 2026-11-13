"""Importar/Exportar — motor de IA (agentes, prompts, variáveis, ferramentas com código, ajustes).

O que se prova, de propósito:
  * ``ai_tools.code`` é Python que roda no boot: a ferramenta SEMPRE entra DESLIGADA (``enabled=0``),
    mesmo que o pacote traga ``enabled: true``, e nunca é gravada sem ``accept_code``;
  * a dependência pip passa por regex fechada (``--index-url=…`` e ``pkg @ http://…`` são recusadas);
  * a prévia lista o código mesmo quando a estratégia o pularia, e dry-run e import dão as mesmas contagens;
  * variável com NOME de segredo só viaja cifrada, e a decisão é pelo nome (a marca do pacote não vale);
  * ``tool_overrides`` nunca cria linha (o boot apaga as órfãs) e avisa a ferramenta desconhecida;
  * ``ai_engine_enabled`` continua fora (importar um agente não liga o motor);
  * reimportar é idempotente; ``merge`` não pisa em prompt/agente editado neste servidor.

Roda direto (``python tests/test_portability_agents.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import test_portability_roundtrip as rt  # noqa: E402
from test_portability_roundtrip import (_build_pkg, _drain, _dryrun, _export, _import,  # noqa: E402,F401
                                        _clean, _engine, needs_crypto)
from portability import entities_ai as ai  # noqa: E402
from portability import jobs  # noqa: E402
from portability.jobs import State  # noqa: E402

PW = rt.PW

@pytest.fixture(autouse=True)
def _clean_ai(_engine):
    _wipe_ai()
    yield
    _wipe_ai()

def _wipe_ai() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db import tables as t
    with get_engine().begin() as c:
        for tb in (t.ai_agents_history, t.ai_prompts_history, t.ai_tools_history, t.ai_agents,
                   t.ai_prompts, t.ai_variables, t.ai_tools, t.tool_overrides):
            c.execute(delete(tb))

# ── semente ─────────────────────────────────────────────────────────────────

CODE = "SCHEMA = {'type':'function','function':{'name':'minha_tool'}}\ndef execute(ctx, args):\n    return 'x'\n"

def _seed() -> None:
    from db.repositories import agent_repo, prompt_repo, tool_override_repo, tool_repo, variable_repo
    prompt_repo.save("vendas", "Você é o vendedor da {empresa}.")
    prompt_repo.save("vendas", "Você é o vendedor da {empresa}. Seja breve.")      # version 2: editado
    variable_repo.save("empresa", "Acme", "geral")
    variable_repo.save("webhook_token", "TOKEN-SECRETO-DA-VARIAVEL", "integracao")
    tool_repo.save("minha_tool", description="faz x", code=CODE, dependencies=["httpx>=0.27,<0.28"],
                   enabled=True)
    agent_repo.save("vendas", display_name="Vendas", prompt_key="vendas",
                    model_config={"model": "m/x", "temperature": 0.3, "max_tokens": 900},
                    tool_names=["minha_tool", "outra"], enabled=True)
    tool_override_repo.ensure("send_text", None)
    tool_override_repo.upsert_override("send_text", enabled=False, display_label="Enviar texto")
    tool_override_repo.ensure("intocada", None)              # padrão: não viaja

def _wipe_all_but_overrides() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db import tables as t
    with get_engine().begin() as c:
        for tb in (t.ai_agents_history, t.ai_prompts_history, t.ai_tools_history, t.ai_agents,
                   t.ai_prompts, t.ai_variables, t.ai_tools):
            c.execute(delete(tb))

def _import_all(pkg, *, accept_code=True, password=None, strategy=None, overwrite_secrets=False):
    dry = _dryrun(pkg, password=password, strategy=strategy)
    assert dry["state"] == State.SUCCEEDED, dry["error"]
    params = {"package_path": dry["params"]["package_path"],
              "package_sha256": dry["params"]["package_sha256"], "has_password": bool(password),
              "overwrite_secrets": overwrite_secrets, "accept_code": accept_code,
              "strategy": strategy if strategy is not None else dry["report"].get("strategy")}
    j = jobs.create("import", params=params, package_path=params["package_path"],
                    package_sha256=params["package_sha256"], source_job=dry["id"],
                    affinity=jobs.PROCESS_WID if password else None)
    if password:
        jobs.stash_password(j["id"], password)
    _drain()
    return dry, jobs.get(j["id"])

def _tool(name="minha_tool"):
    from db.repositories import tool_repo
    return tool_repo.get(name)

# ── export ──────────────────────────────────────────────────────────────────

def test_export_carries_the_engine_but_not_install_state_nor_secret_values():
    from portability.format import PackageReader
    _seed()
    from db.repositories import tool_repo
    tool_repo.set_status("minha_tool", "failed", "boom: caminho /home/x")
    with PackageReader(_export(["agents"])) as r:
        tools = list(r.iter_ndjson("ai_tools"))
        assert tools == [{"name": "minha_tool", "description": "faz x", "code": CODE,
                          "dependencies": ["httpx>=0.27,<0.28"], "enabled": True}]
        everything = b"".join(r.read_member(n) for n in r.names())
        assert b"boom" not in everything and b"/home/x" not in everything      # estado desta máquina
        assert b"TOKEN-SECRETO-DA-VARIAVEL" not in everything                  # sem senha, sem segredo
        variables = {v["name"]: v for v in r.iter_ndjson("ai_variables")}
        assert variables["webhook_token"] == {"name": "webhook_token", "value": None,
                                              "category": "integracao", "secret": True}
        assert variables["empresa"]["value"] == "Acme"
        ov = list(r.iter_ndjson("tool_overrides"))
        assert [o["name"] for o in ov] == ["send_text"]                        # só o que foi mudado
        assert not r.has("data/ai_credentials.ndjson")
        assert r.manifest.counts["ai_agents"] == 1


def test_agents_scope_never_carries_the_engine_switch():
    from db.repositories import config_repo
    from portability.format import PackageReader
    config_repo.set("ai_engine_enabled", True)
    with PackageReader(_export(["agents", "settings"])) as r:
        assert "ai_engine_enabled" not in {c["key"] for c in r.iter_ndjson("config")}

# ── código: sempre desligado ────────────────────────────────────────────────

def test_tool_arrives_disabled_even_if_the_package_says_enabled():
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    dry, done = _import_all(pkg)
    assert done["state"] == State.SUCCEEDED, done["error"]
    t = _tool()
    assert t is not None and t["code"] == CODE
    assert t["enabled"] is False                         # o pacote dizia enabled: true
    assert t["install_status"] == "pending"
    from db.repositories import tool_repo
    assert tool_repo.list_enabled() == []                # nada que o boot importe


def test_forged_package_with_enabled_true_is_still_disabled():
    pkg = _build_pkg({"ai_tools": [{"name": "evil", "description": "", "code": "import os\nos.system('x')",
                                    "dependencies": [], "enabled": True}]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    assert done["state"] == State.SUCCEEDED
    assert _tool("evil")["enabled"] is False


def test_engine_refuses_to_write_code_without_accept_code():
    """Defesa em profundidade: mesmo que a rota seja contornada, o motor não grava código."""
    pkg = _build_pkg({"ai_tools": [{"name": "evil", "description": "", "code": "x=1",
                                    "dependencies": [], "enabled": True}]}, scopes=("agents",))
    dry, done = _import_all(pkg, accept_code=False)
    assert done["state"] == State.SUCCEEDED
    assert _tool("evil") is None
    assert done["report"]["entities"]["ai_tools"] == {"create": 0, "update": 0, "skip": 1, "error": 0}
    assert "tool_code_not_accepted" in done["report"]["warnings"]["ai_tools"]


def test_preview_lists_code_even_when_the_strategy_would_skip_it():
    """Ferramenta que já existe e DIFERE: com 'skip' nada seria gravado, mas o código está no pacote e a
    confirmação tem de ser exigida do mesmo jeito (trocar a estratégia não pode esconder código)."""
    _seed()
    pkg = _export(["agents"])
    from db.repositories import tool_repo
    tool_repo.save("minha_tool", description="outra", code="x=2", dependencies=[], enabled=True)
    dry = _dryrun(pkg, strategy={"ai_tools": "skip"})
    assert dry["report"]["entities"]["ai_tools"]["skip"] == 1
    assert dry["report"]["code_tools_total"] == 1
    assert dry["report"]["code_tools"] == [{"name": "minha_tool", "state": "differs", "dependencies": True}]


def test_default_strategy_never_replaces_code_that_already_exists():
    """O padrão é ``skip``: sem o operador escolher "substituir", a ferramenta daqui não é tocada."""
    _seed()
    pkg = _export(["agents"])
    from db.repositories import tool_repo
    tool_repo.save("minha_tool", description="local", code="x = 'LOCAL'", dependencies=[], enabled=True)
    dry = _dryrun(pkg)                                        # sem estratégia: vale o padrão
    assert dry["report"]["strategy"]["ai_tools"] == "skip"
    _import_all(pkg)
    t = _tool()
    assert t["code"] == "x = 'LOCAL'" and t["enabled"] is True


def test_overwrite_replaces_code_and_turns_the_running_tool_off():
    _seed()
    pkg = _export(["agents"])
    from db.repositories import tool_repo
    tool_repo.save("minha_tool", description="velha", code="x=2", dependencies=[], enabled=True)
    dry, done = _import_all(pkg, strategy={"ai_tools": "overwrite"})
    assert done["report"]["entities"]["ai_tools"]["update"] == 1
    t = _tool()
    assert t["code"] == CODE and t["enabled"] is False
    assert "tool_replaced_disabled" in done["report"]["warnings"]["ai_tools"]


def test_identical_tool_is_left_alone_and_keeps_running():
    _seed()
    pkg = _export(["agents"])
    dry, done = _import_all(pkg, strategy={"ai_tools": "overwrite"})
    assert done["report"]["entities"]["ai_tools"]["skip"] == 1
    assert _tool()["enabled"] is True                    # não desliga o que já roda e é idêntico


@pytest.mark.parametrize("spec", [
    "--index-url=http://evil.example/simple", "-r /etc/passwd", "pkg @ http://evil.example/x.whl",
    "pkg; python_version>'3'", "../local", "https://evil.example/x.tar.gz", "pkg --pre", "a b", "",
    "x" * 300, 5, None,
])
def test_malicious_pip_specs_are_refused(spec):
    assert not ai.is_pip_spec(spec)
    pkg = _build_pkg({"ai_tools": [{"name": "t", "description": "", "code": "x=1",
                                    "dependencies": [spec], "enabled": False}]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    assert _tool("t") is None
    assert done["report"]["entities"]["ai_tools"]["error"] == 1


def test_a_leading_dash_is_never_a_pip_spec():
    """O ``pip install`` recebe a lista no argv: um ``-`` inicial é opção (``--index-url``, ``-r``)."""
    for spec in ("-e git+https://x", "--pre", "-rrequirements.txt", "-httpx"):
        assert not ai.is_pip_spec(spec), spec

@pytest.mark.parametrize("spec", ["httpx", "httpx>=0.27,<0.28", "requests[socks]==2.32.3", "pydantic~=2.0"])
def test_ordinary_pip_specs_pass(spec):
    assert ai.is_pip_spec(spec)


@pytest.mark.parametrize("name", ["../x", "Maiuscula", "a-b", "1abc", "a" * 70, "x.py", "", None, 3])
def test_tool_name_must_match_the_installer_rule(name):
    pkg = _build_pkg({"ai_tools": [{"name": name, "description": "", "code": "x=1",
                                    "dependencies": [], "enabled": False}]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    from db.repositories import tool_repo
    assert tool_repo.list_all() == []


def test_code_tools_are_counted_in_the_final_report_too():
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    dry, done = _import_all(pkg)
    assert dry["report"]["code_tools_total"] == 1 == done["report"]["code_tools_total"]
    assert dry["report"]["entities"] == {k: v for k, v in done["report"]["entities"].items()}

# ── prévia = import, e idempotência ─────────────────────────────────────────

def test_dry_run_counts_equal_import_counts_and_dry_run_writes_nothing():
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    dry = _dryrun(pkg)
    from db.repositories import agent_repo, prompt_repo, tool_repo, variable_repo
    assert (agent_repo.list_all(), prompt_repo.list_all(), tool_repo.list_all()) == ([], [], [])
    assert variable_repo.list_all() == []
    done = _import(dry, accept_code=True)             # a rota exige accept_code quando há código
    assert done["state"] == State.SUCCEEDED
    assert dry["report"]["entities"] == done["report"]["entities"]


def test_reimport_is_idempotent_and_does_not_bump_versions():
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    _import_all(pkg)
    from db.repositories import agent_repo, prompt_repo
    v = (agent_repo.get("vendas")["version"], prompt_repo.get("vendas")["version"])
    dry, done = _import_all(pkg)
    ents = done["report"]["entities"]
    for name in ("ai_variables", "ai_prompts", "ai_agents", "ai_tools"):
        assert ents[name]["create"] == 0 and ents[name]["update"] == 0, name
    assert (agent_repo.get("vendas")["version"], prompt_repo.get("vendas")["version"]) == v

# ── agentes, prompts e variáveis ────────────────────────────────────────────

def test_roundtrip_restores_agent_prompt_and_variables_through_the_repositories():
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    dry, done = _import_all(pkg)
    assert done["state"] == State.SUCCEEDED, done["error"]
    from db.repositories import agent_repo, prompt_repo, variable_repo
    a = agent_repo.get("vendas")
    assert a["prompt_key"] == "vendas" and a["tool_names"] == ["minha_tool", "outra"]
    assert a["model_config"] == {"model": "m/x", "temperature": 0.3, "max_tokens": 900}
    assert prompt_repo.get("vendas")["body"].endswith("Seja breve.")
    assert variable_repo.get("empresa")["value"] == "Acme"
    assert variable_repo.get("webhook_token") is None          # segredo sem senha: não entra
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import ai_agents_history
    with get_engine().connect() as c:
        assert c.execute(select(ai_agents_history)).first() is not None      # gerou histórico


def test_merge_keeps_a_prompt_and_agent_edited_on_this_server():
    from db.repositories import agent_repo, prompt_repo
    _seed()
    pkg = _export(["agents"])
    prompt_repo.save("vendas", "Editado AQUI")                  # version 3
    agent_repo.save("vendas", display_name="Meu", prompt_key="vendas", model_config={}, tool_names=None,
                    enabled=True)
    dry, done = _import_all(pkg, strategy={"ai_prompts": "merge", "ai_agents": "merge"})
    assert prompt_repo.get("vendas")["body"] == "Editado AQUI"
    assert agent_repo.get("vendas")["display_name"] == "Meu"
    dry, done = _import_all(pkg, strategy={"ai_prompts": "overwrite", "ai_agents": "overwrite"})
    assert prompt_repo.get("vendas")["body"].endswith("Seja breve.")
    assert agent_repo.get("vendas")["display_name"] == "Vendas"


def test_merge_replaces_a_boot_seed_that_was_never_edited():
    """O prompt 'default' semeado no boot está em version 1: sem isto importar numa instalação nova não
    traria o prompt do pacote."""
    from db.repositories import prompt_repo
    prompt_repo.ensure("default", "semente")
    pkg = _build_pkg({"ai_prompts": [{"prompt_key": "default", "body": "do pacote"}]}, scopes=("agents",))
    _import_all(pkg, strategy={"ai_prompts": "merge"})
    assert prompt_repo.get("default")["body"] == "do pacote"


@pytest.mark.parametrize("row", [
    {"agent_key": "../x", "prompt_key": "p"},
    {"agent_key": "a", "prompt_key": "p", "model_config": "texto"},
    {"agent_key": "a", "prompt_key": "p", "model_config": {"temperature": 9}},
    {"agent_key": "a", "prompt_key": "p", "model_config": {"max_tokens": 1.5}},
    {"agent_key": "a", "prompt_key": "p", "tool_names": "all"},
    {"agent_key": "a", "prompt_key": "p", "tool_names": [1]},
    {"agent_key": "a", "prompt_key": "p", "enabled": "sim"},
    {"agent_key": "a", "prompt_key": ["x"]},
    {"agent_key": 7},
])
def test_invalid_agent_rows_are_errors_not_writes(row):
    from db.repositories import agent_repo
    pkg = _build_pkg({"ai_agents": [row]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    assert done["state"] == State.SUCCEEDED
    assert agent_repo.list_all() == []
    assert done["report"]["entities"]["ai_agents"]["error"] == 1


def test_model_config_keeps_only_the_four_fields_the_engine_reads():
    from db.repositories import agent_repo
    pkg = _build_pkg({"ai_agents": [{"agent_key": "a", "prompt_key": "", "enabled": True,
                                     "model_config": {"model": "m", "temperature": 1, "api_key": "SEGREDO",
                                                      "base_url": "http://evil"}}]}, scopes=("agents",))
    _import_all(pkg)
    assert agent_repo.get("a")["model_config"] == {"model": "m", "temperature": 1}


def test_agent_with_dangling_references_is_imported_with_warnings():
    pkg = _build_pkg({"ai_agents": [{"agent_key": "a", "prompt_key": "inexistente", "enabled": True,
                                     "tool_names": ["nao_existe"]}]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    w = done["report"]["warnings"]["ai_agents"]
    assert "agent_prompt_missing" in w and "agent_tool_missing" in w


def test_agent_referencing_a_tool_the_package_creates_gets_no_warning():
    pkg = _build_pkg({
        "ai_tools": [{"name": "nova_tool", "description": "", "code": "x=1", "dependencies": [],
                      "enabled": False}],
        "ai_agents": [{"agent_key": "a", "prompt_key": "", "enabled": True, "tool_names": ["nova_tool"]}],
    }, scopes=("agents",))
    # no pacote a ordem é a de IMPORT_ORDER (ferramentas antes dos agentes), então o ``virtual`` resolve
    dry = _dryrun(pkg)
    assert "agent_tool_missing" not in dry["report"]["warnings"].get("ai_agents", {})


@pytest.mark.parametrize("name", ["1x", "a b", "a-b", "x" * 70, "{x}", "", None])
def test_variable_names_follow_the_placeholder_rule(name):
    from db.repositories import variable_repo
    pkg = _build_pkg({"ai_variables": [{"name": name, "value": "v", "category": ""}]}, scopes=("agents",))
    _import_all(pkg)
    assert variable_repo.list_all() == []

# ── segredos ────────────────────────────────────────────────────────────────

@needs_crypto
def test_secret_named_variable_travels_only_encrypted_and_by_name():
    _seed()
    pkg = _export(["agents"], password=PW)
    from portability.format import PackageReader
    with PackageReader(pkg) as r:
        assert r.has("secrets.enc")
        assert b"TOKEN-SECRETO-DA-VARIAVEL" not in b"".join(r.read_member(n) for n in r.names())
    _wipe_all_but_overrides()
    dry, done = _import_all(pkg, password=PW)
    assert done["state"] == State.SUCCEEDED, done["error"]
    from db.repositories import variable_repo
    assert variable_repo.get("webhook_token")["value"] == "TOKEN-SECRETO-DA-VARIAVEL"


def test_a_forged_non_secret_flag_cannot_smuggle_a_secret_named_variable_in_clear():
    """O nome decide, não a marca ``secret`` do pacote: valor em claro para nome de segredo é ignorado."""
    from db.repositories import variable_repo
    pkg = _build_pkg({"ai_variables": [{"name": "api_key_x", "value": "EM-CLARO", "category": "",
                                        "secret": False}]}, scopes=("agents",))
    dry, done = _import_all(pkg)
    assert variable_repo.get("api_key_x") is None
    assert "secret_missing" in done["report"]["warnings"]["ai_variables"]


@needs_crypto
def test_existing_secret_variable_is_kept_unless_overwrite_secrets():
    from db.repositories import variable_repo
    _seed()
    pkg = _export(["agents"], password=PW)
    variable_repo.save("webhook_token", "DO-DESTINO", "x")
    _import_all(pkg, password=PW)
    assert variable_repo.get("webhook_token")["value"] == "DO-DESTINO"
    _import_all(pkg, password=PW, overwrite_secrets=True)
    assert variable_repo.get("webhook_token")["value"] == "TOKEN-SECRETO-DA-VARIAVEL"

# ── ajustes de ferramentas ──────────────────────────────────────────────────

def test_overrides_never_create_rows_and_warn_about_unknown_tools():
    from db.repositories import tool_override_repo
    _seed()
    pkg = _export(["agents"])
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import tool_overrides
    with get_engine().begin() as c:
        c.execute(delete(tool_overrides))
    dry, done = _import_all(pkg)
    assert tool_override_repo.list_all() == []                   # o boot as cria; aqui nunca
    assert done["report"]["entities"]["tool_overrides"]["skip"] == 1
    assert "override_tool_unknown" in done["report"]["warnings"]["tool_overrides"]


def test_override_overwrite_and_merge():
    from db.repositories import tool_override_repo
    _seed()
    pkg = _export(["agents"])
    tool_override_repo.upsert_override("send_text", enabled=True, display_label="Meu rótulo")
    _import_all(pkg, strategy={"tool_overrides": "merge"})
    row = tool_override_repo.get("send_text")
    assert row["display_label"] == "Meu rótulo" and row["enabled"] == 1          # merge não pisa
    _import_all(pkg, strategy={"tool_overrides": "overwrite"})
    row = tool_override_repo.get("send_text")
    assert row["display_label"] == "Enviar texto" and row["enabled"] == 0


def test_import_refreshes_the_tool_list_only_when_an_override_changed():
    calls = []

    class H:
        def refresh_tool_overrides(self):
            calls.append(1)

    class D:
        agent_handler = H()
        settings = None

    _seed()
    pkg = _export(["agents"])
    from db.repositories import tool_override_repo
    tool_override_repo.upsert_override("send_text", enabled=True, display_label=None)
    jobs.set_runtime_deps(D())
    try:
        _import_all(pkg, strategy={"tool_overrides": "overwrite"})
        assert calls == [1]
        calls.clear()
        _import_all(pkg, strategy={"tool_overrides": "overwrite"})       # nada mudou
        assert calls == []
    finally:
        jobs.set_runtime_deps(None)

# ── nada de evento ──────────────────────────────────────────────────────────

def test_import_emits_no_plugin_event(monkeypatch):
    import plugins.events as ev
    sent = []
    monkeypatch.setattr(ev, "emit", lambda *a, **k: sent.append(a))
    _seed()
    pkg = _export(["agents"])
    _wipe_all_but_overrides()
    _import_all(pkg)
    assert sent == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
