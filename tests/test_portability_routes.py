"""Importar/Exportar — rotas HTTP (``server/routes/portability.py``) com o app real.

Banco temporário (SQLite, ou Postgres com ``WHATSBOT_TEST_DB_URL``), GOWA mockado, laço do worker
substituído por ``jobs.run_one`` chamado à mão (o lifespan não sobe no teste).

O que se prova, de propósito:
  * sem senha de painel TODA rota recusa com 403 ``password_required`` (e com senha, sem token, é 401);
  * a API nunca devolve ``params``, ``package_path`` nem a senha do pacote;
  * um job por vez (409) e o teto de upload conta bytes REAIS (413, sem deixar o parcial);
  * zip inválido não cria job nem deixa pasta;
  * o import só nasce de uma verificação concluída E com "revisei" marcado, e uma verificação só vira
    um import;
  * a senha do pacote não chega à tabela de jobs.

Roda direto (``python tests/test_portability_routes.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from db import init_db, init_engine  # noqa: E402
from portability import handlers, jobs  # noqa: E402
from portability.jobs import State  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - depende do ambiente
    HAVE_CRYPTO = False
needs_crypto = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography não instalada")

PW = "senha-do-pacote-12345"
PANEL_PW = "senha-do-painel"

@pytest.fixture(scope="module")
def env():
    tmp = Path(tempfile.mkdtemp(prefix="wb_port_rt_"))
    url = os.environ.get("WHATSBOT_TEST_DB_URL", "").strip()
    if url:
        init_engine(url)
        from db.connection import _run_alembic_upgrade
        _run_alembic_upgrade()
    else:
        init_db(tmp / "whatsbot.db")
    from agent.handler import AgentHandler
    from config.settings import Settings
    from server.app import create_app
    from starlette.testclient import TestClient
    gowa = MagicMock()
    app = create_app(settings=Settings(), gowa_manager=MagicMock(), gowa_client=gowa,
                     agent_handler=AgentHandler(api_key="x", system_prompt="t", max_context_messages=10,
                                                model="openai/gpt-4o-mini"))
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(app):
        yield
    app.router.lifespan_context = _noop
    yield {"client": TestClient(app, raise_server_exceptions=False), "tmp": tmp}
    shutil.rmtree(tmp, ignore_errors=True)

@pytest.fixture(autouse=True)
def _clean(env, monkeypatch):
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.repositories import config_repo
    from db.tables import config, contact_tags, portability_job_errors, portability_jobs, tags
    with get_engine().begin() as c:
        for t in (portability_job_errors, portability_jobs, contact_tags, tags):
            c.execute(delete(t))
    config_repo.set_many({"web_password_hash": "", "web_password_salt": ""})
    jobs._PASSWORDS.clear()
    jobs._HANDLERS.clear()
    handlers.register_all()
    jobs.set_runtime_deps(None)
    root = env["tmp"] / "storages" / "portability"
    shutil.rmtree(root, ignore_errors=True)     # pacotes de testes anteriores
    monkeypatch.setattr(jobs, "jobs_root", lambda: (root.mkdir(parents=True, exist_ok=True), str(root))[1])
    yield

def _set_panel_password() -> dict:
    """Liga a senha do painel e devolve o cabeçalho de sessão."""
    from db.repositories import config_repo
    from server.auth import generate_salt, generate_token, hash_password
    salt = generate_salt()
    h = hash_password(PANEL_PW, salt)
    config_repo.set_many({"web_password_hash": h, "web_password_salt": salt})
    return {"Authorization": f"Bearer {generate_token(h, salt)}"}

def _drain() -> None:
    while asyncio.run(jobs.run_one(jobs.PROCESS_WID)):
        pass

def _seed_tags() -> None:
    from db.repositories import tag_repo
    tag_repo.create("vip", "#00ff00")
    tag_repo.create("lead", "red")

def _export(client, h, scopes=("catalog",), **extra) -> dict:
    r = client.post("/api/portability/exports", json={"scopes": list(scopes), **extra}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()["data"]
    _drain()
    return client.get(f"/api/portability/jobs/{job['id']}", headers=h).json()["data"]

def _download(client, h, job_id: str) -> bytes:
    r = client.post(f"/api/portability/jobs/{job_id}/download", headers=h)
    assert r.status_code == 200, r.text
    return r.content

def _upload(client, h, blob: bytes, password: str = ""):
    return client.post("/api/portability/imports", headers=h, data={"password": password},
                       files={"file": ("pacote.zip", blob, "application/zip")})

def _wipe_tags() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import contact_tags, tags
    with get_engine().begin() as c:
        c.execute(delete(contact_tags))
        c.execute(delete(tags))

# ── gate de senha e autenticação ────────────────────────────────────────────

_ROUTES = [
    ("post", "/api/portability/exports", {"scopes": ["catalog"]}),
    ("post", "/api/portability/jobs/x/download", None),
    ("post", "/api/portability/imports/x/confirm", {}),
    ("post", "/api/portability/imports/x/recalculate", {}),
    ("post", "/api/portability/jobs/x/resume", {}),
    ("get", "/api/portability/jobs/x/errors", None),
    ("get", "/api/portability/jobs", None),
    ("get", "/api/portability/jobs/x", None),
    ("post", "/api/portability/jobs/x/cancel", None),
]

def _call(client, method, path, body, headers=None):
    return getattr(client, method)(path, headers=headers or {}, **({"json": body} if body is not None else {}))

@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_every_route_requires_a_panel_password(env, method, path, body):
    r = _call(env["client"], method, path, body)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert r.json()["error_code"] == "password_required"

def test_upload_requires_a_panel_password_and_leaves_nothing_behind(env):
    r = _upload(env["client"], {}, b"PK\x03\x04qualquer")
    assert r.status_code == 403 and r.json()["error_code"] == "password_required"
    assert jobs.list_recent() == []

@pytest.mark.parametrize("method,path,body", _ROUTES)
def test_with_a_password_but_no_token_the_middleware_answers_401(env, method, path, body):
    _set_panel_password()
    r = _call(env["client"], method, path, body)
    assert r.status_code == 401, (path, r.status_code)

def test_upload_with_a_password_but_no_token_is_401(env):
    _set_panel_password()
    assert _upload(env["client"], {}, b"x").status_code == 401
    assert jobs.list_recent() == []

# ── export ──────────────────────────────────────────────────────────────────

def test_export_runs_and_downloads_without_cache(env):
    c, h = env["client"], _set_panel_password()
    _seed_tags()
    job = _export(c, h)
    assert job["state"] == State.SUCCEEDED and job["has_package"] is True
    r = c.post(f"/api/portability/jobs/{job['id']}/download", headers=h)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None

def test_download_is_post_only(env):
    c, h = env["client"], _set_panel_password()
    job = _export(c, h)
    assert c.get(f"/api/portability/jobs/{job['id']}/download", headers=h).status_code in (404, 405)

def test_download_of_an_unfinished_or_unknown_job_is_refused(env):
    c, h = env["client"], _set_panel_password()
    assert c.post("/api/portability/jobs/nao-existe/download", headers=h).status_code == 404
    r = c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h)
    jid = r.json()["data"]["id"]                               # ainda ``queued``
    assert c.post(f"/api/portability/jobs/{jid}/download", headers=h).status_code == 409

def test_download_refuses_a_job_that_is_not_an_export_even_with_a_package_on_disk(env):
    """O pacote de uma VERIFICAÇÃO é o que o usuário enviou (pode ter segredos): não sai por aqui."""
    c, h = env["client"], _set_panel_password()
    _seed_tags()
    blob = _download(c, h, _export(c, h)["id"])
    dry = _upload(c, h, blob).json()["data"]
    _drain()
    assert jobs.get(dry["id"])["package_path"] and os.path.isfile(jobs.get(dry["id"])["package_path"])
    r = c.post(f"/api/portability/jobs/{dry['id']}/download", headers=h)
    assert r.status_code == 404 and not r.content.startswith(b"PK")

def test_download_refuses_an_export_that_has_not_succeeded_even_if_a_file_exists(env):
    """Export cancelado/falho/em andamento com arquivo parcial no disco não pode ser servido."""
    c, h = env["client"], _set_panel_password()
    job = jobs.create("export", params={"scopes": ["catalog"]}, state=State.RUNNING)
    pkg = env["tmp"] / "storages" / "portability" / job["id"] / "package.zip"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_bytes(b"PK-parcial")
    jobs.set_package_path(job["id"], str(pkg))
    for state in (State.RUNNING, State.FAILED, State.CANCELLED):
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import portability_jobs as J
        with get_engine().begin() as conn:
            conn.execute(update(J).where(J.c.id == job["id"]).values(state=state))
        r = c.post(f"/api/portability/jobs/{job['id']}/download", headers=h)
        assert r.status_code == 409 and r.content != b"PK-parcial", state

def test_the_api_never_exposes_params_or_the_server_path(env):
    c, h = env["client"], _set_panel_password()
    job = _export(c, h, include_secrets=False)
    body = json.dumps(c.get("/api/portability/jobs", headers=h).json())
    one = json.dumps(c.get(f"/api/portability/jobs/{job['id']}", headers=h).json())
    for text in (body, one):
        assert "package_path" not in text and "params" not in text
        assert str(env["tmp"]) not in text and "package.zip" not in text
    assert set(job) == {"id", "kind", "state", "progress", "report", "error", "created_at", "started_at",
                        "finished_at", "expires_at", "cancel_requested", "resume_stage", "source_job",
                        "interrupted", "needs_password", "has_package"}

def test_invalid_export_requests_are_400(env):
    c, h = env["client"], _set_panel_password()
    assert c.post("/api/portability/exports", json={"scopes": []}, headers=h).status_code == 400
    assert c.post("/api/portability/exports", json={"scopes": ["nada"]}, headers=h).status_code == 400
    assert c.post("/api/portability/exports", json={"scopes": ["catalog"], "include_secrets": True,
                                                    "password": "curta"}, headers=h).status_code == 400
    r = c.post("/api/portability/exports", json={"scopes": ["catalog"], "include_media": True}, headers=h)
    assert r.status_code == 400                               # mídia pede o escopo de conversas
    assert jobs.list_recent() == []

def test_only_one_job_at_a_time(env):
    c, h = env["client"], _set_panel_password()
    first = c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h)
    assert first.status_code == 202
    second = c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h)
    assert second.status_code == 409 and second.json()["error_code"] == "job_busy"
    assert len(jobs.list_recent()) == 1
    _drain()
    assert c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h).status_code == 202

@needs_crypto
def test_the_package_password_goes_to_memory_and_never_to_the_jobs_table(env):
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import portability_jobs as J
    c, h = env["client"], _set_panel_password()
    r = c.post("/api/portability/exports", json={"scopes": ["settings"], "include_secrets": True,
                                                 "password": PW}, headers=h)
    assert r.status_code == 202
    jid = r.json()["data"]["id"]
    assert jobs._PASSWORDS.get(jid) == PW
    with get_engine().connect() as conn:
        dump = json.dumps([[str(v) for v in row] for row in conn.execute(select(J)).all()])
    assert PW not in dump and PW not in r.text
    _drain()
    assert jid not in jobs._PASSWORDS                          # consumida

def test_export_with_secrets_fails_early_without_cryptography(env, monkeypatch):
    # O módulo que a ROTA usa (outro teste recarrega ``portability.crypto`` em ``sys.modules``, e
    # ``from portability import crypto`` poderia devolver uma cópia diferente da que a rota já importou).
    from server.routes import portability as routes
    crypto = routes.crypto
    c, h = env["client"], _set_panel_password()

    def _boom():
        raise crypto.CryptoUnavailableError("sem cryptography")
    monkeypatch.setattr(crypto, "_backend", _boom)
    r = c.post("/api/portability/exports", json={"scopes": ["settings"], "include_secrets": True,
                                                 "password": PW}, headers=h)
    assert r.status_code == 503 and r.json()["error_code"] == "crypto_unavailable"
    assert jobs.list_recent() == [] and jobs._PASSWORDS == {}

# ── upload ──────────────────────────────────────────────────────────────────

def test_a_file_that_is_not_a_zip_creates_no_job_and_no_folder(env):
    c, h = env["client"], _set_panel_password()
    r = _upload(c, h, b"isto nao e um zip" * 100)
    assert r.status_code == 400 and "zip" in r.json()["error"].lower()
    assert jobs.list_recent() == []
    assert list((env["tmp"] / "storages" / "portability").iterdir()) == []

def test_a_zip_without_manifest_is_refused(env):
    c, h = env["client"], _set_panel_password()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data/tags.ndjson", "")
    r = _upload(c, h, buf.getvalue())
    assert r.status_code == 400
    assert jobs.list_recent() == []
    assert list((env["tmp"] / "storages" / "portability").iterdir()) == []

def test_the_upload_cap_counts_real_bytes_and_removes_the_partial(env, monkeypatch):
    from server.routes import portability as routes
    c, h = env["client"], _set_panel_password()
    monkeypatch.setattr(routes, "MAX_IMPORT_BYTES", 1024)
    r = _upload(c, h, b"A" * 5000)
    assert r.status_code == 413
    assert jobs.list_recent() == []
    assert list((env["tmp"] / "storages" / "portability").iterdir()) == []

def test_the_declared_content_length_is_refused_before_the_body(env, monkeypatch):
    from server.routes import portability as routes
    c, h = env["client"], _set_panel_password()
    monkeypatch.setattr(routes, "MAX_IMPORT_BYTES", 10)
    r = c.post("/api/portability/imports", headers={**h, "Content-Length": str(10 + 64 * 1024 + 1)},
               content=b"x")
    assert r.status_code == 413

def test_upload_while_another_job_runs_is_409_and_keeps_nothing(env):
    c, h = env["client"], _set_panel_password()
    assert c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h).status_code == 202
    r = _upload(c, h, b"PK")
    assert r.status_code == 409
    assert len(jobs.list_recent()) == 1

# ── import: verificação → confirmação ───────────────────────────────────────

def _verified(env, h) -> tuple[str, bytes]:
    """Exporta tags, zera, envia o pacote e devolve ``(id da verificação, pacote)``."""
    c = env["client"]
    _seed_tags()
    blob = _download(c, h, _export(c, h)["id"])
    _wipe_tags()
    r = _upload(c, h, blob)
    assert r.status_code == 202, r.text
    dry = r.json()["data"]
    assert dry["kind"] == "import_dryrun"
    _drain()
    done = c.get(f"/api/portability/jobs/{dry['id']}", headers=h).json()["data"]
    assert done["state"] == State.SUCCEEDED, done
    return dry["id"], blob

def test_dry_run_writes_nothing_and_reports_what_would_happen(env):
    from db.repositories import tag_repo
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    assert tag_repo.get_all() == {}                            # a verificação nunca grava
    rep = c.get(f"/api/portability/jobs/{dry_id}", headers=h).json()["data"]["report"]
    assert rep["entities"]["tags"]["create"] == 2

def test_confirm_requires_the_review_acknowledgement(env):
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/confirm", json={}, headers=h)
    assert r.status_code == 400 and r.json()["error_code"] == "not_acknowledged"
    assert [j for j in jobs.list_recent() if j["kind"] == "import"] == []

def test_confirm_imports_once_and_a_second_confirm_is_refused(env):
    from db.repositories import tag_repo
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/confirm", json={"acknowledged": True}, headers=h)
    assert r.status_code == 202, r.text
    job = r.json()["data"]
    assert job["kind"] == "import" and job["source_job"] == dry_id
    _drain()
    done = c.get(f"/api/portability/jobs/{job['id']}", headers=h).json()["data"]
    assert done["state"] == State.SUCCEEDED
    assert sorted(tag_repo.get_all()) == ["lead", "vip"]
    again = c.post(f"/api/portability/imports/{dry_id}/confirm", json={"acknowledged": True}, headers=h)
    assert again.status_code == 409
    assert len([j for j in jobs.list_recent() if j["kind"] == "import"]) == 1

def _verified_with_code(env, h) -> tuple[str, bytes]:
    """Como ``_verified``, mas o pacote traz uma ferramenta de IA com código."""
    from db.repositories import tool_repo
    c = env["client"]
    tool_repo.save("rota_tool", description="d", code="x = 1\n", dependencies=[], enabled=True)
    blob = _download(c, h, _export(c, h, scopes=("agents",))["id"])
    tool_repo.delete("rota_tool")
    r = _upload(c, h, blob)
    assert r.status_code == 202, r.text
    dry = r.json()["data"]
    _drain()
    done = c.get(f"/api/portability/jobs/{dry['id']}", headers=h).json()["data"]
    assert done["state"] == State.SUCCEEDED, done
    assert done["report"]["code_tools_total"] == 1
    return dry["id"], blob

def test_confirm_refuses_a_package_with_code_unless_the_code_was_accepted(env):
    from db.repositories import tool_repo
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified_with_code(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/confirm", json={"acknowledged": True}, headers=h)
    assert r.status_code == 400 and r.json()["error_code"] == "code_not_accepted"
    assert [j for j in jobs.list_recent() if j["kind"] == "import"] == []     # nem criou o job
    assert tool_repo.get("rota_tool") is None
    r = c.post(f"/api/portability/imports/{dry_id}/confirm",
               json={"acknowledged": True, "accept_code": "talvez"}, headers=h)
    assert r.status_code == 400                                               # "talvez" não vale

def test_accepted_code_is_imported_disabled_through_the_route(env):
    from db.repositories import tool_repo
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified_with_code(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/confirm",
               json={"acknowledged": True, "accept_code": True}, headers=h)
    assert r.status_code == 202, r.text
    assert "accept_code" not in r.json()["data"] and "params" not in r.json()["data"]
    _drain()
    t = tool_repo.get("rota_tool")
    assert t is not None and t["enabled"] is False and tool_repo.list_enabled() == []

def test_a_package_without_code_does_not_need_accept_code(env):
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/confirm", json={"acknowledged": True}, headers=h)
    assert r.status_code == 202, r.text

def test_confirm_refuses_a_verification_that_has_not_finished(env):
    c, h = env["client"], _set_panel_password()
    _seed_tags()
    blob = _download(c, h, _export(c, h)["id"])
    dry = _upload(c, h, blob).json()["data"]                   # ainda na fila
    r = c.post(f"/api/portability/imports/{dry['id']}/confirm", json={"acknowledged": True}, headers=h)
    assert r.status_code == 409

def test_confirm_refuses_an_export_job_and_an_unknown_one(env):
    c, h = env["client"], _set_panel_password()
    exp = _export(c, h)
    assert c.post(f"/api/portability/imports/{exp['id']}/confirm", json={"acknowledged": True},
                  headers=h).status_code == 404
    assert c.post("/api/portability/imports/nao-existe/confirm", json={"acknowledged": True},
                  headers=h).status_code == 404

def test_confirm_rejects_an_invalid_strategy(env):
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    for strategy in ({"tags": "apagar_tudo"}, {"contacts": "merge"}, "texto"):
        r = c.post(f"/api/portability/imports/{dry_id}/confirm",
                   json={"acknowledged": True, "strategy": strategy}, headers=h)
        assert r.status_code == 400, strategy
    assert [j for j in jobs.list_recent() if j["kind"] == "import"] == []

def test_recalculate_reuses_the_same_job_and_clears_the_old_report(env):
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    r = c.post(f"/api/portability/imports/{dry_id}/recalculate",
               json={"strategy": {"tags": "skip"}}, headers=h)
    assert r.status_code == 202 and r.json()["data"]["id"] == dry_id
    _drain()
    rep = c.get(f"/api/portability/jobs/{dry_id}", headers=h).json()["data"]["report"]
    # destino zerado: nada para "pular", então a estratégia nova aparece no relatório e as tags seguem como criação
    assert rep["strategy"] == {"tags": "skip"} and rep["entities"]["tags"]["create"] == 2
    # o export e a verificação; recalcular não criou um terceiro job
    assert sorted(j["kind"] for j in jobs.list_recent()) == ["export", "import_dryrun"]

def test_recalculate_is_refused_once_the_verification_became_an_import(env):
    c, h = env["client"], _set_panel_password()
    dry_id, _ = _verified(env, h)
    assert c.post(f"/api/portability/imports/{dry_id}/confirm", json={"acknowledged": True},
                  headers=h).status_code == 202
    _drain()
    assert c.post(f"/api/portability/imports/{dry_id}/recalculate", json={}, headers=h).status_code == 409

@needs_crypto
def test_a_package_with_secrets_asks_for_the_password_and_keeps_it_out_of_the_table(env):
    from db.repositories import config_repo
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import portability_jobs as J
    c, h = env["client"], _set_panel_password()
    config_repo.set_many({"openrouter_api_key": "sk-CHAVE-DE-TESTE-9999"})
    exp = _export(c, h, scopes=("settings",), include_secrets=True, password=PW)
    blob = _download(c, h, exp["id"])
    config_repo.set_many({"openrouter_api_key": ""})
    dry = _upload(c, h, blob, password=PW).json()["data"]
    _drain()
    done = c.get(f"/api/portability/jobs/{dry['id']}", headers=h).json()["data"]
    assert done["state"] == State.SUCCEEDED and done["report"]["manifest"]["secrets"] == "ok"
    no_pw = c.post(f"/api/portability/imports/{dry['id']}/confirm", json={"acknowledged": True}, headers=h)
    assert no_pw.status_code == 400 and "senha" in no_pw.json()["error"].lower()
    ok = c.post(f"/api/portability/imports/{dry['id']}/confirm",
                json={"acknowledged": True, "password": PW}, headers=h)
    assert ok.status_code == 202 and PW not in ok.text
    with get_engine().connect() as conn:
        dump = json.dumps([[str(v) for v in row] for row in conn.execute(select(J)).all()])
    assert PW not in dump
    _drain()
    assert config_repo.get("openrouter_api_key") == "sk-CHAVE-DE-TESTE-9999"

# ── cancelar, listar, erros, retomar ────────────────────────────────────────

def test_cancel_a_queued_job_drops_its_password(env):
    c, h = env["client"], _set_panel_password()
    r = c.post("/api/portability/exports", json={"scopes": ["catalog"]}, headers=h)
    jid = r.json()["data"]["id"]
    jobs.stash_password(jid, PW)
    out = c.post(f"/api/portability/jobs/{jid}/cancel", headers=h)
    assert out.status_code == 200 and out.json()["data"]["state"] == State.CANCELLED
    assert jid not in jobs._PASSWORDS
    assert c.post(f"/api/portability/jobs/{jid}/cancel", headers=h).status_code == 409
    assert c.post("/api/portability/jobs/nao-existe/cancel", headers=h).status_code == 404

def test_list_filters_by_kind_and_rejects_an_unknown_kind(env):
    c, h = env["client"], _set_panel_password()
    _export(c, h)
    got = c.get("/api/portability/jobs?kind=export", headers=h).json()["data"]
    assert [j["kind"] for j in got] == ["export"]
    assert c.get("/api/portability/jobs?kind=import", headers=h).json()["data"] == []
    assert c.get("/api/portability/jobs?kind=rm_rf", headers=h).status_code == 400

def test_job_errors_are_paged_with_totals(env):
    c, h = env["client"], _set_panel_password()
    job = jobs.create("import", params={})
    sink = jobs.ErrorSink(job["id"])
    for i in range(3):
        sink.add("tags", i, "invalid_row", {"identifier": f"t{i}"})
    sink.flush()
    jobs.set_report(job["id"], {"errors": sink.summary()})
    r = c.get(f"/api/portability/jobs/{job['id']}/errors?limit=2", headers=h).json()["data"]
    assert len(r["items"]) == 2 and r["totals"]["tags"]["total"] == 3 and r["next_after"]
    rest = c.get(f"/api/portability/jobs/{job['id']}/errors?after={r['next_after']}", headers=h).json()["data"]
    assert len(rest["items"]) == 1
    assert c.get("/api/portability/jobs/nao-existe/errors", headers=h).status_code == 404

def test_resume_only_works_on_an_interrupted_import(env):
    c, h = env["client"], _set_panel_password()
    plain = jobs.create("import", params={})
    assert c.post(f"/api/portability/jobs/{plain['id']}/resume", json={}, headers=h).status_code == 409
    assert c.post("/api/portability/jobs/nao-existe/resume", json={}, headers=h).status_code == 404

def test_resume_of_an_interrupted_import_requires_the_password_again(env):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import portability_jobs as J
    c, h = env["client"], _set_panel_password()
    pkg = env["tmp"] / "storages" / "portability" / "j" / "package.zip"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_bytes(b"x")
    job = jobs.create("import", params={"has_password": True, "package_path": str(pkg)},
                      state=State.AWAITING_RESTART)
    with get_engine().begin() as conn:
        conn.execute(update(J).where(J.c.id == job["id"]).values(interrupted=1))
    r = c.post(f"/api/portability/jobs/{job['id']}/resume", json={}, headers=h)
    assert r.status_code == 400 and "senha" in r.json()["error"].lower()
    ok = c.post(f"/api/portability/jobs/{job['id']}/resume", json={"password": PW}, headers=h)
    assert ok.status_code == 202
    assert jobs._PASSWORDS.get(job["id"]) == PW

# ── o pacote não é servido por nenhum mount estático ────────────────────────

def test_no_static_mount_serves_the_packages_folder(env):
    c, h = env["client"], _set_panel_password()
    job = _export(c, h)
    for url in (f"/storages/portability/{job['id']}/package.zip",
                f"/statics/../storages/portability/{job['id']}/package.zip",
                f"/static/../storages/portability/{job['id']}/package.zip",
                f"/statics/storages/portability/{job['id']}/package.zip"):
        r = c.get(url)
        assert r.status_code != 200 or r.headers.get("content-type", "").startswith("text/html"), url
        assert not r.content.startswith(b"PK"), url

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
