"""SQLite → Postgres migration helper.

Used by the admin endpoint to copy every row from the currently bound SQLite
engine into a freshly-prepared Postgres database. The flow:

1. Build a one-off engine for the target URL (without touching the module-level
   engine, which is still serving requests).
2. Verify the target is reachable and EMPTY of WhatsBot-Lite tables — refuse to
   overwrite an existing database.
3. Run Alembic migrations on the target so its schema matches the source.
4. Walk every core ``Table`` plus every ``plugin_*`` table (read from the
   source via ``inspect``), copying rows in batches.
5. The caller is responsible for swapping ``storages/database.json`` and
   triggering a server restart once this returns successfully.

This module is pure logic — no FastAPI imports — so it can be unit-tested
without spinning up the web layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text as sa_text
from sqlalchemy.engine import Engine

from db.engine import get_engine
from db.tables import CORE_TABLES, metadata as core_metadata

#: Tabelas que NÃO são copiadas na migração SQLite → Postgres (estado de execução, não dado do usuário).
EPHEMERAL_TABLES = frozenset({"portability_jobs", "portability_job_errors"})

logger = logging.getLogger(__name__)


BATCH_SIZE = 500


class TargetNotEmptyError(RuntimeError):
    """Raised when the target database already contains WhatsBot-Lite tables.

    Carries the list of conflicting table names so the UI can render them and
    offer the destructive ``force_drop`` retry.
    """

    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__(
            "Target database is not empty — found existing tables: "
            + ", ".join(conflicts)
        )


@dataclass
class MigrationProgress:
    """Snapshot of the migration state — emitted via callback."""

    stage: str  # "validating" | "wiping" | "schema" | "copying" | "done" | "failed"
    table: Optional[str] = None
    rows_done: int = 0
    rows_total: int = 0
    tables_done: int = 0
    tables_total: int = 0
    message: str = ""
    error: Optional[str] = None
    per_table: dict = field(default_factory=dict)  # table_name -> rows copied
    # Populated when ``stage="failed"`` due to a non-empty target. The UI uses
    # this to render the destructive-confirm dialog.
    conflicts: list[str] = field(default_factory=list)


ProgressCb = Callable[[MigrationProgress], None]


def _noop_progress(_: MigrationProgress) -> None:
    return None


def _build_target_engine(url: str) -> Engine:
    if not url.startswith(("postgresql://", "postgresql+", "postgres://", "postgres+")):
        raise ValueError(
            "Target URL must be a Postgres URL "
            "(e.g. 'postgresql+psycopg://user:pass@host:5432/db')."
        )
    return create_engine(url, future=True, pool_pre_ping=True)


def _list_conflicts(engine: Engine) -> list[str]:
    """Return WhatsBot-Lite-related tables already present on the target."""
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    collisions = existing & (CORE_TABLES | {"alembic_version"})
    plugin_collisions = {t for t in existing if t.startswith("plugin_")}
    return sorted(collisions | plugin_collisions)


def _ensure_target_empty(engine: Engine) -> None:
    conflicts = _list_conflicts(engine)
    if conflicts:
        raise TargetNotEmptyError(conflicts)


def _drop_target_schema(engine: Engine) -> None:
    """Wipe the target's ``public`` schema. **DESTRUCTIVE** — drops every table.

    Postgres-only: SQLite never reaches this code path because the migration
    targets a Postgres URL by construction. The schema is re-created and
    permissions restored so the subsequent Alembic upgrade can write.
    """
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "force_drop only supported for Postgres targets "
            f"(got dialect '{engine.dialect.name}')"
        )
    with engine.begin() as conn:
        conn.execute(sa_text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(sa_text("CREATE SCHEMA public"))
        # Keep behavior compatible with default Postgres installs where the
        # connecting role owns the schema; on managed services (Neon, RDS) the
        # owner is usually the role we logged in with anyway.
        conn.execute(sa_text("GRANT ALL ON SCHEMA public TO CURRENT_USER"))
        conn.execute(sa_text("GRANT ALL ON SCHEMA public TO public"))


def _apply_alembic_to_target(engine: Engine) -> None:
    """Run ``alembic upgrade head`` against the target engine."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "db" / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))
    # The alembic env reads `db.engine.get_engine()` first; we briefly swap it.
    from db import engine as engine_module

    saved_engine = engine_module._engine
    saved_url = engine_module._db_url
    try:
        engine_module._engine = engine
        engine_module._db_url = str(engine.url)
        command.upgrade(cfg, "head")
    finally:
        engine_module._engine = saved_engine
        engine_module._db_url = saved_url


def _discover_plugin_tables(source: Engine) -> list[str]:
    """Plugin-OWNED tables (``plugin_<id>_*``), excluding the core registry
    tables that happen to share the ``plugin_`` prefix (``plugin_migrations``).
    """
    insp = inspect(source)
    return sorted(
        t for t in insp.get_table_names()
        if t.startswith("plugin_") and t not in CORE_TABLES
    )


def _reflect_plugin_tables(source: Engine, target: Engine, names: list[str]) -> dict[str, Table]:
    """Reflect plugin tables from the source and create them on the target.

    Plugin migrations live inside the plugin folders, not in the core Alembic
    revisions, so the target Postgres schema does not have these tables yet.
    Reflecting + creating preserves columns/types without re-running plugin SQL
    (which may contain SQLite-only constructs).
    """
    md = MetaData()
    reflected = {}
    for name in names:
        try:
            reflected[name] = Table(name, md, autoload_with=source)
        except Exception as exc:
            raise RuntimeError(f"Could not reflect plugin table '{name}': {exc}") from exc
    md.create_all(target)
    return reflected


def _count_rows(engine: Engine, table: Table) -> int:
    from sqlalchemy import func
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar() or 0


def repair_postgres_sequences(engine: Engine) -> dict[str, int]:
    """Re-anchor every Postgres sequence to ``MAX(<pk>)`` of its table.

    SQLite generates row IDs implicitly via ``rowid``, but our migrator copies
    rows with the **explicit** id from the source so foreign keys stay
    consistent. In Postgres, an explicit value supplied to an IDENTITY column
    does NOT advance the underlying sequence — so the next implicit INSERT
    tries to reuse id=1 and hits a unique-constraint violation.

    Walks every table with a single-column integer primary key, asks Postgres
    for the bound sequence via ``pg_get_serial_sequence`` (works for both
    classic ``SERIAL`` columns and modern ``GENERATED BY DEFAULT AS IDENTITY``
    columns) and calls ``setval`` with the current ``MAX(id)``. Returns a
    ``{table: new_value}`` map for telemetry.

    Idempotent and safe to call repeatedly.
    """
    if engine.dialect.name != "postgresql":
        return {}

    insp = inspect(engine)
    fixed: dict[str, int] = {}

    with engine.begin() as conn:
        for table_name in insp.get_table_names():
            if table_name == "alembic_version":
                continue
            pks = insp.get_pk_constraint(table_name).get("constrained_columns") or []
            if len(pks) != 1:
                continue  # composite PK — no sequence to repair
            pk_col = pks[0]
            cols = {c["name"]: c for c in insp.get_columns(table_name)}
            col_def = cols.get(pk_col)
            if col_def is None:
                continue
            # Only integer PKs have sequences. Skip TEXT PKs like ``config.key``.
            type_repr = str(col_def["type"]).upper()
            if "INT" not in type_repr:
                continue

            seq_name = conn.execute(
                sa_text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table_name, "c": pk_col},
            ).scalar()
            if not seq_name:
                continue  # column isn't bound to a sequence (manual id only)

            max_id = conn.execute(
                sa_text(f'SELECT COALESCE(MAX("{pk_col}"), 0) FROM "{table_name}"')
            ).scalar() or 0

            # ``setval(seq, n, true)`` — next nextval() returns n+1. When the
            # table is empty we pass 1 with ``is_called=false`` so the first
            # implicit insert still starts at 1.
            if max_id > 0:
                conn.execute(
                    sa_text("SELECT setval(:s, :v, true)"),
                    {"s": seq_name, "v": int(max_id)},
                )
            else:
                conn.execute(
                    sa_text("SELECT setval(:s, 1, false)"),
                    {"s": seq_name},
                )
            fixed[table_name] = int(max_id)

    return fixed


def _copy_table(source: Engine, target: Engine, table: Table,
                progress: MigrationProgress, cb: ProgressCb) -> int:
    """Copy every row of ``table`` from source to target. Returns rows copied."""
    total = _count_rows(source, table)
    progress.table = table.name
    progress.rows_total = total
    progress.rows_done = 0
    cb(progress)

    if total == 0:
        progress.per_table[table.name] = 0
        return 0

    copied = 0
    with source.connect() as src, target.begin() as dst:
        result = src.execution_options(stream_results=True).execute(select(table))
        while True:
            chunk = result.fetchmany(BATCH_SIZE)
            if not chunk:
                break
            dst.execute(table.insert(), [dict(r._mapping) for r in chunk])
            copied += len(chunk)
            progress.rows_done = copied
            cb(progress)

    progress.per_table[table.name] = copied
    return copied


def migrate_sqlite_to_postgres(
    target_url: str,
    on_progress: Optional[ProgressCb] = None,
    *,
    force_drop: bool = False,
) -> MigrationProgress:
    """Run the full SQLite → Postgres migration. Returns final progress snapshot.

    Args:
        target_url: SQLAlchemy URL of the destination Postgres.
        on_progress: Callback receiving ``MigrationProgress`` snapshots — emitted
            on every meaningful state change so the UI can stream progress.
        force_drop: If ``True``, ``DROP SCHEMA public CASCADE`` runs against the
            target before the schema is applied. **DESTRUCTIVE** — every table
            in the destination disappears. The caller is responsible for the
            explicit user confirmation.

    Every failure path emits ``stage="failed"`` and a message before returning,
    so the UI never gets stuck on "validating".
    """
    cb = on_progress or _noop_progress
    progress = MigrationProgress(stage="validating", message="Conectando ao banco de destino")
    cb(progress)

    source = get_engine()
    if source.dialect.name != "sqlite":
        progress.stage = "failed"
        progress.error = "A origem precisa ser SQLite. Já está em Postgres?"
        cb(progress)
        return progress

    target = None
    try:
        target = _build_target_engine(target_url)

        if force_drop:
            progress.stage = "wiping"
            progress.message = "Apagando schema existente no destino"
            cb(progress)
            _drop_target_schema(target)

        _ensure_target_empty(target)

        progress.stage = "schema"
        progress.message = "Aplicando schema no destino (Alembic)"
        cb(progress)
        _apply_alembic_to_target(target)

        plugin_names = _discover_plugin_tables(source)
        plugin_tables = _reflect_plugin_tables(source, target, plugin_names) if plugin_names else {}

        # Jobs de importar/exportar são efêmeros e apontam para pacotes e processos DESTA instalação:
        # a tabela existe no destino (Alembic) mas fica vazia.
        ordered = ([t for t in core_metadata.sorted_tables if t.name not in EPHEMERAL_TABLES]
                   + [plugin_tables[n] for n in plugin_names])
        progress.stage = "copying"
        progress.tables_total = len(ordered)
        progress.message = "Copiando dados"
        cb(progress)

        for idx, table in enumerate(ordered, start=1):
            _copy_table(source, target, table, progress, cb)
            progress.tables_done = idx
            cb(progress)

        # Re-anchor sequences so new inserts don't collide with the migrated
        # rows. Without this step, the next implicit ``INSERT`` reuses id=1.
        progress.message = "Reanchorando sequences (Postgres)"
        progress.table = None
        cb(progress)
        repaired = repair_postgres_sequences(target)
        logger.info("Reanchored %d Postgres sequences after migration", len(repaired))

        progress.stage = "done"
        progress.message = "Migração concluída"
        progress.table = None
        cb(progress)
        return progress
    except TargetNotEmptyError as exc:
        logger.warning("Target not empty: %s", exc.conflicts)
        progress.stage = "failed"
        progress.error = str(exc)
        progress.conflicts = exc.conflicts
        cb(progress)
        return progress
    except ModuleNotFoundError as exc:
        logger.exception("SQLite → Postgres migration failed: driver not found")
        progress.stage = "failed"
        progress.error = (
            f"Driver Postgres não encontrado: {exc.name}. "
            "Use uma URL no formato 'postgresql+psycopg://user:pass@host:5432/db' "
            "(o WhatsBot-Lite ships com psycopg, não psycopg2)."
        )
        cb(progress)
        return progress
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        logger.exception("SQLite → Postgres migration failed")
        progress.stage = "failed"
        progress.error = str(exc) or exc.__class__.__name__
        cb(progress)
        return progress
    finally:
        if target is not None:
            target.dispose()
