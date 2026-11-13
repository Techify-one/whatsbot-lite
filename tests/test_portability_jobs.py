"""Importar/Exportar — jobs assíncronos (``portability.jobs``).

Prova o que o runner promete: claim atômico (sem ``FOR UPDATE SKIP LOCKED``, que o SQLite não tem),
afinidade da senha ao processo, órfão voltando à fila, cancelamento, teto de erros por linha e limpeza
de pacote vencido. Roda em SQLite temporário; com ``WHATSBOT_TEST_DB_URL`` roda no Postgres.

Roda direto (``python tests/test_portability_jobs.py``) ou coletado pelo pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from db import init_db, init_engine  # noqa: E402
from portability import jobs  # noqa: E402
from portability.jobs import State  # noqa: E402

@pytest.fixture(scope="module", autouse=True)
def _engine():
    """Engine próprio do módulo (outros testes do repo inicializam o seu no import)."""
    tmp = Path(tempfile.mkdtemp(prefix="wb_port_jobs_"))
    url = os.environ.get("WHATSBOT_TEST_DB_URL", "").strip()
    if url:
        init_engine(url)
        from db.connection import _run_alembic_upgrade
        _run_alembic_upgrade()
    else:
        init_db(tmp / "whatsbot.db")
    yield tmp

@pytest.fixture(autouse=True)
def _clean(_engine, monkeypatch):
    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import portability_job_errors, portability_jobs
    with get_engine().begin() as c:
        c.execute(delete(portability_job_errors))
        c.execute(delete(portability_jobs))
    jobs._PASSWORDS.clear()
    jobs._HANDLERS.clear()
    root = _engine / "storages" / "portability"
    monkeypatch.setattr(jobs, "jobs_root", lambda: (root.mkdir(parents=True, exist_ok=True), str(root))[1])
    yield

def _drain() -> int:
    n = 0
    while asyncio.run(jobs.run_one(jobs.PROCESS_WID)):
        n += 1
    return n

# ── criação e leitura ───────────────────────────────────────────────────────

def test_create_roundtrips_json_params_with_unicode():
    j = jobs.create("export", params={"scopes": ["settings"], "nome": "ação"})
    got = jobs.get(j["id"])
    assert got["state"] == State.QUEUED and got["kind"] == "export"
    assert got["params"] == {"scopes": ["settings"], "nome": "ação"}
    assert got["report"] == {} and got["progress"] == {}
    assert got["cancel_requested"] is False and got["interrupted"] is False

def test_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        jobs.create("apagar_tudo")

def test_kind_check_constraint_in_database():
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError
    from db.engine import get_engine
    from db.tables import portability_jobs
    with pytest.raises(IntegrityError):
        with get_engine().begin() as c:
            c.execute(insert(portability_jobs).values(id="x", kind="lixo", created_at=1.0))

def test_list_recent_newest_first_and_filter():
    a = jobs.create("export")
    time.sleep(0.01)
    b = jobs.create("import")
    assert [r["id"] for r in jobs.list_recent()] == [b["id"], a["id"]]
    assert [r["id"] for r in jobs.list_recent(kind="export")] == [a["id"]]
    assert len(jobs.list_recent(limit=1)) == 1

# ── claim ───────────────────────────────────────────────────────────────────

def test_claim_takes_oldest_first_and_marks_running():
    a = jobs.create("export")
    time.sleep(0.01)
    jobs.create("export")
    got = jobs.claim_next("w1")
    assert got["id"] == a["id"] and got["state"] == State.RUNNING
    assert got["worker_id"] == "w1" and got["started_at"] and got["heartbeat_at"]

def test_claim_on_empty_queue_is_none():
    assert jobs.claim_next("w1") is None

def test_claim_skips_job_with_another_workers_affinity():
    j = jobs.create("export", affinity="outro-processo")
    assert jobs.claim_next("w1") is None                    # a senha desse job vive em OUTRO processo
    assert jobs.claim_next("outro-processo")["id"] == j["id"]

def test_concurrent_claims_yield_exactly_one_winner():
    """O desempate é o rowcount do UPDATE: 20 threads disputando 1 job, 1 vence."""
    jobs.create("export")
    winners, barrier = [], threading.Barrier(20)

    def go(i):
        barrier.wait()
        got = jobs.claim_next(f"w{i}")
        if got:
            winners.append(got["worker_id"])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(winners) == 1, winners

def test_concurrent_claims_never_hand_the_same_job_twice():
    for _ in range(6):
        jobs.create("export")
    got, lock, barrier = [], threading.Lock(), threading.Barrier(12)

    def go(i):
        barrier.wait()
        while True:
            j = jobs.claim_next(f"w{i}")
            if not j:
                return
            with lock:
                got.append(j["id"])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 6 and len(set(got)) == 6

# ── heartbeat, progresso, fim ───────────────────────────────────────────────

def test_heartbeat_only_for_the_owner():
    j = jobs.create("export")
    jobs.claim_next("w1")
    assert jobs.heartbeat(j["id"], "w1") is True
    assert jobs.heartbeat(j["id"], "intruso") is False

def test_progress_report_and_finish():
    j = jobs.create("export")
    jobs.claim_next("w1")
    jobs.set_progress(j["id"], {"stage": "export", "percent": 50})
    jobs.finish(j["id"], State.SUCCEEDED, report={"counts": {"tags": 3}}, expires_in=60)
    got = jobs.get(j["id"])
    assert got["state"] == State.SUCCEEDED and got["finished_at"] and got["expires_at"]
    assert got["progress"]["percent"] == 50 and got["report"]["counts"]["tags"] == 3

def test_finish_truncates_error_and_rejects_bad_state():
    j = jobs.create("export")
    jobs.finish(j["id"], State.FAILED, error="x" * 9000)
    assert len(jobs.get(j["id"])["error"]) == jobs.MAX_ERROR_CHARS
    with pytest.raises(ValueError):
        jobs.finish(j["id"], State.RUNNING)

def test_pause_keeps_job_open_and_clears_worker():
    j = jobs.create("import")
    jobs.claim_next("w1")
    jobs.finish(j["id"], State.AWAITING_RESTART)
    got = jobs.get(j["id"])
    assert got["state"] == State.AWAITING_RESTART and got["worker_id"] is None
    assert got["finished_at"] is None

# ── cancelamento ────────────────────────────────────────────────────────────

def test_cancel_queued_is_immediate():
    j = jobs.create("export")
    assert jobs.request_cancel(j["id"]) == State.CANCELLED
    assert jobs.get(j["id"])["finished_at"] is not None

def test_cancel_running_only_raises_the_flag():
    j = jobs.create("export")
    jobs.claim_next("w1")
    assert jobs.request_cancel(j["id"]) == State.RUNNING     # o handler confere entre lotes
    assert jobs.is_cancel_requested(j["id"]) is True

def test_cancel_terminal_is_noop_and_unknown_is_none():
    j = jobs.create("export")
    jobs.finish(j["id"], State.SUCCEEDED)
    assert jobs.request_cancel(j["id"]) == State.SUCCEEDED
    assert jobs.is_cancel_requested(j["id"]) is False
    assert jobs.request_cancel("nao-existe") is None

# ── confirmação e retomada ──────────────────────────────────────────────────

def test_enqueue_dryrun_only_from_awaiting_confirmation():
    j = jobs.create("import_dryrun", state=State.AWAITING_CONFIRMATION)
    assert jobs.enqueue_dryrun(j["id"], {"a": 1}, package_sha256="abc") is True
    got = jobs.get(j["id"])
    assert got["state"] == State.QUEUED and got["params"] == {"a": 1} and got["package_sha256"] == "abc"
    assert jobs.enqueue_dryrun(j["id"], {"a": 2}) is False   # já saiu desse estado

def test_resume_requires_a_reason_to_resume():
    j = jobs.create("import")
    jobs.finish(j["id"], State.AWAITING_RESTART)
    assert jobs.resume(j["id"]) is False                    # sem resume_stage e sem interrupted
    jobs.set_resume_stage(j["id"], "plugins")
    assert jobs.resume(j["id"], affinity="p1") is True
    got = jobs.get(j["id"])
    assert got["state"] == State.QUEUED and got["affinity"] == "p1"

def test_resume_drops_dead_affinity_when_no_password():
    j = jobs.create("import", affinity="processo-morto")
    jobs.finish(j["id"], State.AWAITING_RESTART)
    jobs.set_resume_stage(j["id"], "plugins")
    assert jobs.resume(j["id"], affinity=None) is True
    assert jobs.get(j["id"])["affinity"] is None            # senão ninguém o reivindicaria nunca

def test_recalculate_resets_the_same_dryrun_and_clears_errors():
    d = jobs.create("import_dryrun", params={"strategy": {"tags": "skip"}})
    jobs.claim_next("w")
    jobs.add_errors(d["id"], [{"entity": "tags", "row_index": 1, "code": "x"}])
    jobs.finish(d["id"], State.SUCCEEDED, report={"old": True})
    assert jobs.requeue_dryrun(d["id"], {"tags": "overwrite"}, affinity="p1") is True
    got = jobs.get(d["id"])
    assert got["state"] == State.QUEUED and got["report"] == {} and got["finished_at"] is None
    assert got["params"]["strategy"] == {"tags": "overwrite"} and got["affinity"] == "p1"
    assert jobs.list_errors(d["id"]) == []

def test_recalculate_refused_once_an_import_was_created_from_it():
    d = jobs.create("import_dryrun")
    jobs.claim_next("w")
    jobs.finish(d["id"], State.SUCCEEDED)
    jobs.create("import", source_job=d["id"])
    assert jobs.has_import_from(d["id"]) is True
    assert jobs.requeue_dryrun(d["id"], {}) is False

def test_recalculate_refused_for_non_dryrun_or_unfinished():
    e = jobs.create("export")
    jobs.finish(e["id"], State.SUCCEEDED)
    assert jobs.requeue_dryrun(e["id"], {}) is False
    q = jobs.create("import_dryrun")
    assert jobs.requeue_dryrun(q["id"], {}) is False        # ainda na fila
    assert jobs.requeue_dryrun("nao-existe", {}) is False

def test_find_imported_by_hash():
    a = jobs.create("import", package_sha256="h1")
    jobs.claim_next("w")
    jobs.finish(a["id"], State.SUCCEEDED)
    b = jobs.create("import", package_sha256="h2")
    jobs.claim_next("w")
    jobs.finish(b["id"], State.FAILED, error="x")
    assert jobs.find_imported("h1") == a["id"]
    assert jobs.find_imported("h2") is None                 # só import CONCLUÍDO conta
    assert jobs.find_imported("zzz") is None

def test_busy_jobs_excludes_the_given_one():
    a = jobs.create("export")
    b = jobs.create("import")
    jobs.claim_next("w")
    assert {x["id"] for x in jobs.busy_jobs()} == {a["id"], b["id"]}
    assert [x["id"] for x in jobs.busy_jobs(except_id=a["id"])] == [b["id"]]

# ── órfãos ──────────────────────────────────────────────────────────────────

def _make_stale(kind: str, *, has_password: bool = False, recoveries: int = 0) -> str:
    j = jobs.create(kind, params={"has_password": has_password})
    jobs.claim_next("morto")
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import portability_jobs
    with get_engine().begin() as c:
        c.execute(update(portability_jobs).where(portability_jobs.c.id == j["id"]).values(
            heartbeat_at=time.time() - 3600, recoveries=recoveries))
    return j["id"]

def test_orphan_import_without_password_goes_back_to_the_queue():
    jid = _make_stale("import")
    assert jobs.recover_orphans()["requeued"] == 1
    got = jobs.get(jid)
    assert got["state"] == State.QUEUED and got["worker_id"] is None and got["recoveries"] == 1

def test_orphan_import_with_password_waits_for_the_operator():
    jid = _make_stale("import", has_password=True)          # a senha morreu junto com o processo
    assert jobs.recover_orphans()["interrupted"] == 1
    got = jobs.get(jid)
    assert got["state"] == State.AWAITING_RESTART and got["interrupted"] is True
    assert got["expires_at"] is not None

def test_orphan_import_stops_requeueing_after_the_limit():
    jid = _make_stale("import", recoveries=jobs.MAX_AUTO_RECOVERIES)
    assert jobs.recover_orphans()["interrupted"] == 1       # não fica em laço para sempre
    assert jobs.get(jid)["state"] == State.AWAITING_RESTART

def test_orphan_export_and_dryrun_fail():
    a, b = _make_stale("export"), _make_stale("import_dryrun")
    assert jobs.recover_orphans()["failed"] == 2
    for jid in (a, b):
        got = jobs.get(jid)
        assert got["state"] == State.FAILED and "reiniciou" in got["error"]

def test_fresh_heartbeat_is_not_an_orphan():
    j = jobs.create("import")
    jobs.claim_next("vivo")
    assert jobs.recover_orphans() == {"requeued": 0, "interrupted": 0, "failed": 0}
    assert jobs.get(j["id"])["state"] == State.RUNNING

def test_queued_job_with_dead_process_affinity_is_recovered():
    """Confirmado e o servidor caiu antes do claim: a senha estava na memória do processo morto."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import portability_jobs
    e = jobs.create("export", affinity="processo-morto")
    i = jobs.create("import", affinity="processo-morto")
    with get_engine().begin() as c:
        c.execute(update(portability_jobs).values(created_at=time.time() - 3600))
    out = jobs.recover_orphans(wid="eu")
    assert out["failed"] == 1 and out["requeued"] + out["interrupted"] == 1
    assert jobs.get(e["id"])["state"] == State.FAILED
    assert jobs.get(i["id"])["state"] in (State.QUEUED, State.AWAITING_RESTART)

def test_job_owned_by_this_process_is_not_recovered():
    jobs.create("export", affinity=jobs.PROCESS_WID)
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import portability_jobs
    with get_engine().begin() as c:
        c.execute(update(portability_jobs).values(created_at=time.time() - 3600))
    assert jobs.recover_orphans()["failed"] == 0

# ── erros por linha ─────────────────────────────────────────────────────────

def test_error_sink_caps_per_entity_and_counts_everything():
    j = jobs.create("import")
    sink = jobs.ErrorSink(j["id"])
    for i in range(jobs.MAX_ERRORS_PER_ENTITY + 50):
        sink.add("contacts", i, "phone_invalid", {"key": "***1234"})
    sink.add("tags", 0, "color_invalid")
    sink.flush()
    s = sink.summary()
    assert s["contacts"] == {"total": jobs.MAX_ERRORS_PER_ENTITY + 50, "stored": jobs.MAX_ERRORS_PER_ENTITY}
    assert s["tags"] == {"total": 1, "stored": 1}
    assert jobs.stored_error_counts(j["id"]) == {"contacts": jobs.MAX_ERRORS_PER_ENTITY, "tags": 1}

def test_error_sink_total_cap(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_ERRORS_TOTAL", 30)
    monkeypatch.setattr(jobs, "MAX_ERRORS_PER_ENTITY", 100)
    j = jobs.create("import")
    sink = jobs.ErrorSink(j["id"])
    for i in range(50):
        sink.add("a" if i % 2 else "b", i, "x")
    sink.flush()
    assert sum(jobs.stored_error_counts(j["id"]).values()) == 30
    assert sum(v["total"] for v in sink.summary().values()) == 50

def test_error_sink_flushes_in_blocks():
    j = jobs.create("import")
    sink = jobs.ErrorSink(j["id"])
    for i in range(jobs.ErrorSink.FLUSH_AT):
        sink.add("contacts", i, "x")
    assert jobs.stored_error_counts(j["id"]) == {"contacts": jobs.ErrorSink.FLUSH_AT}   # sem flush()

def test_list_errors_paginates_by_id_and_filters_entity():
    j = jobs.create("import")
    jobs.add_errors(j["id"], [{"entity": "contacts", "row_index": i, "code": "c",
                               "detail": {"key": f"k{i}"}} for i in range(5)]
                    + [{"entity": "tags", "row_index": 0, "code": "t"}])
    first = jobs.list_errors(j["id"], limit=3)
    assert len(first) == 3 and first[0]["detail"] == {"key": "k0"}
    rest = jobs.list_errors(j["id"], after=first[-1]["id"], limit=10)
    assert len(rest) == 3
    assert [r["entity"] for r in jobs.list_errors(j["id"], entity="tags")] == ["tags"]

def test_clear_errors_scoped_by_entity():
    j = jobs.create("import")
    jobs.add_errors(j["id"], [{"entity": "a", "code": "x"}, {"entity": "b", "code": "x"}])
    jobs.clear_errors(j["id"], ["a"])
    assert jobs.stored_error_counts(j["id"]) == {"b": 1}
    jobs.clear_errors(j["id"], [])                          # lista vazia não apaga nada
    assert jobs.stored_error_counts(j["id"]) == {"b": 1}
    jobs.clear_errors(j["id"])
    assert jobs.stored_error_counts(j["id"]) == {}

def test_deleting_a_job_cascades_to_its_errors():
    j = jobs.create("import")
    jobs.add_errors(j["id"], [{"entity": "a", "code": "x"}])
    jobs.delete_job(j["id"])
    assert jobs.list_errors(j["id"]) == []

# ── limpeza ─────────────────────────────────────────────────────────────────

def test_purge_removes_expired_package_and_keeps_fresh_one(_engine):
    old, new = jobs.create("export"), jobs.create("export")
    root = Path(jobs.jobs_root())
    for j, exp in ((old, time.time() - 10), (new, time.time() + 3600)):
        d = root / j["id"]
        d.mkdir()
        (d / "package.zip").write_bytes(b"zip")
        jobs.finish(j["id"], State.SUCCEEDED, package_path=str(d / "package.zip"))
        jobs._set(j["id"], expires_at=exp)
    assert jobs.purge_expired()["packages"] == 1
    assert not (root / old["id"]).exists() and (root / new["id"]).exists()
    assert jobs.get(old["id"])["package_path"] is None

def test_purge_cancels_unconfirmed_upload_after_ttl():
    j = jobs.create("import_dryrun", state=State.AWAITING_CONFIRMATION)
    jobs._set(j["id"], created_at=time.time() - jobs.PACKAGE_TTL_SEC - 10)
    jobs.purge_expired()
    assert jobs.get(j["id"])["state"] == State.CANCELLED

def test_purge_expired_awaiting_restart_becomes_cancelled():
    j = jobs.create("import")
    jobs.finish(j["id"], State.AWAITING_RESTART, expires_in=1)
    jobs._set(j["id"], expires_at=time.time() - 5)
    jobs.purge_expired()
    assert jobs.get(j["id"])["state"] == State.CANCELLED

def test_purge_never_removes_package_a_live_import_still_needs():
    root = Path(jobs.jobs_root())
    dry = jobs.create("import_dryrun")
    d = root / dry["id"]
    d.mkdir()
    (d / "package.zip").write_bytes(b"zip")
    jobs.finish(dry["id"], State.SUCCEEDED, package_path=str(d / "package.zip"))
    jobs._set(dry["id"], expires_at=time.time() - 10)
    imp = jobs.create("import", params={"package_path": str(d / "package.zip")})
    jobs.finish(imp["id"], State.AWAITING_RESTART)
    assert jobs.purge_expired()["packages"] == 0           # o import retomável ainda lê esta pasta
    assert d.exists()

def test_purge_deletes_old_terminal_jobs_and_their_folder():
    j = jobs.create("export")
    root = Path(jobs.jobs_root())
    (root / j["id"]).mkdir()
    jobs.finish(j["id"], State.SUCCEEDED)
    jobs._set(j["id"], finished_at=time.time() - jobs.JOB_TTL_SEC - 10)
    assert jobs.purge_expired()["jobs"] == 1
    assert jobs.get(j["id"]) is None and not (root / j["id"]).exists()

def test_purge_orphan_dirs_only_old_uuid_dirs_without_a_row():
    import uuid
    root = Path(jobs.jobs_root())
    known = jobs.create("export")
    (root / known["id"]).mkdir()
    stray_old, stray_new = str(uuid.uuid4()), str(uuid.uuid4())
    for n in (stray_old, stray_new, "minha-pasta"):
        (root / n).mkdir()
    old = time.time() - 7200
    os.utime(root / stray_old, (old, old))
    os.utime(root / known["id"], (old, old))
    os.utime(root / "minha-pasta", (old, old))
    assert jobs.purge_orphan_dirs()["dirs"] == 1
    assert not (root / stray_old).exists()
    assert (root / stray_new).exists() and (root / known["id"]).exists() and (root / "minha-pasta").exists()

def test_remove_package_dir_refuses_paths_outside_the_jobs_root(tmp_path):
    outside = tmp_path / "x"
    outside.mkdir()
    (outside / "f").write_text("1")
    with pytest.raises(ValueError):
        jobs.remove_package_dir(str(outside / "f"))
    with pytest.raises(ValueError):
        jobs.remove_package_dir(jobs.jobs_root())            # nem a própria raiz
    assert (outside / "f").exists()

# ── runner ──────────────────────────────────────────────────────────────────

def test_runner_success_failure_cancel_and_crash():
    jobs.register_handler("export", lambda ctx: {"counts": {"x": 1}})
    ok = jobs.create("export")
    assert _drain() == 1
    got = jobs.get(ok["id"])
    assert got["state"] == State.SUCCEEDED and got["report"] == {"counts": {"x": 1}}
    assert got["expires_at"] is not None                    # pacote de export tem prazo

    def boom(ctx):
        raise ValueError("conteúdo secreto da conversa: 5511999999999")
    jobs.register_handler("export", boom)
    bad = jobs.create("export")
    _drain()
    err = jobs.get(bad["id"])["error"]
    assert jobs.get(bad["id"])["state"] == State.FAILED
    assert "ValueError" in err and "5511999999999" not in err   # só o TIPO da exceção chega à tela

    def expected(ctx):
        raise jobs.JobFailed("Escolha ao menos um escopo para exportar.")
    jobs.register_handler("export", expected)
    ex = jobs.create("export")
    _drain()
    assert jobs.get(ex["id"])["error"] == "Escolha ao menos um escopo para exportar."

    def cancels(ctx):
        raise jobs.JobCancelled()
    jobs.register_handler("export", cancels)
    ca = jobs.create("export")
    _drain()
    assert jobs.get(ca["id"])["state"] == State.CANCELLED

def test_runner_without_handler_fails_cleanly():
    j = jobs.create("export")
    _drain()
    assert jobs.get(j["id"])["state"] == State.FAILED

def test_runner_pause_sets_resume_stage_and_keeps_the_package_alive():
    def pauses(ctx):
        raise jobs.JobPaused(stage="plugins", report={"pending": ["x"]})
    jobs.register_handler("import", pauses)
    j = jobs.create("import")
    _drain()
    got = jobs.get(j["id"])
    assert got["state"] == State.AWAITING_RESTART and got["resume_stage"] == "plugins"
    assert got["report"] == {"pending": ["x"]} and got["expires_at"] is not None

def test_password_is_consumed_once_and_dropped_after_the_run():
    seen = {}

    def handler(ctx):
        seen["first"] = ctx.take_password()
        seen["second"] = ctx.take_password()
        return {}
    jobs.register_handler("export", handler)
    j = jobs.create("export", affinity=jobs.PROCESS_WID)
    jobs.stash_password(j["id"], "senha-do-pacote-123")
    _drain()
    assert seen == {"first": "senha-do-pacote-123", "second": None}   # vale uma vez só

    j2 = jobs.create("export")
    jobs.stash_password(j2["id"], "nunca-usada-aaaa")
    _drain()
    assert j2["id"] not in jobs._PASSWORDS                             # descartada ao fim, mesmo sem uso

def test_password_never_reaches_the_database():
    from db.engine import get_engine
    from sqlalchemy import text
    j = jobs.create("export", params={"include_secrets": True}, affinity=jobs.PROCESS_WID)
    jobs.stash_password(j["id"], "SENHA-SECRETA-XYZ-123")
    jobs.register_handler("export", lambda ctx: {})
    _drain()
    with get_engine().connect() as c:
        dump = " ".join(str(v) for row in c.execute(text("select * from portability_jobs")) for v in row)
    assert "SENHA-SECRETA-XYZ-123" not in dump

def test_progress_helper_computes_percent():
    j = jobs.create("export")
    seen = {}

    def handler(ctx):
        ctx.progress(stage="export", entity="tags", done=1, total=4)
        seen["p"] = jobs.get(ctx.id)["progress"]
        return {}
    jobs.register_handler("export", handler)
    _drain()
    assert seen["p"]["percent"] == 25.0

def test_cancel_during_run_is_seen_by_check_cancel():
    def handler(ctx):
        jobs.request_cancel(ctx.id)
        ctx.check_cancel()
        return {"nao": "chega aqui"}
    jobs.register_handler("export", handler)
    j = jobs.create("export")
    _drain()
    assert jobs.get(j["id"])["state"] == State.CANCELLED

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
