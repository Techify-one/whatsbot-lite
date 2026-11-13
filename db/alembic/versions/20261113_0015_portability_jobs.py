"""jobs do Importar/Exportar (portability_jobs, portability_job_errors)

Revision ID: 0015_portability_jobs
Revises: 0014_contact_ai_disabled_at
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_portability_jobs"
down_revision: Union[str, Sequence[str], None] = "0014_contact_ai_disabled_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # JSON como Text (convenção do lite, como ``messages.reactions``): o mesmo código roda em SQLite
    # e em Postgres. O que o worker precisa CONSULTAR (afinidade, job de origem, hash do pacote,
    # etapa de retomada) é coluna de verdade — não existe ``->>`` portável.
    op.create_table(
        "portability_jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("progress", sa.Text, nullable=False, server_default="{}"),
        sa.Column("params", sa.Text, nullable=False, server_default="{}"),
        sa.Column("report", sa.Text, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text),
        sa.Column("package_path", sa.Text),
        sa.Column("package_sha256", sa.Text),
        sa.Column("source_job", sa.Text),
        sa.Column("affinity", sa.Text),
        sa.Column("resume_stage", sa.Text),
        sa.Column("interrupted", sa.Integer, nullable=False, server_default="0"),
        sa.Column("recoveries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("started_at", sa.Float),
        sa.Column("finished_at", sa.Float),
        sa.Column("expires_at", sa.Float),
        sa.Column("heartbeat_at", sa.Float),
        sa.Column("cancel_requested", sa.Integer, nullable=False, server_default="0"),
        sa.Column("worker_id", sa.Text),
        sa.CheckConstraint("kind IN ('export','import_dryrun','import')", name="ck_portability_jobs_kind"),
    )
    op.create_index("idx_portability_jobs_state", "portability_jobs", ["state", "created_at"])
    op.create_index("idx_portability_jobs_source", "portability_jobs", ["source_job"])
    op.create_index("idx_portability_jobs_sha", "portability_jobs", ["package_sha256"])

    # Integer (não BigInteger): no SQLite só ``INTEGER PRIMARY KEY`` vira rowid autoincrementável.
    op.create_table(
        "portability_job_errors",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("portability_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity", sa.Text, nullable=False),
        sa.Column("row_index", sa.Integer),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("idx_portability_errors_job", "portability_job_errors", ["job_id", "entity", "id"])

def downgrade() -> None:
    op.drop_index("idx_portability_errors_job", table_name="portability_job_errors")
    op.drop_table("portability_job_errors")
    op.drop_index("idx_portability_jobs_sha", table_name="portability_jobs")
    op.drop_index("idx_portability_jobs_source", table_name="portability_jobs")
    op.drop_index("idx_portability_jobs_state", table_name="portability_jobs")
    op.drop_table("portability_jobs")
