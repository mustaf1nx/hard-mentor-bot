"""Persistent one-hour grace period for new empty topics."""
import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    if 'topic_expiries' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table('topic_expiries',
                        sa.Column('topic_id', sa.Integer(), sa.ForeignKey('mentor_topics.id', ondelete='CASCADE'), primary_key=True),
                        sa.Column('expires_at', sa.DateTime(), nullable=False))
        op.create_index('ix_topic_expiries_expires_at', 'topic_expiries', ['expires_at'])


def downgrade():
    op.drop_table('topic_expiries')
