from datetime import datetime, timedelta
import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from bot import clock, repo
from bot.db import Base, LearningMaterial, Topic, TopicExpiry
from bot.mentoring import service as svc
from bot.mentoring.reminders import check_once
from tests.conftest import COORD, MENTOR, SETTINGS

START = datetime(2026, 11, 14, 10)
ADMIN = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coordinator')


async def new_topic(h, monkeypatch):
    monkeypatch.setattr(clock, 'now', lambda: START)
    async with h.sm() as s:
        topic = await svc.ensure_topic(s, 'Calculus', 'New topic')
        await s.commit()
        return topic.id


async def test_one_hour_boundary_and_restart_safe_cleanup(h, monkeypatch):
    tid = await new_topic(h, monkeypatch)
    h.dp.storage.storage.clear()
    await check_once(h.bot, h.sm, SETTINGS, START + timedelta(minutes=59, seconds=59))
    async with h.sm() as s:
        assert await s.get(Topic, tid) is not None
    await check_once(h.bot, h.sm, SETTINGS, START + timedelta(hours=1))
    async with h.sm() as s:
        assert await s.get(Topic, tid) is None
        assert await s.get(TopicExpiry, tid) is None
    await check_once(h.bot, h.sm, SETTINGS, START + timedelta(hours=2))


async def test_existing_topic_does_not_restart_timer(h, monkeypatch):
    tid = await new_topic(h, monkeypatch)
    monkeypatch.setattr(clock, 'now', lambda: START + timedelta(minutes=50))
    async with h.sm() as s:
        assert (await svc.ensure_topic(s, 'Calculus', 'NEW TOPIC')).id == tid
        assert (await s.get(TopicExpiry, tid)).expires_at == START + timedelta(hours=1)
        await s.commit()


@pytest.mark.parametrize('archived', [False, True])
async def test_any_material_protects_topic(h, monkeypatch, archived):
    tid = await new_topic(h, monkeypatch)
    async with h.sm() as s:
        material = await svc.add_material(s, ADMIN, topic_id=tid, kind='material', visibility='staff',
                                          file_id='f', file_unique_id='u', file_name='exam.pdf')
        material.archived = archived
        await s.commit()
    async with h.sm() as s:
        assert await svc.cleanup_expired_topics(s, START + timedelta(hours=2)) == 0
        await s.commit()
    async with h.sm() as s:
        assert await s.get(Topic, tid) is not None
        assert (await s.scalars(select(LearningMaterial))).one().topic_id == tid
        assert await s.get(TopicExpiry, tid) is None


async def test_checklist_protects_topic(h, monkeypatch):
    tid = await new_topic(h, monkeypatch)
    async with h.sm() as s:
        await svc.put_checklist(s, ADMIN, tid, ['Review derivatives'])
        await s.commit()
    async with h.sm() as s:
        assert await svc.cleanup_expired_topics(s, START + timedelta(hours=2)) == 0
        assert await s.get(Topic, tid) is not None


@pytest.mark.parametrize('status', ['scheduled', 'cancelled', 'completed'])
async def test_session_history_protects_topic(h, monkeypatch, status):
    tid = await new_topic(h, monkeypatch)
    await h.say(COORD, f'/addmentor {MENTOR}')
    async with h.sm() as s:
        lesson = await svc.create_session(s, ADMIN, SETTINGS, topic_id=tid, mentor_id=MENTOR,
                                          starts_at=START + timedelta(days=2), duration=60,
                                          location='Room', creation_key='test', now=START)
        lesson.status = status
        await s.commit()
    async with h.sm() as s:
        assert await svc.cleanup_expired_topics(s, START + timedelta(hours=2)) == 0
        assert await s.get(Topic, tid) is not None


async def test_upload_to_deleted_topic_has_readable_error(h, monkeypatch):
    tid = await new_topic(h, monkeypatch)
    async with h.sm() as s:
        await svc.cleanup_expired_topics(s, START + timedelta(hours=1))
        await s.commit()
    async with h.sm() as s:
        with pytest.raises(svc.RuleError, match='Тема не найдена'):
            await svc.add_material(s, ADMIN, topic_id=tid, kind='material', visibility='staff',
                                   file_id='f', file_unique_id='u', file_name='exam.pdf')


async def test_legacy_topic_is_not_scheduled_for_deletion(h, monkeypatch):
    tid = await new_topic(h, monkeypatch)
    async with h.sm() as s:
        await s.delete(await s.get(TopicExpiry, tid))
        await s.commit()
    async with h.sm() as s:
        await svc.ensure_topic(s, 'Calculus', 'New topic')
        assert await svc.cleanup_expired_topics(s, START + timedelta(days=30)) == 0
        assert await s.get(Topic, tid) is not None


def test_migration_and_startup_create_same_table_idempotently():
    engine = create_engine('sqlite://')
    migration = importlib.import_module('migrations.versions.0012_topic_expiry')
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=[t for t in Base.metadata.sorted_tables if t.name != 'topic_expiries'])
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
            Base.metadata.create_all(conn)
            assert {c['name'] for c in inspect(conn).get_columns('topic_expiries')} == {'topic_id', 'expires_at'}
    finally:
        engine.dispose()
