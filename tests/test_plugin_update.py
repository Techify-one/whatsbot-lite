"""Plugin UPDATE — atualizar um plugin sem apagar suas tabelas/dados (Community).

Espelha o teste do whatsbot-pro. Prova a garantia central da feature "atualizar
plugin": trocar o código por um novo .zip e rodar SÓ as migrations novas,
preservando as linhas já gravadas (ao contrário de Deletar + Importar, que dropa
as tabelas do plugin).

Estilo script (como ``tests/test_endpoints.py``): o engine é inicializado no topo
do módulo, num SQLite temporário, ANTES de importar os repositórios. Pode rodar
direto (``python tests/test_plugin_update.py``) ou ser coletado pelo pytest.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# Project root importável (espelha test_endpoints.py).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Engine num temp dir ANTES de qualquer import de repo/tabela.
_tmpdir = tempfile.mkdtemp(prefix="whatsbot_test_upd_")
_db_path = Path(_tmpdir) / "whatsbot.db"

from db import init_db, init_engine  # noqa: E402

_test_url = os.environ.get("WHATSBOT_TEST_DB_URL", "").strip()
if _test_url:
    init_engine(_test_url)
    from db.connection import _run_alembic_upgrade  # noqa: E402
    _run_alembic_upgrade()
else:
    init_db(_db_path)

from sqlalchemy import text as sa_text  # noqa: E402

from db.engine import get_engine  # noqa: E402
from db.repositories import plugin_repo  # noqa: E402
from plugins.loader import _recover_interrupted_updates  # noqa: E402
from plugins.manifest import load_manifest  # noqa: E402
from plugins.migrator import run_pending_migrations  # noqa: E402
from server.routes.plugins import (  # noqa: E402
    _read_zip_manifest,
    _reject_unsafe_zip_paths,
    _swap_plugin_dir,
    _version_key,
)


def _write_plugin(root: Path, pid: str, version: str,
                  migrations: dict, body: str = "") -> Path:
    d = root / pid
    (d / "migrations").mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(
        f"id: {pid}\n"
        f"name: Demo {pid}\n"
        f"version: {version}\n"
        f'whatsbot_api_version: ">=1.0,<2.0"\n'
        f"migrations: migrations\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(body, encoding="utf-8")
    for fname, sql in migrations.items():
        (d / "migrations" / fname).write_text(sql, encoding="utf-8")
    return d


def _bytes_of(plugin_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in plugin_dir.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(plugin_dir).as_posix())
    return buf.getvalue()


def _zip_of(plugin_dir: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(_bytes_of(plugin_dir)))


def test_update_preserves_table_data():
    """A atualização mantém as linhas existentes e roda só as migrations novas."""
    pid = "demoupd"
    tbl = f"plugin_{pid}_items"
    work = Path(tempfile.mkdtemp(prefix="upd_work_"))
    plugins_dir = work / "plugins"
    plugins_dir.mkdir()
    eng = get_engine()

    v1 = _write_plugin(plugins_dir, pid, "1.0.0", {
        "001_init.sql": (
            f"CREATE TABLE {tbl} "
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);"
        ),
    }, body="# v1\n")
    plugin_repo.upsert(pid, "1.0.0")
    assert run_pending_migrations(load_manifest(v1), v1) == [1]
    (v1 / "uploads").mkdir()
    (v1 / "uploads" / "user-file.txt").write_text("keep-file", encoding="utf-8")
    with eng.begin() as conn:
        conn.execute(sa_text(f"INSERT INTO {tbl} (name) VALUES ('keep-me')"))

    try:
        src2 = work / "src2"
        src2.mkdir()
        v2 = _write_plugin(src2, pid, "2.0.0", {
            "001_init.sql": (
                f"CREATE TABLE {tbl} "
                f"(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);"
            ),
            "002_add_col.sql": f"ALTER TABLE {tbl} ADD COLUMN note TEXT;",
        }, body="# v2\n")

        # Núcleo do endpoint /update: troca o código, sobe versão, roda migrations
        # pendentes. NUNCA chama drop_plugin_tables.
        _swap_plugin_dir(_zip_of(v2), plugins_dir, pid)
        plugin_repo.upsert(pid, "2.0.0")
        applied = run_pending_migrations(load_manifest(plugins_dir / pid),
                                         plugins_dir / pid)

        assert applied == [2], "apenas a migration nova (002) deve rodar"
        with eng.connect() as conn:
            rows = conn.execute(sa_text(f"SELECT name, note FROM {tbl}")).all()
        assert rows == [("keep-me", None)], "linha preservada + coluna nova"
        assert plugin_repo.get(pid)["version"] == "2.0.0"
        assert "# v2" in (plugins_dir / pid / "__init__.py").read_text(encoding="utf-8")
        assert (plugins_dir / pid / "uploads" / "user-file.txt").read_text() == "keep-file"
    finally:
        plugin_repo.drop_plugin_tables(pid)
        plugin_repo.delete(pid)


def test_version_key_orders_and_flags_downgrade():
    assert _version_key("2.0.0") > _version_key("1.9.9")
    assert _version_key("1.0.10") > _version_key("1.0.2")
    assert _version_key("1.2.0-rc1") == _version_key("1.2.0")
    assert _version_key("1.0.0") < _version_key("1.1.0")


def test_version_key_strips_v_prefix():
    assert _version_key("v2.0.0") == _version_key("2.0.0")
    assert _version_key("V2.0.0") > _version_key("v1.9.0")


def test_migration_batch_rolls_back_bookkeeping_on_failure():
    """A later bad migration cannot mark earlier files in the batch as applied."""
    pid = "demotxn"
    tbl = f"plugin_{pid}_items"
    work = Path(tempfile.mkdtemp(prefix="upd_txn_"))
    plugin = _write_plugin(work, pid, "1.0.0", {
        "001_init.sql": f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);",
    })
    plugin_repo.upsert(pid, "1.0.0")
    assert run_pending_migrations(load_manifest(plugin), plugin) == [1]
    (plugin / "migrations" / "002_insert.sql").write_text(
        f"INSERT INTO {tbl} (name) VALUES ('must-rollback');", encoding="utf-8",
    )
    (plugin / "migrations" / "003_broken.sql").write_text(
        f"INSERT INTO plugin_{pid}_missing (name) VALUES ('boom');", encoding="utf-8",
    )
    try:
        run_pending_migrations(load_manifest(plugin), plugin)
    except RuntimeError:
        pass
    else:
        raise AssertionError("broken migration should fail")
    assert plugin_repo.applied_migrations(pid) == {1}
    with get_engine().connect() as conn:
        count = conn.execute(sa_text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
    assert count == 0
    plugin_repo.drop_plugin_tables(pid)
    plugin_repo.delete(pid)


def _put_dir(parent: Path, name: str, marker_value: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "marker.txt").write_text(marker_value, encoding="utf-8")
    return d


def test_recover_promotes_staging_when_target_missing():
    pdir = Path(tempfile.mkdtemp(prefix="rec_a_")) / "plugins"
    pdir.mkdir()
    _put_dir(pdir, ".demo.update-staging", "new")
    _put_dir(pdir, ".demo.update-backup", "old")
    _recover_interrupted_updates(pdir)
    assert (pdir / "demo" / "marker.txt").read_text() == "new"
    assert not (pdir / ".demo.update-staging").exists()
    assert not (pdir / ".demo.update-backup").exists()


def test_recover_restores_backup_when_no_staging():
    pdir = Path(tempfile.mkdtemp(prefix="rec_b_")) / "plugins"
    pdir.mkdir()
    _put_dir(pdir, ".demo.update-backup", "old")
    _recover_interrupted_updates(pdir)
    assert (pdir / "demo" / "marker.txt").read_text() == "old"
    assert not (pdir / ".demo.update-backup").exists()


def test_recover_clears_leftovers_when_target_present():
    pdir = Path(tempfile.mkdtemp(prefix="rec_c_")) / "plugins"
    pdir.mkdir()
    _put_dir(pdir, "demo", "live")
    _put_dir(pdir, ".demo.update-staging", "x")
    _put_dir(pdir, ".demo.update-backup", "y")
    _recover_interrupted_updates(pdir)
    assert (pdir / "demo" / "marker.txt").read_text() == "live"
    assert not (pdir / ".demo.update-staging").exists()
    assert not (pdir / ".demo.update-backup").exists()


def test_read_zip_manifest_extracts_id():
    work = Path(tempfile.mkdtemp(prefix="upd_man_"))
    p = _write_plugin(work, "alpha", "1.0.0", {})
    _zf, meta, pid = _read_zip_manifest(_bytes_of(p))
    assert pid == "alpha"
    assert meta["version"] == "1.0.0"


def test_reject_unsafe_zip_paths():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", "x")
    try:
        _reject_unsafe_zip_paths(zipfile.ZipFile(io.BytesIO(buf.getvalue())))
    except ValueError:
        return
    raise AssertionError("deveria rejeitar caminho com '..'")


def test_whatsbot_self_update_schedules_restart():
    """A atualização do core precisa carregar as novas rotas sem ação manual."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import FastAPI
    import server.routes.update as update_routes

    original_fetch = update_routes._fetch_latest_release
    original_perform = update_routes._perform_update
    original_restart = update_routes.schedule_restart
    original_to_thread = asyncio.to_thread
    restart_reasons = []
    try:
        async def inline_to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        asyncio.to_thread = inline_to_thread
        update_routes._fetch_latest_release = lambda: {
            "tag": "v9.9.9", "version": "9.9.9", "description": "", "url": "",
        }
        update_routes._perform_update = lambda _root, _tag: {
            "version": "9.9.9", "changelog": [], "files_updated": 12,
            "message": "Atualizado para v9.9.9 — 12 arquivos atualizados.",
        }
        update_routes.schedule_restart = lambda reason="": restart_reasons.append(reason)

        app = FastAPI()
        deps = SimpleNamespace(settings=SimpleNamespace(data_dir=Path(_tmpdir)))
        update_routes.register_routes(app, deps)
        endpoint = next(
            route.endpoint for route in app.routes
            if getattr(route, "path", None) == "/api/update"
            and "POST" in getattr(route, "methods", set())
        )
        response = asyncio.run(endpoint())
        assert response["ok"] is True
        assert response["data"]["restarting"] is True
        assert "reiniciado automaticamente" in response["data"]["message"]
        assert restart_reasons == ["WhatsBot-Lite updated to v9.9.9"]
    finally:
        update_routes._fetch_latest_release = original_fetch
        update_routes._perform_update = original_perform
        update_routes.schedule_restart = original_restart
        asyncio.to_thread = original_to_thread


def test_whatsbot_update_notification_choices():
    """Skip is per release; disabling notifications suppresses every release."""
    import asyncio
    from fastapi import FastAPI
    import server.routes.update as update_routes

    root = Path(tempfile.mkdtemp(prefix="whatsbot_release_notice_"))
    (root / "WHATSBOT_VERSION").write_text(json.dumps({
        "version": "1.0.0",
        "changelog": [{"version": "1.0.0", "description": "Atual"}],
    }), encoding="utf-8")

    class FakeSettings(dict):
        data_dir = root

        def save(self):
            return None

    settings = FakeSettings(
        whatsbot_update_notifications_enabled=True,
        whatsbot_skipped_version="",
    )
    latest = {
        "tag": "v1.1.0", "version": "1.1.0", "description": "- melhoria",
        "url": "https://example.test/v1.1.0",
    }
    original_fetch = update_routes._fetch_latest_release
    original_to_thread = asyncio.to_thread
    try:
        async def inline_to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        asyncio.to_thread = inline_to_thread
        update_routes._fetch_latest_release = lambda _force=False: dict(latest)
        app = FastAPI()
        update_routes.register_routes(app, type("Deps", (), {"settings": settings})())

        def endpoint(path, method):
            return next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
            )

        check_update = endpoint("/api/update/check", "GET")
        skip_version = endpoint("/api/update/skip-version", "POST")

        available = asyncio.run(check_update(False))["data"]
        assert available["update_available"] is True
        assert available["should_notify"] is True

        skipped = asyncio.run(skip_version({"version": "1.1.0"}))
        assert skipped["ok"] is True
        assert asyncio.run(check_update(False))["data"]["should_notify"] is False

        latest.update(tag="v1.2.0", version="1.2.0", description="- outra melhoria")
        assert asyncio.run(check_update(False))["data"]["should_notify"] is True

        settings["whatsbot_update_notifications_enabled"] = False
        disabled = asyncio.run(check_update(False))["data"]
        assert disabled["notifications_enabled"] is False
        assert disabled["should_notify"] is False

        settings["whatsbot_update_notifications_enabled"] = True
        latest.update(tag="v0.9.0", version="0.9.0")
        older = asyncio.run(check_update(False))["data"]
        assert older["update_available"] is False
        assert older["should_notify"] is False
    finally:
        update_routes._fetch_latest_release = original_fetch
        asyncio.to_thread = original_to_thread


def test_release_lookup_falls_back_without_github_api():
    """The public release page still yields a changelog after API rate limits."""
    import server.routes.update as update_routes

    class FakeResponse:
        def __init__(self, url, payload=b""):
            self._url = url
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self._url

        def read(self):
            return self._payload

    version_payload = json.dumps({
        "version": "1.2.3",
        "changelog": [{"version": "1.2.3", "description": "- correção testada"}],
    }).encode()
    original_urlopen = update_routes.urllib.request.urlopen
    try:
        def fake_urlopen(request, timeout=0):
            url = request.full_url
            if url.endswith("/releases/latest"):
                return FakeResponse("https://github.com/Techify-one/whatsbot-lite/releases/tag/v1.2.3")
            if "/v1.2.3/WHATSBOT_VERSION" in url:
                return FakeResponse(url, version_payload)
            raise AssertionError(f"URL inesperada: {url}")

        update_routes.urllib.request.urlopen = fake_urlopen
        release = update_routes._fetch_release_without_api()
        assert release["tag"] == "v1.2.3"
        assert release["version"] == "1.2.3"
        assert release["description"] == "- correção testada"
    finally:
        update_routes.urllib.request.urlopen = original_urlopen


def main() -> int:
    tests = [
        test_update_preserves_table_data,
        test_version_key_orders_and_flags_downgrade,
        test_version_key_strips_v_prefix,
        test_migration_batch_rolls_back_bookkeeping_on_failure,
        test_read_zip_manifest_extracts_id,
        test_reject_unsafe_zip_paths,
        test_recover_promotes_staging_when_target_missing,
        test_recover_restores_backup_when_no_staging,
        test_recover_clears_leftovers_when_target_present,
        test_whatsbot_self_update_schedules_restart,
        test_whatsbot_update_notification_choices,
        test_release_lookup_falls_back_without_github_api,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passaram")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
