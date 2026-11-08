"""Тип медиа материала: документ или фотография заметок.

Добавочная ревизия: существующие PDF/DOCX остаются document.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if 'learning_materials' not in _tables():
        return
    columns = {row['name'] for row in sa.inspect(op.get_bind()).get_columns('learning_materials')}
    if 'media_type' in columns:
        return
    op.add_column('learning_materials', sa.Column(
        'media_type', sa.String(8), nullable=False, server_default='document'))


def downgrade() -> None:
    if 'learning_materials' in _tables():
        op.drop_column('learning_materials', 'media_type')
