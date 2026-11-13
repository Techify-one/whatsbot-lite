"""Importar/Exportar — o reinício que carrega os plugins importados.

O pacote traz o plugin DESLIGADO. A tabela de dados dele só existe depois de ligar o plugin e reiniciar o
servidor (as migrations rodam no boot). Este arquivo prova as pontas dessa pausa:

  * o opt-in ``activate_plugins`` só liga plugin que o import INSTALOU ou que tem dado esperando (um plugin
    que já estava aqui, desligado e sem nada esperando, fica como está) e só existe junto de
    ``accept_code`` (rota e motor);
  * ligar um plugin pausa o job mesmo sem dado pendente, e a etapa 2 não regrava nada;
  * ``resume`` recusa (409, dizendo qual plugin) enquanto o plugin não está ligado e carregado;
  * ``/restart`` só reinicia com um import ESPERANDO e sem outro job em andamento;
  * ``/health`` devolve um ``boot_id`` estável por processo; o reinício toca o gatilho do uvicorn;
  * ``is_supervised`` reconhece os supervisores do Lite (launchers, systemd, Docker).

Roda direto (``python tests/test_portability_restart.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from test_portability_plugins import (_clean, _drain, _engine, _export_plugins, _fresh_destination,  # noqa: E402,F401
                                      _install, _installed_row, _plugins_env, _resume, _rows,
                                      _source_with_data)
from test_portability_roundtrip import _dryrun  # noqa: E402
from portability import handlers, jobs  # noqa: E402
from portability.jobs import State  # noqa: E402

def _import(pkg, *, accept_code=True, activate=False):
    dry = _dryrun(pkg)
    assert dry["state"] == State.SUCCEEDED, dry["error"]
    params = {"package_path": dry["params"]["package_path"], "package_sha256": dry["params"]["package_sha256"],
              "has_password": False, "overwrite_secrets": False, "accept_code": accept_code,
              "activate_plugins": activate, "strategy": dry["report"].get("strategy")}
    j = jobs.create("import", params=params, package_path=params["package_path"],
                    package_sha256=params["package_sha256"], source_job=dry["id"])
    _drain()
    return jobs.get(j["id"])

def _migrate(pdir):
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    run_pending_migrations(load_manifest(pdir / "demo"), pdir / "demo")

# ── opt-in "ativar" no motor ────────────────────────────────────────────────

def test_activating_turns_on_what_the_import_installed_and_pauses_for_the_restart(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    done = _import(pkg, activate=True)
    assert done["state"] == State.AWAITING_RESTART and done["resume_stage"] == "plugins"
    assert _installed_row()["enabled"]                                   # ligado no registro, nada carregado
    assert done["report"]["plugins_activated"] == ["demo"]
    assert done["report"]["pending_plugin_data"] == ["demo/items"]

def test_without_the_opt_in_the_plugin_stays_off(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    done = _import(pkg, activate=False)
    assert not _installed_row()["enabled"]
    assert "plugins_activated" not in done["report"]

def test_activation_without_accepting_the_code_does_nothing(_plugins_env):
    """Defesa em profundidade: mesmo que alguém grave ``activate_plugins`` sem ``accept_code``."""
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    done = _import(pkg, accept_code=False, activate=True)
    assert _installed_row() is None                                      # nem o código entrou
    assert "plugins_activated" not in done["report"]

def test_a_plugin_that_was_already_here_is_never_turned_on(_plugins_env):
    """Sem dado esperando e sem ter sido instalado agora, um plugin que o operador deixou desligado fica como está."""
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    _install(_plugins_env, enabled=False, migrate=True)                  # já existe, mesma versão, desligado
    done = _import(pkg, activate=True)
    assert not _installed_row()["enabled"]
    assert "plugins_activated" not in done["report"]

def test_activating_pauses_even_when_there_is_no_data_to_wait_for(_plugins_env):
    """Plugin sem tabela de dados: ainda assim só passa a valer depois do reinício, então o job espera."""
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    _fresh_destination(_plugins_env)
    done = _import(pkg, activate=True)
    assert done["state"] == State.AWAITING_RESTART
    assert done["report"]["pending_plugin_data"] == []
    assert done["report"]["plugins_activated"] == ["demo"]
    assert _installed_row()["enabled"]

def test_stage_two_with_nothing_pending_just_finishes_and_keeps_the_numbers(_plugins_env):
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    _fresh_destination(_plugins_env)
    done = _import(pkg, activate=True)
    final = _resume(done["id"])
    assert final["state"] == State.SUCCEEDED, final["error"]
    assert final["report"]["entities"]["plugins"]["create"] == 1         # a 1ª etapa não foi refeita nem zerada
    assert "pending_plugin_data" not in final["report"]

def test_activate_then_restart_then_resume_brings_the_data_in(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    done = _import(pkg, activate=True)
    _migrate(_plugins_env)                                               # o que o boot faz ao carregar o plugin
    final = _resume(done["id"])
    assert final["state"] == State.SUCCEEDED, final["error"]
    assert [r["title"] for r in _rows()] == ["de a", "de b", "sem contato"]

# ── bloqueios do "Continuar" ────────────────────────────────────────────────

def _deps(loaded=()):
    return SimpleNamespace(plugins_registry=SimpleNamespace(loaded={k: object() for k in loaded}))

def test_blockers_name_the_plugin_that_is_not_enabled(_plugins_env):
    rep = {"pending_plugin_data": ["demo/items"]}
    _install(_plugins_env, enabled=False)
    assert handlers.resume_blockers(rep, _deps()) == [{"plugin": "demo", "why": "not_enabled"}]

def test_blockers_name_the_plugin_that_is_enabled_but_not_loaded_yet(_plugins_env):
    _install(_plugins_env, enabled=True)
    rep = {"pending_plugin_data": ["demo/items"]}
    assert handlers.resume_blockers(rep, _deps(loaded=())) == [{"plugin": "demo", "why": "not_loaded"}]

def test_blockers_name_the_plugin_whose_tables_were_not_created(_plugins_env):
    _install(_plugins_env, enabled=True)
    rep = {"pending_plugin_data": ["demo/items"]}
    assert handlers.resume_blockers(rep, _deps(loaded=("demo",))) == [{"plugin": "demo", "why": "no_tables"}]

def test_no_blockers_once_the_plugin_is_loaded_and_migrated(_plugins_env):
    _install(_plugins_env, enabled=True, migrate=True)
    assert handlers.resume_blockers({"pending_plugin_data": ["demo/items"]}, _deps(loaded=("demo",))) == []

def test_a_plugin_activated_without_data_only_needs_to_be_loaded(_plugins_env):
    _install(_plugins_env, enabled=True)
    rep = {"pending_plugin_data": [], "plugins_activated": ["demo"]}
    assert handlers.resume_blockers(rep, _deps(loaded=())) == [{"plugin": "demo", "why": "not_loaded"}]
    assert handlers.resume_blockers(rep, _deps(loaded=("demo",))) == []   # sem tabela esperada, não exige migration

def test_an_unknown_registry_does_not_block_on_loading(_plugins_env):
    """Sem registro de plugins (ex.: teste), não dá para saber se carregou: não bloqueia por isso."""
    _install(_plugins_env, enabled=True, migrate=True)
    assert handlers.resume_blockers({"pending_plugin_data": ["demo/items"]}, SimpleNamespace()) == []

# ── rotas ───────────────────────────────────────────────────────────────────

import test_portability_routes as rr  # noqa: E402
from test_portability_routes import env  # noqa: E402,F401

@pytest.fixture()
def _routes_clean(env, _plugins_env, monkeypatch):
    """Banco limpo de jobs/plugins para as rotas, e o app apontando para a pasta de plugins isolada."""
    monkeypatch.setattr(jobs, "set_runtime_deps", jobs.set_runtime_deps)
    yield env

def _waiting_job(pdir, *, activated=True, enabled=True, loaded=False, migrate=False, pending=("demo/items",)):
    """Import em pausa à espera do reinício, com um pacote que existe no disco."""
    pkg = Path(pdir).parent / "pkg.zip"
    pkg.write_bytes(b"x")
    _install(pdir, enabled=enabled, migrate=migrate)
    job = jobs.create("import", params={"package_path": str(pkg), "has_password": False},
                      package_path=str(pkg), state=State.AWAITING_RESTART)
    jobs.set_resume_stage(job["id"], "plugins")
    jobs.set_report(job["id"], {"pending_plugin_data": list(pending),
                                **({"plugins_activated": ["demo"]} if activated else {})})
    return job

def test_resume_route_refuses_and_names_the_plugin_when_it_is_not_enabled(_routes_clean, _plugins_env):
    c, h = _routes_clean["client"], rr._set_panel_password()
    job = _waiting_job(_plugins_env, enabled=False)
    r = c.post(f"/api/portability/jobs/{job['id']}/resume", json={}, headers=h)
    assert r.status_code == 409, r.text
    assert "demo" in r.json()["error"] and "ativo" in r.json()["error"]
    assert r.json()["data"]["blockers"] == [{"plugin": "demo", "why": "not_enabled"}]
    assert jobs.get(job["id"])["state"] == State.AWAITING_RESTART        # não gastou a tentativa

def test_resume_route_accepts_when_nothing_blocks(_routes_clean, _plugins_env, monkeypatch):
    c, h = _routes_clean["client"], rr._set_panel_password()
    job = _waiting_job(_plugins_env, enabled=True, migrate=True)
    monkeypatch.setattr(handlers, "resume_blockers", lambda report, deps: [])
    r = c.post(f"/api/portability/jobs/{job['id']}/resume", json={}, headers=h)
    assert r.status_code == 202, r.text

def test_restart_info_needs_the_panel_password_and_reports_the_boot_id(_routes_clean):
    from plugins.restart import BOOT_ID
    c = _routes_clean["client"]
    from db.repositories import config_repo
    config_repo.set_many({"web_password_hash": "", "web_password_salt": ""})
    assert c.get("/api/portability/restart-info").status_code == 403
    h = rr._set_panel_password()
    assert c.get("/api/portability/restart-info").status_code == 401     # com senha, sem token
    r = c.get("/api/portability/restart-info", headers=h)
    assert r.status_code == 200 and r.json()["data"]["boot_id"] == BOOT_ID
    assert isinstance(r.json()["data"]["supervised"], bool)
    assert r.headers["cache-control"] == "no-store"

def test_restart_route_needs_the_panel_password(_routes_clean):
    from db.repositories import config_repo
    config_repo.set_many({"web_password_hash": "", "web_password_salt": ""})
    assert _routes_clean["client"].post("/api/portability/restart").status_code == 403
    rr._set_panel_password()
    assert _routes_clean["client"].post("/api/portability/restart").status_code == 401

def test_restart_route_refuses_without_an_import_waiting(_routes_clean, monkeypatch):
    """Sem isto, qualquer um com a senha do painel derrubaria a instalação à toa."""
    import plugins.restart as restart
    calls = []
    monkeypatch.setattr(restart, "schedule_restart", lambda reason="": calls.append(reason))
    c, h = _routes_clean["client"], rr._set_panel_password()
    r = c.post("/api/portability/restart", headers=h)
    assert r.status_code == 409 and calls == []
    jobs.create("import", params={})                                      # na fila, não esperando reinício
    assert c.post("/api/portability/restart", headers=h).status_code == 409 and calls == []

def test_restart_route_restarts_when_an_import_is_waiting(_routes_clean, _plugins_env, monkeypatch):
    import plugins.restart as restart
    calls = []
    monkeypatch.setattr(restart, "schedule_restart", lambda reason="": calls.append(reason))
    c, h = _routes_clean["client"], rr._set_panel_password()
    _waiting_job(_plugins_env)
    r = c.post("/api/portability/restart", headers=h)
    assert r.status_code == 200 and r.json()["data"] == {"restarting": True}
    assert len(calls) == 1 and "portability" in calls[0]

def test_restart_route_refuses_while_another_job_is_running(_routes_clean, _plugins_env, monkeypatch):
    """Reiniciar com um export rodando o perderia."""
    import plugins.restart as restart
    calls = []
    monkeypatch.setattr(restart, "schedule_restart", lambda reason="": calls.append(reason))
    c, h = _routes_clean["client"], rr._set_panel_password()
    _waiting_job(_plugins_env)
    jobs.create("export", params={"scopes": ["catalog"]})                 # queued = em andamento
    r = c.post("/api/portability/restart", headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "job_busy" and calls == []

def test_an_interrupted_import_does_not_count_as_waiting_for_a_restart(_routes_clean, monkeypatch):
    """``AWAITING_RESTART`` sem ``resume_stage`` é um import interrompido por queda, não uma pausa de plugin."""
    import plugins.restart as restart
    calls = []
    monkeypatch.setattr(restart, "schedule_restart", lambda reason="": calls.append(reason))
    c, h = _routes_clean["client"], rr._set_panel_password()
    jobs.create("import", params={}, state=State.AWAITING_RESTART)
    assert c.post("/api/portability/restart", headers=h).status_code == 409 and calls == []

def test_confirm_activates_only_with_the_code_accepted(_routes_clean, _plugins_env):
    """``activate_plugins`` sem ``accept_code`` é ignorado pela rota (e a confirmação de código é exigida)."""
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import portability_jobs as J
    import json
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    _fresh_destination(_plugins_env)
    c, h = _routes_clean["client"], rr._set_panel_password()
    dry = _dryrun(pkg)
    assert dry["state"] == State.SUCCEEDED and dry["report"]["code_plugins_total"] == 1
    refused = c.post(f"/api/portability/imports/{dry['id']}/confirm",
                     json={"acknowledged": True, "activate_plugins": True}, headers=h)
    assert refused.status_code == 400 and refused.json()["error_code"] == "code_not_accepted"
    ok = c.post(f"/api/portability/imports/{dry['id']}/confirm",
                json={"acknowledged": True, "accept_code": True, "activate_plugins": True}, headers=h)
    assert ok.status_code == 202, ok.text
    assert "params" not in ok.json()["data"]
    with get_engine().connect() as conn:
        raw = conn.execute(select(J.c.params).where(J.c.id == ok.json()["data"]["id"])).scalar()
    assert json.loads(raw)["activate_plugins"] is True

# ── /health e o gatilho de reinício ─────────────────────────────────────────

def test_health_returns_a_boot_id_that_is_stable_within_the_process(_routes_clean):
    from plugins.restart import BOOT_ID
    c = _routes_clean["client"]
    a, b = c.get("/health").json(), c.get("/health").json()
    assert a["ok"] is True and a["boot_id"] == b["boot_id"] == BOOT_ID
    assert len(BOOT_ID) == 32

def test_health_stays_open_without_a_token(_routes_clean):
    rr._set_panel_password()
    assert _routes_clean["client"].get("/health").status_code == 200

@pytest.mark.parametrize("var,val", [("WHATSBOT_SUPERVISED", "1"), ("WHATSBOT_SERVICE", "1"),
                                     ("WHATSBOT_DOCKER", "1")])
def test_the_supervisors_of_the_lite_are_recognised(monkeypatch, var, val):
    import plugins.restart as restart
    for v in ("WHATSBOT_SUPERVISED", "WHATSBOT_SERVICE", "WHATSBOT_DOCKER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(restart, "_under_reloader", lambda: False)
    monkeypatch.setattr(restart.Path, "exists", lambda self: False)
    assert restart.is_supervised() is False
    monkeypatch.setenv(var, val)
    assert restart.is_supervised() is True

def test_a_worker_under_uvicorn_reload_counts_as_supervised(monkeypatch):
    import plugins.restart as restart
    for v in ("WHATSBOT_SUPERVISED", "WHATSBOT_SERVICE", "WHATSBOT_DOCKER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(restart.Path, "exists", lambda self: False)
    monkeypatch.setattr(restart, "_under_reloader", lambda: True)
    assert restart.is_supervised() is True

def test_the_launchers_that_relaunch_the_server_export_the_supervisor_flag():
    """O reinício automático só é oferecido com supervisor: os launchers que o têm precisam dizê-lo."""
    for name in ("linux_start.sh", "macos_start.command", "windows_start.bat"):
        text = (_ROOT / name).read_text(encoding="utf-8")
        assert "WHATSBOT_SUPERVISED" in text, f"{name} não exporta WHATSBOT_SUPERVISED"

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
