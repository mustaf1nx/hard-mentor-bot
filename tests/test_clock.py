"""Виртуальные часы: автоотмена проверяется мгновенно, без ожидания.

До этого тест автоотмены либо ждал реальные 75 секунд, либо проверял правило
на границе суток и падал в полночь.
"""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from bot import clock, repo
from bot.db import Mentor, MentoringSession, SessionBooking, Term, utcnow
from bot.mentoring import service as svc
from bot.mentoring.reminders import check_once
from tests.conftest import ALICE, COORD, MENTOR, SETTINGS, ensure_users

START = clock.freeze(utcnow())


def teardown_function(_):
    clock.reset()


async def _lesson(h, hours_ahead: int) -> MentoringSession:
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
        lesson_start = clock.now() + timedelta(hours=hours_ahead)
        local = lesson_start.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(SETTINGS.timezone))
        monday = local.date() - timedelta(days=local.weekday())
        if await s.scalar(select(Term).where(Term.name == 'clock-term')) is None:
            # Триместр строится от недели самого занятия: тест не должен попадать
            # в пиковую неделю 5 на границе суток.
            s.add(Term(name='clock-term', starts_on=(monday - timedelta(weeks=3)).isoformat(),
                       weeks=12, cohort=2500, active=True))
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                         starts_at=lesson_start,
                                         duration=60, location='Room 1', creation_key='clock-1',
                                         format='intensive')
        await s.commit()
        return lesson


async def _book(h, lesson) -> None:
    student = repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice')
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        await svc.register(s, student, lesson.id, 'Не понимаю пределы, помогите разобраться')
        await s.commit()


async def test_slot_with_one_booking_is_cancelled_without_waiting(h):
    lesson = await _lesson(h, hours_ahead=20)
    await _book(h, lesson)
    await check_once(h.bot, h.sm, SETTINGS)  # время заморожено — ждать нечего
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
        bookings = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id == lesson.id, SessionBooking.status == 'active')))
    assert row.status == 'cancelled'
    assert 'п. 8.1.3' in (row.cancel_reason or '')
    assert bookings, 'запись не должна исчезнуть при отмене слота'


async def test_slot_with_two_bookings_survives(h):
    from tests.conftest import BOB
    lesson = await _lesson(h, hours_ahead=20)
    await _book(h, lesson)
    student = repo.Viewer(id=BOB, role='mentee', lang='ru', name='Bob')
    async with h.sm() as s:
        await ensure_users(s, BOB)
        await svc.register(s, student, lesson.id, 'Не понимаю интегралы, помогите разобраться')
        await s.commit()
    await check_once(h.bot, h.sm, SETTINGS)
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


async def test_time_can_move_forward(h):
    lesson = await _lesson(h, hours_ahead=30)
    await _book(h, lesson)
    await check_once(h.bot, h.sm, SETTINGS)
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled', \
            'до начала больше суток — отменять рано'
    clock.travel(timedelta(hours=10))
    await check_once(h.bot, h.sm, SETTINGS)
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'cancelled'
