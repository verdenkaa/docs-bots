"""Technical model-call journal: retry replays, never an expiring inference lease."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "003_model_calls"
down_revision = "002_username_access"


def upgrade():
    op.create_table(
        "model_calls",
        sa.Column("call_key", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.Uuid, nullable=False),
        sa.Column("owner_pid", sa.Integer, nullable=False),
        sa.Column("owner_host", sa.Text, nullable=False),
        sa.Column("work_ref", sa.Text, nullable=False),
        sa.Column("profile_id", sa.Text, nullable=False),
        sa.Column("model_role", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("result", JSONB(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('executing','completed','uncertain')", name="model_calls_state"
        ),
        sa.CheckConstraint(
            "(state='completed')=(result IS NOT NULL)", name="model_calls_result"
        ),
    )
    op.create_index("model_calls_pending", "model_calls", ["state", "created_at"])


def downgrade():
    op.drop_table("model_calls")
