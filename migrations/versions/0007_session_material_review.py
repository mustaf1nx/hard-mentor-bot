"""Назначение Автора и подтверждение методической проверки материалов.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Базлайн 0001 намеренно пустой: схема до переработки создавалась через
    # create_all. На старой базе какой-то таблицы может не быть — это не повод
    # ронять всю цепочку миграций.
    tables = _tables()
    if 'mentoring_sessions' in tables:
        existing = _columns('mentoring_sessions')
        with op.batch_alter_table('mentoring_sessions') as batch:
            if 'author_id' not in existing:
                batch.add_column(sa.Column('author_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', name='fk_session_author'), nullable=True))
            if 'format_confirmed' not in existing:
                batch.add_column(sa.Column('format_confirmed', sa.Boolean(), nullable=True))
    if 'learning_materials' in tables:
        existing = _columns('learning_materials')
        with op.batch_alter_table('learning_materials') as batch:
            for column in (
                sa.Column('cycle_stage', sa.String(16), nullable=True),
                sa.Column('review_status', sa.String(16), nullable=True),
                sa.Column('lecturer_name', sa.String(200), nullable=True),
                sa.Column('reviewed_at', sa.DateTime(), nullable=True),
                sa.Column('review_recorded_by', sa.BigInteger(), sa.ForeignKey('users.telegram_id', name='fk_material_review_recorder'), nullable=True),
            ):
                if column.name not in existing:
                    batch.add_column(column)


def downgrade():
    tables = _tables()
    if 'learning_materials' in tables:
        existing = _columns('learning_materials')
        with op.batch_alter_table('learning_materials') as batch:
            for name in ('review_recorded_by', 'reviewed_at', 'lecturer_name', 'review_status', 'cycle_stage'):
                if name in existing:
                    batch.drop_column(name)
    if 'mentoring_sessions' in tables:
        existing = _columns('mentoring_sessions')
        with op.batch_alter_table('mentoring_sessions') as batch:
            if 'format_confirmed' in existing:
                batch.drop_column('format_confirmed')
            if 'author_id' in existing:
                batch.drop_column('author_id')
