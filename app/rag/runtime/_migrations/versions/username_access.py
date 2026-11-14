"""Username allowlist, static admin principals and public ticket messages.

Keep the first migration immutable so existing installations can upgrade safely.
"""

from alembic import op
import sqlalchemy as sa

revision = "002_username_access"
down_revision = "001_runtime"


def upgrade():
    connection = op.get_bind()
    # Telegram IDs cannot be converted to current usernames without Telegram data.
    # Retain old permissions for the operator, but never grant access by guessing.
    employees = sa.table("employees", sa.column("user_id", sa.BigInteger))
    if connection.scalar(sa.select(sa.exists().select_from(employees))):
        op.rename_table("employees", "legacy_employee_access")
    else:
        op.drop_table("employees")
    username = sa.column("username", sa.Text)
    op.create_table(
        "employees",
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.PrimaryKeyConstraint("username", name="employees_username_pkey"),
        sa.CheckConstraint(
            username.regexp_match(r"^[a-z0-9_]{1,32}$"), name="employees_username_format"
        ),
    )
    op.add_column("rag_requests", sa.Column("username", sa.Text(), nullable=True))
    op.create_check_constraint(
        "rag_requests_username_format",
        "rag_requests",
        sa.or_(username.is_(None), username.regexp_match(r"^[a-z0-9_]{1,32}$")),
    )
    for constraint in sa.inspect(connection).get_unique_constraints("support_messages"):
        if set(constraint["column_names"]) == {"case_id", "author_id", "client_message_id"}:
            op.drop_constraint(constraint["name"], "support_messages", type_="unique")
    op.drop_constraint("support_messages_author_role_check", "support_messages", type_="check")
    op.alter_column(
        "support_messages", "author_id", new_column_name="legacy_author_id", nullable=True
    )
    op.add_column("support_messages", sa.Column("legacy_author_role", sa.Text(), nullable=True))
    op.add_column(
        "support_messages", sa.Column("legacy_client_message_id", sa.Uuid(), nullable=True)
    )
    op.add_column("support_messages", sa.Column("author_login", sa.Text(), nullable=True))
    messages = sa.table(
        "support_messages",
        sa.column("message_id", sa.BigInteger),
        sa.column("case_id", sa.Uuid),
        sa.column("author_role", sa.Text),
        sa.column("author_login", sa.Text),
        sa.column("client_message_id", sa.Uuid),
        sa.column("legacy_author_id", sa.BigInteger),
        sa.column("legacy_author_role", sa.Text),
        sa.column("legacy_client_message_id", sa.Uuid),
    )
    operator = messages.c.author_role.in_(("operator", "admin"))
    op.execute(
        sa.update(messages).values(
            legacy_author_role=messages.c.author_role,
            legacy_client_message_id=messages.c.client_message_id,
            author_login=sa.case(
                (
                    operator,
                    sa.literal("legacy-telegram:") + sa.cast(messages.c.legacy_author_id, sa.Text),
                )
            ),
            author_role=sa.case((operator, "operator"), else_="visitor"),
        )
    )
    # Different old Telegram authors could have reused a client key. Preserve all
    # messages and the original keys while assigning deterministic replacement keys.
    duplicates = sa.select(
        messages.c.message_id,
        sa.func.row_number()
        .over(
            partition_by=(messages.c.case_id, messages.c.author_role, messages.c.client_message_id),
            order_by=messages.c.message_id,
        )
        .label("n"),
    ).cte("duplicates")
    op.execute(
        sa.update(messages)
        .where(messages.c.message_id == duplicates.c.message_id, duplicates.c.n > 1)
        .values(
            client_message_id=sa.cast(
                sa.func.md5(
                    sa.literal("legacy-support-message:") + sa.cast(messages.c.message_id, sa.Text)
                ),
                sa.Uuid,
            )
        )
    )
    op.create_unique_constraint(
        "support_messages_case_role_client_key",
        "support_messages",
        ["case_id", "author_role", "client_message_id"],
    )
    op.create_check_constraint(
        "support_messages_public_author",
        "support_messages",
        sa.and_(
            messages.c.author_role.in_(("visitor", "operator")),
            (messages.c.author_role == "operator") == messages.c.author_login.is_not(None),
        ),
    )


def downgrade():
    raise RuntimeError(
        "Identity migration requires an operator-managed restore; automatic downgrade is unsafe"
    )
