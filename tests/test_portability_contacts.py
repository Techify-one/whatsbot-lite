"""Importar/Exportar — contatos, observações, tags de contato e mensagens.

É o conteúdo que os CLIENTES escreveram, então os testes miram o que não pode dar errado:
  * nada duplica ao reimportar, nem com a variante BR do mesmo número (12 x 13 dígitos);
  * importar NÃO é atividade: contadores de não lidas zerados, ``updated_at`` intocado, a ordem da
    lista de conversas não muda por causa de um import;
  * o caminho de arquivo da origem nunca viaja nem é confiado (``media_path`` do pacote);
  * a prévia e o import dão as mesmas contagens, inclusive nas mensagens;
  * pacote adulterado (papel inventado, id de contato forjado, localização maliciosa) não grava lixo;
  * nenhum evento de plugin é emitido.

Roda direto (``python tests/test_portability_contacts.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from db import init_db, init_engine  # noqa: E402
from portability import entities_contacts as ec  # noqa: E402
from portability import handlers, jobs  # noqa: E402
from portability.format import Manifest, PackageReader, PackageWriter  # noqa: E402
from portability.jobs import State  # noqa: E402

@pytest.fixture(scope="module", autouse=True)
def _engine():
    tmp = Path(tempfile.mkdtemp(prefix="wb_port_ct_"))
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
    yield

def _wipe() -> None:
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import (config, contacts, portability_job_errors, portability_jobs, tags)
    with get_engine().begin() as c:
        for t in (portability_job_errors, portability_jobs, contacts, tags, config):
            c.execute(delete(t))          # contacts apaga mensagens, observações e vínculos (CASCADE)

# ── helpers ─────────────────────────────────────────────────────────────────

def _drain() -> None:
    while asyncio.run(jobs.run_one(jobs.PROCESS_WID)):
        pass

def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _export(scopes) -> str:
    j = jobs.create("export", params={"scopes": scopes})
    _drain()
    got = jobs.get(j["id"])
    assert got["state"] == State.SUCCEEDED, got["error"]
    return got["package_path"]

def _stage(pkg: str) -> str:
    d = Path(jobs.jobs_root()) / f"in-{os.urandom(4).hex()}"
    d.mkdir()
    shutil.copy(pkg, d / "package.zip")
    return str(d / "package.zip")

def _dryrun(pkg: str, strategy: dict | None = None) -> dict:
    path = _stage(pkg)
    params = {"package_path": path, "package_sha256": _sha(path)}
    if strategy is not None:
        params["strategy"] = strategy
    j = jobs.create("import_dryrun", params=params, package_path=path, package_sha256=_sha(path))
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

def _build_pkg(rows: dict[str, list[dict]], scopes=("conversations",)) -> str:
    d = Path(tempfile.mkdtemp(prefix="wb_forged_"))
    path = str(d / "forged.zip")
    with PackageWriter(path) as w:
        for ent, items in rows.items():
            w.write_ndjson(ent, items)
        w.finish(Manifest(scopes=list(scopes), created_at=1.0))
    return path

def _contact(phone="5511999990001", **kw) -> dict:
    return {"id": 1, "phone": phone, "name": "Ana", "ai_enabled": True, **kw}

def _msg(contact=1, content="oi", ts=1000.0, role="user", **kw) -> dict:
    return {"contact": contact, "role": role, "content": content, "ts": ts, **kw}

def _set_contact(contact_id: int, **fields) -> None:
    """Grava colunas direto na tabela. ``contact_repo.update`` SEMPRE sobrescreve ``updated_at`` com
    'agora' (mesmo que se passe outro valor), então não serve para semear um ``updated_at`` fixo."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import contacts
    with get_engine().begin() as c:
        c.execute(update(contacts).where(contacts.c.id == contact_id).values(**fields))

def _seed() -> dict:
    """Origem com dados de verdade: dois contatos, um grupo, tags, observações e mensagens de todo
    tipo — inclusive o que o app guarda e o import não pode levar de brinde."""
    from db.repositories import contact_repo, message_repo, tag_repo
    tag_repo.create("vip", "#0f0")
    tag_repo.create("lead", "red")
    ana = contact_repo.get_or_create("5511999990001")["id"]
    bia = contact_repo.get_or_create("5521888880002")["id"]
    grp = contact_repo.get_or_create("120363000000000001@g.us")["id"]
    _set_contact(ana, name="Ana Souza", email="ana@ex.com", profession="dev", company="ACME",
                 address="Rua A, 1", is_pinned=1, unread_count=7, unread_ai_count=3,
                 has_unread_mention=1, updated_at=111.0, created_at=100.0)
    _set_contact(bia, name="Bia", ai_enabled=0, ai_disabled_at=555.0, is_archived=1,
                 archived_by_app=1, updated_at=222.0, created_at=200.0)
    _set_contact(grp, is_group=1, group_name="Grupo da Firma", updated_at=333.0, created_at=300.0)
    tag_repo.set_contact_tags(ana, ["vip", "lead"])
    contact_repo.set_observations(ana, ["prefere áudio", "cliente desde 2020"])
    message_repo.add(ana, "user", "oi, tudo bem?", msg_id="3EB0AAAA", ts=1000.0)
    message_repo.add(ana, "assistant", "Olá! Como posso ajudar?", status="ai", ts=1001.0)
    message_repo.add(ana, "private_note", "cliente VIP, atenção", ts=1002.0)
    message_repo.add(ana, "system_notice", "Conversa transferida", ts=1003.0)
    message_repo.add(ana, "transcription", "texto do áudio", ts=1004.0)
    message_repo.add(ana, "user", "olha onde estou", media_type="location",
                     media_path="geo:-23.55,-46.63", msg_id="3EB0LOC", ts=1005.0)
    message_repo.add(ana, "user", "foto", media_type="image", media_path="statics/media/abc.jpg",
                     media_name="foto.jpg", msg_id="3EB0IMG", ts=1006.0, reply_to_msg_id="3EB0AAAA")
    message_repo.add(ana, "user", "vídeo velho", media_type="video", media_path=None,
                     media_state="discarded", msg_id="3EB0VID", ts=1007.0)
    message_repo.add(ana, "user", "enquete", media_type="poll", msg_id="3EB0POLL", ts=1008.0)
    message_repo.add(bia, "user", "sou a Bia", ts=2000.0)
    message_repo.add(grp, "user", "[Fulano]: bom dia, pessoal", ts=3000.0)
    from db.engine import get_engine
    from db.tables import messages
    from sqlalchemy import update
    with get_engine().begin() as c:        # reação e revogação (colunas que só o app preenche)
        c.execute(update(messages).where(messages.c.msg_id == "3EB0AAAA").values(
            reactions=json.dumps({"👍": ["5511"]}), revoked=0))
        c.execute(update(messages).where(messages.c.msg_id == "3EB0VID").values(revoked=1))
    return {"ana": ana, "bia": bia, "grp": grp}

def _contacts() -> dict:
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import contacts
    with get_engine().connect() as c:
        return {r["phone"]: dict(r) for r in c.execute(select(contacts)).mappings()}

def _messages(phone: str) -> list[dict]:
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import contacts, messages
    with get_engine().connect() as c:
        cid = c.execute(select(contacts.c.id).where(contacts.c.phone == phone)).scalar()
        return [dict(r) for r in c.execute(
            select(messages).where(messages.c.contact_id == cid).order_by(messages.c.ts, messages.c.id)
        ).mappings()]

def _counts() -> tuple[int, int, int]:
    from sqlalchemy import func, select
    from db.engine import get_engine
    from db.tables import contacts, messages, observations
    with get_engine().connect() as c:
        return tuple(c.execute(select(func.count()).select_from(t)).scalar()
                     for t in (contacts, messages, observations))

# ── export ──────────────────────────────────────────────────────────────────

def test_export_never_carries_a_disk_path_or_unread_state():
    _seed()
    pkg = _export(["settings", "catalog", "conversations"])
    with PackageReader(pkg) as r:
        blob = b"".join(r.read_member(n) for n in r.names())
        msgs = list(r.iter_ndjson("messages"))
        contacts = {c["phone"]: c for c in r.iter_ndjson("contacts")}
    assert b"statics/media" not in blob and b"abc.jpg" not in blob.replace(b"foto.jpg", b"")
    assert all("media_path" not in m for m in msgs)
    assert all("unread_count" not in c and "has_unread_mention" not in c for c in contacts.values())
    img = next(m for m in msgs if m["msg_id"] == "3EB0IMG")
    assert img["has_file"] is True and img["media_geo"] is None and img["media_name"] == "foto.jpg"
    geo = next(m for m in msgs if m["msg_id"] == "3EB0LOC")
    assert geo["media_geo"] == "geo:-23.55,-46.63" and geo["has_file"] is False

def test_export_keeps_conversation_order_and_every_role():
    src = _seed()
    with PackageReader(_export(["conversations"])) as r:
        msgs = list(r.iter_ndjson("messages"))
        contacts = {c["id"]: c["phone"] for c in r.iter_ndjson("contacts")}
    assert {"user", "assistant", "private_note", "system_notice", "transcription"} <= {m["role"] for m in msgs}
    per_contact: dict[int, list[float]] = {}
    for m in msgs:
        per_contact.setdefault(m["contact"], []).append(m["ts"])
    assert all(v == sorted(v) for v in per_contact.values())               # ordem cronológica por contato
    assert len(msgs) == 11 and len(per_contact) == 3
    assert contacts[src["ana"]] == "5511999990001" and len(per_contact[src["ana"]]) == 9
    # cada mensagem aponta para um contato que existe no pacote (senão o import a descartaria)
    assert set(per_contact) <= set(contacts)

def test_export_pages_with_the_composite_cursor_even_with_equal_timestamps(monkeypatch):
    """O ``ts`` sozinho pularia mensagens com o mesmo timestamp na virada de página."""
    from db.repositories import contact_repo, message_repo
    monkeypatch.setattr(ec, "_PAGE", 3)
    cid = contact_repo.get_or_create("5511900000000")["id"]
    for i in range(10):
        message_repo.add(cid, "user", f"m{i}", ts=500.0)            # tudo no MESMO segundo
    with PackageReader(_export(["conversations"])) as r:
        got = [m["content"] for m in r.iter_ndjson("messages")]
    assert sorted(got) == sorted(f"m{i}" for i in range(10)) and len(got) == 10

# ── ida e volta ─────────────────────────────────────────────────────────────

def test_roundtrip_restores_contacts_messages_notes_and_links():
    src = _seed()
    pkg = _export(["settings", "catalog", "conversations"])
    _wipe()
    dry = _dryrun(pkg)
    assert dry["state"] == State.SUCCEEDED, dry["error"]
    imp = _import(dry)
    assert imp["state"] == State.SUCCEEDED, imp["error"]
    cs = _contacts()
    ana = cs["5511999990001"]
    assert (ana["name"], ana["email"], ana["profession"], ana["company"], ana["address"]) == (
        "Ana Souza", "ana@ex.com", "dev", "ACME", "Rua A, 1")
    assert ana["is_pinned"] == 1 and ana["created_at"] == 100.0
    bia = cs["5521888880002"]
    assert bia["ai_enabled"] == 0 and bia["ai_disabled_at"] == 555.0 and bia["is_archived"] == 1
    grp = cs["120363000000000001@g.us"]
    assert grp["is_group"] == 1 and grp["group_name"] == "Grupo da Firma"
    from db.repositories import contact_repo, tag_repo
    assert sorted(tag_repo.get_contact_tags(ana["id"])) == ["lead", "vip"]
    assert contact_repo.get_observations(ana["id"]) == ["prefere áudio", "cliente desde 2020"]
    msgs = _messages("5511999990001")
    assert [m["content"] for m in msgs] == [
        "oi, tudo bem?", "Olá! Como posso ajudar?", "cliente VIP, atenção", "Conversa transferida",
        "texto do áudio", "olha onde estou", "foto", "vídeo velho", "enquete"]
    assert [m["role"] for m in msgs][2:5] == ["private_note", "system_notice", "transcription"]
    assert msgs[0]["reactions"] == json.dumps({"👍": ["5511"]}) and msgs[0]["msg_id"] == "3EB0AAAA"
    assert msgs[6]["reply_to_msg_id"] == "3EB0AAAA" and msgs[7]["revoked"] == 1
    # a mensagem foi parar no contato certo pelo TELEFONE, não pelo id de origem
    assert {m["contact_id"] for m in _messages("5511999990001")} == {ana["id"]}
    assert src["ana"] and len(_messages("5521888880002")) == 1 and len(_messages("120363000000000001@g.us")) == 1

def test_dryrun_and_import_report_the_same_counts_including_messages():
    _seed()
    pkg = _export(["catalog", "conversations"])
    _wipe()
    dry = _dryrun(pkg)
    imp = _import(dry)
    assert dry["report"]["entities"] == imp["report"]["entities"]
    t = imp["report"]["entities"]
    assert t["contacts"]["create"] == 3 and t["messages"]["create"] == 11
    assert dry["report"]["warnings"] == imp["report"]["warnings"]

def test_dryrun_writes_nothing():
    _seed()
    pkg = _export(["catalog", "conversations"])
    _wipe()
    _dryrun(pkg)
    assert _counts() == (0, 0, 0)

def test_reimporting_never_duplicates_anything():
    _seed()
    pkg = _export(["catalog", "conversations"])
    _wipe()
    _import(_dryrun(pkg))
    before = _counts()
    imp2 = _import(_dryrun(pkg))
    assert _counts() == before
    t = imp2["report"]["entities"]
    # nada novo E nada "atualizado": contato idêntico é SKIP, senão o relatório mente a cada reimport
    assert t["contacts"] == {"create": 0, "update": 0, "skip": 3, "error": 0}
    assert t["messages"] == {"create": 0, "update": 0, "skip": 11, "error": 0}

def test_import_adds_only_the_missing_messages_to_an_existing_conversation():
    _seed()
    pkg = _export(["conversations"])
    _wipe()
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    message_repo.add(cid, "user", "oi, tudo bem?", msg_id="3EB0AAAA", ts=1000.0)   # já existe
    message_repo.add(cid, "user", "mensagem só daqui", ts=1500.0)                  # só no destino
    imp = _import(_dryrun(pkg))
    contents = [m["content"] for m in _messages("5511999990001")]
    assert contents.count("oi, tudo bem?") == 1 and "mensagem só daqui" in contents
    assert len(contents) == 9 + 1 and imp["report"]["entities"]["messages"]["skip"] >= 1

def test_identical_messages_in_the_same_second_are_not_collapsed():
    """Duas mensagens iguais no mesmo segundo (sem msg_id) são DUAS: vira multiconjunto, não conjunto."""
    pkg = _build_pkg({"contacts": [_contact()], "messages": [_msg(content="ok"), _msg(content="ok"),
                                                              _msg(content="ok")]})
    _import(_dryrun(pkg))
    assert [m["content"] for m in _messages("5511999990001")] == ["ok", "ok", "ok"]
    _import(_dryrun(pkg))                                           # reimport: continua 3
    assert len(_messages("5511999990001")) == 3

def test_only_the_missing_copies_of_identical_messages_are_added():
    """Destino já tem 1 'ok' no mesmo segundo; o pacote tem 3. Entram 2 (total 3), não 0 nem 3.
    É a diferença entre multiconjunto e conjunto: só aparece com o destino parcialmente preenchido."""
    from db.repositories import contact_repo, message_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    message_repo.add(cid, "user", "ok", ts=1000.0)
    pkg = _build_pkg({"contacts": [_contact()], "messages": [_msg(content="ok") for _ in range(3)]})
    imp = _import(_dryrun(pkg))
    assert len(_messages("5511999990001")) == 3
    assert imp["report"]["entities"]["messages"] == {"create": 2, "update": 0, "skip": 1, "error": 0}

# ── telefone ────────────────────────────────────────────────────────────────

def test_brazilian_phone_variants_are_the_same_contact():
    """12 x 13 dígitos: o WhatsApp entrega um, o operador digita o outro. Não pode virar 2 contatos."""
    from db.repositories import contact_repo
    contact_repo.get_or_create("5511999990001")                    # 13 dígitos no destino
    pkg = _build_pkg({"contacts": [_contact(phone="551199990001", name="Ana")],     # 12 dígitos no pacote
                      "messages": [_msg(content="oi")]})
    imp = _import(_dryrun(pkg), strategy={"contacts": "merge", "messages": "skip"})
    assert set(_contacts()) == {"5511999990001"}
    assert imp["report"]["entities"]["contacts"]["update"] == 1
    assert _contacts()["5511999990001"]["name"] == "Ana"
    assert [m["content"] for m in _messages("5511999990001")] == ["oi"]

def test_the_same_number_twice_in_one_package_creates_one_contact_in_both_runs():
    pkg = _build_pkg({"contacts": [_contact(id=1, phone="5511999990001", name="A"),
                                   _contact(id=2, phone="551199990001", name="B")],
                      "messages": [_msg(contact=1, content="da A"), _msg(contact=2, content="da B")]})
    dry = _dryrun(pkg)
    imp = _import(dry)
    assert dry["report"]["entities"] == imp["report"]["entities"]
    assert imp["report"]["entities"]["contacts"] == {"create": 1, "update": 0, "skip": 1, "error": 0}
    assert len(_contacts()) == 1
    assert {m["content"] for m in _messages("5511999990001")} == {"da A", "da B"}   # nada perdido
    assert imp["report"]["warnings"]["contacts"]["duplicate_in_package"]["count"] == 1

# ── importar não é atividade ────────────────────────────────────────────────

def test_unread_state_enters_zeroed_and_updated_at_is_kept():
    _seed()
    pkg = _export(["conversations"])
    _wipe()
    _import(_dryrun(pkg))
    ana = _contacts()["5511999990001"]
    assert (ana["unread_count"], ana["unread_ai_count"], ana["has_unread_mention"]) == (0, 0, 0)
    assert ana["archived_by_app"] == 0
    assert ana["updated_at"] == 111.0                              # NÃO virou "agora"
    from db.engine import get_engine
    from sqlalchemy import func, select
    from db.tables import unread_msg_ids
    with get_engine().connect() as c:
        assert c.execute(select(func.count()).select_from(unread_msg_ids)).scalar() == 0

def test_importing_does_not_reorder_the_conversation_list():
    """A lista ordena por COALESCE(última mensagem, updated_at). Contato importado SEM mensagens tem
    de ficar onde o ``updated_at`` dele manda, não subir ao topo por ter sido criado agora."""
    from db.repositories import contact_repo, message_repo
    novo = contact_repo.get_or_create("5500000000001")["id"]
    message_repo.add(novo, "user", "recente", ts=time.time())
    pkg = _build_pkg({"contacts": [_contact(phone="5511999990001", updated_at=50.0, created_at=50.0)]})
    _import(_dryrun(pkg))
    order = [c["phone"] for c in contact_repo.list_contacts()]
    assert order == ["5500000000001", "5511999990001"]

def test_merge_and_overwrite_do_not_touch_updated_at_either():
    from db.repositories import contact_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    pkg = _build_pkg({"contacts": [_contact(name="Ana Nova")]})
    for strat in ("merge", "overwrite"):
        _set_contact(cid, name="", updated_at=42.0)
        _import(_dryrun(pkg), strategy={"contacts": strat})
        assert _contacts()["5511999990001"]["name"] == "Ana Nova", strat
        assert _contacts()["5511999990001"]["updated_at"] == 42.0, strat

def test_ai_disabled_at_is_only_kept_when_the_ai_is_off():
    pkg = _build_pkg({"contacts": [
        _contact(id=1, phone="5511900000001", ai_enabled=True, ai_disabled_at=999.0),
        _contact(id=2, phone="5511900000002", ai_enabled=False, ai_disabled_at=777.0),
        _contact(id=3, phone="5511900000003", ai_enabled=False)]})
    _import(_dryrun(pkg))
    cs = _contacts()
    assert cs["5511900000001"]["ai_disabled_at"] is None            # ligada ⇒ NULL, como o app grava
    assert cs["5511900000002"]["ai_disabled_at"] == 777.0
    assert cs["5511900000003"]["ai_disabled_at"] == cs["5511900000003"]["created_at"]

# ── estratégias de contato ──────────────────────────────────────────────────

def test_skip_never_changes_an_existing_contact_but_merge_only_fills_blanks():
    from db.repositories import contact_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    contact_repo.update(cid, name="Nome do destino", company="")
    pkg = _build_pkg({"contacts": [_contact(name="Nome do pacote", company="ACME", email="a@b.c")]})
    _import(_dryrun(pkg), strategy={"contacts": "skip"})
    c = _contacts()["5511999990001"]
    assert (c["name"], c["company"], c["email"]) == ("Nome do destino", "", "")
    _import(_dryrun(pkg), strategy={"contacts": "merge"})
    c = _contacts()["5511999990001"]
    assert (c["name"], c["company"], c["email"]) == ("Nome do destino", "ACME", "a@b.c")
    _import(_dryrun(pkg), strategy={"contacts": "overwrite"})
    assert _contacts()["5511999990001"]["name"] == "Nome do pacote"

def test_overwrite_replaces_tags_but_merge_only_adds_them():
    from db.repositories import contact_repo, tag_repo
    tag_repo.create("a", "red")
    tag_repo.create("b", "blue")
    tag_repo.create("c", "green")
    cid = contact_repo.get_or_create("5511999990001")["id"]
    tag_repo.set_contact_tags(cid, ["a", "b"])
    pkg = _build_pkg({"contacts": [_contact(tags=["b", "c"])]})
    _import(_dryrun(pkg), strategy={"contacts": "merge"})
    assert sorted(tag_repo.get_contact_tags(cid)) == ["a", "b", "c"]
    tag_repo.set_contact_tags(cid, ["a", "b"])
    _import(_dryrun(pkg), strategy={"contacts": "overwrite"})
    assert sorted(tag_repo.get_contact_tags(cid)) == ["b", "c"]

def test_unknown_tag_is_dropped_with_a_warning_not_created():
    from db.repositories import tag_repo
    pkg = _build_pkg({"contacts": [_contact(tags=["inexistente", "vip"])]})
    imp = _import(_dryrun(pkg))
    assert tag_repo.get_all() == {}                                  # tag nova nunca nasce por contato
    assert imp["report"]["warnings"]["contacts"]["tag_unknown"]["count"] == 2

def test_a_tag_created_by_the_same_package_counts_as_known_in_the_dryrun():
    pkg = _build_pkg({"tags": [{"name": "vip", "color": "red"}], "contacts": [_contact(tags=["vip"])]},
                     scopes=("catalog", "conversations"))
    dry = _dryrun(pkg)
    assert "tag_unknown" not in dry["report"]["warnings"].get("contacts", {})
    _import(dry)
    from db.repositories import tag_repo
    assert tag_repo.get_contact_tags(_contacts()["5511999990001"]["id"]) == ["vip"]

def test_contact_tag_names_obey_the_same_30_char_limit_as_the_tags_route():
    """POST /api/tags recusa nome com mais de 30 caracteres; o vínculo do contato não pode ser a porta
    dos fundos para uma tag que a tela nunca aceitaria."""
    pkg = _build_pkg({"contacts": [
        _contact(id=1, phone="5511900000001", tags=["t" * 30]),
        _contact(id=2, phone="5511900000002", tags=["t" * 31]),
        _contact(id=3, phone="5511900000003", tags=[""]),
        _contact(id=4, phone="5511900000004", tags=[7])]})
    imp = _import(_dryrun(pkg))
    assert imp["report"]["entities"]["contacts"] == {"create": 1, "update": 0, "skip": 0, "error": 3}

def test_observations_are_added_once_and_never_duplicated():
    pkg = _build_pkg({"contacts": [_contact(observations=[
        {"text": "a", "created_at": 1.0}, {"text": "a", "created_at": 2.0},
        {"text": "b", "created_at": 3.0}, {"text": "   ", "created_at": 4.0}])]})
    _import(_dryrun(pkg))
    _import(_dryrun(pkg), strategy={"contacts": "merge"})
    from db.repositories import contact_repo
    assert contact_repo.get_observations(_contacts()["5511999990001"]["id"]) == ["a", "b"]

# ── mídia ───────────────────────────────────────────────────────────────────

def test_media_without_its_file_becomes_removed_but_keeps_the_text():
    pkg = _build_pkg({"contacts": [_contact()], "messages": [
        _msg(ts=1.0, content="legenda da foto", media_type="image", has_file=True, media_name="f.jpg")]})
    imp = _import(_dryrun(pkg))
    m = _messages("5511999990001")[0]
    assert (m["content"], m["media_type"], m["media_path"], m["media_state"], m["media_name"]) == (
        "legenda da foto", "image", None, "removed", "f.jpg")
    assert imp["report"]["warnings"]["messages"]["media_not_included"]["count"] == 1

def test_location_poll_and_discarded_media_are_not_reported_as_lost():
    pkg = _build_pkg({"contacts": [_contact()], "messages": [
        _msg(ts=1.0, media_type="location", media_geo="geo:-23.5,-46.6"),
        _msg(ts=2.0, media_type="poll"),
        _msg(ts=3.0, media_type="video", media_state="discarded"),
        _msg(ts=4.0, media_type="image", media_state="removed")]})
    imp = _import(_dryrun(pkg))
    ms = _messages("5511999990001")
    assert (ms[0]["media_path"], ms[0]["media_state"]) == ("geo:-23.5,-46.6", None)
    assert (ms[1]["media_path"], ms[1]["media_state"]) == (None, None)
    assert (ms[2]["media_state"], ms[3]["media_state"]) == ("discarded", "removed")
    assert "media_not_included" not in imp["report"]["warnings"].get("messages", {})

def test_a_media_path_smuggled_in_the_package_is_never_used():
    """Pacote adulterado com ``media_path`` apontando para fora (ou para um arquivo do servidor)."""
    for evil in ("../../../etc/passwd", "/etc/passwd", "statics/media/../../.env", "C:\\Windows\\x"):
        _wipe()
        pkg = _build_pkg({"contacts": [_contact()], "messages": [
            _msg(media_type="document", media_path=evil, has_file=True, media_file=evil, media_root=evil)]})
        _import(_dryrun(pkg))
        m = _messages("5511999990001")[0]
        assert m["media_path"] is None, evil
        assert m["media_state"] == "removed", evil

def test_malicious_geo_values_are_rejected_not_stored():
    for bad in ("geo:1,2;DROP TABLE x", "geo:../../x", "http://evil/x", "geo:", "geo:1,2,3", "/etc/passwd"):
        _wipe()
        pkg = _build_pkg({"contacts": [_contact()], "messages": [
            _msg(media_type="location", media_geo=bad), _msg(ts=2.0, content="ok")]})
        imp = _import(_dryrun(pkg))
        assert imp["report"]["entities"]["messages"]["error"] == 1, bad
        assert [m["content"] for m in _messages("5511999990001")] == ["ok"], bad

# ── pacote adulterado ───────────────────────────────────────────────────────

def test_invalid_rows_are_reported_individually_and_never_stop_the_import():
    pkg = _build_pkg({
        "contacts": [_contact(id=1), _contact(id=2, phone="!!"), _contact(id=3, phone="5511900000003", name=5),
                     _contact(id=4, phone="5511900000004", ai_enabled=7), {"id": 5},
                     _contact(id=6, phone="5511900000006", tags="vip"),
                     _contact(id=-1, phone="5511900000007")],
        "messages": [_msg(), _msg(role="hacker"), _msg(content=5), _msg(ts="ontem"), _msg(ts=float("inf")),
                     _msg(contact=-1), _msg(contact="1"), _msg(reactions="não é json"),
                     _msg(reactions='["lista"]'), _msg(media_state="weird"), _msg(has_file=3),
                     _msg(ts=9.0, content="boa")]})
    imp = _import(_dryrun(pkg))
    t = imp["report"]["entities"]
    assert t["contacts"] == {"create": 1, "update": 0, "skip": 0, "error": 6}
    assert t["messages"]["create"] == 2 and t["messages"]["error"] == 10
    assert imp["state"] == State.SUCCEEDED
    errs = jobs.list_errors(imp["id"])
    assert len(errs) == 16 and {e["code"] for e in errs} == {"invalid_row"}

def test_error_details_never_contain_message_content_or_a_full_phone():
    pkg = _build_pkg({"contacts": [_contact(id=1), _contact(id=2, phone="5511999990002", name=5)],
                      "messages": [_msg(content="SEGREDO DA CONVERSA", role="hacker")]})
    imp = _import(_dryrun(pkg))
    dump = json.dumps(jobs.list_errors(imp["id"])) + json.dumps(imp["report"])
    assert "SEGREDO DA CONVERSA" not in dump and "5511999990002" not in dump
    assert any(e["detail"]["key"].startswith("***") for e in jobs.list_errors(imp["id"]) if e["entity"] == "contacts")

def test_message_for_a_contact_that_is_not_in_the_package_is_skipped_with_a_warning():
    """Pacote só com mensagens (ou com ids que não batem): nunca grava no contato errado."""
    from db.repositories import contact_repo
    cid = contact_repo.get_or_create("5511999990001")["id"]
    pkg = _build_pkg({"messages": [_msg(contact=1, content="órfã")]})
    imp = _import(_dryrun(pkg))
    assert imp["report"]["entities"]["messages"]["skip"] == 1
    assert imp["report"]["warnings"]["messages"]["contact_unavailable"]["count"] == 1
    assert _messages("5511999990001") == [] and cid

def test_ids_in_the_package_never_pick_the_destination_row():
    """O id do contato no pacote é só um rótulo: um id que COINCIDE com o de outro contato do destino
    não pode jogar mensagens lá."""
    from db.repositories import contact_repo
    outro = contact_repo.get_or_create("5599000000009")["id"]
    pkg = _build_pkg({"contacts": [_contact(id=outro, phone="5511999990001")],
                      "messages": [_msg(contact=outro, content="é da Ana")]})
    _import(_dryrun(pkg))
    assert [m["content"] for m in _messages("5511999990001")] == ["é da Ana"]
    assert _messages("5599000000009") == []

def test_limits_are_enforced():
    big = "x" * (ec.MAX_FIELD + 1)
    pkg = _build_pkg({"contacts": [
        _contact(id=1, phone="5511900000001", name=big),
        _contact(id=2, phone="5511900000002", tags=[f"t{i}" for i in range(ec.MAX_TAGS + 1)]),
        _contact(id=3, phone="5511900000003", observations=[{"text": "o"}] * (ec.MAX_OBS + 1)),
        _contact(id=4, phone="5511900000004", observations=[{"text": "y" * (ec.MAX_OBS_TEXT + 1)}]),
        _contact(id=5, phone="5511900000005")]})
    imp = _import(_dryrun(pkg))
    assert imp["report"]["entities"]["contacts"] == {"create": 1, "update": 0, "skip": 0, "error": 4}

# ── lote ────────────────────────────────────────────────────────────────────

def test_a_failing_batch_is_rewritten_row_by_row_and_the_report_is_corrected(monkeypatch):
    """Uma linha ruim derruba o INSERT em lote; as boas têm de entrar e a contagem não pode mentir."""
    real = ec._insert_batch
    poison = "VENENO"

    def fake(batch):
        if any(r["content"] == poison for r in batch):
            if len(batch) > 1:
                raise RuntimeError("lote falhou")
            raise RuntimeError("linha ruim")
        return real(batch)
    monkeypatch.setattr(ec, "_insert_batch", fake)
    pkg = _build_pkg({"contacts": [_contact()], "messages": [
        _msg(ts=1.0, content="a"), _msg(ts=2.0, content=poison), _msg(ts=3.0, content="c")]})
    imp = _import(_dryrun(pkg))
    assert [m["content"] for m in _messages("5511999990001")] == ["a", "c"]
    t = imp["report"]["entities"]["messages"]
    assert t["create"] == 2 and t["error"] == 1
    errs = [e for e in jobs.list_errors(imp["id"]) if e["entity"] == "messages"]
    assert len(errs) == 1 and errs[0]["code"] == "row_failed" and poison not in json.dumps(errs)

def test_flush_happens_at_the_batch_size_and_at_the_end(monkeypatch):
    sizes = []
    real = ec._insert_batch
    monkeypatch.setattr(ec, "FLUSH_AT", 4)
    monkeypatch.setattr(ec, "_insert_batch", lambda b: (sizes.append(len(b)), real(b))[1])
    pkg = _build_pkg({"contacts": [_contact()], "messages": [_msg(ts=float(i), content=f"m{i}") for i in range(10)]})
    _import(_dryrun(pkg))
    assert sizes == [4, 4, 2] and len(_messages("5511999990001")) == 10

def test_sqlite_lock_is_retried_instead_of_losing_the_batch(monkeypatch):
    from sqlalchemy.exc import OperationalError
    import db.engine as eng
    from db.repositories import contact_repo
    calls = {"n": 0}
    real_get = eng.get_engine

    class FlakyEngine:
        """O primeiro ``begin()`` estoura como o SQLite quando o webhook segura o lock de escrita."""
        def __init__(self, e):
            self._e = e

        def begin(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("insert", {}, Exception("database is locked"))
            return self._e.begin()

        def __getattr__(self, k):
            return getattr(self._e, k)
    cid = contact_repo.get_or_create("5511999990001")["id"]
    monkeypatch.setattr(ec, "_LOCK_RETRIES", 3)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "get_engine", lambda: FlakyEngine(real_get()))
    ec._insert_batch([{"contact_id": cid, "role": "user", "content": "x", "ts": 1.0}])
    monkeypatch.setattr(eng, "get_engine", real_get)
    assert calls["n"] == 2 and len(_messages("5511999990001")) == 1

def test_a_lock_that_never_clears_is_raised_not_swallowed(monkeypatch):
    from sqlalchemy.exc import OperationalError
    import db.engine as eng
    real_get = eng.get_engine

    class Locked:
        def __init__(self, e):
            self._e = e

        def begin(self):
            raise OperationalError("insert", {}, Exception("database is locked"))

        def __getattr__(self, k):
            return getattr(self._e, k)
    monkeypatch.setattr(ec, "_LOCK_RETRIES", 2)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(eng, "get_engine", lambda: Locked(real_get()))
    with pytest.raises(OperationalError):
        ec._insert_batch([{"contact_id": 1, "role": "user", "content": "x", "ts": 1.0}])

# ── sem efeito colateral ────────────────────────────────────────────────────

def test_import_fires_no_plugin_event_and_no_new_message_broadcast(monkeypatch):
    import plugins.events as pe
    import plugins.context as pc
    fired = []
    monkeypatch.setattr(pe, "emit", lambda *a, **k: fired.append(("emit", a)))
    monkeypatch.setattr(pe, "emit_with_filter", lambda *a, **k: fired.append(("filter", a)))
    monkeypatch.setattr(pc, "broadcast", lambda ev, data: fired.append(("broadcast", ev)))
    pkg = _build_pkg({"contacts": [_contact()], "messages": [_msg()]})
    _import(_dryrun(pkg))
    assert not [f for f in fired if f[0] != "broadcast"]
    assert all(f[1] not in ("new_message", "message_status") for f in fired if f[0] == "broadcast")

# ── escopo ──────────────────────────────────────────────────────────────────

def test_conversations_scope_exports_contacts_and_messages_together():
    _seed()
    with PackageReader(_export(["conversations"])) as r:
        assert r.has("data/contacts.ndjson") and r.has("data/messages.ndjson")
        assert not r.has("data/config.ndjson") and not r.has("data/tags.ndjson")

def test_a_package_with_tags_scope_only_does_not_touch_contacts():
    _seed()
    pkg = _export(["catalog"])
    _wipe()
    _import(_dryrun(pkg))
    assert _counts() == (0, 0, 0)

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
