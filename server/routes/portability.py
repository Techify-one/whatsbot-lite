"""Importar/Exportar (migração entre instalações) — rotas ``/api/portability/*``.

Regras que valem para toda rota deste módulo:

* **Exige senha de painel configurada.** O lite só tem UMA senha e, sem ela, ``/api/*`` fica aberto a
  quem alcançar a porta. Exportar leva a instalação inteira (conversas, chaves) e importar grava nela,
  então sem senha a feature responde 403 ``password_required`` e a tela leva ao card de senha. A
  autenticação em si (401) é do middleware do ``app.py``.
* **Um job por vez** (409): o import grava no mesmo banco que o webhook usa, e dois pacotes em paralelo
  só disputariam o lock do SQLite.
* **Polling, não WebSocket**: o ``/ws`` é broadcast para todos os painéis abertos; o progresso de um
  pacote não pode vazar por ele.
* **Nunca devolve ``package_path``, ``params`` nem senha** — ``_public`` é uma lista branca, não um
  filtro de remoção.
* A senha do pacote só existe em memória (``jobs.stash_password``) e é guardada ANTES de o job existir
  na fila: o loop reivindica ``queued`` a qualquer instante.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from portability import avatars as avatars_io, crypto, jobs, media as media_io, registry as reg
from portability.format import PackageError, inspect_zip, sha256_file
from server.auth import auth_required
from server.helpers import _err, _ok

logger = logging.getLogger(__name__)

#: Teto do pacote enviado. Mídia não comprime, então um pacote com mídia chega a alguns GB; o teto
#: fica acima do total de mídia (4 GB) e abaixo do que um disco de cliente comum aguenta em dobro.
MAX_IMPORT_BYTES = 5 * 1024 ** 3
_COPY_CHUNK = 1024 * 1024
_UPLOAD_PATH = "/api/portability/imports"

_PUBLIC_FIELDS = ("id", "kind", "state", "progress", "report", "error", "created_at", "started_at",
                  "finished_at", "expires_at", "cancel_requested", "resume_stage", "source_job",
                  "interrupted")

def _public(job: dict) -> dict:
    """LISTA BRANCA do que sai pela API. ``params`` fica de fora (escopos e opções escolhidos podem
    sugerir o conteúdo) e ``package_path`` é caminho do servidor."""
    out = {k: job.get(k) for k in _PUBLIC_FIELDS}
    prm = job.get("params") or {}
    # A tela precisa saber se deve pedir a senha de novo (import interrompido com segredos).
    out["needs_password"] = prm.get("has_password") is True
    out["has_package"] = bool(job.get("package_path")) and job.get("state") == jobs.State.SUCCEEDED
    return out

def _truthy(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "on", "yes"))

def _no_store(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

def _too_large_message() -> str:
    return f"O arquivo passa do limite de {MAX_IMPORT_BYTES // 1024 ** 3} GB para importar."

def _save_upload(upload: UploadFile, dest: str, limit: int) -> int:
    """Copia o upload (já em arquivo temporário do Starlette) para ``dest`` em blocos, contando os
    bytes REAIS — o ``Content-Length`` declarado não é confiável. Aborta e apaga o parcial ao passar de
    ``limit``. Bloqueante: chame via ``to_thread``."""
    total = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = upload.file.read(_COPY_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise OverflowError
                out.write(chunk)
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return total

def _discard_job_dir(job_id: str) -> None:
    shutil.rmtree(os.path.join(jobs.jobs_root(), job_id), ignore_errors=True)

def _package_ok(path) -> bool:
    return bool(path) and os.path.isfile(path)

def register_routes(app, deps):
    settings = deps.settings
    #: Serializa "há job em andamento?" + "criar job": sem isso dois cliques passariam juntos.
    create_lock = asyncio.Lock()

    def _gate():
        """403 ``password_required`` quando o painel não tem senha. ``None`` = pode seguir."""
        if not auth_required(settings):
            return _err("Defina uma senha para o painel antes de importar ou exportar: o pacote leva "
                        "as conversas e as chaves desta instalação.", 403,
                        error_code="password_required")
        return None

    async def _busy_error(except_id: str | None = None):
        busy = await asyncio.to_thread(jobs.busy_jobs, except_id=except_id)
        if busy:
            return _err("Já existe uma operação de importar/exportar em andamento. Espere terminar.", 409,
                        error_code="job_busy")
        return None

    @app.middleware("http")
    async def _portability_upload_cap(request: Request, call_next):
        """Recusa pelo ``Content-Length`` ANTES de o Starlette receber o corpo. A contagem real em
        ``_save_upload`` continua sendo a barreira (o cabeçalho é declarado pelo cliente)."""
        if request.method == "POST" and request.url.path == _UPLOAD_PATH:
            try:
                declared = int(request.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > MAX_IMPORT_BYTES + 64 * 1024:
                return _err(_too_large_message(), 413)
        return await call_next(request)

    # ── export ──────────────────────────────────────────────────────────────

    @app.post("/api/portability/exports")
    async def create_export(body: dict):
        """Cria um job de export. A senha (se houver segredos) fica SÓ em memória."""
        denied = _gate()
        if denied:
            return denied
        try:
            scopes = reg.validate_scopes(body.get("scopes"))
        except ValueError as exc:
            return _err(str(exc))
        include_secrets = _truthy(body.get("include_secrets"))
        password = body.get("password")
        if include_secrets:
            try:
                crypto.check_password(password)
            except crypto.WeakPasswordError as exc:
                return _err(str(exc))
            try:
                crypto._backend()   # falha cedo, com a instrução, em vez de só no fim do job
            except crypto.CryptoUnavailableError as exc:
                return _err(str(exc), 503, error_code="crypto_unavailable")
        try:
            media = media_io.parse_params(body, scopes)
        except ValueError as exc:
            return _err(str(exc))
        params = {"scopes": scopes, "include_secrets": include_secrets, **media}
        if "conversations" in scopes:
            params["include_avatars"] = avatars_io.parse_params(body, scopes)
        job_id = str(uuid.uuid4())
        async with create_lock:
            busy = await _busy_error()
            if busy:
                return busy
            # A senha entra na memória ANTES de o job existir (o loop reivindica ``queued`` já).
            if include_secrets:
                jobs.stash_password(job_id, password)
            try:
                job = await asyncio.to_thread(
                    jobs.create, "export", params=params, job_id=job_id,
                    affinity=jobs.PROCESS_WID if include_secrets else None)
            except Exception:
                jobs.drop_password(job_id)
                raise
        logger.info("[Portability] export %s criado (escopos=%s, segredos=%s, midia=%s)",
                    job_id[:8], ",".join(scopes), include_secrets, bool(media))
        return _no_store({"ok": True, "data": _public(job)}, 202)

    @app.post("/api/portability/jobs/{job_id}/download")
    async def download_export(job_id: str):
        """O pacote pronto. POST (não entra em cache/prefetch/histórico) e ``no-store``."""
        denied = _gate()
        if denied:
            return denied
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None or job["kind"] != "export":
            return _err("Job não encontrado.", 404)
        path = job.get("package_path")
        if job["state"] != jobs.State.SUCCEEDED or not _package_ok(path):
            return _err("O pacote não está disponível (ainda não terminou ou já expirou).", 409)
        report = job.get("report") or {}
        logger.info("[Portability] download do export %s (%s bytes)", job_id[:8], report.get("bytes"))
        return FileResponse(path, media_type="application/zip",
                            filename=f"whatsbot-lite-export-{job_id[:8]}.zip",
                            headers={"Cache-Control": "no-store"})

    # ── import ──────────────────────────────────────────────────────────────

    @app.post(_UPLOAD_PATH)
    async def upload_import(file: UploadFile = File(...), password: str = Form("")):
        """Recebe o pacote (em blocos para o disco) e cria o job de VERIFICAÇÃO (dry-run: nada é gravado)."""
        denied = _gate()
        if denied:
            await file.close()
            return denied
        async with create_lock:
            busy = await _busy_error()
            if busy:
                await file.close()
                return busy
            job = await asyncio.to_thread(
                jobs.create, "import_dryrun", state=jobs.State.AWAITING_CONFIRMATION)
        job_id = job["id"]
        dest = os.path.join(jobs.jobs_root(), job_id, "package.zip")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            size = await asyncio.to_thread(_save_upload, file, dest, MAX_IMPORT_BYTES)
            # zip-slip, zip-bomb e manifest ANTES de aceitar o job.
            await asyncio.to_thread(inspect_zip, dest)
            sha = await asyncio.to_thread(sha256_file, dest)
        except OverflowError:
            await asyncio.to_thread(_discard_job_dir, job_id)
            await asyncio.to_thread(jobs.delete_job, job_id)
            return _err(_too_large_message(), 413)
        except PackageError as exc:
            await asyncio.to_thread(_discard_job_dir, job_id)
            await asyncio.to_thread(jobs.delete_job, job_id)
            return _err(str(exc))
        except Exception:
            await asyncio.to_thread(_discard_job_dir, job_id)
            await asyncio.to_thread(jobs.delete_job, job_id)
            raise
        finally:
            await file.close()
        await asyncio.to_thread(jobs.set_package_path, job_id, dest)
        if password:
            jobs.stash_password(job_id, password)       # antes de enfileirar
        ok = await asyncio.to_thread(
            lambda: jobs.enqueue_dryrun(
                job_id, {"package_sha256": sha, "package_bytes": size, "has_password": bool(password)},
                package_sha256=sha, affinity=jobs.PROCESS_WID if password else None))
        if not ok:
            jobs.drop_password(job_id)
            await asyncio.to_thread(_discard_job_dir, job_id)
            await asyncio.to_thread(jobs.delete_job, job_id)
            return _err("Não foi possível iniciar a verificação. Envie o arquivo de novo.", 409)
        logger.info("[Portability] pacote %s recebido (%d bytes)", job_id[:8], size)
        return _no_store({"ok": True, "data": _public(await asyncio.to_thread(jobs.get, job_id))}, 202)

    async def _finished_dryrun(job_id: str):
        """``(dry, erro)``: a verificação concluída com pacote ainda no disco."""
        dry = await asyncio.to_thread(jobs.get, job_id)
        if dry is None or dry["kind"] != "import_dryrun":
            return None, _err("Job não encontrado.", 404)
        if dry["state"] != jobs.State.SUCCEEDED or not _package_ok(dry.get("package_path")):
            return None, _err("A verificação ainda não terminou com sucesso (ou o pacote expirou).", 409)
        return dry, None

    @app.post("/api/portability/imports/{job_id}/confirm")
    async def confirm_import(job_id: str, body: dict):
        """Confirma o import depois da verificação: cria o job ``import`` sobre o MESMO pacote."""
        denied = _gate()
        if denied:
            return denied
        dry, err = await _finished_dryrun(job_id)
        if err:
            return err
        if await asyncio.to_thread(jobs.has_import_from, job_id):
            return _err("Esta verificação já virou uma importação.", 409)
        report = dry.get("report") or {}
        names = list((report.get("entities") or {}).keys())
        try:
            strategy = reg.validate_strategy(body.get("strategy"), names)
        except ValueError as exc:
            return _err(str(exc))
        secrets_state = (report.get("manifest") or {}).get("secrets")
        password = body.get("password") or ""
        if secrets_state in ("ok", "locked") and not password:
            return _err("Este pacote tem segredos: informe a senha para importá-los.")
        # Confirmação explícita de quem leu o que será substituído (o popup da tela a exige).
        if not _truthy(body.get("acknowledged")):
            return _err("Confirme que revisou o que será importado.", 400, error_code="not_acknowledged")
        params = {"strategy": strategy, "package_path": dry["package_path"],
                  "overwrite_secrets": _truthy(body.get("overwrite_secrets")),
                  "has_password": bool(password), "package_sha256": dry.get("package_sha256")}
        new_id = str(uuid.uuid4())
        async with create_lock:
            busy = await _busy_error()
            if busy:
                return busy
            if password:
                jobs.stash_password(new_id, password)    # antes de o job existir
            try:
                job = await asyncio.to_thread(
                    jobs.create, "import", params=params, job_id=new_id, source_job=job_id,
                    package_path=dry["package_path"], package_sha256=dry.get("package_sha256"),
                    affinity=jobs.PROCESS_WID if password else None)
            except Exception:
                jobs.drop_password(new_id)
                raise
        logger.info("[Portability] import %s confirmado (estrategia=%s, segredos=%s)",
                    new_id[:8], strategy, bool(password))
        return _no_store({"ok": True, "data": _public(job)}, 202)

    @app.post("/api/portability/imports/{job_id}/recalculate")
    async def recalculate_import(job_id: str, body: dict):
        """"Recalcular prévia": refaz a verificação (sem gravar nada) com as estratégias escolhidas.
        Reaproveita o MESMO job, então várias verificações não poluem o histórico."""
        denied = _gate()
        if denied:
            return denied
        dry, err = await _finished_dryrun(job_id)
        if err:
            return err
        if await asyncio.to_thread(jobs.has_import_from, job_id):
            return _err("Esta verificação já virou uma importação e não pode ser refeita.", 409)
        report = dry.get("report") or {}
        names = list((report.get("entities") or {}).keys())
        try:
            strategy = reg.validate_strategy(body.get("strategy"), names)
        except ValueError as exc:
            return _err(str(exc))
        secrets_state = (report.get("manifest") or {}).get("secrets")
        password = body.get("password") or ""
        if secrets_state in ("ok", "locked") and not password:
            return _err("Este pacote tem segredos: informe a senha para refazer a verificação.")
        async with create_lock:
            busy = await _busy_error()
            if busy:
                return busy
            if password:
                jobs.stash_password(job_id, password)    # antes de voltar à fila
            try:
                ok = await asyncio.to_thread(
                    lambda: jobs.requeue_dryrun(
                        job_id, strategy, affinity=jobs.PROCESS_WID if password else None))
            except Exception:
                jobs.drop_password(job_id)
                raise
        if not ok:
            jobs.drop_password(job_id)
            return _err("Esta verificação não pode ser refeita agora.", 409)
        return _no_store({"ok": True, "data": _public(await asyncio.to_thread(jobs.get, job_id))}, 202)

    @app.post("/api/portability/jobs/{job_id}/resume")
    async def resume_import(job_id: str, body: dict):
        """Continua um import que o servidor perdeu no meio (reinício). Pede a senha DE NOVO quando o
        pacote tinha segredos: ela só existia na memória do processo que morreu. O que já entrou não
        duplica (tudo casa por chave natural)."""
        denied = _gate()
        if denied:
            return denied
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None or job["kind"] != "import":
            return _err("Job não encontrado.", 404)
        if job["state"] != jobs.State.AWAITING_RESTART or not job.get("interrupted"):
            return _err("Este import não está esperando para continuar.", 409)
        pkg = (job.get("params") or {}).get("package_path") or job.get("package_path")
        if not _package_ok(pkg):
            return _err("O pacote expirou. Envie-o de novo.", 409)
        password = body.get("password") or ""
        if (job.get("params") or {}).get("has_password") is True and not password:
            return _err("Este pacote tem segredos: informe a senha para continuar.")
        async with create_lock:
            busy = await _busy_error(except_id=job_id)
            if busy:
                return busy
            if password:
                jobs.stash_password(job_id, password)    # antes de voltar à fila
            try:
                ok = await asyncio.to_thread(
                    lambda: jobs.resume(job_id, affinity=jobs.PROCESS_WID if password else None))
            except Exception:
                jobs.drop_password(job_id)
                raise
        if not ok:
            jobs.drop_password(job_id)
            return _err("Este import não está esperando para continuar.", 409)
        logger.info("[Portability] import %s retomado pelo operador", job_id[:8])
        return _no_store({"ok": True, "data": _public(await asyncio.to_thread(jobs.get, job_id))}, 202)

    # ── consulta ────────────────────────────────────────────────────────────

    @app.get("/api/portability/jobs/{job_id}/errors")
    async def job_errors(job_id: str, entity: str | None = None, limit: int = 100,
                         after: int | None = None):
        """Erros por linha de um job (entidade, posição, código e identificador MASCARADO)."""
        denied = _gate()
        if denied:
            return denied
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None:
            return _err("Job não encontrado.", 404)
        items = await asyncio.to_thread(
            lambda: jobs.list_errors(job_id, entity=entity, limit=limit, after=after))
        summary = (job.get("report") or {}).get("errors") or {}
        stored = await asyncio.to_thread(jobs.stored_error_counts, job_id)
        totals = {e: {"total": max(int((summary.get(e) or {}).get("total", 0)), n), "stored": n}
                  for e, n in stored.items()}
        for e, v in summary.items():                    # erro contado mas não gravado (teto)
            totals.setdefault(e, {"total": int(v.get("total", 0)), "stored": 0})
        truncated = any(v["total"] > v["stored"] for v in totals.values())
        return _no_store({"ok": True, "data": {
            "items": items, "totals": totals, "truncated": truncated,
            "next_after": items[-1]["id"] if items else None}})

    @app.get("/api/portability/jobs")
    async def list_jobs(limit: int = 50, kind: str | None = None):
        denied = _gate()
        if denied:
            return denied
        if kind is not None and kind not in jobs.KINDS:
            return _err("Tipo de job inválido.")
        rows = await asyncio.to_thread(jobs.list_recent, limit, kind)
        return _no_store({"ok": True, "data": [_public(r) for r in rows]})

    @app.get("/api/portability/jobs/{job_id}")
    async def get_job(job_id: str):
        denied = _gate()
        if denied:
            return denied
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None:
            return _err("Job não encontrado.", 404)
        return _no_store({"ok": True, "data": _public(job)})

    @app.post("/api/portability/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        denied = _gate()
        if denied:
            return denied
        job = await asyncio.to_thread(jobs.get, job_id)
        if job is None:
            return _err("Job não encontrado.", 404)
        if job["state"] in jobs.TERMINAL:
            return _err("O job já terminou.", 409)
        state = await asyncio.to_thread(jobs.request_cancel, job_id)
        if state == jobs.State.CANCELLED:
            jobs.drop_password(job_id)                  # nunca vai rodar: a senha não fica na memória
        logger.info("[Portability] cancelamento do job %s (%s) → %s", job_id[:8], job["kind"], state)
        return _ok({"id": job_id, "state": state})
