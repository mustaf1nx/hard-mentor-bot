"""Единый реестр стаффа: профиль, статус и история зон ответственности.

Добавочная ревизия: только новые таблицы, существующие данные не меняются.
Полную схему на чистой базе по-прежнему создаёт `create_all`; на старой базе
миграция создаёт недостающие таблицы и не падает, если `users` ещё нет.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if 'users' not in tables:
        # Базлайн 0001 пустой; на пустой базе ядро создаёт create_all.
        return

    if 'staff_members' not in tables:
        op.create_table(
            'staff_members',
            sa.Column('telegram_id', sa.BigInteger(),
                      sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), primary_key=True),
            sa.Column('status', sa.String(16), nullable=True),
            sa.Column('team', sa.String(24), nullable=True),
            sa.Column('title', sa.String(120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('updated_by', sa.BigInteger(),
                      sa.ForeignKey('users.telegram_id'), nullable=True),
            sa.CheckConstraint("status IS NULL OR status IN ('active','paused','left')",
                               name='ck_staff_status'),
            sa.CheckConstraint(
                "team IS NULL OR team IN ('hard','hard_leadership','school_leadership')",
                name='ck_staff_team'),
        )
        op.create_index('ix_staff_members_status', 'staff_members', ['status'])

    if 'mentor_subjects' in tables and 'responsibility_assignments' not in tables:
        op.create_table(
            'responsibility_assignments',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('person_id', sa.BigInteger(),
                      sa.ForeignKey('users.telegram_id'), nullable=False),
            sa.Column('zone', sa.String(16), nullable=False),
            sa.Column('subject_id', sa.Integer(),
                      sa.ForeignKey('mentor_subjects.id'), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.BigInteger(),
                      sa.ForeignKey('users.telegram_id'), nullable=False),
            sa.Column('ended_by', sa.BigInteger(),
                      sa.ForeignKey('users.telegram_id'), nullable=True),
            sa.CheckConstraint("zone IN ('subject','schedule','materials')", name='ck_resp_zone'),
            sa.CheckConstraint(
                "(zone = 'subject' AND subject_id IS NOT NULL) OR "
                "(zone <> 'subject' AND subject_id IS NULL)",
                name='ck_resp_subject'),
            sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name='ck_resp_dates'),
        )
        op.create_index('ix_responsibility_assignments_person_id',
                        'responsibility_assignments', ['person_id'])
        op.create_index('ix_responsibility_assignments_zone',
                        'responsibility_assignments', ['zone'])
        op.create_index('ix_responsibility_assignments_subject_id',
                        'responsibility_assignments', ['subject_id'])
        op.create_index('ix_responsibility_assignments_started_at',
                        'responsibility_assignments', ['started_at'])
        op.create_index('ix_responsibility_assignments_ended_at',
                        'responsibility_assignments', ['ended_at'])
        # SQLite и PostgreSQL поддерживают частичные уникальные индексы.
        op.execute(
            "CREATE UNIQUE INDEX uq_resp_subject_active "
            "ON responsibility_assignments (subject_id) "
            "WHERE ended_at IS NULL AND zone = 'subject'")
        op.execute(
            "CREATE UNIQUE INDEX uq_resp_global_active "
            "ON responsibility_assignments (zone) "
            "WHERE ended_at IS NULL AND zone IN ('schedule','materials')")


def downgrade() -> None:
    tables = _tables()
    if 'responsibility_assignments' in tables:
        for name in ('uq_resp_global_active', 'uq_resp_subject_active',
                     'ix_responsibility_assignments_ended_at',
                     'ix_responsibility_assignments_started_at',
                     'ix_responsibility_assignments_subject_id',
                     'ix_responsibility_assignments_zone',
                     'ix_responsibility_assignments_person_id'):
            op.execute(f'DROP INDEX IF EXISTS {name}')
        op.drop_table('responsibility_assignments')
    if 'staff_members' in tables:
        op.execute('DROP INDEX IF EXISTS ix_staff_members_status')
        op.drop_table('staff_members')
