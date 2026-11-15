"""Bound ticket messages and index pagination/rate checks."""

from alembic import op

revision = '002_ticket_resources'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_check_constraint('support_messages_text_length', 'support_messages', 'length(text)<=4096')
    op.create_index('support_messages_page', 'support_messages', ['case_id', 'message_id'])
    op.create_index('support_messages_rate', 'support_messages', ['case_id', 'author_role', 'created_at'])


def downgrade():
    op.drop_index('support_messages_rate', table_name='support_messages')
    op.drop_index('support_messages_page', table_name='support_messages')
    op.drop_constraint('support_messages_text_length', 'support_messages', type_='check')
