"""A rota ``POST /api/plugins/import`` (o "Importar (.zip)" da tela de plugins) pelo COMPORTAMENTO.

Antes ela extraía com ``extractall`` e sem teto: um ``.zip`` de ~300 KB virava 300 MB em disco. Agora usa
``plugins.package`` (zip-slip, link simbólico e bytes REAIS contados). Estes testes sobem o app de verdade e
mandam o zip pela rota, para pegar uma regressão que um teste de texto no código-fonte não pegaria.

O app lê ``settings.data_dir`` (a raiz do projeto): a pasta de dados é trocada por uma DESCARTÁVEL antes de
criá-lo, para nunca tocar em ``storages/plugins`` de verdade.

Roda direto (``python tests/test_plugin_import_route.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import io
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

from db import init_db  # noqa: E402

MANIFEST = "id: demo\nname: Demo\nversion: 1.0.0\n"

@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="wb_plug_route_"))
    init_db(tmp / "whatsbot.db")
    from config import settings as settings_mod
    real_get = settings_mod.get_data_dir
    settings_mod.get_data_dir = lambda: tmp                       # o app cria storages/ aqui, não no projeto
    try:
        from agent.handler import AgentHandler
        from contextlib import asynccontextmanager
        from server.app import create_app
        from starlette.testclient import TestClient
        settings = settings_mod.Settings()
        assert settings.data_dir == tmp
        app = create_app(settings=settings, gowa_manager=MagicMock(), gowa_client=MagicMock(),
                         agent_handler=AgentHandler(api_key="x", system_prompt="t", max_context_messages=10,
                                                    model="openai/gpt-4o-mini"))

        @asynccontextmanager
        async def _noop(app):
            yield
        app.router.lifespan_context = _noop
        yield TestClient(app, raise_server_exceptions=False), tmp / "storages" / "plugins"
    finally:
        settings_mod.get_data_dir = real_get
        shutil.rmtree(tmp, ignore_errors=True)

def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    return buf.getvalue()

def _post(client, blob: bytes):
    return client.post("/api/plugins/import", files={"file": ("p.zip", blob, "application/zip")})

@pytest.fixture(autouse=True)
def _clean(app_env):
    _, pdir = app_env
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import plugins
    for child in list(pdir.iterdir()):
        shutil.rmtree(child, ignore_errors=True)
    with get_engine().begin() as c:
        c.execute(delete(plugins))
    yield

def test_the_app_is_pointed_at_the_throwaway_folder(app_env):
    _, pdir = app_env
    assert str(pdir).startswith(tempfile.gettempdir()) and "wb_plug_route_" in str(pdir)

def test_a_good_plugin_is_installed_disabled(app_env):
    client, pdir = app_env
    r = _post(client, _zip({"plugin.yaml": MANIFEST, "__init__.py": "x = 1\n"}))
    assert r.status_code == 200 and r.json()["data"] == {"id": "demo", "version": "1.0.0", "enabled": False}
    assert (pdir / "demo" / "__init__.py").read_text() == "x = 1\n"
    assert [p.name for p in pdir.iterdir()] == ["demo"]            # sem rascunho

def test_a_zip_bomb_is_refused_by_the_route_and_leaves_nothing(app_env, monkeypatch):
    """Um ``.zip`` pequeno que descomprime em dezenas de MB: recusado, sem pasta nem linha no banco."""
    client, pdir = app_env
    from plugins import package
    monkeypatch.setattr(package, "MAX_MEMBER_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(package, "MAX_UNCOMPRESSED_BYTES", 4 * 1024 * 1024)
    blob = _zip({"plugin.yaml": MANIFEST, "lixo.bin": b"\0" * (30 * 1024 * 1024)})
    assert len(blob) < 100 * 1024                                   # é mesmo uma bomba
    r = _post(client, blob)
    assert r.status_code == 400 and r.json()["ok"] is False and "grande demais" in r.json()["error"]
    assert list(pdir.iterdir()) == []
    from db.repositories import plugin_repo
    assert plugin_repo.get("demo") is None

@pytest.mark.parametrize("name", ["../evil.py", "/etc/evil.py", "a/../../evil.py", "a\\..\\evil.py"])
def test_zip_slip_is_refused_by_the_route(app_env, name):
    client, pdir = app_env
    r = _post(client, _zip({"plugin.yaml": MANIFEST, name: "x"}))
    assert r.json()["ok"] is False
    assert list(pdir.iterdir()) == [] and not (pdir.parent / "evil.py").exists()

def test_an_already_installed_plugin_is_refused(app_env):
    client, pdir = app_env
    assert _post(client, _zip({"plugin.yaml": MANIFEST})).json()["ok"] is True
    again = _post(client, _zip({"plugin.yaml": MANIFEST, "novo.py": "1"}))
    assert again.json()["ok"] is False and "já instalado" in again.json()["error"]
    assert not (pdir / "demo" / "novo.py").exists()

def test_garbage_is_refused(app_env):
    client, pdir = app_env
    assert _post(client, b"isto nao e um zip").json()["ok"] is False
    assert list(pdir.iterdir()) == []

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
