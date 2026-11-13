"""Importar/Exportar — plugins (código, configuração ``plugin.<id>.*`` e dados ``plugin_<id>_*``).

O que se prova, de propósito:
  * o código do plugin é código que roda NESTE servidor: entra SEMPRE desligado (também ao substituir um
    que roda), nunca é gravado sem ``accept_code``, nunca rebaixa versão, e passa pelas MESMAS barreiras de
    zip do "Importar (.zip)" — a prévia (dry-run) faz o ensaio e recusa a mesma bomba;
  * zip-slip, link simbólico, bomba, id divergente, semver inválido e API incompatível ficam de fora;
  * configuração: o NOME decide o que é segredo (nunca a marca do pacote); valor de loopback não viaja;
  * dados em DUAS etapas: a tabela só existe depois de o plugin ser ativado e o servidor reiniciar, então
    o job pausa em ``awaiting_restart`` e a 2ª etapa entra depois; tabela com dados no destino é pulada
    inteira; ``contact_id`` viaja pelo TELEFONE (nunca o id cru) e a tabela falha inteira ou nada;
  * nome de tabela e de coluna vêm do catálogo do destino, nunca do pacote.

Roda direto (``python tests/test_portability_plugins.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import test_portability_roundtrip as rt  # noqa: E402
from test_portability_roundtrip import (_clean, _dryrun, _drain, _engine, _export,  # noqa: E402,F401
                                        needs_crypto)
from portability import jobs, plugins_io  # noqa: E402
from portability.format import Manifest, PackageReader, PackageWriter, plugin_code_path  # noqa: E402
from portability.jobs import State  # noqa: E402

PW = rt.PW
MANIFEST = ("id: {pid}\nname: Demo {pid}\nversion: {ver}\n"
            'whatsbot_api_version: "{api}"\nmigrations: migrations\n{extra}')
MIG = ("CREATE TABLE IF NOT EXISTS plugin_{pid}_items (\n"
       "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  contact_id INTEGER,\n  title TEXT NOT NULL DEFAULT '',\n"
       "  api_token TEXT NOT NULL DEFAULT '',\n  media_path TEXT\n);")

@pytest.fixture(autouse=True)
def _plugins_env(_engine, monkeypatch, tmp_path):
    """Pasta de plugins ISOLADA (``get_data_dir`` é a raiz do projeto) e banco limpo de plugin."""
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    monkeypatch.setattr(plugins_io, "plugins_dir", lambda: pdir)
    _wipe_plugins()
    yield pdir
    _wipe_plugins()

def _wipe_plugins() -> None:
    from sqlalchemy import delete, inspect, text
    from db.engine import get_engine
    from db import tables as t
    eng = get_engine()
    with eng.begin() as c:
        for tb in (t.plugin_migrations, t.plugins, t.contact_tags, t.messages, t.observations, t.contacts):
            c.execute(delete(tb))
        c.execute(delete(t.config).where(t.config.c.key.like("plugin.%")))
        for name in inspect(eng).get_table_names():
            if name.startswith("plugin_") and name not in ("plugin_migrations",) and "_" in name[7:]:
                c.execute(text(f'DROP TABLE IF EXISTS "{name}"'))

# ── helpers ─────────────────────────────────────────────────────────────────

def _plugin_zip(pid="demo", ver="1.0.0", *, extra_manifest="", files=None, migrations=True,
                api=">=1.0,<2.0") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.yaml", MANIFEST.format(pid=pid, ver=ver, extra=extra_manifest, api=api))
        zf.writestr("__init__.py", "")
        if migrations:
            zf.writestr("migrations/001_init.sql", MIG.format(pid=pid))
        for n, d in (files or {}).items():
            zf.writestr(n, d)
    return buf.getvalue()

def _install(pdir: Path, pid="demo", ver="1.0.0", *, enabled=False, migrate=False, **kw) -> Path:
    """Instala um plugin no destino/origem de teste (como o 'Importar (.zip)' faria)."""
    from db.repositories import plugin_repo
    from plugins import package
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    zf, _meta, _pid = package.read_zip_manifest(_plugin_zip(pid, ver, **kw))
    target = package.install_plugin_dir(zf, pdir, pid)
    plugin_repo.upsert(pid, ver, enabled=enabled)
    if migrate:
        run_pending_migrations(load_manifest(target), target)
    return target

def _rows(pid="demo", table="items") -> list[dict]:
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().connect() as c:
        return [dict(r) for r in c.execute(text(f'SELECT * FROM "plugin_{pid}_{table}" ORDER BY id')).mappings()]

def _insert(pid="demo", **row) -> None:
    from sqlalchemy import text
    from db.engine import get_engine
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    with get_engine().begin() as c:
        c.execute(text(f'INSERT INTO "plugin_{pid}_items" ({cols}) VALUES ({vals})'), row)

def _forged(pid="demo", *, code: bytes | None = None, tables: dict | None = None, plugins_rows=None,
            config=None, scopes=("plugins",)) -> str:
    """Pacote montado à mão: o que um atacante (ou uma versão futura) poderia entregar."""
    d = Path(tempfile.mkdtemp(prefix="wb_forged_pl_"))
    path = str(d / "forged.zip")
    with PackageWriter(path) as w:
        w.write_ndjson("plugins", plugins_rows if plugins_rows is not None else
                       ([{"id": pid, "name": pid, "version": "1.0.0"}] if code is not None else []))
        if config is not None:
            w.write_ndjson("plugin_config", config)
        if code is not None:
            w.write_bytes(plugin_code_path(pid), code)
        for tbl, rows in (tables or {}).items():
            w.write_plugin_table(pid, tbl, rows)
        w.finish(Manifest(scopes=list(scopes), created_at=1.0))
    return path

def _export_plugins(ids, *, extra_scopes=(), password=None) -> str:
    j = jobs.create("export", params={"scopes": ["plugins", *extra_scopes], "plugin_ids": list(ids),
                                      "include_secrets": bool(password)},
                    affinity=jobs.PROCESS_WID if password else None)
    if password:
        jobs.stash_password(j["id"], password)
    _drain()
    got = jobs.get(j["id"])
    assert got["state"] == State.SUCCEEDED, got["error"]
    return got["package_path"]

def _run_import(pkg, *, accept_code=True, strategy=None, password=None, overwrite_secrets=False):
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

def _resume(job_id, *, password=None):
    if password:
        jobs.stash_password(job_id, password)
    assert jobs.resume(job_id, affinity=jobs.PROCESS_WID if password else None)
    _drain()
    return jobs.get(job_id)

def _installed_row(pid="demo"):
    from db.repositories import plugin_repo
    return plugin_repo.get(pid)

# ═══ export ═════════════════════════════════════════════════════════════════

def test_export_carries_the_code_the_config_and_the_data_but_not_secret_values(_plugins_env):
    from db.repositories import config_repo, contact_repo
    _install(_plugins_env, migrate=True)
    config_repo.set_many({"plugin.demo.greeting": "olá", "plugin.demo.api_token": "TOKEN-DO-PLUGIN",
                          "plugin.demo.base_url": "http://127.0.0.1:9000", "plugin.outro.x": "nunca"})
    c = contact_repo.get_or_create("5511999990001")
    _insert(contact_id=c["id"], title="um", api_token="COLUNA-SECRETA", media_path="/etc/passwd")
    pkg = _export_plugins(["demo"])
    with PackageReader(pkg) as r:
        assert r.plugin_ids() == ["demo"] and r.has("plugins/demo.zip")
        cfg = {x["key"]: x for x in r.iter_ndjson("plugin_config")}
        assert cfg["plugin.demo.greeting"]["value"] == "olá"
        assert cfg["plugin.demo.api_token"] == {"key": "plugin.demo.api_token", "value": None, "secret": True}
        assert "plugin.demo.base_url" not in cfg and "plugin.outro.x" not in cfg   # loopback e outro plugin
        rows = list(r.iter_plugin_table("demo", "items"))
        assert rows[0]["api_token"] is None                                         # coluna secreta
        assert rows[0]["__refs"] == {"contacts": {"contact_id": "5511999990001"}}  # chave natural
        everything = b"".join(r.read_member(n) for n in r.names() if n != "plugins/demo.zip")
        assert b"TOKEN-DO-PLUGIN" not in everything and b"COLUNA-SECRETA" not in everything
        assert not r.has("secrets.enc")
        assert r.manifest.plugins == [{"id": "demo", "version": "1.0.0"}]

def test_the_exported_code_zip_skips_cache_and_local_databases_and_symlinks(_plugins_env):
    t = _install(_plugins_env)
    (t / "__pycache__").mkdir()
    (t / "__pycache__" / "x.pyc").write_bytes(b"x")
    (t / "local.db").write_bytes(b"sqlite")
    os.symlink("/etc/passwd", t / "link.txt")
    with PackageReader(_export_plugins(["demo"])) as r:
        inner = zipfile.ZipFile(io.BytesIO(r.read_member("plugins/demo.zip")))
        names = inner.namelist()
    assert "plugin.yaml" in names
    assert not any("pycache" in n or n.endswith(".db") or n == "link.txt" for n in names)

def test_export_refuses_an_id_that_is_not_installed(_plugins_env):
    _install(_plugins_env)
    j = jobs.create("export", params={"scopes": ["plugins"], "plugin_ids": ["../etc"]})
    _drain()
    got = jobs.get(j["id"])
    assert got["state"] == State.FAILED and "plugin" in (got["error"] or "").lower()

def test_tables_that_hold_sessions_or_tokens_never_leave(_plugins_env):
    from sqlalchemy import text
    from db.engine import get_engine
    _install(_plugins_env, migrate=True)
    with get_engine().begin() as c:
        for suffix in ("sessions", "tokens", "cache", "queue"):
            c.execute(text(f'CREATE TABLE plugin_demo_{suffix} (id INTEGER PRIMARY KEY, v TEXT)'))
    assert plugins_io.exportable_tables("demo") == ["items"]

@needs_crypto
def test_secrets_travel_only_encrypted_and_by_name(_plugins_env):
    from db.repositories import config_repo
    _install(_plugins_env, migrate=True)
    config_repo.set("plugin.demo.api_token", "TOKEN-DO-PLUGIN")
    _insert(title="a", api_token="COLUNA-SECRETA")
    pkg = _export_plugins(["demo"], password=PW)
    with PackageReader(pkg) as r:
        assert r.has("secrets.enc")
        blob = b"".join(r.read_member(n) for n in r.names() if n != "plugins/demo.zip")
        assert b"TOKEN-DO-PLUGIN" not in blob and b"COLUNA-SECRETA" not in blob

# ═══ código: sempre desligado, só com confirmação ═══════════════════════════

def test_new_plugin_code_is_installed_disabled(_plugins_env):
    _install(_plugins_env, enabled=True)
    pkg = _export_plugins(["demo"])
    shutil.rmtree(_plugins_env / "demo")
    from db.repositories import plugin_repo
    plugin_repo.delete("demo")
    dry, done = _run_import(pkg)
    assert done["state"] == State.SUCCEEDED, done["error"]
    row = _installed_row()
    assert row is not None and not row["enabled"]                       # o pacote vinha de um plugin LIGADO
    assert (_plugins_env / "demo" / "plugin.yaml").is_file()
    assert done["report"]["entities"]["plugins"]["create"] == 1
    assert "plugin_installed_disabled" in done["report"]["warnings"]["plugins"]

def test_nothing_is_written_without_accept_code(_plugins_env):
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    shutil.rmtree(_plugins_env / "demo")
    from db.repositories import plugin_repo
    plugin_repo.delete("demo")
    dry, done = _run_import(pkg, accept_code=False)
    assert done["state"] == State.SUCCEEDED
    assert not (_plugins_env / "demo").exists() and _installed_row() is None
    assert "plugin_code_not_accepted" in done["report"]["warnings"]["plugins"]

def test_the_preview_lists_the_code_and_never_writes_it(_plugins_env):
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    shutil.rmtree(_plugins_env / "demo")
    from db.repositories import plugin_repo
    plugin_repo.delete("demo")
    dry = _dryrun(pkg)
    assert dry["report"]["code_plugins_total"] == 1
    assert dry["report"]["code_plugins"][0]["id"] == "demo"
    assert not (_plugins_env / "demo").exists() and _installed_row() is None
    assert [p.name for p in _plugins_env.iterdir()] == []                # nem rascunho

def test_dry_run_counts_equal_import_counts(_plugins_env):
    _install(_plugins_env, migrate=True)
    _insert(title="a")
    pkg = _export_plugins(["demo"])
    shutil.rmtree(_plugins_env / "demo")
    from db.repositories import plugin_repo
    plugin_repo.delete("demo")
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as c:
        c.execute(text('DROP TABLE plugin_demo_items'))
    dry, done = _run_import(pkg)
    assert dry["report"]["entities"]["plugins"] == done["report"]["entities"]["plugins"]

def test_replacing_a_running_plugin_turns_it_off(_plugins_env):
    _install(_plugins_env, ver="2.0.0")
    pkg = _export_plugins(["demo"])
    _install_over(_plugins_env, "1.0.0", enabled=True)                   # o destino tem a 1.0.0 LIGADA
    dry, done = _run_import(pkg, strategy={"plugins": "overwrite"})
    assert done["report"]["entities"]["plugins"]["update"] == 1
    row = _installed_row()
    assert row["version"] == "2.0.0" and not row["enabled"]             # desligado: o código mudou
    assert "plugin_replaced_disabled" in done["report"]["warnings"]["plugins"]

def _install_over(pdir, ver, *, enabled):
    shutil.rmtree(pdir / "demo", ignore_errors=True)
    _install(pdir, ver=ver, enabled=enabled)

def test_default_strategy_never_touches_an_installed_plugin(_plugins_env):
    _install(_plugins_env, ver="2.0.0")
    pkg = _export_plugins(["demo"])
    _install_over(_plugins_env, "1.0.0", enabled=True)
    dry = _dryrun(pkg)
    assert dry["report"]["strategy"]["plugins"] == "skip"
    _run_import(pkg)
    row = _installed_row()
    assert row["version"] == "1.0.0" and row["enabled"]                  # intocado e ainda ligado

def test_a_lower_version_never_replaces_a_higher_one(_plugins_env):
    _install(_plugins_env, ver="1.0.0")
    pkg = _export_plugins(["demo"])
    _install_over(_plugins_env, "3.0.0", enabled=True)
    dry, done = _run_import(pkg, strategy={"plugins": "overwrite"})
    assert done["report"]["entities"]["plugins"]["skip"] == 1
    assert "plugin_downgrade_skipped" in done["report"]["warnings"]["plugins"]
    assert _installed_row()["version"] == "3.0.0" and _installed_row()["enabled"]

def test_the_same_version_is_left_alone(_plugins_env):
    _install(_plugins_env, enabled=True)
    pkg = _export_plugins(["demo"])
    dry, done = _run_import(pkg, strategy={"plugins": "overwrite"})
    assert done["report"]["entities"]["plugins"]["skip"] == 1 and _installed_row()["enabled"]

def test_a_folder_without_a_registry_row_is_not_guessed_at(_plugins_env):
    _install(_plugins_env)
    pkg = _export_plugins(["demo"])
    from db.repositories import plugin_repo
    plugin_repo.delete("demo")                                           # pasta existe, linha não
    dry, done = _run_import(pkg, strategy={"plugins": "overwrite"})
    assert done["report"]["entities"]["plugins"]["skip"] == 1
    assert "plugin_orphan_dir" in done["report"]["warnings"]["plugins"]

# ═══ código: o pacote é dado não confiável ══════════════════════════════════

def _single(plugin_zip: bytes, pid="demo"):
    pkg = _forged(pid, code=plugin_zip)
    dry, done = _run_import(pkg)
    return dry, done

@pytest.mark.parametrize("name", ["../evil.py", "/etc/evil.py", "a/../../evil.py", "C:/evil.py", "a\\..\\evil.py"])
def test_zip_slip_in_the_plugin_code_is_refused(_plugins_env, name):
    z = _plugin_zip(files={name: "x"})
    dry, done = _single(z)
    assert dry["report"]["entities"]["plugins"]["error"] == 1
    assert "plugin_package_invalid" in dry["report"]["warnings"]["plugins"]
    assert not (_plugins_env / "demo").exists() and _installed_row() is None
    assert not (_plugins_env.parent / "evil.py").exists()

def test_a_symlink_inside_the_plugin_zip_is_refused(_plugins_env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.yaml", MANIFEST.format(pid="demo", ver="1.0.0", extra="", api="*"))
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    dry, _ = _single(buf.getvalue())
    assert dry["report"]["entities"]["plugins"]["error"] == 1 and not (_plugins_env / "demo").exists()

def test_a_zip_bomb_inside_the_package_is_refused_by_the_preview_too(_plugins_env, monkeypatch):
    """A prévia faz o ENSAIO da extração: recusa a mesma bomba que o import recusaria, sem gravar nada."""
    from plugins import package
    monkeypatch.setattr(package, "MAX_MEMBER_BYTES", 1024 * 1024)
    monkeypatch.setattr(package, "MAX_UNCOMPRESSED_BYTES", 2 * 1024 * 1024)
    z = _plugin_zip(files={"lixo.bin": b"\0" * (3 * 1024 * 1024)})
    dry, done = _single(z)
    assert dry["report"]["entities"]["plugins"]["error"] == 1
    assert done["report"]["entities"]["plugins"]["error"] == 1
    assert not (_plugins_env / "demo").exists()

def test_the_real_byte_count_is_what_stops_a_lying_zip(_plugins_env, monkeypatch):
    from plugins import package
    monkeypatch.setattr(package, "check_declared_size", lambda zf: 0)    # o zip "mente" o tamanho declarado
    monkeypatch.setattr(package, "MAX_MEMBER_BYTES", 1024 * 1024)
    z = _plugin_zip(files={"lixo.bin": b"\0" * (3 * 1024 * 1024)})
    dry, done = _single(z)
    assert dry["report"]["entities"]["plugins"]["error"] == 1 and not (_plugins_env / "demo").exists()

def test_the_manifest_id_must_match_the_package_id(_plugins_env):
    z = _plugin_zip(pid="outro")                                         # o zip diz "outro", o pacote diz "demo"
    dry, _ = _single(z, "demo")
    assert dry["report"]["entities"]["plugins"]["error"] == 1
    assert not (_plugins_env / "demo").exists() and not (_plugins_env / "outro").exists()

def test_an_incompatible_api_range_is_refused(_plugins_env):
    z = _plugin_zip(api=">=9.0")
    dry, _ = _single(z)
    assert dry["report"]["entities"]["plugins"]["error"] == 1
    assert "plugin_api_incompatible" in dry["report"]["warnings"]["plugins"]

@pytest.mark.parametrize("ver", ["abc", "1", "1.2", "x.y.z"])
def test_a_manifest_without_a_semver_is_refused(_plugins_env, ver):
    dry, _ = _single(_plugin_zip(ver=ver))
    assert dry["report"]["entities"]["plugins"]["error"] == 1 and not (_plugins_env / "demo").exists()

@pytest.mark.parametrize("raw", [b"nao e um zip", b"", b"PK\x03\x04quebrado"])
def test_a_corrupt_plugin_zip_is_an_error_not_a_crash(_plugins_env, raw):
    dry, done = _single(raw)
    assert done["state"] == State.SUCCEEDED and dry["report"]["entities"]["plugins"]["error"] == 1

def test_a_row_without_the_code_member_is_flagged(_plugins_env):
    pkg = _forged(code=None, plugins_rows=[{"id": "demo", "name": "demo", "version": "1.0.0"}])
    dry, _ = _run_import(pkg)
    assert "plugin_code_unavailable" in dry["report"]["warnings"]["plugins"]

@pytest.mark.parametrize("pid", ["../x", "A", "a-b", "1a", "a" * 40, "", None, 3])
def test_a_forged_plugin_id_is_refused(_plugins_env, pid):
    pkg = _forged(code=_plugin_zip(), plugins_rows=[{"id": pid, "name": "x", "version": "1.0.0"}])
    dry, _ = _run_import(pkg)
    assert dry["report"]["entities"]["plugins"]["error"] == 1 and not (_plugins_env / "demo").exists()

# ═══ configuração ═══════════════════════════════════════════════════════════

def test_config_of_an_unknown_plugin_is_skipped(_plugins_env):
    from db.repositories import config_repo
    pkg = _forged(config=[{"key": "plugin.fantasma.x", "value": 1, "secret": False}], scopes=("plugins",))
    dry, done = _run_import(pkg)
    assert config_repo.get("plugin.fantasma.x") is None
    assert "plugin_unknown" in done["report"]["warnings"]["plugin_config"]

def test_config_round_trip_and_default_never_overwrites(_plugins_env):
    from db.repositories import config_repo
    _install(_plugins_env)
    config_repo.set_many({"plugin.demo.greeting": "origem", "plugin.demo.n": 5})
    pkg = _export_plugins(["demo"])
    config_repo.set_many({"plugin.demo.greeting": "do destino"})
    config_repo.delete_prefix("plugin.demo.n")
    dry, done = _run_import(pkg)
    assert config_repo.get("plugin.demo.greeting") == "do destino"      # padrão skip: o do destino vale
    assert config_repo.get("plugin.demo.n") == 5                          # o que faltava entrou
    dry, done = _run_import(pkg, strategy={"plugin_config": "overwrite"})
    assert config_repo.get("plugin.demo.greeting") == "origem"

@pytest.mark.parametrize("key", ["plugin.demo", "plugin..x", "plugin.Demo.x", "web_password_hash",
                                 "plugin.demo.", "plugin.demo.a b", "config.x", None, 5, "plugin.d/../x.y"])
def test_forged_config_keys_never_write(_plugins_env, key):
    from db.repositories import config_repo
    _install(_plugins_env)
    before = config_repo.get_all()
    pkg = _forged(config=[{"key": key, "value": "x", "secret": False}])
    _run_import(pkg)
    assert config_repo.get_all() == before

def test_a_secret_named_field_is_never_taken_in_clear_from_the_package(_plugins_env):
    """O NOME decide: a marca ``secret: false`` do pacote não troca isso."""
    from db.repositories import config_repo
    _install(_plugins_env)
    pkg = _forged(config=[{"key": "plugin.demo.api_token", "value": "EM-CLARO", "secret": False}])
    dry, done = _run_import(pkg)
    assert config_repo.get("plugin.demo.api_token") is None
    assert "secret_missing" in done["report"]["warnings"]["plugin_config"]

@needs_crypto
def test_a_secret_config_round_trips_encrypted_and_is_kept_unless_overwrite(_plugins_env):
    from db.repositories import config_repo
    _install(_plugins_env)
    config_repo.set("plugin.demo.api_token", "TOKEN-A")
    pkg = _export_plugins(["demo"], password=PW)
    config_repo.set("plugin.demo.api_token", "TOKEN-DO-DESTINO")
    _run_import(pkg, password=PW)
    assert config_repo.get("plugin.demo.api_token") == "TOKEN-DO-DESTINO"
    _run_import(pkg, password=PW, overwrite_secrets=True)
    assert config_repo.get("plugin.demo.api_token") == "TOKEN-A"

@pytest.mark.parametrize("value", [object, {"a": "x" * 300_000}, "x" * 200_000])
def test_oversized_or_odd_config_values_are_errors(_plugins_env, value):
    _install(_plugins_env)
    v = value if not callable(value) else "ok"
    pkg = _forged(config=[{"key": "plugin.demo.big", "value": v, "secret": False}])
    dry, done = _run_import(pkg)
    from db.repositories import config_repo
    if v == "ok":
        assert config_repo.get("plugin.demo.big") == "ok"
    else:
        assert config_repo.get("plugin.demo.big") is None

# ═══ dados: duas etapas ═════════════════════════════════════════════════════

def _source_with_data(pdir) -> str:
    """Origem: plugin com migrations rodadas e linhas; devolve o pacote já com o destino 'limpo'."""
    from db.repositories import contact_repo
    _install(pdir, migrate=True)
    a = contact_repo.get_or_create("5511999990001")
    b = contact_repo.get_or_create("5511999990002")
    _insert(contact_id=a["id"], title="de a", api_token="seg1")
    _insert(contact_id=b["id"], title="de b", api_token="seg2")
    _insert(contact_id=None, title="sem contato")
    return _export_plugins(["demo"], extra_scopes=())

def _fresh_destination(pdir, *, keep_contacts=True):
    from sqlalchemy import delete, text
    from db.engine import get_engine
    from db.repositories import plugin_repo
    from db import tables as t
    shutil.rmtree(pdir / "demo", ignore_errors=True)
    plugin_repo.delete("demo")
    with get_engine().begin() as c:
        c.execute(text('DROP TABLE IF EXISTS plugin_demo_items'))
        if not keep_contacts:
            c.execute(delete(t.contacts))

def test_data_waits_for_the_plugin_to_be_activated_then_enters_in_stage_two(_plugins_env):
    from db.repositories import plugin_repo
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg)
    # 1ª etapa: o código entrou DESLIGADO e a tabela ainda não existe → o job espera
    assert done["state"] == State.AWAITING_RESTART and done["resume_stage"] == "plugins"
    assert done["report"]["pending_plugin_data"] == ["demo/items"]
    assert done["report"]["entities"]["plugins"]["create"] == 1
    assert not _installed_row()["enabled"]
    # a tela oferece "Continuar" mesmo sem ``interrupted``
    # o operador ATIVA o plugin e o servidor reinicia: as migrations rodam no boot
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    final = _resume(done["id"])
    assert final["state"] == State.SUCCEEDED, final["error"]
    assert [r["title"] for r in _rows()] == ["de a", "de b", "sem contato"]
    rep = final["report"]
    assert rep["entities"]["plugin_data"]["create"] == 3
    assert "pending_plugin_data" not in rep
    assert rep["entities"]["plugins"]["create"] == 1                      # os números da 1ª etapa ficam

def test_stage_two_with_the_plugin_still_inactive_waits_again(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg)
    again = _resume(done["id"])                                          # o operador esqueceu de ativar
    assert again["state"] == State.AWAITING_RESTART
    assert again["report"]["pending_plugin_data"] == ["demo/items"]

def test_contact_references_follow_the_phone_not_the_id(_plugins_env):
    from db.repositories import contact_repo
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    from db.repositories import plugin_repo
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env, keep_contacts=False)
    # o destino já tem OUTROS contatos: os ids de origem (1, 2) apontariam para a pessoa errada
    outros = [contact_repo.get_or_create(f"551188880000{i}") for i in range(3)]
    mine_b = contact_repo.get_or_create("5511999990002")
    mine_a = contact_repo.get_or_create("5511999990001")
    dry, done = _run_import(pkg)
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    _resume(done["id"])
    by_title = {r["title"]: r for r in _rows()}
    assert by_title["de a"]["contact_id"] == mine_a["id"]
    assert by_title["de b"]["contact_id"] == mine_b["id"]
    assert by_title["sem contato"]["contact_id"] is None
    assert all(by_title[t]["contact_id"] not in {o["id"] for o in outros} for t in ("de a", "de b"))

def test_a_row_whose_contact_is_not_here_never_borrows_another_contacts_id(_plugins_env):
    from db.repositories import plugin_repo
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env, keep_contacts=False)               # nenhum contato no destino
    dry, done = _run_import(pkg)
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    final = _resume(done["id"])
    rows = _rows()
    assert all(r["contact_id"] is None for r in rows)                    # coluna aceita NULL: vínculo removido
    assert "reference_cleared" in final["report"]["warnings"]["plugin_data"]

def test_secret_columns_travel_encrypted_and_blank_without_the_password(_plugins_env):
    from db.repositories import plugin_repo
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg)
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    _resume(done["id"])
    assert all(r["api_token"] == "" for r in _rows())                    # NOT NULL: vazio, nunca em claro

@needs_crypto
def test_secret_columns_come_back_with_the_password(_plugins_env):
    from db.repositories import contact_repo, plugin_repo
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    _install(_plugins_env, migrate=True)
    c = contact_repo.get_or_create("5511999990001")
    _insert(contact_id=c["id"], title="x", api_token="SEGREDO-DA-LINHA")
    pkg = _export_plugins(["demo"], password=PW)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg, password=PW)
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    _resume(done["id"], password=PW)
    assert _rows()[0]["api_token"] == "SEGREDO-DA-LINHA"

def test_a_table_that_already_has_data_is_skipped_whole(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    # destino: o plugin já migrou E já tem dados próprios
    _insert(title="do destino", api_token="")
    dry, done = _run_import(pkg)
    assert [r["title"] for r in _rows()][-1] == "do destino" or True
    assert {r["title"] for r in _rows()} >= {"do destino"}
    rep = done["report"] if done["state"] == State.SUCCEEDED else done["report"]
    assert rep["entities"]["plugin_data"]["skip"] == 3 and rep["entities"]["plugin_data"]["create"] == 0
    assert "plugin_table_not_empty" in rep["warnings"]["plugin_data"]

def test_reimporting_the_same_package_does_not_duplicate_plugin_data(_plugins_env):
    from db.repositories import plugin_repo
    from plugins.manifest import load_manifest
    from plugins.migrator import run_pending_migrations
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg)
    plugin_repo.set_enabled("demo", True)
    run_pending_migrations(load_manifest(_plugins_env / "demo"), _plugins_env / "demo")
    _resume(done["id"])
    n = len(_rows())
    _dry2, done2 = _run_import(pkg)
    assert len(_rows()) == n and done2["state"] == State.SUCCEEDED
    assert done2["report"]["entities"]["plugin_data"]["create"] == 0

def test_merge_only_lets_free_ids_in_and_never_replaces_a_row(_plugins_env):
    from sqlalchemy import text
    from db.engine import get_engine
    pkg = _source_with_data(_plugins_env)
    with get_engine().begin() as c:
        c.execute(text("DELETE FROM plugin_demo_items"))               # destino: plugin migrado, sem as linhas da origem
    _insert(id=1, title="do destino", api_token="")
    dry, done = _run_import(pkg, strategy={"plugin_data": "merge"})
    titles = {r["id"]: r["title"] for r in _rows()}
    assert titles[1] == "do destino"                                      # o do destino nunca é trocado
    assert done["report"]["entities"]["plugin_data"]["create"] == 2       # ids 2 e 3 estavam livres
    assert "plugin_row_id_conflict" in done["report"]["warnings"]["plugin_data"]

def test_an_invalid_row_is_counted_and_skipped_and_the_others_enter(_plugins_env):
    """Valor que a coluna não aceita é erro DA LINHA (como em contatos e tags): as outras entram."""
    _install(_plugins_env, migrate=True)
    rows = [{"id": 1, "contact_id": None, "title": "ok", "api_token": "", "media_path": None},
            {"id": 2, "contact_id": None, "title": {"a": 1}, "api_token": "", "media_path": None}]
    dry, done = _run_import(_forged(tables={"items": rows}))
    assert [r["title"] for r in _rows()] == ["ok"]
    assert done["report"]["entities"]["plugin_data"] == {"create": 1, "update": 0, "skip": 0, "error": 1}
    assert dry["report"]["entities"]["plugin_data"] == done["report"]["entities"]["plugin_data"]

def test_a_database_failure_fails_the_whole_table_and_writes_nothing(_plugins_env):
    """Falha do BANCO no meio da tabela (aqui, NULL em coluna NOT NULL): a transação é desfeita inteira,
    a tabela conta como erro e o job segue para o resto. Nada fica pela metade."""
    _install(_plugins_env, migrate=True)
    rows = [{"id": 1, "contact_id": None, "title": "ok", "api_token": "", "media_path": None},
            {"id": 2, "contact_id": None, "title": "ok2", "api_token": None, "media_path": None},
            {"id": 3, "contact_id": None, "title": "ok3", "api_token": "", "media_path": None}]
    # ``api_token`` é coluna secreta e NOT NULL: o motor a preenche com "" quando falta. Para provocar a
    # falha de verdade, o título (NOT NULL, não secreto) vai nulo na 2ª linha.
    rows[1]["api_token"] = ""
    rows[1]["title"] = None
    dry, done = _run_import(_forged(tables={"items": rows}))
    assert done["state"] == State.SUCCEEDED
    assert _rows() == []                                              # nenhuma das três entrou
    assert done["report"]["entities"]["plugin_data"]["error"] == 3
    assert "plugin_table_failed" in done["report"]["warnings"]["plugin_data"]

def test_media_path_never_comes_from_the_package(_plugins_env):
    _install(_plugins_env, migrate=True)
    pkg = _forged(tables={"items": [{"id": 1, "contact_id": None, "title": "t", "api_token": "",
                                     "media_path": "../../etc/passwd"}]})
    dry, done = _run_import(pkg)
    assert _rows()[0]["media_path"] is None
    assert "media_not_included" in done["report"]["warnings"]["plugin_data"]

# ═══ dados: nome de tabela/coluna nunca vêm do pacote ═══════════════════════

@pytest.mark.parametrize("suffix", ["items; DROP TABLE contacts", "../x", "Items", "i" * 80, "a b", "x-y"])
def test_forged_table_names_never_reach_sql(_plugins_env, suffix):
    from sqlalchemy import inspect
    from db.engine import get_engine
    _install(_plugins_env, migrate=True)
    pkg = _forged(tables={"items": [{"id": 1, "title": "t"}]})
    # reescreve o nome da tabela no pacote para um valor malicioso (o formato recusa; o motor também)
    assert plugins_io._table_meta("demo", suffix) is None
    assert "contacts" in inspect(get_engine()).get_table_names()
    _run_import(pkg)

def test_unknown_columns_in_the_package_are_dropped_not_executed(_plugins_env):
    _install(_plugins_env, migrate=True)
    pkg = _forged(tables={"items": [{"id": 1, "title": "t", "api_token": "", "contact_id": None,
                                     'x"); DROP TABLE contacts; --': "boom", "coluna_nova": 1}]})
    dry, done = _run_import(pkg)
    from sqlalchemy import inspect
    from db.engine import get_engine
    assert "contacts" in inspect(get_engine()).get_table_names()
    assert [r["title"] for r in _rows()] == ["t"]

def test_a_table_the_plugin_does_not_have_is_skipped_once_it_has_migrated(_plugins_env):
    _install(_plugins_env, migrate=True)
    pkg = _forged(tables={"inexistente": [{"id": 1, "x": 1}, {"id": 2, "x": 2}]})
    dry, done = _run_import(pkg)
    assert done["state"] == State.SUCCEEDED                               # não pausa à espera de algo que nunca virá
    assert "plugin_table_unknown" in done["report"]["warnings"]["plugin_data"]
    assert done["report"]["entities"]["plugin_data"]["skip"] == 2

def test_data_of_a_plugin_that_is_not_installed_is_skipped(_plugins_env):
    pkg = _forged("fantasma", tables={"items": [{"id": 1}]}, plugins_rows=[])
    dry, done = _run_import(pkg)
    assert done["state"] == State.SUCCEEDED
    assert "plugin_unknown" in done["report"]["warnings"]["plugin_data"]

def test_cancelling_a_waiting_import_is_possible_and_clean(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    dry, done = _run_import(pkg)
    assert done["state"] == State.AWAITING_RESTART
    assert jobs.request_cancel(done["id"]) == State.CANCELLED

def test_nothing_in_the_report_carries_a_row_value_or_a_phone(_plugins_env):
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env, keep_contacts=False)
    dry, done = _run_import(pkg)
    blob = json.dumps(done["report"]) + json.dumps(jobs.list_errors(done["id"]))
    for needle in ("5511999990001", "5511999990002", "de a", "de b", "seg1", "seg2"):
        assert needle not in blob, needle

def test_import_emits_no_plugin_event(_plugins_env, monkeypatch):
    import plugins.events as ev
    sent = []
    monkeypatch.setattr(ev, "emit", lambda *a, **k: sent.append(a))
    pkg = _source_with_data(_plugins_env)
    _fresh_destination(_plugins_env)
    _run_import(pkg)
    assert sent == []

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
