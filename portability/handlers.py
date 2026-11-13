"""Orquestração do importar/exportar: ``run_export``, ``run_dryrun`` e ``run_import``.

Dry-run e import chamam o MESMO ``apply(row, strategy, env, dry_run)`` de cada entidade, então as
contagens da prévia batem com o que o import grava. O handler só decide ORDEM, progresso, segredos e o
que fazer ao final; nunca conhece uma entidade pelo nome (ver ``IMPORT_ORDER``).

Diferenças para o WhatsBot Pro, por desenho: o lite tem UMA senha de painel (sem usuários, grupos nem
papéis), UM canal e conversa implícita por contato — então não há checagem de administrador, etapa 2,
canal adiado nem mapa de ids persistido.

⚠️ Relatório, ``error`` e log nunca carregam conteúdo de conversa, valor de config, nome nem segredo:
só códigos, contagens e chaves mascaradas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Iterator

from sqlalchemy import text

from portability import crypto
from portability import entities as ents
from portability import jobs
from portability import registry as reg
from portability.base import mask_phone
from portability.format import (SECRETS_NAME, Manifest, PackageError, PackageReader,
                                PackageWriter, entity_path)
from portability.jobs import JobContext, JobFailed

logger = logging.getLogger(__name__)

#: A cada quantas linhas confere cancelamento e grava progresso.
BATCH = 200
MAX_REPORT_WARNING_KEYS = 5

#: Ordem GLOBAL de import (dependências primeiro): config e tags antes de contatos; a mensagem aponta
#: para o contato do pacote, então vem depois dele. É um subconjunto do que o pacote traz.
IMPORT_ORDER = ("config", "tags", "contacts", "messages")

# ── helpers ─────────────────────────────────────────────────────────────────

def _alembic_head() -> str:
    from db.engine import get_engine
    try:
        with get_engine().connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar() or ""
    except Exception:  # noqa: BLE001 — informativo; nunca impede o job
        return ""

def _app_version() -> str:
    """Versão do WhatsBot-Lite (arquivo ``WHATSBOT_VERSION``, nunca ``git describe``)."""
    try:
        from config.settings import get_data_dir
        with open(os.path.join(str(get_data_dir()), "WHATSBOT_VERSION"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"

def _source_instance() -> str:
    """Hash curto do ``installation_id`` — identifica a origem sem revelá-la."""
    from db.repositories import config_repo
    iid = config_repo.get("installation_id") or ""
    return hashlib.sha256(str(iid).encode()).hexdigest()[:16] if iid else ""

def summarize_warnings(warnings: list[dict]) -> dict:
    """``{entidade: {código: {count, keys[≤5]}}}`` — compacto e sem dado sensível."""
    out: dict[str, dict[str, dict]] = {}
    for w in warnings:
        slot = out.setdefault(w["entity"], {}).setdefault(w["code"], {"count": 0, "keys": []})
        slot["count"] += int(w.get("n", 1))
        if len(slot["keys"]) < MAX_REPORT_WARNING_KEYS and w["key"] not in slot["keys"]:
            slot["keys"].append(w["key"])
    return out

def _mask_key(raw: Any) -> str:
    """Identificador da linha para o erro: telefone sai MASCARADO, o resto é cortado. Nunca conteúdo
    de mensagem, telefone completo nem segredo."""
    k = str(raw if raw is not None else "?").strip()
    digits = "".join(ch for ch in k if ch.isdigit())
    if len(digits) >= 7 and len(digits) >= len(k.split("@")[0]) - 4:    # parece telefone/JID
        return mask_phone(digits)
    return k[:60]

def _row_error(env: ents.Env, name: str, row: dict, index: int) -> tuple:
    """``(code, detail)`` do erro de UMA linha — só id de origem, chave mascarada e motivo."""
    lw = env.last_warn if env.last_warn and env.last_warn[0] == name else None
    code = lw[2] if lw else "unspecified"
    key_raw = (lw[1] if lw and lw[1] not in ("*", "?") else None) or row.get("name") \
        or row.get("key") or row.get("phone") or row.get("id")
    src = row.get("id") if isinstance(row.get("id"), (int, str)) else None
    return code, {"source_id": None if src is None else str(src)[:40],
                  "key": _mask_key(key_raw), "reason": code}

def _metered(ctx: JobContext, stage: str, entity: str, rows: Iterator[dict],
             sink: dict) -> Iterator[dict]:
    """Conta linhas, confere cancelamento e grava progresso a cada lote."""
    n = 0
    for row in rows:
        if n % BATCH == 0:
            ctx.check_cancel()
            ctx.progress(stage=stage, entity=entity, done=n)
        n += 1
        yield row
    sink[entity] = n
    ctx.progress(stage=stage, entity=entity, done=n)

def _job_dir(ctx: JobContext) -> str:
    d = os.path.join(jobs.jobs_root(), ctx.id)
    os.makedirs(d, exist_ok=True)
    return d

def _rm_quiet(path: str) -> None:
    try:
        jobs.remove_package_dir(path)
    except Exception:  # noqa: BLE001
        logger.warning("[Portability] não removeu %s", path, exc_info=True)

# ═══ export ═════════════════════════════════════════════════════════════════

def run_export(ctx: JobContext) -> dict:
    params = ctx.params
    try:
        scopes = reg.validate_scopes(params.get("scopes"))
    except ValueError as exc:
        raise JobFailed(str(exc)) from None
    include_secrets = bool(params.get("include_secrets"))
    password = ctx.take_password() if include_secrets else None
    if include_secrets:
        try:
            crypto.check_password(password or "")
        except crypto.WeakPasswordError as exc:
            raise JobFailed(str(exc)) from None
    names = [e for e in IMPORT_ORDER if e in reg.entities_for(scopes)]
    env = ents.Env(include_secrets=include_secrets)
    job_dir = _job_dir(ctx)
    part = os.path.join(job_dir, "package.zip.part")
    final = os.path.join(job_dir, "package.zip")
    counts: dict[str, int] = {}
    try:
        manifest = Manifest(app_version=_app_version(), alembic_head=_alembic_head(),
                            created_at=time.time(), scopes=scopes,
                            options={"include_secrets": include_secrets},
                            source_instance=_source_instance())
        with PackageWriter(part) as w:
            for name in names:
                w.write_ndjson(name, _metered(
                    ctx, "export", name, ents.entity(name).export(env), counts))
            ctx.progress(stage="export", entity="finalize", done=1, total=1, percent=100)
            ctx.check_cancel()
            if include_secrets and env.secrets:
                try:
                    blob = crypto.encrypt(env.secrets, password or "", manifest.aad())
                except crypto.CryptoUnavailableError as exc:
                    raise JobFailed(str(exc)) from None
                w.write_bytes(SECRETS_NAME, blob)
                manifest.secrets = {"present": True, "kdf": "scrypt", "cipher": "aes-256-gcm"}
            manifest.counts = dict(counts)
            w.finish(manifest)
        os.replace(part, final)
    except BaseException:
        _rm_quiet(job_dir)           # nada de pacote pela metade (nem a senha) — o erro segue ao runner
        raise
    ctx.set_package(final)
    return {"counts": counts, "scopes": scopes, "secrets_included": bool(env.secrets),
            "bytes": os.path.getsize(final), "warnings": summarize_warnings(env.warnings)}

# ═══ leitura do pacote ══════════════════════════════════════════════════════

def _open_package(ctx: JobContext) -> PackageReader:
    path = ctx.params.get("package_path") or ctx.job.get("package_path")
    if not path or not os.path.isfile(path):
        raise JobFailed("O arquivo do pacote não está mais disponível. Envie-o de novo.")
    base = os.path.realpath(jobs.jobs_root())
    if os.path.commonpath([base, os.path.realpath(path)]) != base:
        raise JobFailed("Caminho de pacote inválido.")
    try:
        return PackageReader(path)
    except PackageError as exc:
        raise JobFailed(str(exc)) from None

def _check_integrity(reader: PackageReader) -> None:
    problems = reader.verify_checksums()
    if problems:
        # só a CONTAGEM e o primeiro item (um nome de arquivo do pacote, não conteúdo)
        raise JobFailed(f"Pacote corrompido ou adulterado ({len(problems)} problema(s); "
                        f"ex.: {problems[0]}).")

def _load_secrets(ctx: JobContext, reader: PackageReader) -> tuple[dict, str]:
    """``(segredos, estado)`` — estado: absent | locked | ok. Senha errada FALHA o job."""
    if not reader.has(SECRETS_NAME):
        return {}, "absent"
    password = ctx.take_password()
    if not password:
        return {}, "locked"
    try:
        blob = reader.read_member(SECRETS_NAME, limit=64 * 1024 * 1024)
        return crypto.decrypt(blob, password, reader.manifest.aad()), "ok"
    except crypto.SecretsDecryptError as exc:
        raise JobFailed(str(exc)) from None
    except crypto.CryptoUnavailableError as exc:
        raise JobFailed(str(exc)) from None
    except (crypto.SecretsFormatError, PackageError):
        raise JobFailed("Arquivo de segredos inválido.") from None

def _present_entities(reader: PackageReader) -> list[str]:
    """Entidades do pacote que ESTA versão sabe aplicar, na ordem de dependência. Arquivo
    desconhecido é ignorado (pacote de uma versão mais nova que acrescentou entidade)."""
    have = set(reader.names())
    return [n for n in IMPORT_ORDER if n in ents.ENTITIES and entity_path(n) in have]

def _manifest_view(m: Manifest, secrets_state: str) -> dict:
    return {"schema_version": m.schema_version, "app_version": m.app_version,
            "alembic_head": m.alembic_head, "created_at": m.created_at,
            "scopes": m.scopes, "counts": m.counts, "secrets": secrets_state,
            "source_instance": m.source_instance}

def _chosen_strategy(raw: Any, names: list[str]) -> dict[str, str]:
    try:
        return reg.validate_strategy(
            {k: v for k, v in raw.items() if k in names} if isinstance(raw, dict) else None, names)
    except ValueError as exc:
        raise JobFailed(str(exc)) from None

def _run_entities(ctx: JobContext, reader: PackageReader, names: list[str], env: ents.Env,
                  strategy: dict[str, str], dry_run: bool) -> dict[str, dict[str, int]]:
    stage = "dryrun" if dry_run else "import"
    result: dict[str, dict[str, int]] = {}
    env.finalizers.clear()
    env._finalizer_keys.clear()
    try:
        for name in names:
            tally = {o: 0 for o in ents.OUTCOMES}
            spec = ents.entity(name)
            sink: dict[str, int] = {}
            for idx, row in enumerate(_metered(ctx, stage, name, reader.iter_ndjson(name), sink), 1):
                env.last_warn = None                 # o motivo do erro é o aviso DESTA linha
                env.cache["row_index"] = idx
                try:
                    outcome = spec.apply(row, strategy.get(name, reg.SKIP), env, dry_run)
                except Exception:  # noqa: BLE001 — uma linha ruim não derruba o lote
                    logger.warning("[Portability] linha de %s falhou", name, exc_info=True)
                    env.warn(name, str(row.get("name") or row.get("key") or "?"), "row_failed")
                    outcome = ents.ERROR
                tally[outcome] += 1
                if outcome == ents.ERROR and env.error_sink is not None:
                    code, detail = _row_error(env, name, row, idx)
                    env.error_sink.add(name, idx, code, detail)
            result[name] = tally
    finally:
        # Lotes pendentes (mensagens) — inclusive quando o job foi CANCELADO no meio.
        for hook in env.flush_hooks:
            try:
                hook(env, dry_run)
            except Exception:  # noqa: BLE001
                logger.warning("[Portability] descarga final falhou", exc_info=True)
    # Linhas já contadas como criadas que o lote depois não conseguiu gravar.
    for name, n in env.late_errors.items():
        if name in result:
            moved = min(n, result[name][ents.CREATE])
            result[name][ents.CREATE] -= moved
            result[name][ents.ERROR] += moved
    try:
        env.finalize(dry_run)
    except Exception:  # noqa: BLE001
        logger.warning("[Portability] finalização falhou", exc_info=True)
    return result

# ═══ dry-run ════════════════════════════════════════════════════════════════

def run_dryrun(ctx: JobContext) -> dict:
    with _open_package(ctx) as reader:
        _check_integrity(reader)
        m = reader.manifest
        secrets, secrets_state = _load_secrets(ctx, reader)
        env = ents.Env(secrets=secrets)
        names = _present_entities(reader)
        if not names:
            raise JobFailed("O pacote não traz nenhuma entidade que esta versão saiba importar.")
        # "Recalcular prévia" grava a escolha do operador em ``params.strategy``; sem ela vale o padrão.
        strategy = _chosen_strategy(ctx.params.get("strategy"), names)
        jobs.clear_errors(ctx.id)
        env.error_sink = jobs.ErrorSink(ctx.id)
        try:
            tallies = _run_entities(ctx, reader, names, env, strategy, dry_run=True)
        finally:
            env.error_sink.flush()
        head = _alembic_head()
        notes = []
        if m.alembic_head and head and m.alembic_head != head:
            notes.append("alembic_head_differs")
        if m.source_instance and m.source_instance == _source_instance():
            notes.append("same_instance")
        return {
            "manifest": _manifest_view(m, secrets_state),
            "entities": tallies,
            "default_strategy": {n: reg.default_strategy(n) for n in names},
            "strategy": strategy,
            "errors": env.error_sink.summary(),
            "supported_strategies": {n: list(reg.SUPPORTED_STRATEGIES.get(n, (reg.SKIP,)))
                                     for n in names},
            "warnings": summarize_warnings(env.warnings),
            "notes": notes,
            "already_imported": jobs.find_imported(ctx.params.get("package_sha256") or "") or None,
        }

# ═══ import ═════════════════════════════════════════════════════════════════

def run_import(ctx: JobContext) -> dict:
    with _open_package(ctx) as reader:
        _check_integrity(reader)
        secrets, secrets_state = _load_secrets(ctx, reader)
        env = ents.Env(secrets=secrets, overwrite_secrets=bool(ctx.params.get("overwrite_secrets")))
        names = _present_entities(reader)
        strategy = _chosen_strategy(ctx.params.get("strategy"), names)
        jobs.clear_errors(ctx.id)
        env.error_sink = jobs.ErrorSink(ctx.id)
        started = time.time()
        try:
            tallies = _run_entities(ctx, reader, names, env, strategy, dry_run=False)
        finally:
            env.error_sink.flush()
        post = _postprocess(ctx, env)
        report = {"entities": tallies, "errors": env.error_sink.summary(), "strategy": strategy,
                  "secrets": secrets_state, "warnings": summarize_warnings(env.warnings),
                  "post": post}
        _log_done(ctx, report, reader.manifest, time.time() - started)
        return report

def _postprocess(ctx: JobContext, env: ents.Env) -> dict:
    """Depois de gravar: recarrega o que o processo guarda em memória e avisa o painel.

    NÃO emite ``tag.created``/``config.changed`` nem nenhum evento de plugin: um import não é ação do
    operador sobre cada registro, e plugin nenhum deve reagir a ele (nem a IA responder)."""
    out = {"config_keys_changed": len(env.config_changed), "tags_reloaded": False}
    deps = ctx.deps
    if deps is None:
        return out
    handler = getattr(deps, "agent_handler", None)
    settings = getattr(deps, "settings", None)
    try:
        if env.config_changed and handler is not None and settings is not None:
            # O mesmo repasse que o PUT /api/config faz: sem ele o handler seguiria com o prompt, o
            # modelo e a chave antigos em memória até o próximo reinício.
            handler.update_config(
                api_key=settings.get("openrouter_api_key", ""),
                system_prompt=settings.get("system_prompt", ""),
                model=settings.get("model", "deepseek/deepseek-v4.1-flash"),
                improvement_model=settings.get("improvement_model", ""),
                audio_model=settings.get("audio_model", "google/gemini-2.5-flash"),
                image_model=settings.get("image_model", "google/gemini-2.5-flash"),
                document_model=settings.get("document_model", "google/gemini-2.5-flash"),
                max_context_messages=settings.get("max_context_messages", 10),
                split_messages=settings.get("split_messages", True),
                default_ai_enabled=settings.get("default_ai_enabled", True),
                ai_engine_enabled=settings.get("ai_engine_enabled", False),
            )
        if env.cache.get("tags_changed") and handler is not None:
            handler.tag_registry._load()
            out["tags_reloaded"] = True
        from plugins.context import broadcast       # thread-safe; o handler roda numa thread
        if env.config_changed:
            broadcast("config_saved", {})
        if out["tags_reloaded"]:
            broadcast("tags_changed", handler.tag_registry.all())
    except Exception:  # noqa: BLE001 — o que já foi gravado vale; o painel se atualiza no F5
        logger.warning("[Portability] pós-processo falhou", exc_info=True)
        out["post_failed"] = True
    return out

def _log_done(ctx: JobContext, report: dict, manifest: Manifest, seconds: float) -> None:
    """Sem trilha de auditoria no lite: fica o log, SÓ com contagens (nunca conteúdo nem nome)."""
    totals = {e: {k: v for k, v in t.items() if v} for e, t in report["entities"].items()}
    logger.info("[Portability] import %s concluído em %.1fs (origem v%s): %s",
                ctx.id[:8], seconds, manifest.app_version or "?", json.dumps(totals))

def _log_failed(job: dict, error: str) -> None:
    if job.get("kind") == "import":
        logger.warning("[Portability] import %s falhou: %s", str(job.get("id"))[:8], error)

def register_all() -> None:
    """Liga os executores ao runner (chamado no boot pelo laço de fundo)."""
    jobs.register_handler("export", run_export)
    jobs.register_handler("import_dryrun", run_dryrun)
    jobs.register_handler("import", run_import)
    if _log_failed not in jobs._FAIL_HOOKS:
        jobs._FAIL_HOOKS.append(_log_failed)
