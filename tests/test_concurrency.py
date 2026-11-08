"""Гонки, которые видно только на Postgres.

`with_for_update` в SQLite — пустышка, поэтому на обычных прогонах этот код
не проверялся вообще. Здесь два запроса идут одновременно.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot import repo
from bot.db import Mentor, MentoringSession, SessionBooking, utcnow
from bot.mentoring import service as svc
from tests.conftest import ALICE, BOB, COORD, MENTOR, SETTINGS, ensure_users

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="нужен Postgres: в SQLite блокировки строк отсутствуют")


async def _lesson(h) -> MentoringSession:
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        await ensure_users(s, ALICE, BOB)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                         starts_at=utcnow() + timedelta(days=3), duration=60,
                                         location='Room 1', creation_key='race-1')
        await s.commit()
        return lesson


async def _book(h, lesson, question: str) -> None:
    student = repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice')
    async with h.sm() as s:
        await svc.register(s, student, lesson.id, question)
        await s.commit()


async def test_two_simultaneous_registrations_create_one_booking(h):
    lesson = await _lesson(h)
    await asyncio.gather(
        _book(h, lesson, 'Не понимаю пределы, помогите разобраться'),
        _book(h, lesson, 'Не понимаю интегралы, помогите разобраться'),
    )
    async with h.sm() as s:
        rows = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id == lesson.id, SessionBooking.status == 'active')))
    assert len(rows) == 1, f"записей {len(rows)} — блокировка не сработала"


async def test_two_students_booking_at_once_both_get_a_place(h):
    """Параллельные записи не должны терять одна другую."""
    lesson = await _lesson(h)
    second = repo.Viewer(id=BOB, role='mentee', lang='ru', name='Bob')

    async def book(viewer, question):
        async with h.sm() as s:
            await svc.register(s, viewer, lesson.id, question)
            await s.commit()

    await asyncio.gather(
        book(repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice'),
             'Не понимаю пределы, помогите разобраться'),
        book(second, 'Не понимаю интегралы, помогите разобраться'),
    )
    async with h.sm() as s:
        rows = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id == lesson.id, SessionBooking.status == 'active')))
    assert len(rows) == 2, f"записей {len(rows)} — одна потерялась"
