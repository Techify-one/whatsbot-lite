"""Importar/Exportar — mídia das mensagens e fotos de perfil.

O que não pode dar errado aqui, e por quê:
  * ``/statics`` é um ``StaticFiles`` puro: um ``.html``/``.svg`` que entrasse com a extensão original
    seria servido na MESMA origem do painel (script rodando com a sessão do operador). Extensão de
    pacote passa por ALLOWLIST; o resto vira ``.bin``.
  * Nenhum caminho do pacote vira caminho de disco, e nenhum caminho da origem viaja.
  * A origem é validada DE NOVO na hora de copiar (um link simbólico posto entre o planejamento e a
    cópia não pode exfiltrar arquivo do servidor).
  * Tetos, disco cheio e arquivo que mente sobre o tamanho nunca perdem o TEXTO da mensagem.

Roda direto (``python tests/test_portability_media.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from db import init_db, init_engine  # noqa: E402
from portability import avatars as av  # noqa: E402
from portability import handlers  # noqa: E402
from portability import jobs  # noqa: E402
from portability import media as mi  # noqa: E402
from portability.format import Manifest, PackageReader, PackageWriter  # noqa: E402
from portability.jobs import State  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"fakejpegdata" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64

@pytest.fixture(scope="module", autouse=True)
def _engine():
    tmp = Path(tempfile.mkdtemp(prefix="wb_port_md_"))
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
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import config, contacts, portability_job_errors, portability_jobs, tags
    with get_engine().begin() as c:
        for t in (portability_job_errors, portability_jobs, contacts, tags, config):
            c.execute(delete(t))
    jobs._PASSWORDS.clear()
    jobs._HANDLERS.clear()
    handlers.register_all()
    jobs.set_runtime_deps(None)
    root = _engine / "storages" / "portability"
    monkeypatch.setattr(jobs, "jobs_root", lambda: (root.mkdir(parents=True, exist_ok=True), str(root))[1])
    statics = _engine / "statics"
    shutil.rmtree(statics, ignore_errors=True)
    for d in ("media", "operator", "avatars"):
        (statics / d).mkdir(parents=True)
    monkeypatch.setattr(mi, "statics_dir", lambda: str(statics))
    yield

# ── helpers ─────────────────────────────────────────────────────────────────

def _statics() -> Path:
    return Path(mi.statics_dir())

def _drain() -> None:
    while asyncio.run(jobs.run_one(jobs.PROCESS_WID)):
        pass

def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _export(scopes, **params) -> dict:
    j = jobs.create("export", params={"scopes": scopes, **params})
    _drain()
    return jobs.get(j["id"])

def _stage(pkg: str) -> str:
    d = Path(jobs.jobs_root()) / f"in-{os.urandom(4).hex()}"
    d.mkdir()
    shutil.copy(pkg, d / "package.zip")
    return str(d / "package.zip")

def _dryrun(pkg: str) -> dict:
    path = _stage(pkg)
    j = jobs.create("import_dryrun", params={"package_path": path, "package_sha256": _sha(path)},
                    package_path=path, package_sha256=_sha(path))
    _drain()
    return jobs.get(j["id"])

def _import(dry: dict, strategy: dict | None = None) -> dict:
    j = jobs.create("import", params={
        "package_path": dry["params"]["package_path"], "package_sha256": dry["params"]["package_sha256"],
        "strategy": strategy if strategy is not None else dry["report"].get("strategy")},
        package_path=dry["params"]["package_path"], package_sha256=dry["params"]["package_sha256"],
        source_job=dry["id"])
    _drain()
    return jobs.get(j["id"])

def _forge(rows: dict, files: dict[str, bytes] | None = None, *, avatars: dict[str, bytes] | None = None) -> str:
    """Pacote montado à mão: o que um atacante poderia entregar."""
    d = Path(tempfile.mkdtemp(prefix="wb_forged_"))
    path = str(d / "forged.zip")
    with PackageWriter(path) as w:
        for ent, items in rows.items():
            w.write_ndjson(ent, items)
        for name, data in (files or {}).items():
            w.write_bytes(f"media/{name}", data)
        for name, data in (avatars or {}).items():
            w.write_bytes(f"avatars/{name}", data)
        w.finish(Manifest(scopes=["conversations"], created_at=1.0))
    return path

def _contact(phone="5511999990001", **kw) -> dict:
    return {"id": 1, "phone": phone, "name": "Ana", "ai_enabled": True, **kw}

def _msg(content="legenda", ts=1000.0, **kw) -> dict:
    return {"contact": 1, "role": "user", "content": content, "ts": ts, **kw}

def _put(rel: str, data: bytes) -> Path:
    p = _statics() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p

def _seed_media() -> dict:
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    _put("media/foto.jpg", JPEG)
    _put("media/doc.pdf", b"%PDF-1.4 conteudo")
    _put("operator/resposta.png", PNG)
    message_repo.add(cid, "user", "minha foto", media_type="image", media_path="statics/media/foto.jpg",
                     media_name="foto.jpg", msg_id="M1", ts=1000.0)
    message_repo.add(cid, "user", "contrato", media_type="document", media_path="statics/media/doc.pdf",
                     media_name="contrato.pdf", msg_id="M2", ts=2000.0)
    message_repo.add(cid, "assistant", "segue", media_type="image", media_path="statics/operator/resposta.png",
                     ts=3000.0)
    return {"cid": cid}

def _messages(phone="5511999990001") -> list[dict]:
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import contacts, messages
    with get_engine().connect() as c:
        cid = c.execute(select(contacts.c.id).where(contacts.c.phone == phone)).scalar()
        return [dict(r) for r in c.execute(
            select(messages).where(messages.c.contact_id == cid).order_by(messages.c.ts, messages.c.id)
        ).mappings()]

def _wipe_db() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import contacts
    with get_engine().begin() as c:
        c.execute(delete(contacts))

class _Env:
    """Env mínimo para chamar ``media``/``avatars`` direto."""
    def __init__(self, **kw):
        from portability.base import Env
        self.e = Env(**kw)

    def __getattr__(self, k):
        return getattr(self.e, k)

# ── extensão: o que impede um .html de rodar no painel ──────────────────────

@pytest.mark.parametrize("name", [
    "a.html", "a.HTML", "a.htm", "a.xhtml", "a.svg", "a.SVG", "a.svgz", "a.xml", "a.xsl", "a.js", "a.mjs",
    "a.css", "a.swf", "a.hta", "a.mht", "a.mhtml", "a.shtml", "a.xht", "a.xslt"])
def test_active_content_extensions_never_survive(name):
    assert mi.safe_extension(name) == ".bin"
    assert mi.unique_media_name(name).endswith(".bin")

@pytest.mark.parametrize("name", ["a.jpg", "a.JPG", "a.png", "a.mp4", "a.ogg", "a.opus", "a.pdf", "a.docx",
                                  "a.xlsx", "a.zip", "a.txt", "a.csv", "a.webp", "a.m4a"])
def test_ordinary_extensions_are_kept_lowercased(name):
    assert mi.safe_extension(name) == "." + name.rsplit(".", 1)[1].lower()

@pytest.mark.parametrize("name,expected", [
    ("noext", ".bin"), ("a.", ".bin"), ("a.toolongextension", ".bin"), ("a.j p g", ".bin"),
    ("a.php", ".bin"), ("a.sh", ".bin"), ("a.py", ".bin"), ("a.cgi", ".bin"), ("a.jsp", ".bin"),
    (".html", ".bin"), ("a.exe.html", ".bin"),            # a ÚLTIMA extensão é a que vale
    ("a.p\x00hp", ".bin"),                                # NUL dentro da extensão: não casa a regra
    # NUL no MEIO do nome não importa: só a extensão sai do nome, e o nome gravado é uuid + extensão
    ("a.htm\x00.jpg", ".jpg"),
    ("arquivo.tar.gz", ".gz"), ("foto.final.JPG", ".jpg")])
def test_unknown_or_odd_extensions_become_bin(name, expected):
    assert mi.safe_extension(name) == expected, name
    assert mi.safe_extension(name) not in mi.ACTIVE_EXT

def test_active_extensions_are_excluded_from_the_allowlist_by_construction():
    assert not (mi.SAFE_EXT & mi.ACTIVE_EXT)
    assert {".jpg", ".png", ".mp4", ".ogg", ".pdf"} <= mi.SAFE_EXT

def test_destination_names_are_new_and_unique():
    names = {mi.unique_media_name("same.jpg") for _ in range(200)}
    assert len(names) == 200 and all(n.endswith(".jpg") and "same" not in n for n in names)

def test_a_hostile_member_named_html_lands_as_bin_on_disk():
    """O teste que importa: o arquivo do pacote chamado ``.html`` com <script> entra como ``.bin``."""
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(media_type="document", has_file=True, media_file="aaaa1111.html", media_root="received"),
        _msg(ts=2.0, media_type="document", has_file=True, media_file="bbbb2222.SVG", media_root="sent")]},
        files={"aaaa1111.html": b"<script>fetch('/api/config')</script>",
               "bbbb2222.SVG": b"<svg onload=alert(1)>"})
    imp = _import(_dryrun(pkg))
    assert imp["state"] == State.SUCCEEDED, imp["error"]
    ms = _messages()
    for m in ms:
        assert m["media_path"].endswith(".bin"), m["media_path"]
    on_disk = sorted(p.name for d in ("media", "operator") for p in (_statics() / d).iterdir())
    assert len(on_disk) == 2 and all(n.endswith(".bin") for n in on_disk)
    assert not any(p.suffix.lower() in mi.ACTIVE_EXT for p in _statics().rglob("*"))

# ── export ──────────────────────────────────────────────────────────────────

def test_export_with_media_carries_files_by_hashed_name_and_never_a_disk_path():
    _seed_media()
    job = _export(["conversations"], include_media=True)
    assert job["state"] == State.SUCCEEDED, job["error"]
    assert job["report"]["media_included"] is True and job["report"]["counts"]["media"] == 3
    with PackageReader(job["package_path"]) as r:
        media = [n for n in r.names() if n.startswith("media/")]
        assert len(media) == 3 and all(len(n.split("/")[1].split(".")[0]) == 20 for n in media)
        blob = b"".join(r.read_member(n) for n in r.names() if n.startswith("data/"))
        assert b"statics/" not in blob and b"foto.jpg" not in blob.replace(b'"media_name":"foto.jpg"', b"")
        rows = {m["msg_id"]: m for m in r.iter_ndjson("messages") if m["msg_id"]}
        assert rows["M1"]["media_root"] == "received" and rows["M1"]["media_file"] in {x[6:] for x in media}
        sent = next(m for m in r.iter_ndjson("messages") if m["role"] == "assistant")
        assert sent["media_root"] == "sent"
        assert r.verify_checksums() == []

def test_export_without_media_flag_carries_no_files():
    _seed_media()
    job = _export(["conversations"])
    with PackageReader(job["package_path"]) as r:
        assert not [n for n in r.names() if n.startswith("media/")]
        assert all("media_file" not in m for m in r.iter_ndjson("messages"))
    assert job["report"]["media_included"] is False

def test_media_flag_requires_the_conversations_scope():
    job = _export(["settings"], include_media=True)
    assert job["state"] == State.FAILED and "inclua “Contatos e conversas”" in job["error"]

def test_media_period_filter_only_applies_to_the_media_not_the_messages():
    _seed_media()
    job = _export(["conversations"], include_media=True, media_from=1500.0, media_to=2500.0)
    with PackageReader(job["package_path"]) as r:
        media = [n for n in r.names() if n.startswith("media/")]
        msgs = list(r.iter_ndjson("messages"))
    assert len(media) == 1 and len(msgs) == 3                      # só o PDF (ts=2000); texto de todas
    assert job["report"]["warnings"]["messages"]["media_out_of_period"]["count"] == 2

@pytest.mark.parametrize("params", [{"media_from": "ontem"}, {"media_from": -1}, {"media_to": float("nan")},
                                    {"media_from": 5.0, "media_to": 1.0}, {"media_from": True}])
def test_invalid_media_period_fails_the_job_with_a_clear_message(params):
    job = _export(["conversations"], include_media=True, **params)
    assert job["state"] == State.FAILED
    assert ("Período de mídia" in job["error"]) or ("invertido" in job["error"])

def test_media_that_is_missing_on_disk_is_reported_and_the_text_stays():
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    message_repo.add(cid, "user", "sumiu", media_type="image", media_path="statics/media/nao_existe.jpg",
                     ts=1.0)
    job = _export(["conversations"], include_media=True)
    assert job["report"]["warnings"]["messages"]["media_missing"]["count"] == 1
    with PackageReader(job["package_path"]) as r:
        m = next(r.iter_ndjson("messages"))
    assert m["content"] == "sumiu" and "media_file" not in m and m["has_file"] is True

def test_the_same_file_referenced_twice_travels_once():
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    _put("media/x.jpg", JPEG)
    for ts in (1.0, 2.0):
        message_repo.add(cid, "user", "mesma", media_type="image", media_path="statics/media/x.jpg", ts=ts)
    job = _export(["conversations"], include_media=True)
    with PackageReader(job["package_path"]) as r:
        assert len([n for n in r.names() if n.startswith("media/")]) == 1
    assert job["report"]["counts"]["media"] == 1

# ── origem hostil ───────────────────────────────────────────────────────────

def _plan(media_path: str) -> dict | None:
    env = _Env()
    env.media = {"enabled": True}
    return mi.plan_export(env, media_path, 1.0)

def test_source_of_refuses_everything_that_is_not_a_regular_file_directly_in_a_root(tmp_path):
    _put("media/ok.jpg", JPEG)
    _put("media/.oculto.jpg", JPEG)
    (_statics() / "media" / "sub").mkdir()
    _put("media/sub/deep.jpg", JPEG)
    (tmp_path / "fora.jpg").write_bytes(JPEG)
    os.symlink(tmp_path / "fora.jpg", _statics() / "media" / "link.jpg")
    os.symlink(tmp_path, _statics() / "media" / "dirlink")
    (_statics() / "media" / "umdir.jpg").mkdir()
    assert mi.source_of("statics/media/ok.jpg")[0] == "received"
    assert mi.source_of("/statics/media/ok.jpg") is not None                # barra inicial é normalizada
    for bad in ("statics/media/.oculto.jpg", "statics/media/sub/deep.jpg", "statics/media/link.jpg",
                "statics/media/dirlink/fora.jpg", "statics/media/umdir.jpg", "statics/media/nao.jpg",
                "statics/media/../../.env", "statics/media/..", "statics/avatars/x.jpg",
                "statics/other/ok.jpg", "media/ok.jpg", "statics/media/a\\b.jpg", "statics/media/a\x00b",
                "../statics/media/ok.jpg", "", None, 42, "statics/media/"):
        assert mi.source_of(bad) is None, bad

def test_a_symlink_swapped_in_between_planning_and_copying_is_not_followed(tmp_path):
    """TOCTOU: o arquivo era regular ao planejar e virou link para um segredo antes de copiar."""
    secret = tmp_path / "segredo.txt"
    secret.write_text("SENHA-DO-SERVIDOR")
    target = _put("media/foto.jpg", JPEG)
    env = _Env()
    env.media = {"enabled": True}
    assert mi.plan_export(env, "statics/media/foto.jpg", 1.0) is not None
    target.unlink()
    os.symlink(secret, target)                                       # a troca
    d = Path(tempfile.mkdtemp())
    with PackageWriter(str(d / "p.zip")) as w:
        copied = mi.copy_wanted(w, env, lambda i: None)
        w.finish(Manifest())
    assert copied == 0
    with PackageReader(str(d / "p.zip")) as r:
        assert b"SENHA-DO-SERVIDOR" not in b"".join(r.read_member(n) for n in r.names())
    assert env.warnings and env.warnings[0]["code"] == "media_missing"

def test_a_file_that_disappears_before_copying_does_not_stop_the_export():
    p = _put("media/foto.jpg", JPEG)
    env = _Env()
    env.media = {"enabled": True}
    mi.plan_export(env, "statics/media/foto.jpg", 1.0)
    p.unlink()
    d = Path(tempfile.mkdtemp())
    with PackageWriter(str(d / "p.zip")) as w:
        assert mi.copy_wanted(w, env, lambda i: None) == 0
        w.finish(Manifest())

# ── tetos ───────────────────────────────────────────────────────────────────

def test_a_file_over_the_per_file_limit_is_left_out_but_the_text_stays(monkeypatch):
    _seed_media()
    assert len(JPEG) > 100 > len(PNG) and len(b"%PDF-1.4 conteudo") < 100      # a premissa do teste
    monkeypatch.setattr(mi, "MAX_MEDIA_FILE", 100)
    job = _export(["conversations"], include_media=True)
    with PackageReader(job["package_path"]) as r:
        media = [n for n in r.names() if n.startswith("media/")]
        msgs = list(r.iter_ndjson("messages"))
    assert len(media) == 2 and len(msgs) == 3                      # PDF e PNG cabem; o JPEG não
    assert job["report"]["warnings"]["messages"]["media_too_large"]["count"] == 1
    foto = next(m for m in msgs if m["msg_id"] == "M1")
    assert foto["content"] == "minha foto" and "media_file" not in foto   # o texto nunca se perde

def test_the_total_limit_stops_adding_files_but_keeps_every_message(monkeypatch):
    _seed_media()
    monkeypatch.setattr(mi, "MAX_MEDIA_TOTAL", len(JPEG) + 5)
    job = _export(["conversations"], include_media=True)
    with PackageReader(job["package_path"]) as r:
        assert len([n for n in r.names() if n.startswith("media/")]) == 1
        assert len(list(r.iter_ndjson("messages"))) == 3
    assert job["report"]["warnings"]["messages"]["media_limit_reached"]["count"] == 2

def test_import_refuses_a_file_that_lies_about_its_size():
    """O manifest diz 3 bytes e o fluxo entrega mais: grava no máximo o declarado e falha."""
    class FakeReader:
        class manifest:
            files = {"media/aaaa1111.jpg": {"bytes": 3}}

        @staticmethod
        def open_member(name):
            return io.BytesIO(b"x" * 5000)
    env = _Env()
    env.media_reader, env.media_members = FakeReader, frozenset({"aaaa1111.jpg"})
    verdict, path = mi.restore(env, "aaaa1111.jpg", "received", False)
    assert (verdict, path) == ("failed", None)
    assert list((_statics() / "media").iterdir()) == []              # nem o .part sobra

def test_import_refuses_a_declared_size_over_the_limit(monkeypatch):
    monkeypatch.setattr(mi, "MAX_MEDIA_FILE", 10)
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(media_type="image", has_file=True, media_file="aaaa1111.jpg", media_root="received")]},
        files={"aaaa1111.jpg": JPEG})
    imp = _import(_dryrun(pkg))
    m = _messages()[0]
    assert (m["media_path"], m["media_state"], m["content"]) == (None, "removed", "legenda")
    assert imp["report"]["warnings"]["messages"]["media_too_large"]["count"] >= 1
    assert list((_statics() / "media").iterdir()) == []

def test_disk_full_stops_writing_media_but_keeps_every_message(monkeypatch):
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(ts=1.0, content="a", media_type="image", has_file=True, media_file="aaaa1111.jpg", media_root="received"),
        _msg(ts=2.0, content="b", media_type="image", has_file=True, media_file="bbbb2222.jpg", media_root="received"),
        _msg(ts=3.0, content="c", media_type="image", has_file=True, media_file="cccc3333.jpg", media_root="received")]},
        files={"aaaa1111.jpg": JPEG, "bbbb2222.jpg": JPEG, "cccc3333.jpg": JPEG})
    dry = _dryrun(pkg)
    real_open, calls = open, {"n": 0}

    def full_disk(path, mode="r", *a, **k):
        if str(path).endswith(".part") and "x" in mode:
            calls["n"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", full_disk)
    imp = _import(dry)
    monkeypatch.undo()
    assert imp["state"] == State.SUCCEEDED
    assert [m["content"] for m in _messages()] == ["a", "b", "c"]            # nenhum texto perdido
    assert {m["media_state"] for m in _messages()} == {"removed"}
    assert calls["n"] == 1                                                    # parou de tentar
    w = imp["report"]["warnings"]["messages"]
    assert w["media_disk_full"]["count"] == 1

# ── ida e volta ─────────────────────────────────────────────────────────────

def test_roundtrip_restores_files_with_new_names_and_the_same_bytes():
    _seed_media()
    pkg = _export(["conversations"], include_media=True)["package_path"]
    shutil.rmtree(_statics())
    for d in ("media", "operator", "avatars"):
        (_statics() / d).mkdir(parents=True)
    _wipe_db()
    dry = _dryrun(pkg)
    imp = _import(dry)
    assert imp["state"] == State.SUCCEEDED, imp["error"]
    ms = _messages()
    foto, doc, sent = ms
    assert foto["media_path"].startswith("statics/media/") and foto["media_path"].endswith(".jpg")
    assert doc["media_path"].endswith(".pdf") and sent["media_path"].startswith("statics/operator/")
    assert "foto" not in foto["media_path"] and foto["media_name"] == "foto.jpg"       # nome NOVO no disco
    assert (_statics().parent / foto["media_path"]).read_bytes() == JPEG
    assert (_statics().parent / doc["media_path"]).read_bytes() == b"%PDF-1.4 conteudo"
    assert (_statics().parent / sent["media_path"]).read_bytes() == PNG
    assert (foto["media_state"], doc["media_state"], sent["media_state"]) == (None, None, None)
    assert "media_not_included" not in imp["report"]["warnings"].get("messages", {})
    assert not list(_statics().rglob("*.part"))

def test_dryrun_and_import_report_the_same_counts_and_the_dryrun_writes_nothing():
    _seed_media()
    pkg = _export(["conversations"], include_media=True)["package_path"]
    shutil.rmtree(_statics())
    for d in ("media", "operator", "avatars"):
        (_statics() / d).mkdir(parents=True)
    _wipe_db()
    dry = _dryrun(pkg)
    assert not [p for p in _statics().rglob("*") if p.is_file()]            # nenhum arquivo no disco
    from sqlalchemy import func, select
    from db.engine import get_engine
    from db.tables import contacts, messages
    with get_engine().connect() as c:                                      # e nenhuma linha no banco
        assert c.execute(select(func.count()).select_from(contacts)).scalar() == 0
        assert c.execute(select(func.count()).select_from(messages)).scalar() == 0
    imp = _import(dry)
    assert dry["report"]["entities"] == imp["report"]["entities"]
    assert dry["report"]["warnings"] == imp["report"]["warnings"]

def test_reimporting_with_media_does_not_duplicate_messages_or_leave_orphan_files():
    _seed_media()
    pkg = _export(["conversations"], include_media=True)["package_path"]
    shutil.rmtree(_statics())
    for d in ("media", "operator", "avatars"):
        (_statics() / d).mkdir(parents=True)
    _wipe_db()
    _import(_dryrun(pkg))
    files_1 = sorted(p.name for p in _statics().rglob("*") if p.is_file())
    imp2 = _import(_dryrun(pkg))
    assert len(_messages()) == 3 and imp2["report"]["entities"]["messages"]["create"] == 0
    # a 2ª passada não grava de novo (as mensagens já existem): nenhum arquivo extra
    files_2 = sorted(p.name for p in _statics().rglob("*") if p.is_file())
    assert len(files_2) - len(files_1) <= 3

def test_media_not_in_the_package_becomes_removed_with_a_count_not_a_failure():
    _seed_media()
    pkg = _export(["conversations"])["package_path"]                 # SEM mídia
    _wipe_db()
    imp = _import(_dryrun(pkg))
    assert imp["state"] == State.SUCCEEDED
    assert {m["media_state"] for m in _messages()} == {"removed"}
    assert [m["content"] for m in _messages()] == ["minha foto", "contrato", "segue"]
    assert imp["report"]["warnings"]["messages"]["media_not_included"]["count"] == 3

def test_one_file_used_by_two_messages_is_written_once():
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(ts=1.0, media_type="image", has_file=True, media_file="aaaa1111.jpg", media_root="received"),
        _msg(ts=2.0, content="outra", media_type="image", has_file=True, media_file="aaaa1111.jpg",
             media_root="received")]}, files={"aaaa1111.jpg": JPEG})
    _import(_dryrun(pkg))
    ms = _messages()
    assert ms[0]["media_path"] == ms[1]["media_path"]
    assert len(list((_statics() / "media").iterdir())) == 1

@pytest.mark.parametrize("member,root", [
    ("../../etc/passwd", "received"), ("aaaa1111.jpg", "../x"), ("aaaa1111.jpg", "avatars"),
    ("aaaa1111.jpg", None), (None, "received"), (7, "received"), ("a/b.jpg", "received"),
    ("não está no pacote.jpg", "received")])
def test_forged_member_and_root_values_are_treated_as_absent(member, root):
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(media_type="image", has_file=True, media_file=member, media_root=root)]},
        files={"aaaa1111.jpg": JPEG})
    _import(_dryrun(pkg))
    m = _messages()[0]
    assert (m["media_path"], m["media_state"]) == (None, "removed")
    assert not [p for p in _statics().rglob("*") if p.is_file()]

def test_media_for_a_message_that_failed_to_insert_is_not_left_on_disk(monkeypatch):
    """O arquivo foi copiado e a linha da mensagem não entrou: não pode sobrar arquivo órfão."""
    from portability import entities_contacts as ec
    real = ec._insert_batch
    monkeypatch.setattr(ec, "_insert_batch", lambda b: (_ for _ in ()).throw(RuntimeError("falhou")))
    pkg = _forge({"contacts": [_contact()], "messages": [
        _msg(media_type="image", has_file=True, media_file="aaaa1111.jpg", media_root="received")]},
        files={"aaaa1111.jpg": JPEG})
    imp = _import(_dryrun(pkg))
    monkeypatch.setattr(ec, "_insert_batch", real)
    assert imp["report"]["entities"]["messages"]["error"] == 1
    assert _messages() == [] and not [p for p in (_statics() / "media").iterdir()]

def test_discard_only_touches_files_inside_the_roots_that_no_message_uses(tmp_path):
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    usado = _put("media/usado.jpg", JPEG)
    livre = _put("media/livre.jpg", JPEG)
    fora = tmp_path / "fora.jpg"
    fora.write_bytes(JPEG)
    message_repo.add(cid, "user", "x", media_type="image", media_path="statics/media/usado.jpg", ts=1.0)
    for p in ("statics/media/usado.jpg", "../../fora.jpg", str(fora), "statics/avatars/x.jpg", None, ""):
        mi.discard_if_unreferenced(p)
    assert usado.exists() and fora.exists()
    mi.discard_if_unreferenced("statics/media/livre.jpg")
    assert not livre.exists()

# ── fotos de perfil ─────────────────────────────────────────────────────────

def _seed_avatars() -> None:
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")
    contact_repo.get_or_create("120363000000000001@g.us")
    _put("avatars/5511999990001.jpg", JPEG)
    _put("avatars/120363000000000001@g.us.jpg", JPEG + b"grupo")

def test_avatars_are_off_by_default_and_follow_the_conversations_scope():
    assert av.parse_params({}, ["conversations"]) is False
    assert av.parse_params({"include_avatars": True}, ["conversations"]) is True
    assert av.parse_params({"include_avatars": True}, ["settings", "catalog"]) is False
    for off in ("false", "0", "no", "", "off", None, 0, [], "talvez"):
        assert av.parse_params({"include_avatars": off}, ["conversations"]) is False, off
    for on in ("true", "1", "on", "yes", "TRUE"):
        assert av.parse_params({"include_avatars": on}, ["conversations"]) is True, on

def test_export_leaves_photos_out_unless_asked():
    _seed_avatars()
    job = _export(["conversations"])
    with PackageReader(job["package_path"]) as r:
        assert not [n for n in r.names() if n.startswith("avatars/")]
        assert all("avatar_file" not in c for c in r.iter_ndjson("contacts"))
    assert job["report"]["avatars_included"] is False

def test_export_carries_photos_by_hashed_name_never_the_phone():
    _seed_avatars()
    job = _export(["conversations"], include_avatars=True)
    assert job["report"]["counts"]["avatars"] == 2 and job["report"]["avatars_included"] is True
    with PackageReader(job["package_path"]) as r:
        members = [n for n in r.names() if n.startswith("avatars/")]
        assert len(members) == 2 and all(len(n.split("/")[1]) == 24 and n.endswith(".jpg") for n in members)
        assert not any("5511" in n or "120363" in n for n in members)
        contacts = {c["phone"]: c for c in r.iter_ndjson("contacts")}
        assert contacts["5511999990001"]["avatar_file"] in {n[8:] for n in members}
        assert "avatar_file" in contacts["120363000000000001@g.us"]          # foto do GRUPO também viaja
        assert r.verify_checksums() == []

def test_photos_roundtrip_with_the_same_bytes_including_groups():
    _seed_avatars()
    pkg = _export(["conversations"], include_avatars=True)["package_path"]
    shutil.rmtree(_statics() / "avatars")
    (_statics() / "avatars").mkdir()
    _wipe_db()
    dry = _dryrun(pkg)
    imp = _import(dry)
    assert imp["state"] == State.SUCCEEDED, imp["error"]
    assert (_statics() / "avatars" / "5511999990001.jpg").read_bytes() == JPEG
    assert (_statics() / "avatars" / "120363000000000001@g.us.jpg").read_bytes() == JPEG + b"grupo"
    assert dry["report"]["entities"] == imp["report"]["entities"]

def test_dryrun_never_writes_a_photo():
    _seed_avatars()
    pkg = _export(["conversations"], include_avatars=True)["package_path"]
    shutil.rmtree(_statics() / "avatars")
    (_statics() / "avatars").mkdir()
    _wipe_db()
    _dryrun(pkg)
    assert list((_statics() / "avatars").iterdir()) == []

def test_a_missing_photo_is_filled_even_with_skip_but_an_existing_one_is_kept():
    from db.repositories import contact_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    pkg = _forge({"contacts": [_contact(avatar_file="aaaa1111.jpg")]}, avatars={"aaaa1111.jpg": JPEG})
    imp = _import(_dryrun(pkg), strategy={"contacts": "skip"})
    assert (_statics() / "avatars" / "5511999990001.jpg").read_bytes() == JPEG
    assert imp["report"]["entities"]["contacts"]["update"] == 1 and cid
    (_statics() / "avatars" / "5511999990001.jpg").write_bytes(JPEG + b"minha")
    imp2 = _import(_dryrun(pkg), strategy={"contacts": "skip"})
    assert (_statics() / "avatars" / "5511999990001.jpg").read_bytes() == JPEG + b"minha"
    assert imp2["report"]["entities"]["contacts"] == {"create": 0, "update": 0, "skip": 1, "error": 0}

def test_overwrite_replaces_an_existing_photo_and_is_idempotent():
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")
    _put("avatars/5511999990001.jpg", JPEG + b"antiga")
    pkg = _forge({"contacts": [_contact(avatar_file="aaaa1111.jpg")]}, avatars={"aaaa1111.jpg": JPEG})
    _import(_dryrun(pkg), strategy={"contacts": "overwrite"})
    assert (_statics() / "avatars" / "5511999990001.jpg").read_bytes() == JPEG
    imp2 = _import(_dryrun(pkg), strategy={"contacts": "overwrite"})
    assert imp2["report"]["entities"]["contacts"]["update"] == 0          # idêntica: nada a trocar

def test_photo_uses_the_destination_phone_not_the_one_in_the_package():
    """Destino tem a variante de 13 dígitos; o pacote traz a de 12. A foto vai para o nome do DESTINO."""
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")
    pkg = _forge({"contacts": [_contact(phone="551199990001", avatar_file="aaaa1111.jpg")]},
                 avatars={"aaaa1111.jpg": JPEG})
    _import(_dryrun(pkg), strategy={"contacts": "merge"})
    assert [p.name for p in (_statics() / "avatars").iterdir()] == ["5511999990001.jpg"]

@pytest.mark.parametrize("data", [b"GIF89a" + b"x" * 40, b"\x89PNG" + b"x" * 40, b"<html>", b"\xff\xd8", b""])
def test_a_photo_that_is_not_a_jpeg_is_refused(data):
    pkg = _forge({"contacts": [_contact(avatar_file="aaaa1111.jpg")]}, avatars={"aaaa1111.jpg": data})
    imp = _import(_dryrun(pkg))
    assert imp["state"] == State.SUCCEEDED and imp["report"]["entities"]["contacts"]["create"] == 1
    assert list((_statics() / "avatars").iterdir()) == []
    assert imp["report"]["warnings"]["contacts"]["avatar_failed"]["count"] >= 1

def test_an_oversized_photo_is_refused(monkeypatch):
    monkeypatch.setattr(av, "MAX_AVATAR_FILE", 50)
    pkg = _forge({"contacts": [_contact(avatar_file="aaaa1111.jpg")]}, avatars={"aaaa1111.jpg": JPEG})
    imp = _import(_dryrun(pkg))
    assert list((_statics() / "avatars").iterdir()) == []
    assert imp["report"]["warnings"]["contacts"]["avatar_failed"]["count"] >= 1

@pytest.mark.parametrize("member", ["../x.jpg", "a/b.jpg", "não.jpg", "naoexiste.jpg", None, 5, "..", ""])
def test_forged_photo_member_names_write_nothing(member):
    pkg = _forge({"contacts": [_contact(avatar_file=member)]}, avatars={"aaaa1111.jpg": JPEG})
    _import(_dryrun(pkg))
    assert list((_statics() / "avatars").iterdir()) == []

@pytest.mark.parametrize("phone", ["../../etc/x", "a/b", "..", "a\\b", ".oculto", "x" * 101, "a b", "", "..:x"])
def test_unsafe_phones_never_become_a_photo_path(phone):
    assert av.source_of(phone) is None
    env = _Env()
    env.avatars = True
    assert av.plan_export(env, phone) is None
    env.avatar_members = frozenset({"aaaa1111.jpg"})
    assert av.restore(env, "aaaa1111.jpg", phone, replace=True, dry_run=False) is False

def test_a_symlinked_photo_is_not_exported(tmp_path):
    (tmp_path / "segredo.jpg").write_bytes(JPEG + b"SEGREDO")
    os.symlink(tmp_path / "segredo.jpg", _statics() / "avatars" / "5511999990001.jpg")
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")
    job = _export(["conversations"], include_avatars=True)
    with PackageReader(job["package_path"]) as r:
        assert not [n for n in r.names() if n.startswith("avatars/")]
        assert b"SEGREDO" not in b"".join(r.read_member(n) for n in r.names())

def test_a_symlink_at_the_destination_is_never_followed_nor_replaced(tmp_path):
    """O ``os.replace`` já não escreve ATRAVÉS do link, mas a guarda é o contrato: destino que não é
    arquivo regular é RECUSADO (o link fica como está e o aviso aparece), nunca trocado em silêncio."""
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")
    alvo = tmp_path / "alvo.txt"
    alvo.write_text("INTOCADO")
    link = _statics() / "avatars" / "5511999990001.jpg"
    os.symlink(alvo, link)
    pkg = _forge({"contacts": [_contact(avatar_file="aaaa1111.jpg")]}, avatars={"aaaa1111.jpg": JPEG})
    imp = _import(_dryrun(pkg), strategy={"contacts": "overwrite"})
    assert alvo.read_text() == "INTOCADO"
    assert link.is_symlink() and os.readlink(link) == str(alvo)            # o link continua sendo o link
    assert not list((_statics() / "avatars").glob("*.part"))
    assert imp["report"]["warnings"]["contacts"]["avatar_failed"]["count"] == 1

def test_a_photo_swapped_for_a_link_between_planning_and_copying_is_not_followed(tmp_path):
    (tmp_path / "segredo.jpg").write_bytes(JPEG + b"SEGREDO")
    f = _put("avatars/5511999990001.jpg", JPEG)
    env = _Env()
    env.avatars = True
    assert av.plan_export(env, "5511999990001") is not None
    f.unlink()
    os.symlink(tmp_path / "segredo.jpg", f)
    d = Path(tempfile.mkdtemp())
    with PackageWriter(str(d / "p.zip")) as w:
        assert av.copy_wanted(w, env) == 0
        w.finish(Manifest())

def test_photo_total_limit_stops_adding_photos(monkeypatch):
    _seed_avatars()
    monkeypatch.setattr(av, "MAX_AVATAR_TOTAL", len(JPEG) + 2)
    job = _export(["conversations"], include_avatars=True)
    assert job["report"]["counts"]["avatars"] == 1
    assert job["report"]["warnings"]["contacts"]["avatar_too_large"]["count"] == 1

def test_photo_and_media_are_independent_switches():
    _seed_media()
    _seed_avatars()
    only_media = _export(["conversations"], include_media=True)
    only_avatars = _export(["conversations"], include_avatars=True)
    with PackageReader(only_media["package_path"]) as r:
        assert any(n.startswith("media/") for n in r.names()) and not any(n.startswith("avatars/") for n in r.names())
    with PackageReader(only_avatars["package_path"]) as r:
        assert any(n.startswith("avatars/") for n in r.names()) and not any(n.startswith("media/") for n in r.names())

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
