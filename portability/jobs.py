"""Jobs assíncronos de importar/exportar: repositório + runner.

Estado em TABELA (``portability_jobs``), nunca em memória — o processo reinicia a qualquer momento
(toggle de plugin, self-update) e o job precisa sobreviver.

Ciclo de vida::

    queued ──claim──▶ running ──▶ succeeded | failed | cancelled
    (import) awaiting_confirmation ──confirm──▶ queued
    (import) running ──▶ awaiting_restart ──▶ queued          (Fase 3: etapa de dados dos plugins)

Portável entre SQLite e Postgres (o lite roda nos dois): SQLAlchemy Core, JSON como ``Text`` e as
colunas que o worker CONSULTA (``affinity``, ``source_job``, ``package_sha256``, ``resume_stage``,
``interrupted``, ``recoveries``) são colunas de verdade. Não existe ``->>`` nem ``FOR UPDATE SKIP
LOCKED`` aqui: o claim é um ``UPDATE ... WHERE state='queued'`` cujo ``rowcount`` diz quem ganhou,
o que é atômico nos dois bancos.

⚠️ A senha do pacote NUNCA passa por aqui: não entra em ``params``, ``progress``, ``report`` nem
``error``. Ela vive só na memória do processo (``stash_password``). Se o processo morre, ela morre junto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import and_, delete, exists, func, insert, or_, select, update

from db.engine import get_engine
from db.tables import portability_job_errors as E
from db.tables import portability_jobs as J

logger = logging.getLogger(__name__)

class State:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    AWAITING_RESTART = "awaiting_restart"

TERMINAL = frozenset({State.SUCCEEDED, State.FAILED, State.CANCELLED})
KINDS = ("export", "import_dryrun", "import")

HEARTBEAT_INTERVAL_SEC = 10.0
ORPHAN_AFTER_SEC = 60.0
POLL_INTERVAL_SEC = 3.0
#: Pacote exportado fica disponível por 24 h; o registro do job, 7 dias.
PACKAGE_TTL_SEC = 24 * 3600
JOB_TTL_SEC = 7 * 24 * 3600
#: ``error`` e relatório são limitados: vão para a tela e para a resposta da API.
MAX_ERROR_CHARS = 2000
#: Quantas vezes um import interrompido volta sozinho para a fila. Um import que derruba o processo
#: toda vez não pode ficar em laço para sempre.
MAX_AUTO_RECOVERIES = 3

def worker_id() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:6]}"

#: Um id por PROCESSO. A rota que recebe a senha e o loop que executa o job são o mesmo processo, e é
#: isso que liga um ao outro (``affinity``).
PROCESS_WID = worker_id()

# ── senha do pacote: SÓ em memória ──────────────────────────────────────────
# O job é executado depois, pelo loop, e a senha não pode ir para a tabela. A rota a guarda aqui e marca
# o job com ``affinity`` = este processo; ``claim_next`` só entrega um job com afinidade ao processo dono.
_PASSWORDS: dict[str, str] = {}
_PW_LOCK = threading.Lock()

def stash_password(job_id: str, password: str) -> None:
    with _PW_LOCK:
        _PASSWORDS[job_id] = password

def take_password(job_id: str) -> str | None:
    """Consome a senha (uma vez só)."""
    with _PW_LOCK:
        return _PASSWORDS.pop(job_id, None)

def drop_password(job_id: str) -> None:
    take_password(job_id)

# ── deps do servidor (para o pós-processo: cache de config, broadcast) ──────
_RUNTIME: dict[str, Any] = {"deps": None}

def set_runtime_deps(deps: Any) -> None:
    _RUNTIME["deps"] = deps

def runtime_deps() -> Any:
    return _RUNTIME["deps"]

# ── (de)serialização ────────────────────────────────────────────────────────

def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)

def _loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}

def _row(r) -> dict:
    d = dict(r)
    for k in ("progress", "params", "report"):
        d[k] = _loads(d.get(k))
    d["cancel_requested"] = bool(d.get("cancel_requested"))
    d["interrupted"] = bool(d.get("interrupted"))
    return d

# ── repositório ─────────────────────────────────────────────────────────────

def create(kind: str, *, params: dict | None = None, state: str = State.QUEUED,
           package_path: str | None = None, package_sha256: str | None = None,
           source_job: str | None = None, affinity: str | None = None,
           job_id: str | None = None) -> dict:
    """Cria um job. ``params`` não pode conter senha (responsabilidade do chamador)."""
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    jid = job_id or str(uuid.uuid4())
    with get_engine().begin() as conn:
        conn.execute(insert(J).values(
            id=jid, kind=kind, state=state, params=_dumps(params), package_path=package_path,
            package_sha256=package_sha256, source_job=source_job, affinity=affinity,
            created_at=time.time()))
    return get(jid)  # type: ignore[return-value]

def get(job_id: str) -> dict | None:
    with get_engine().connect() as conn:
        r = conn.execute(select(J).where(J.c.id == job_id)).mappings().first()
    return _row(r) if r else None

def list_recent(limit: int = 50, kind: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    stmt = select(J).order_by(J.c.created_at.desc(), J.c.id).limit(limit)
    if kind:
        stmt = stmt.where(J.c.kind == kind)
    with get_engine().connect() as conn:
        return [_row(r) for r in conn.execute(stmt).mappings()]

def enqueue(job_id: str, *, params: dict | None = None, affinity: str | None = None) -> bool:
    """``awaiting_*`` → ``queued`` (confirmação do import, retomada após restart)."""
    values: dict[str, Any] = {"state": State.QUEUED, "affinity": affinity}
    if params is not None:
        values["params"] = _dumps(params)
    with get_engine().begin() as conn:
        res = conn.execute(update(J).where(
            J.c.id == job_id,
            J.c.state.in_((State.AWAITING_CONFIRMATION, State.AWAITING_RESTART))).values(**values))
    return res.rowcount > 0

def enqueue_dryrun(job_id: str, params: dict, *, package_sha256: str | None = None,
                   affinity: str | None = None) -> bool:
    """``awaiting_confirmation`` → ``queued`` JÁ com os ``params`` do pacote recebido."""
    values: dict[str, Any] = {"state": State.QUEUED, "params": _dumps(params), "affinity": affinity}
    if package_sha256 is not None:
        values["package_sha256"] = package_sha256
    with get_engine().begin() as conn:
        res = conn.execute(update(J).where(
            J.c.id == job_id, J.c.state == State.AWAITING_CONFIRMATION).values(**values))
    return res.rowcount > 0

def requeue_dryrun(job_id: str, strategy: dict | None, *, affinity: str | None = None) -> bool:
    """``succeeded`` → ``queued`` no MESMO job de verificação ("Recalcular prévia").

    Mantém o id e o pacote, limpa relatório, progresso, erros por linha e a data de término, e grava a
    estratégia escolhida em ``params.strategy`` (o dry-run a usa). ``affinity`` amarra a execução a este
    processo quando há senha em memória; sem ela a afinidade antiga (de um processo que pode ter morrido)
    é REMOVIDA. Falso se o job não é uma verificação concluída ou se já virou import (``source_job``)."""
    imp = J.alias("imp")
    with get_engine().begin() as conn:
        cur = conn.execute(select(J.c.params).where(J.c.id == job_id)).scalar()
        if cur is None:
            return False
        params = _loads(cur)
        params["strategy"] = strategy or {}
        n = conn.execute(update(J).where(
            J.c.id == job_id, J.c.kind == "import_dryrun",
            J.c.state.in_((State.SUCCEEDED, State.FAILED)),
            ~exists().where(and_(imp.c.kind == "import", imp.c.source_job == job_id))
        ).values(state=State.QUEUED, report="{}", progress="{}", error=None, finished_at=None,
                 worker_id=None, cancel_requested=0, affinity=affinity,
                 params=_dumps(params))).rowcount
        if n:
            conn.execute(delete(E).where(E.c.job_id == job_id))
    return bool(n)

def has_import_from(dryrun_id: str) -> bool:
    """Já existe um import criado a partir desta verificação?"""
    with get_engine().connect() as conn:
        return conn.execute(select(J.c.id).where(
            J.c.kind == "import", J.c.source_job == dryrun_id).limit(1)).first() is not None

def busy_jobs(*, except_id: str | None = None) -> list[dict]:
    """Jobs ``running``/``queued`` (qualquer tipo) — quem derrubaria o servidor perde trabalho."""
    stmt = select(J.c.id, J.c.kind, J.c.state).where(J.c.state.in_((State.RUNNING, State.QUEUED)))
    if except_id:
        stmt = stmt.where(J.c.id != except_id)
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]

# ── erros por linha ─────────────────────────────────────────────────────────

#: Teto por job: 1.000 por entidade, 5.000 no total. O que passa disso é só CONTADO.
MAX_ERRORS_PER_ENTITY = 1000
MAX_ERRORS_TOTAL = 5000

def add_errors(job_id: str, rows: list[dict]) -> None:
    """Grava erros em bloco. ``rows``: ``{entity, row_index, code, detail}``."""
    if not rows:
        return
    now = time.time()
    payload = [{"job_id": job_id, "entity": r["entity"], "row_index": r.get("row_index"),
                "code": r["code"], "detail": _dumps(r.get("detail") or {}), "created_at": now}
               for r in rows]
    with get_engine().begin() as conn:
        conn.execute(insert(E), payload)

def clear_errors(job_id: str, entities: list[str] | None = None) -> None:
    """Apaga os erros do job (todos, ou só das ``entities``)."""
    stmt = delete(E).where(E.c.job_id == job_id)
    if entities is not None:
        if not entities:
            return
        stmt = stmt.where(E.c.entity.in_(list(entities)))
    with get_engine().begin() as conn:
        conn.execute(stmt)

def stored_error_counts(job_id: str) -> dict[str, int]:
    with get_engine().connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            select(E.c.entity, func.count()).where(E.c.job_id == job_id).group_by(E.c.entity))}

def list_errors(job_id: str, *, entity: str | None = None, limit: int = 100,
                after: int | None = None) -> list[dict]:
    """Página de erros, por ``id`` crescente (``after`` = último id já visto)."""
    limit = max(1, min(int(limit), 500))
    stmt = select(E).where(E.c.job_id == job_id).order_by(E.c.id).limit(limit)
    if entity:
        stmt = stmt.where(E.c.entity == entity)
    if after is not None:
        stmt = stmt.where(E.c.id > after)
    with get_engine().connect() as conn:
        out = []
        for r in conn.execute(stmt).mappings():
            d = dict(r)
            d["detail"] = _loads(d.get("detail"))
            out.append(d)
    return out

class ErrorSink:
    """Junta os erros de linha de UMA execução e os grava em blocos, respeitando o teto.

    ``totals`` conta TUDO (inclusive o que passou do teto); só ``stored`` vai para a tabela.
    ``seed`` parte do que o job já tem gravado."""

    FLUSH_AT = 200

    def __init__(self, job_id: str, seed: dict[str, int] | None = None,
                 seed_totals: dict[str, int] | None = None) -> None:
        self.job_id = job_id
        self.stored: dict[str, int] = dict(seed or {})
        # o total de uma entidade nunca fica abaixo do que já está gravado
        self.totals: dict[str, int] = {**self.stored, **{k: max(v, self.stored.get(k, 0))
                                                        for k, v in (seed_totals or {}).items()}}
        self._buf: list[dict] = []

    def add(self, entity: str, row_index: int | None, code: str, detail: dict | None = None) -> None:
        self.totals[entity] = self.totals.get(entity, 0) + 1
        if self.stored.get(entity, 0) >= MAX_ERRORS_PER_ENTITY \
                or sum(self.stored.values()) >= MAX_ERRORS_TOTAL:
            return
        self.stored[entity] = self.stored.get(entity, 0) + 1
        self._buf.append({"entity": entity, "row_index": row_index, "code": code,
                          "detail": detail or {}})
        if len(self._buf) >= self.FLUSH_AT:
            self.flush()

    def flush(self) -> None:
        buf, self._buf = self._buf, []
        if buf:
            add_errors(self.job_id, buf)

    def summary(self) -> dict[str, dict[str, int]]:
        """``{entidade: {total, stored}}`` — vai para o relatório do job."""
        return {e: {"total": n, "stored": self.stored.get(e, 0)} for e, n in self.totals.items() if n}

# ── ciclo de vida ───────────────────────────────────────────────────────────

def delete_job(job_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(J).where(J.c.id == job_id))

def claim_next(wid: str) -> dict | None:
    """Reivindica UM job ``queued``. O ``UPDATE ... WHERE state='queued'`` é o desempate: se outro
    worker pegou o mesmo job entre o SELECT e o UPDATE, o ``rowcount`` é 0 e tentamos o próximo."""
    now = time.time()
    for _ in range(3):
        with get_engine().begin() as conn:
            cand = conn.execute(select(J.c.id).where(
                J.c.state == State.QUEUED, or_(J.c.affinity.is_(None), J.c.affinity == wid)
            ).order_by(J.c.created_at, J.c.id).limit(1)).scalar()
            if cand is None:
                return None
            won = conn.execute(update(J).where(
                J.c.id == cand, J.c.state == State.QUEUED
            ).values(state=State.RUNNING, worker_id=wid, heartbeat_at=now,
                     started_at=func.coalesce(J.c.started_at, now))).rowcount
            if won:
                row = conn.execute(select(J).where(J.c.id == cand)).mappings().first()
                return _row(row)
    return None

def heartbeat(job_id: str, wid: str) -> bool:
    """Falso se o job deixou de ser deste worker (foi retomado por outro)."""
    with get_engine().begin() as conn:
        res = conn.execute(update(J).where(
            J.c.id == job_id, J.c.worker_id == wid, J.c.state == State.RUNNING
        ).values(heartbeat_at=time.time()))
    return res.rowcount > 0

def _set(job_id: str, **values: Any) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(J).where(J.c.id == job_id).values(**values))

def set_progress(job_id: str, progress: dict) -> None:
    _set(job_id, progress=_dumps(progress))

def set_report(job_id: str, report: dict) -> None:
    _set(job_id, report=_dumps(report))

def set_package_path(job_id: str, path: str | None) -> None:
    _set(job_id, package_path=path)

def set_resume_stage(job_id: str, stage: str | None) -> None:
    _set(job_id, resume_stage=stage)

def find_imported(package_sha256: str) -> str | None:
    """Id de um import JÁ concluído com este pacote (hash), ou ``None`` — é o aviso "este pacote já
    foi importado" (não bloqueia: reimportar é idempotente)."""
    with get_engine().connect() as conn:
        return conn.execute(select(J.c.id).where(
            J.c.kind == "import", J.c.state == State.SUCCEEDED, J.c.package_sha256 == package_sha256
        ).order_by(J.c.finished_at.desc()).limit(1)).scalar()

def finish(job_id: str, state: str, *, error: str | None = None, report: dict | None = None,
           package_path: str | None = None, expires_in: float | None = None) -> None:
    """Fecha o job (terminal) — ou ``awaiting_*`` quando o handler pede uma pausa."""
    if state not in TERMINAL and state not in (State.AWAITING_RESTART, State.AWAITING_CONFIRMATION):
        raise ValueError(f"estado inválido para finish: {state!r}")
    now = time.time()
    values: dict[str, Any] = {"state": state, "error": error[:MAX_ERROR_CHARS] if error else None}
    if state in TERMINAL:
        values["finished_at"] = now
    else:
        values["worker_id"] = None
    if report is not None:
        values["report"] = _dumps(report)
    if package_path is not None:
        values["package_path"] = package_path
    if expires_in:
        values["expires_at"] = now + expires_in
    _set(job_id, **values)

def resume(job_id: str, *, affinity: str | None = None) -> bool:
    """``awaiting_restart`` → ``queued`` (2ª etapa do import, ou retomada de um import INTERROMPIDO).
    ``False`` se o job não está esperando.

    ``affinity`` amarra a execução a este processo quando há senha em memória. Sem senha a afinidade
    antiga é REMOVIDA: ela aponta para o processo que morreu, e deixá-la faria o job nunca ser
    reivindicado (``claim_next``) e ser falhado como "senha perdida"."""
    with get_engine().begin() as conn:
        n = conn.execute(update(J).where(
            J.c.id == job_id, J.c.state == State.AWAITING_RESTART, J.c.kind == "import",
            or_(J.c.resume_stage.is_not(None), J.c.interrupted == 1)
        ).values(state=State.QUEUED, finished_at=None, worker_id=None, cancel_requested=0,
                 affinity=affinity, interrupted=0)).rowcount
    return bool(n)

def request_cancel(job_id: str) -> str | None:
    """Pede o cancelamento. Devolve o estado resultante, ou ``None`` se não existe.

    * ``queued`` / ``awaiting_*`` → cancela na hora (ninguém está executando);
    * ``running`` → só levanta a flag; o handler confere entre lotes;
    * terminal → no-op."""
    now = time.time()
    idle = (State.QUEUED, State.AWAITING_CONFIRMATION, State.AWAITING_RESTART)
    with get_engine().begin() as conn:
        n = conn.execute(update(J).where(J.c.id == job_id, J.c.state.in_(idle)).values(
            state=State.CANCELLED, finished_at=now, cancel_requested=1)).rowcount
        if not n:
            conn.execute(update(J).where(J.c.id == job_id, J.c.state == State.RUNNING).values(
                cancel_requested=1))
        return conn.execute(select(J.c.state).where(J.c.id == job_id)).scalar()

def is_cancel_requested(job_id: str) -> bool:
    with get_engine().connect() as conn:
        return bool(conn.execute(select(J.c.cancel_requested).where(J.c.id == job_id)).scalar())

def recover_orphans(*, now: float | None = None, older_than: float = ORPHAN_AFTER_SEC,
                    wid: str | None = None) -> dict:
    """Job ``running`` com heartbeat parado = o processo morreu no meio.

    * ``import`` — NÃO é falha: o que já entrou continua no banco e o resto se refaz (tudo casa por
      chave natural). Sem senha em jogo volta SOZINHO para a fila; com senha (que morreu junto, só
      existia na memória) vira ``awaiting_restart`` com ``interrupted`` e o operador a informa de novo;
    * demais (export, verificação) → ``failed`` (refaz-se do zero).

    Devolve ``{"requeued", "interrupted", "failed"}``."""
    now = now if now is not None else time.time()
    cutoff = now - older_than
    me = wid or PROCESS_WID
    requeued = interrupted = failed = 0
    stale = and_(J.c.state == State.RUNNING,
                 func.coalesce(J.c.heartbeat_at, J.c.started_at, 0) < cutoff)
    foreign = and_(J.c.state == State.QUEUED, J.c.affinity.is_not(None), J.c.affinity != me,
                   J.c.created_at < cutoff)
    with get_engine().begin() as conn:
        # import cujo dono morreu: ``running`` com heartbeat velho, OU ``queued`` com a afinidade de
        # outro processo (confirmado/retomado e o servidor caiu antes do claim).
        rows = conn.execute(select(J.c.id, J.c.params, J.c.recoveries).where(
            J.c.kind == "import", or_(stale, foreign))).mappings().all()
        for r in rows:
            has_pw = bool(_loads(r["params"]).get("has_password"))
            if not has_pw and (r["recoveries"] or 0) < MAX_AUTO_RECOVERIES:
                conn.execute(update(J).where(J.c.id == r["id"]).values(
                    state=State.QUEUED, worker_id=None, affinity=None,
                    recoveries=(r["recoveries"] or 0) + 1))
                requeued += 1
            else:
                conn.execute(update(J).where(J.c.id == r["id"]).values(
                    state=State.AWAITING_RESTART, worker_id=None, error=None, affinity=None,
                    interrupted=1, expires_at=now + PACKAGE_TTL_SEC))
                interrupted += 1
        failed = conn.execute(update(J).where(J.c.kind != "import", stale).values(
            state=State.FAILED, finished_at=now,
            error="Interrompido (o servidor reiniciou durante a execução). Refaça a operação."
        )).rowcount
        # Verificação/exportação na fila com a senha na memória de um processo morto: nunca roda.
        failed += conn.execute(update(J).where(J.c.kind != "import", foreign).values(
            state=State.FAILED, finished_at=now,
            error="A senha do pacote se perdeu (o servidor reiniciou). Refaça a operação."
        )).rowcount
    if requeued or interrupted or failed:
        logger.warning("[Portability] órfãos: %d voltaram à fila, %d interrompidos (esperam o operador), "
                       "%d falhos", requeued, interrupted, failed)
    return {"requeued": requeued, "interrupted": interrupted, "failed": failed}

# ── limpeza ─────────────────────────────────────────────────────────────────

def jobs_root() -> str:
    """Pasta dos pacotes (``storages/portability``). Fica em ``storages/`` — por instância, persiste
    em Docker/Coolify e é preservada pelo self-update — e FORA de qualquer mount servido ao navegador
    (só ``web/static``, ``statics`` e a estática de cada plugin são servidos)."""
    from config.settings import get_data_dir  # import tardio: evita ciclo no import do pacote
    root = os.path.join(str(get_data_dir()), "storages", "portability")
    os.makedirs(root, exist_ok=True)
    return root

def remove_package_dir(path: str) -> None:
    """Apaga o diretório do job (pai do arquivo). Só dentro da pasta de jobs."""
    if not path:
        return
    base = os.path.realpath(jobs_root())
    target = os.path.realpath(os.path.dirname(path) if os.path.isfile(path) else path)
    if os.path.commonpath([base, target]) != base or target == base:
        raise ValueError("caminho fora da pasta de jobs")
    shutil.rmtree(target, ignore_errors=True)

def _folder_in_use(conn, folder: str, *, except_id: str) -> bool:
    """Um job ``import`` ainda ativo lê o pacote da pasta do DRY-RUN (``params.package_path``). O
    dry-run vence antes dele; apagar a pasta agora mataria um import que ainda é retomável.
    Comparado em Python: não existe ``->>`` portável, e são poucas linhas."""
    prefix = folder.rstrip(os.sep) + os.sep
    for (raw,) in conn.execute(select(J.c.params).where(
            J.c.id != except_id, J.c.kind == "import",
            J.c.state.in_((State.QUEUED, State.RUNNING, State.AWAITING_RESTART)))):
        if str(_loads(raw).get("package_path") or "").startswith(prefix):
            return True
    return False

def purge_expired(*, now: float | None = None, delete_files: Callable[[str], None] | None = None) -> dict:
    """Apaga o pacote vencido (24 h) e o registro do job terminado (7 dias), e a pasta de cada um.

    Pacote vencido = ``expires_at`` passou, **ou** o job nunca foi confirmado (upload em
    ``awaiting_confirmation``) / terminou sem prazo (``failed``/``cancelled``) e já passou o mesmo
    prazo desde que foi criado. Sem isto um upload de GB abandonado ficava para sempre."""
    now = now if now is not None else time.time()
    remove = delete_files or remove_package_dir
    cut_pkg = now - PACKAGE_TTL_SEC
    with get_engine().begin() as conn:
        # Upload que ninguém confirmou: não há mais o que confirmar.
        conn.execute(update(J).where(
            J.c.state == State.AWAITING_CONFIRMATION, J.c.created_at < cut_pkg
        ).values(state=State.CANCELLED, finished_at=now,
                 error=func.coalesce(J.c.error, "Pacote não confirmado a tempo e removido.")))
    with get_engine().connect() as conn:
        pkgs = conn.execute(select(J.c.id, J.c.package_path).where(
            J.c.package_path.is_not(None),
            or_(and_(J.c.expires_at.is_not(None), J.c.expires_at < now),
                and_(J.c.expires_at.is_(None), J.c.state.in_((State.FAILED, State.CANCELLED)),
                     J.c.created_at < cut_pkg)))).all()
    cleared = 0
    for jid, path in pkgs:
        with get_engine().connect() as conn:
            if _folder_in_use(conn, os.path.dirname(path), except_id=jid):
                continue                      # ainda em uso: fica para a próxima varredura
        try:
            remove(path)
        except Exception:  # noqa: BLE001 — limpeza nunca derruba o loop
            logger.warning("[Portability] não removeu o pacote do job %s", jid, exc_info=True)
            continue
        _set(jid, package_path=None)
        cleared += 1
    with get_engine().begin() as conn:
        # Import que esperava o operador e o prazo do pacote acabou: não há mais o que retomar.
        conn.execute(update(J).where(
            J.c.state == State.AWAITING_RESTART, J.c.expires_at.is_not(None), J.c.expires_at < now
        ).values(state=State.CANCELLED, finished_at=now))
    with get_engine().connect() as conn:
        old = [r[0] for r in conn.execute(select(J.c.id).where(
            J.c.state.in_(tuple(TERMINAL)),
            func.coalesce(J.c.finished_at, J.c.created_at) < now - JOB_TTL_SEC))]
    deleted = 0
    for jid in old:
        # A pasta do job some junto com o registro (sobraria quando o job não tem ``package_path`` —
        # export que falhou no meio, por exemplo).
        try:
            remove_package_dir(os.path.join(jobs_root(), jid))
        except Exception:  # noqa: BLE001
            logger.warning("[Portability] não removeu a pasta do job %s", jid, exc_info=True)
            continue
        with get_engine().begin() as conn:
            deleted += conn.execute(delete(J).where(J.c.id == jid)).rowcount
    return {"packages": cleared, "jobs": deleted}

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ORPHAN_DIR_AFTER_SEC = 3600.0

def purge_orphan_dirs(*, now: float | None = None) -> dict:
    """Apaga pasta de job SEM linha no banco e com mais de 1 h. Cobre o que morreu entre criar a pasta
    e gravar o registro, e o ``.part`` de um export interrompido por ``os._exit``.

    Só olha diretório com nome de UUID (nada que o operador tenha posto ali) e nunca uma pasta recente."""
    now = now if now is not None else time.time()
    root = jobs_root()
    with get_engine().connect() as conn:
        known = {r[0] for r in conn.execute(select(J.c.id))}
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not _UUID_RE.match(name) or name in known or os.path.islink(path) \
                or not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) < ORPHAN_DIR_AFTER_SEC:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            logger.warning("[Portability] não removeu a pasta órfã %s", name, exc_info=True)
    return {"dirs": removed}

# ── runner ──────────────────────────────────────────────────────────────────

class JobCancelled(Exception):
    """O handler levanta isto ao ver ``cancel_requested`` (entre lotes)."""

class JobFailed(Exception):
    """Falha ESPERADA com mensagem própria, segura para o usuário (escrita por nós, nunca vinda de
    dado de conversa/segredo). Qualquer outra exceção só expõe o tipo."""

class JobPaused(Exception):
    """O handler levanta isto para pausar o job em ``awaiting_*`` (ex.: restart, Fase 3)."""

    def __init__(self, state: str = State.AWAITING_RESTART, stage: str | None = None,
                 report: dict | None = None) -> None:
        super().__init__(state)
        self.state = state
        self.stage = stage
        self.report = report

@dataclass
class JobContext:
    """O que um handler enxerga. Síncrono: roda numa thread (``asyncio.to_thread``)."""
    job: dict
    wid: str

    @property
    def id(self) -> str:
        return self.job["id"]

    @property
    def params(self) -> dict:
        return self.job.get("params") or {}

    def progress(self, **fields: Any) -> None:
        """Grava o progresso (``stage``, ``entity``, ``done``, ``total``, ``percent``)."""
        total, done = fields.get("total"), fields.get("done")
        if "percent" not in fields and total:
            fields["percent"] = round(100.0 * (done or 0) / total, 1)
        set_progress(self.id, fields)

    @property
    def deps(self) -> Any:
        return runtime_deps()

    def take_password(self) -> str | None:
        return take_password(self.id)

    def set_package(self, path: str | None) -> None:
        set_package_path(self.id, path)

    def check_cancel(self) -> None:
        if is_cancel_requested(self.id):
            raise JobCancelled()

#: kind -> handler(ctx) -> relatório (dict) | None. O runner não conhece nenhuma entidade.
_HANDLERS: dict[str, Callable[[JobContext], dict | None]] = {}

def register_handler(kind: str, fn: Callable[[JobContext], dict | None]) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    _HANDLERS[kind] = fn

def handler_for(kind: str) -> Callable[[JobContext], dict | None] | None:
    return _HANDLERS.get(kind)

#: Chamado quando um job termina em ``failed`` (o handler de import registra o log).
_FAIL_HOOKS: list[Callable[[dict, str], None]] = []

def _on_failed(job: dict, error: str) -> None:
    for hook in _FAIL_HOOKS:
        try:
            hook(job, error)
        except Exception:  # noqa: BLE001 — registrar nunca muda o desfecho do job
            logger.warning("[Portability] hook de falha falhou", exc_info=True)

def _execute(job: dict, wid: str) -> None:
    """Roda o handler (síncrono) e fecha o job. Nunca levanta.

    A senha guardada em memória é descartada em QUALQUER saída — sucesso, falha, cancelamento ou
    pausa: ela vale para uma execução só."""
    jid = job["id"]
    try:
        _execute_inner(job, wid)
    finally:
        drop_password(jid)

def _execute_inner(job: dict, wid: str) -> None:
    jid = job["id"]
    fn = _HANDLERS.get(job["kind"])
    if fn is None:
        finish(jid, State.FAILED, error=f"Sem executor registrado para '{job['kind']}'.")
        return
    ctx = JobContext(job=job, wid=wid)
    try:
        report = fn(ctx)
    except JobCancelled:
        finish(jid, State.CANCELLED)
    except JobFailed as exc:
        finish(jid, State.FAILED, error=str(exc))
        _on_failed(job, str(exc))
    except JobPaused as p:
        if p.stage:
            set_resume_stage(jid, p.stage)
        # O pacote tem de sobreviver à espera, com o mesmo prazo de um pacote pronto — depois o
        # ``purge_expired`` cancela o job.
        finish(jid, p.state, report=p.report, expires_in=PACKAGE_TTL_SEC)
    except Exception as exc:  # noqa: BLE001 — o job falha, o servidor não
        logger.exception("[Portability] job %s (%s) falhou", jid, job["kind"])
        # Só o TIPO da exceção vai ao usuário: a mensagem pode carregar conteúdo.
        msg = f"Falha interna ({type(exc).__name__}). Veja o log do servidor."
        finish(jid, State.FAILED, error=msg)
        _on_failed(job, msg)
    else:
        pkg_ttl = PACKAGE_TTL_SEC if job.get("package_path") or job["kind"] == "export" else None
        finish(jid, State.SUCCEEDED, report=report if isinstance(report, dict) else None,
               expires_in=pkg_ttl)

async def run_one(wid: str) -> bool:
    """Reivindica e executa UM job. ``False`` quando a fila está vazia."""
    job = await asyncio.to_thread(claim_next, wid)
    if job is None:
        return False
    hb = asyncio.create_task(_heartbeat_loop(job["id"], wid))
    try:
        await asyncio.to_thread(_execute, job, wid)
    finally:
        hb.cancel()
    return True

async def _heartbeat_loop(job_id: str, wid: str) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            await asyncio.to_thread(heartbeat, job_id, wid)
    except asyncio.CancelledError:
        pass
