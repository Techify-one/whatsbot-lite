"""Importar/Exportar — ida e volta de config e tags (``portability.handlers`` + ``entities``).

Exporta de uma base, ZERA as tabelas (o "destino novo") e importa o mesmo pacote. Zerar em vez de trocar
de engine deixa o teste igual em SQLite e em Postgres (``WHATSBOT_TEST_DB_URL``).

O que se prova, de propósito, e não só o caminho feliz:
  * a prévia (dry-run) e o import dão as MESMAS contagens (rodam o mesmo ``apply``);
  * reimportar não duplica nem sobrescreve;
  * um pacote ADULTERADO não consegue gravar hash de senha, ``installation_id`` nem ``ai_engine_enabled``;
  * segredo só existe cifrado, e a senha nunca chega ao banco;
  * nenhum evento de plugin é emitido pelo import.

Os testes que cifram precisam de ``cryptography`` (pulam sem ela).

Roda direto (``python tests/test_portability_roundtrip.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import asyncio
import hashlib
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

import pytest  # noqa: E402

from db import init_db, init_engine  # noqa: E402
from portability import handlers, jobs  # noqa: E402
from portability.format import Manifest, PackageReader, PackageWriter  # noqa: E402
from portability.jobs import State  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - depende do ambiente
    HAVE_CRYPTO = False
needs_crypto = pytest.mark.skipif(not HAVE_CRYPTO, reason="cryptography não instalada")

PW = "senha-do-pacote-12345"
API_KEY = "sk-or-CHAVE-MUITO-SECRETA-0001"

@pytest.fixture(scope="module", autouse=True)
def _engine():
    tmp = Path(tempfile.mkdtemp(prefix="wb_port_rt_"))
    url = os.environ.get("WHATSBOT_TEST_DB_URL", "").strip()
    if url:
        init_engine(url)
        from db.connection import _run_alembic_upgrade
        _run_alembic_upgrade()
    else:
        init_db(tmp / "whatsbot.db")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)

@pytest.fixture(autouse=True)
def _clean(_engine, monkeypatch):
    _wipe()
    jobs._PASSWORDS.clear()
    jobs._HANDLERS.clear()
    handlers.register_all()
    jobs.set_runtime_deps(None)
    root = _engine / "storages" / "portability"
    monkeypatch.setattr(jobs, "jobs_root", lambda: (root.mkdir(parents=True, exist_ok=True), str(root))[1])
    for var in ("WHATSBOT_MODEL", "OPENROUTER_API_KEY", "WHATSBOT_SYSTEM_PROMPT", "WHATSBOT_AUTO_REPLY"):
        monkeypatch.delenv(var, raising=False)
    yield

def _wipe() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import config, contact_tags, portability_job_errors, portability_jobs, tags
    with get_engine().begin() as c:
        for t in (portability_job_errors, portability_jobs, contact_tags, tags, config):
            c.execute(delete(t))

# ── helpers ─────────────────────────────────────────────────────────────────

def _drain() -> None:
    while asyncio.run(jobs.run_one(jobs.PROCESS_WID)):
        pass

def _seed_source() -> None:
    from db.repositories import config_repo, tag_repo
    config_repo.set_many({
        "model": "anthropic/claude-x", "system_prompt": "Seja breve, ação.", "auto_reply": True,
        "max_context_messages": 25, "message_batch_delay": 4.5, "group_reply_mode": "always",
        "ai_reactivation_enabled": True, "ai_reactivation_value": 6, "ai_reactivation_unit": "days",
        "media_autodownload": {"image": {"individual": True, "group": False}},
        # nada disto pode viajar:
        "openrouter_api_key": API_KEY,
        "web_password_hash": "HASH-DA-SENHA-DO-PAINEL", "web_password_salt": "SAL",
        "installation_id": "11111111-2222-3333-4444-555555555555", "bot_phone": "5511999990000",
        "gowa_proxy_password": "SENHA-DO-PROXY", "gowa_proxy_url": "socks5://u:p@10.0.0.1:1080",
        "ai_engine_enabled": True, "access_token": "TOKEN-TECHIFY", "setup_completed": True,
        "web_port": 9999, "disk_limit_gb": 7.0,
    })
    tag_repo.create("vip", "#00ff00")
    tag_repo.create("lead", "red")
    tag_repo.create("ação", "rgb(1, 2, 3)")

def _export(scopes, *, password: str | None = None) -> str:
    j = jobs.create("export", params={"scopes": scopes, "include_secrets": bool(password)},
                    affinity=jobs.PROCESS_WID if password else None)
    if password:
        jobs.stash_password(j["id"], password)
    _drain()
    got = jobs.get(j["id"])
    assert got["state"] == State.SUCCEEDED, got["error"]
    return got["package_path"]

def _stage(pkg: str) -> str:
    """Copia o pacote para uma pasta de job nova (é de onde o import o lê)."""
    d = Path(jobs.jobs_root()) / f"in-{os.urandom(4).hex()}"
    d.mkdir()
    dst = d / "package.zip"
    shutil.copy(pkg, dst)
    return str(dst)

def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _dryrun(pkg: str, *, password: str | None = None, strategy: dict | None = None) -> dict:
    path = _stage(pkg)
    params = {"package_path": path, "package_sha256": _sha(path), "has_password": bool(password)}
    if strategy is not None:
        params["strategy"] = strategy
    j = jobs.create("import_dryrun", params=params, package_path=path, package_sha256=_sha(path),
                    affinity=jobs.PROCESS_WID if password else None)
    if password:
        jobs.stash_password(j["id"], password)
    _drain()
    return jobs.get(j["id"])

def _import(dry: dict, *, password: str | None = None, strategy: dict | None = None,
            overwrite_secrets: bool = False) -> dict:
    params = {"package_path": dry["params"]["package_path"],
              "package_sha256": dry["params"]["package_sha256"], "has_password": bool(password),
              "overwrite_secrets": overwrite_secrets,
              "strategy": strategy if strategy is not None else dry["report"].get("strategy")}
    j = jobs.create("import", params=params, package_path=dry["params"]["package_path"],
                    package_sha256=dry["params"]["package_sha256"], source_job=dry["id"],
                    affinity=jobs.PROCESS_WID if password else None)
    if password:
        jobs.stash_password(j["id"], password)
    _drain()
    return jobs.get(j["id"])

def _build_pkg(rows: dict[str, list[dict]], *, scopes=("settings", "catalog"), secrets=None) -> str:
    """Pacote montado à mão: o que um atacante (ou uma versão futura) poderia entregar."""
    d = Path(tempfile.mkdtemp(prefix="wb_forged_"))
    path = str(d / "forged.zip")
    with PackageWriter(path) as w:
        for ent, items in rows.items():
            w.write_ndjson(ent, items)
        w.finish(Manifest(scopes=list(scopes), created_at=1.0))
    return path

def _cfg(key=None, default=None):
    from db.repositories import config_repo
    return config_repo.get_all() if key is None else config_repo.get(key, default)

def _tags() -> dict:
    from db.repositories import tag_repo
    return tag_repo.get_all()

# ── export ──────────────────────────────────────────────────────────────────

def test_export_contains_only_allowlisted_config_in_clear():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    with PackageReader(pkg) as r:
        cfg = {row["key"]: row for row in r.iter_ndjson("config")}
        assert cfg["model"]["value"] == "anthropic/claude-x" and cfg["model"]["secret"] is False
        assert cfg["system_prompt"]["value"] == "Seja breve, ação."
        forbidden = {"web_password_hash", "web_password_salt", "installation_id", "bot_phone",
                     "gowa_proxy_password", "gowa_proxy_url", "ai_engine_enabled", "access_token",
                     "setup_completed", "web_port", "disk_limit_gb"}
        assert not (forbidden & set(cfg)), forbidden & set(cfg)
        assert cfg["openrouter_api_key"] == {"key": "openrouter_api_key", "value": None, "secret": True}
        assert not r.has("secrets.enc")                       # sem senha, sem segredo
        everything = b"".join(r.read_member(n) for n in r.names())
        for needle in (API_KEY, "HASH-DA-SENHA", "SENHA-DO-PROXY", "TOKEN-TECHIFY", "5511999990000",
                       "11111111-2222"):
            assert needle.encode() not in everything, needle
        assert r.manifest.counts == {"config": len(cfg), "tags": 3}

def test_export_reads_the_database_not_the_environment(monkeypatch):
    """A env mascara o valor em ``Settings.get``; o pacote leva o que está SALVO."""
    _seed_source()
    monkeypatch.setenv("WHATSBOT_MODEL", "modelo-vindo-da-env")
    with PackageReader(_export(["settings"])) as r:
        assert {x["key"]: x for x in r.iter_ndjson("config")}["model"]["value"] == "anthropic/claude-x"

@needs_crypto
def test_export_with_password_puts_the_key_only_in_secrets_enc():
    _seed_source()
    pkg = _export(["settings"], password=PW)
    with PackageReader(pkg) as r:
        assert r.has("secrets.enc") and r.manifest.secrets["present"] is True
        plain = b"".join(r.read_member(n) for n in r.names() if n != "secrets.enc")
        assert API_KEY.encode() not in plain and API_KEY.encode() not in r.read_member("secrets.enc")

def test_export_without_secrets_when_the_instance_has_none():
    from db.repositories import config_repo
    config_repo.set("model", "m")
    pkg = _export(["settings"], password=PW) if HAVE_CRYPTO else _export(["settings"])
    with PackageReader(pkg) as r:
        assert not r.has("secrets.enc")                       # nada a cifrar ⇒ não gera arquivo vazio

def test_export_refuses_bad_scope_and_weak_password():
    for params, msg in (({"scopes": ["nope"]}, "Escopo desconhecido"), ({"scopes": []}, "ao menos um")):
        j = jobs.create("export", params=params)
        _drain()
        assert jobs.get(j["id"])["state"] == State.FAILED and msg in jobs.get(j["id"])["error"]
    j = jobs.create("export", params={"scopes": ["settings"], "include_secrets": True},
                    affinity=jobs.PROCESS_WID)
    jobs.stash_password(j["id"], "curta")
    _drain()
    assert jobs.get(j["id"])["state"] == State.FAILED and "12 caracteres" in jobs.get(j["id"])["error"]

def test_failed_export_leaves_no_half_written_package():
    j = jobs.create("export", params={"scopes": ["nope"]})
    _drain()
    assert not (Path(jobs.jobs_root()) / j["id"] / "package.zip").exists()

# ── ida e volta ─────────────────────────────────────────────────────────────

def test_roundtrip_restores_config_and_tags_on_a_clean_destination():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    _wipe()
    dry = _dryrun(pkg)
    assert dry["state"] == State.SUCCEEDED, dry["error"]
    assert dry["report"]["entities"]["tags"]["create"] == 3
    imp = _import(dry)
    assert imp["state"] == State.SUCCEEDED, imp["error"]
    cfg = _cfg()
    assert cfg["model"] == "anthropic/claude-x" and cfg["system_prompt"] == "Seja breve, ação."
    assert cfg["auto_reply"] is True and cfg["max_context_messages"] == 25
    assert cfg["message_batch_delay"] == 4.5 and cfg["ai_reactivation_value"] == 6
    assert cfg["media_autodownload"]["image"] == {"individual": True, "group": False}
    assert cfg["media_autodownload"]["audio"] == {"individual": True, "group": True}   # default mesclado
    assert _tags() == {"vip": {"color": "#00ff00"}, "lead": {"color": "red"},
                       "ação": {"color": "rgb(1, 2, 3)"}}

def test_dryrun_and_import_report_the_same_counts():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    _wipe()
    dry = _dryrun(pkg)
    imp = _import(dry)
    assert dry["report"]["entities"] == imp["report"]["entities"]
    assert dry["report"]["warnings"] == imp["report"]["warnings"]

def test_dryrun_writes_nothing():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    _wipe()
    _dryrun(pkg)
    assert _tags() == {} and _cfg() == {}

def test_reimporting_the_same_package_changes_nothing():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    _wipe()
    _import(_dryrun(pkg))
    before = (_cfg(), _tags())
    dry2 = _dryrun(pkg)
    imp2 = _import(dry2)
    assert (_cfg(), _tags()) == before
    for ent, tally in imp2["report"]["entities"].items():
        assert tally["create"] == 0 and tally["update"] == 0, (ent, tally)
    assert len(_tags()) == 3                                   # não duplicou

def test_a_value_equal_to_the_destination_is_never_counted_as_an_update():
    """Idempotência pelo VALOR, não só pela estratégia: com ``overwrite`` e com a chave no valor
    padrão (onde o ``merge`` também escreveria), reimportar o mesmo conteúdo não pode virar
    'atualizado' nem regravar. Sem isto o relatório mentiria a cada reimport."""
    from db.repositories import config_repo
    from config.settings import DEFAULT_CONFIG
    pkg = _build_pkg({"config": [
        {"key": "model", "value": "m-igual", "secret": False},
        {"key": "auto_reply", "value": DEFAULT_CONFIG["auto_reply"], "secret": False}]})
    config_repo.set("model", "m-igual")
    config_repo.set("auto_reply", DEFAULT_CONFIG["auto_reply"])
    for strat in ("overwrite", "merge", "skip"):
        imp = _import(_dryrun(pkg, strategy={"config": strat}), strategy={"config": strat})
        assert imp["report"]["entities"]["config"] == {"create": 0, "update": 0, "skip": 2,
                                                       "error": 0}, strat
        assert imp["report"]["post"]["config_keys_changed"] == 0, strat   # nada regravado

def test_already_imported_note_and_same_instance_note():
    from db.repositories import config_repo
    config_repo.set("installation_id", "aaaaaaaa-0000-0000-0000-000000000001")
    config_repo.set("model", "m1")
    pkg = _export(["settings"])
    dry = _dryrun(pkg)
    assert "same_instance" in dry["report"]["notes"]          # importar o pacote na MESMA instalação
    _import(dry)
    assert _dryrun(pkg)["report"]["already_imported"] is not None
    other = _build_pkg({"tags": [{"name": "t", "color": "red"}]}, scopes=("catalog",))
    assert _dryrun(other)["report"]["already_imported"] is None

# ── estratégias ─────────────────────────────────────────────────────────────

def test_merge_keeps_what_the_operator_customised_but_fills_defaults():
    from db.repositories import config_repo
    pkg = _build_pkg({"config": [
        {"key": "model", "value": "do-pacote", "secret": False},
        {"key": "improvement_model", "value": "melhoria-do-pacote", "secret": False}]})
    config_repo.set("model", "personalizado-aqui")            # diferente do default ⇒ operador mexeu
    config_repo.set("improvement_model", "")                  # ainda no default
    imp = _import(_dryrun(pkg), strategy={"config": "merge"})
    assert _cfg("model") == "personalizado-aqui" and _cfg("improvement_model") == "melhoria-do-pacote"
    assert imp["report"]["entities"]["config"] == {"create": 0, "update": 1, "skip": 1, "error": 0}

def test_skip_never_touches_an_existing_key_and_overwrite_always_does():
    from db.repositories import config_repo
    pkg = _build_pkg({"config": [{"key": "model", "value": "do-pacote", "secret": False}]})
    config_repo.set("model", "aqui")
    _import(_dryrun(pkg), strategy={"config": "skip"})
    assert _cfg("model") == "aqui"
    _import(_dryrun(pkg), strategy={"config": "overwrite"})
    assert _cfg("model") == "do-pacote"

def test_skip_still_creates_a_key_that_does_not_exist_yet():
    pkg = _build_pkg({"config": [{"key": "model", "value": "novo", "secret": False}]})
    _import(_dryrun(pkg), strategy={"config": "skip"})
    assert _cfg("model") == "novo"

def test_tag_overwrite_changes_the_color_and_skip_keeps_it():
    from db.repositories import tag_repo
    tag_repo.create("vip", "blue")
    pkg = _build_pkg({"tags": [{"name": "vip", "color": "#abcdef"}]}, scopes=("catalog",))
    _import(_dryrun(pkg), strategy={"tags": "skip"})
    assert _tags()["vip"]["color"] == "blue"
    imp = _import(_dryrun(pkg), strategy={"tags": "overwrite"})
    assert _tags()["vip"]["color"] == "#abcdef" and imp["report"]["entities"]["tags"]["update"] == 1

def test_invalid_strategy_fails_the_job_with_a_clear_message():
    pkg = _build_pkg({"tags": [{"name": "t", "color": "red"}]}, scopes=("catalog",))
    dry = _dryrun(pkg, strategy={"tags": "merge"})            # tag não tem merge
    assert dry["state"] == State.FAILED and "não se aplica" in dry["error"]

# ── pacote adulterado ───────────────────────────────────────────────────────

def test_forged_package_cannot_write_forbidden_config_keys():
    """O ponto central de segurança: o import tem UMA porta (a allowlist), igual à do export."""
    from db.repositories import config_repo
    config_repo.set("web_password_hash", "HASH-ORIGINAL")
    config_repo.set("ai_engine_enabled", False)
    config_repo.set("installation_id", "ORIGINAL-ID")
    rows = [{"key": k, "value": "ATACANTE", "secret": False} for k in (
        "web_password_hash", "web_password_salt", "installation_id", "ai_engine_enabled",
        "gowa_proxy_url", "access_token", "bot_phone", "web_port", "plugin.x.y", "chave_inventada")]
    rows.append({"key": "model", "value": "ok", "secret": False})
    imp = _import(_dryrun(_build_pkg({"config": rows})), strategy={"config": "overwrite"})
    assert imp["state"] == State.SUCCEEDED
    assert _cfg("web_password_hash") == "HASH-ORIGINAL" and _cfg("ai_engine_enabled") is False
    assert _cfg("installation_id") == "ORIGINAL-ID"
    for k in ("web_password_salt", "gowa_proxy_url", "access_token", "bot_phone", "web_port",
              "plugin.x.y", "chave_inventada"):
        assert _cfg(k, "AUSENTE") == "AUSENTE", k
    assert _cfg("model") == "ok"
    w = imp["report"]["warnings"]["config"]["key_not_importable"]
    assert w["count"] == 10

def test_invalid_config_values_are_discarded_with_a_row_error():
    pkg = _build_pkg({"config": [
        {"key": "auto_reply", "value": "sim", "secret": False},             # esperava bool
        {"key": "max_context_messages", "value": "muitas", "secret": False},
        {"key": "ai_reactivation_time", "value": "25:99", "secret": False},
        {"key": "ai_reactivation_unit", "value": "séculos", "secret": False},
        {"key": "system_prompt", "value": 12345, "secret": False},
        {"key": "model", "value": "bom", "secret": False}]})
    imp = _import(_dryrun(pkg), strategy={"config": "overwrite"})
    t = imp["report"]["entities"]["config"]
    assert t["error"] == 5 and t["update"] == 1
    assert _cfg("model") == "bom" and _cfg("auto_reply", "AUSENTE") == "AUSENTE"
    errs = jobs.list_errors(imp["id"])
    assert len(errs) == 5 and {e["code"] for e in errs} == {"invalid_value"}
    assert all(e["entity"] == "config" and e["detail"]["reason"] == "invalid_value" for e in errs)

def test_invalid_tags_are_rejected_and_the_color_cannot_inject_css():
    pkg = _build_pkg({"tags": [
        {"name": "ok", "color": "#fff"},
        {"name": "x", "color": "red;background:url(//evil)"},     # injeção em style="color:${cor}"
        {"name": "y", "color": "expression(alert(1))"},
        {"name": " espaço ", "color": "red"}, {"name": "", "color": "red"},
        {"name": "n" * 31, "color": "red"}, {"name": 5, "color": "red"}, {"name": "z", "color": None}],
    }, scopes=("catalog",))
    imp = _import(_dryrun(pkg))
    assert imp["report"]["entities"]["tags"] == {"create": 1, "update": 0, "skip": 0, "error": 7}
    assert set(_tags()) == {"ok"}

def test_corrupted_member_fails_the_job_before_writing_anything():
    _seed_source()
    pkg = _export(["settings", "catalog"])
    forged = Path(tempfile.mkdtemp()) / "x.zip"
    with zipfile.ZipFile(pkg) as src, zipfile.ZipFile(forged, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            dst.writestr(n, data.replace(b"vip", b"VIP") if n == "data/tags.ndjson" else data)
    _wipe()
    dry = _dryrun(str(forged))
    assert dry["state"] == State.FAILED and "corrompido ou adulterado" in dry["error"]
    assert _tags() == {}

def test_package_from_another_product_fails_cleanly():
    d = Path(tempfile.mkdtemp())
    path = d / "pro.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({"schema_version": 1, "files": {}}))   # sem "product"
    dry = _dryrun(str(path))
    assert dry["state"] == State.FAILED and "WhatsBot-Lite" in dry["error"]

def test_package_with_no_known_entity_is_refused():
    dry = _dryrun(_build_pkg({"coisa_do_futuro": [{"a": 1}]}))
    assert dry["state"] == State.FAILED and "nenhuma entidade" in dry["error"]

def test_unknown_entity_files_are_ignored_next_to_known_ones():
    pkg = _build_pkg({"tags": [{"name": "t", "color": "red"}], "coisa_do_futuro": [{"a": 1}]})
    dry = _dryrun(pkg)
    assert dry["state"] == State.SUCCEEDED and set(dry["report"]["entities"]) == {"tags"}

def test_missing_package_file_fails_with_a_clear_message():
    j = jobs.create("import_dryrun", params={"package_path": str(Path(jobs.jobs_root()) / "x" / "p.zip")})
    _drain()
    assert "não está mais disponível" in jobs.get(j["id"])["error"]

def test_package_path_outside_the_jobs_root_is_refused(tmp_path):
    (tmp_path / "p.zip").write_bytes(b"zip")
    j = jobs.create("import_dryrun", params={"package_path": str(tmp_path / "p.zip")})
    _drain()
    assert "Caminho de pacote inválido" in jobs.get(j["id"])["error"]

# ── segredos ────────────────────────────────────────────────────────────────

@needs_crypto
def test_secret_roundtrip_with_the_right_password():
    _seed_source()
    pkg = _export(["settings"], password=PW)
    _wipe()
    dry = _dryrun(pkg, password=PW)
    assert dry["state"] == State.SUCCEEDED and dry["report"]["manifest"]["secrets"] == "ok"
    _import(dry, password=PW)
    assert _cfg("openrouter_api_key") == API_KEY

@needs_crypto
def test_wrong_password_fails_the_job_and_writes_nothing():
    _seed_source()
    pkg = _export(["settings"], password=PW)
    _wipe()
    dry = _dryrun(pkg, password="outra-senha-qualquer-1")
    assert dry["state"] == State.FAILED and "senha incorreta ou pacote adulterado" in dry["error"]
    assert _cfg() == {}

@needs_crypto
def test_package_with_secrets_but_no_password_imports_the_rest_and_warns():
    _seed_source()
    pkg = _export(["settings"], password=PW)
    _wipe()
    dry = _dryrun(pkg)                                       # sem senha
    assert dry["report"]["manifest"]["secrets"] == "locked"
    imp = _import(dry)
    assert imp["state"] == State.SUCCEEDED and _cfg("model") == "anthropic/claude-x"
    assert _cfg("openrouter_api_key", "AUSENTE") == "AUSENTE"
    assert imp["report"]["warnings"]["config"]["secret_missing"]["count"] == 1

@needs_crypto
def test_existing_api_key_is_kept_unless_the_operator_says_overwrite():
    from db.repositories import config_repo
    _seed_source()
    pkg = _export(["settings"], password=PW)
    _wipe()
    config_repo.set("openrouter_api_key", "chave-que-ja-existe")
    imp = _import(_dryrun(pkg, password=PW), password=PW)
    assert _cfg("openrouter_api_key") == "chave-que-ja-existe"
    assert imp["report"]["warnings"]["config"]["secret_kept"]["count"] == 1
    _import(_dryrun(pkg, password=PW), password=PW, overwrite_secrets=True)
    assert _cfg("openrouter_api_key") == API_KEY

@needs_crypto
def test_passwords_never_reach_the_database_or_the_report():
    from db.engine import get_engine
    from sqlalchemy import text
    _seed_source()
    pkg = _export(["settings"], password=PW)
    _wipe()
    _import(_dryrun(pkg, password=PW), password=PW)
    with get_engine().connect() as c:
        dump = " ".join(str(v) for row in c.execute(text("select * from portability_jobs")) for v in row)
        dump += " ".join(str(v) for row in c.execute(text("select * from portability_job_errors")) for v in row)
    assert PW not in dump

def test_secret_row_without_secrets_in_the_package_does_not_blank_the_destination():
    from db.repositories import config_repo
    config_repo.set("openrouter_api_key", "preservada")
    pkg = _build_pkg({"config": [{"key": "openrouter_api_key", "value": None, "secret": True}]})
    _import(_dryrun(pkg), strategy={"config": "overwrite"})
    assert _cfg("openrouter_api_key") == "preservada"

def test_a_secret_value_smuggled_in_clear_is_ignored():
    """Pacote que traz a chave EM CLARO no NDJSON (em vez de cifrada) não a grava."""
    pkg = _build_pkg({"config": [{"key": "openrouter_api_key", "value": "EM-CLARO", "secret": False}]})
    _import(_dryrun(pkg), strategy={"config": "overwrite"})
    assert _cfg("openrouter_api_key", "AUSENTE") == "AUSENTE"

# ── env e pós-processo ──────────────────────────────────────────────────────

def test_warns_when_an_env_variable_will_mask_the_imported_value(monkeypatch):
    monkeypatch.setenv("WHATSBOT_MODEL", "modelo-da-env")
    pkg = _build_pkg({"config": [{"key": "model", "value": "do-pacote", "secret": False},
                                 {"key": "auto_reply", "value": True, "secret": False}]})
    imp = _import(_dryrun(pkg))
    assert _cfg("model") == "do-pacote"                       # gravou no banco…
    assert imp["report"]["warnings"]["config"]["env_override_active"]["keys"] == ["model"]  # …e avisou

class _FakeHandler:
    def __init__(self):
        self.updates, self.reloads = [], 0
        self.tag_registry = type("T", (), {"_load": lambda s_: setattr(self, "reloads", self.reloads + 1),
                                           "all": lambda s_: {}})()

    def update_config(self, **kw):
        self.updates.append(kw)

class _FakeSettings:
    def get(self, key, default=None):
        from db.repositories import config_repo
        return config_repo.get(key, default)

def test_postprocess_refreshes_the_running_handler_without_firing_plugin_events(monkeypatch):
    import plugins.events as pe
    fired = []
    monkeypatch.setattr(pe, "emit", lambda *a, **k: fired.append(a))
    monkeypatch.setattr(pe, "emit_with_filter", lambda *a, **k: fired.append(a))
    h = _FakeHandler()
    jobs.set_runtime_deps(type("D", (), {"agent_handler": h, "settings": _FakeSettings()})())
    pkg = _build_pkg({"config": [{"key": "model", "value": "novo-modelo", "secret": False},
                                 {"key": "system_prompt", "value": "novo prompt", "secret": False}],
                      "tags": [{"name": "t", "color": "red"}]})
    imp = _import(_dryrun(pkg))
    assert imp["state"] == State.SUCCEEDED
    assert len(h.updates) == 1 and h.updates[0]["model"] == "novo-modelo"
    assert h.updates[0]["system_prompt"] == "novo prompt"
    assert h.reloads == 1 and imp["report"]["post"] == {
        "config_keys_changed": 2, "tags_reloaded": True}
    assert fired == []                                         # nenhum evento de plugin

def test_postprocess_is_skipped_for_a_dry_run_and_when_nothing_changed():
    h = _FakeHandler()
    jobs.set_runtime_deps(type("D", (), {"agent_handler": h, "settings": _FakeSettings()})())
    pkg = _build_pkg({"tags": [{"name": "t", "color": "red"}]}, scopes=("catalog",))
    _dryrun(pkg)
    assert h.updates == [] and h.reloads == 0
    _import(_dryrun(pkg))
    _import(_dryrun(pkg))                                      # 2ª vez: nada novo
    assert h.updates == [] and h.reloads == 1                  # só a 1ª vez recarregou

def test_postprocess_failure_does_not_undo_what_was_written():
    class Boom(_FakeHandler):
        def update_config(self, **kw):
            raise RuntimeError("quebrou")
    jobs.set_runtime_deps(type("D", (), {"agent_handler": Boom(), "settings": _FakeSettings()})())
    pkg = _build_pkg({"config": [{"key": "model", "value": "gravado", "secret": False}]})
    imp = _import(_dryrun(pkg))
    assert imp["state"] == State.SUCCEEDED and _cfg("model") == "gravado"
    assert imp["report"]["post"]["post_failed"] is True

# ── cancelamento ────────────────────────────────────────────────────────────

def test_cancelled_import_stops_between_batches(monkeypatch):
    monkeypatch.setattr(handlers, "BATCH", 2)
    pkg = _build_pkg({"tags": [{"name": f"t{i}", "color": "red"} for i in range(10)]}, scopes=("catalog",))
    dry = _dryrun(pkg)
    j = jobs.create("import", params={"package_path": dry["params"]["package_path"],
                                      "strategy": dry["report"]["strategy"]},
                    package_path=dry["params"]["package_path"], source_job=dry["id"])
    jobs.request_cancel(j["id"])                               # cancela na fila: nem começa
    _drain()
    assert jobs.get(j["id"])["state"] == State.CANCELLED and _tags() == {}

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
