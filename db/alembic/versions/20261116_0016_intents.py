"""Persistent opt-in assisted intentions; no changes to existing conversations."""
from alembic import op
from db.tables import intents, intent_tasks, intent_events, intent_operations, intent_audit, intent_limits
revision = "0016_intents"
down_revision = "0015_portability_jobs"
branch_labels = None
depends_on = None

def upgrade():
    for table in (intents, intent_tasks, intent_events, intent_operations, intent_audit, intent_limits):
        table.create(op.get_bind(), checkfirst=True)

def downgrade():
    for table in (intent_limits, intent_audit, intent_operations, intent_events, intent_tasks, intents):
        table.drop(op.get_bind(), checkfirst=True)
