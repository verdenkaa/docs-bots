"""Separate web admission from Telegram updates and deliveries."""

from alembic import op
import sqlalchemy as sa

revision = "003_mini_app"
down_revision = "002_ticket_resources"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "rag_requests",
        sa.Column("channel", sa.Text(), server_default="telegram", nullable=False),
    )
    op.alter_column(
        "rag_requests", "chat_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.alter_column(
        "rag_requests", "update_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.create_check_constraint(
        "rag_requests_channel_check",
        "rag_requests",
        "(channel='telegram' AND chat_id IS NOT NULL AND update_id IS NOT NULL) OR "
        "(channel='web' AND chat_id IS NULL AND update_id IS NULL)",
    )
    op.create_table(
        "web_requests",
        sa.Column(
            "request_id",
            sa.BigInteger(),
            sa.ForeignKey("rag_requests.request_id"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("user_id", "idempotency_key", name="web_requests_user_key"),
    )
    op.create_index("support_owner_page", "support_cases", ["user_id", "request_id"])


def downgrade():
    # Refuse data loss rather than manufacture Telegram identities for web rows.
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM rag_requests WHERE channel='web' LIMIT 1"))
        .first()
    ):
        raise RuntimeError(
            "Cannot downgrade while web requests exist; restore the pre-migration backup"
        )
    op.drop_table("web_requests")
    op.drop_index("support_owner_page", table_name="support_cases")
    op.drop_constraint("rag_requests_channel_check", "rag_requests", type_="check")
    op.alter_column(
        "rag_requests", "chat_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.alter_column(
        "rag_requests", "update_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_column("rag_requests", "channel")
