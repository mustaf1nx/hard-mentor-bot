"""Базлайн: схема до переработки.

Ревизия намеренно пустая. Схема на этом этапе создавалась через
`Base.metadata.create_all` в `bot.db.init_db`, поэтому на действующей базе
достаточно отметить её командой:

    alembic stamp 0001

а на пустой — создать схему через бота (или create_all) и тоже поставить stamp.
Все дальнейшие изменения схемы идут обычными ревизиями от 0001.

Revision ID: 0001
Revises:
"""
from __future__ import annotations

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
