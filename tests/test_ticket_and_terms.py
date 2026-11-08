"""Слайс 1: входной билет по п. 8.1.4, триместр и автоотмена по п. 8.1.3, роли по п. 5.9."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sqlalchemy import select

from bot import repo
from bot.db import Mentor, MentorRole, MentoringSession, SessionBooking, Subject, Term, utcnow
from bot.mentoring import service as svc
from tests.conftest import ensure_users, ALICE, COORD, MENTOR, SETTINGS

GOOD_QUESTION = 'Не понимаю, как выбрать подстановку в интеграле'
GOOD_TRIED = 'Перечитал лекцию, разобрал два примера из семинара, но на третьем шаге тупик.'


def _local_today():
    return utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(SETTINGS.timezone)).date()


def _monday():
    today = _local_today()
    return today - timedelta(days=today.weekday())


async def _lesson(h, hours_ahead: int) -> MentoringSession:
    """Создаёт занятие через чат координатора (нужен ментор в списке)."""
    await h.say(COORD, f'/addmentor {MENTOR}')
    await h.say(MENTOR, '/setchecklist')
    await h.say(MENTOR, 'Calculus')
    await h.say(MENTOR, 'Integrals')
    await h.say(MENTOR, 'Подстановка\nПо частям')
    await h.say(COORD, '/newsession')
    await h.say(COORD, 'Calculus')
    await h.say(COORD, 'Integrals')
    await h.tap(COORD, 'ms:setformat:one_on_one')
    await h.tap(COORD, f'ms:pickmentor:{MENTOR}')
    await h.say(COORD, svc.local_time(utcnow() + timedelta(hours=hours_ahead), SETTINGS))
    await h.say(COORD, '60')
    await h.say(COORD, 'Room 101')
    confirmation = next(x for x in h.buttons(h.last_to(COORD)) if x.startswith('ms:create:'))
    await h.tap(COORD, confirmation)
    async with h.sm() as s:
        return (await s.scalars(select(MentoringSession))).one()


# --------------------------------------------------------------- входной билет

async def test_question_shorter_than_20_chars_is_rejected(h):
    lesson = await _lesson(h, 4 * 24)
    await h.tap(ALICE, f'ms:join:{lesson.id}')
    await h.say(ALICE, 'не понимаю')
    assert 'минимум 20' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionBooking)))


async def test_offload_request_is_rejected_before_booking(h):
    lesson = await _lesson(h, 4 * 24)
    await h.tap(ALICE, f'ms:join:{lesson.id}')
    await h.say(ALICE, 'Сделайте за меня лабу по интегралам, пожалуйста')
    assert 'не принимается' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionBooking)))


async def test_tried_field_rejects_empty_placeholders(h):
    lesson = await _lesson(h, 4 * 24)
    await h.tap(ALICE, f'ms:join:{lesson.id}')
    await h.say(ALICE, GOOD_QUESTION)
    await h.say(ALICE, 'ничего')
    assert 'не принимаются' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionBooking)))


async def test_full_ticket_is_stored_with_self_rating(h):
    lesson = await _lesson(h, 4 * 24)
    await h.tap(ALICE, f'ms:join:{lesson.id}')
    await h.say(ALICE, GOOD_QUESTION)
    await h.say(ALICE, GOOD_TRIED)
    buttons = h.buttons(h.last_to(ALICE))
    assert [b for b in buttons if b.startswith('ms:rate:')] == [f'ms:rate:{n}' for n in (5, 4, 3, 2, 1)]
    await h.tap(ALICE, 'ms:rate:2')
    async with h.sm() as s:
        booking = (await s.scalars(select(SessionBooking))).one()
    assert booking.tried == GOOD_TRIED
    assert booking.self_rating == 2
    assert booking.question == GOOD_QUESTION
    assert 'Входной билет принят' in h.texts_to(ALICE)[-1] or any(
        'Входной билет принят' in t for t in h.texts_to(ALICE))


async def test_registration_closes_24_hours_before_start(h):
    lesson = await _lesson(h, 23)
    h.clear()
    await h.tap(ALICE, f'ms:join:{lesson.id}')
    assert any('меньше 24 часов' in t for t in h.texts_to(ALICE)) or any(
        'меньше 24 часов' in a for a in h.alerts())
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionBooking)))


# ------------------------------------------------------- триместр и автоотмена

async def _term_for_week(h, week: int, name: str) -> Term:
    local_lesson = (utcnow() + timedelta(hours=23)).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(SETTINGS.timezone))
    monday = local_lesson.date() - timedelta(days=local_lesson.weekday())
    start = monday - timedelta(weeks=week - 1)
    async with h.sm() as s:
        term = Term(name=name, starts_on=start.isoformat(), weeks=11, active=True)
        s.add(term)
        await s.commit()
        await s.refresh(term)
    return term


async def _session_at(h, hours_ahead: int, key: str, format: str = 'intensive') -> MentoringSession:
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    moment = utcnow() + timedelta(hours=hours_ahead)
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
            await s.flush()
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                          starts_at=moment, duration=60, location='Room 1',
                                          creation_key=key, now=utcnow(), format=format)
        await s.commit()
        return lesson


async def _book_one(h, lesson) -> None:
    student = repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice')
    async with h.sm() as s:
        await ensure_users(s, student.id)
        await svc.register(s, student, lesson.id, GOOD_QUESTION,
                           now=utcnow() - timedelta(hours=2), tried=GOOD_TRIED, self_rating=3)
        await s.commit()


async def _cancel_and_count(h) -> list[int]:
    async with h.sm() as s:
        cancelled = await svc.autocancel_low_demand(s, SETTINGS, utcnow())
        await s.commit()
        return [x.id for x in cancelled]


async def test_no_term_means_no_automatic_cancellation(h):
    lesson = await _session_at(h, 23, 'n1')
    await _book_one(h, lesson)
    assert await _cancel_and_count(h) == []
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


async def test_week_8_slot_with_one_booking_is_cancelled(h):
    await _term_for_week(h, 8, 'test-week8')
    lesson = await _session_at(h, 23, 'n2')
    await _book_one(h, lesson)
    assert await _cancel_and_count(h) == [lesson.id]
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
    assert row.status == 'cancelled' and 'п. 8.1.3' in (row.cancel_reason or '')


@pytest.mark.parametrize('week', [5, 8, 9, 10])
async def test_one_on_one_with_one_booking_is_kept(h, week):
    await _term_for_week(h, week, f'one-on-one-week{week}')
    lesson = await _session_at(h, 23, 'individual-booked', format='one_on_one')
    await _book_one(h, lesson)
    assert await _cancel_and_count(h) == []
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


@pytest.mark.parametrize('week, cancelled', [(5, False), (8, True), (9, False), (10, False)])
async def test_empty_one_on_one_respects_peak_weeks(h, week, cancelled):
    await _term_for_week(h, week, f'empty-individual-week{week}')
    lesson = await _session_at(h, 23, 'individual-empty', format='one_on_one')
    assert await _cancel_and_count(h) == ([lesson.id] if cancelled else [])
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == ('cancelled' if cancelled else 'scheduled')


async def test_unconfirmed_format_with_one_booking_is_not_auto_cancelled(h):
    """Старый one_on_one без подтверждения не считается 1-на-1 (решение владельца 7.11)."""
    await _term_for_week(h, 8, 'legacy-format')
    lesson = await _session_at(h, 23, 'legacy-format', format=None)
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
        assert row.format == 'one_on_one' and row.format_confirmed is None
    await _book_one(h, lesson)
    assert await _cancel_and_count(h) == []
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


async def test_unconfirmed_format_with_no_bookings_is_still_cancelled(h):
    """Пустой слот отменяется при любом формате: для этого порог не нужен."""
    await _term_for_week(h, 8, 'legacy-empty')
    lesson = await _session_at(h, 23, 'legacy-empty', format=None)
    assert await _cancel_and_count(h) == [lesson.id]
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
    assert row.status == 'cancelled'
    assert 'нет записей' in (row.cancel_reason or '')


async def test_confirmed_one_on_one_has_one_student_place(h):
    """Консультация 1-на-1 — на одного студента; второй получает понятный отказ."""
    await _term_for_week(h, 8, 'one-on-one-cap')
    lesson = await _session_at(h, 26, 'one-on-one-cap', format='one_on_one')
    await _book_one(h, lesson)
    second = repo.Viewer(id=1002, role='mentee', lang='ru', name='Bob')
    async with h.sm() as s:
        await ensure_users(s, second.id)
        with pytest.raises(svc.RuleError, match='одного студента'):
            await svc.register(s, second, lesson.id, GOOD_QUESTION,
                               now=utcnow(), tried=GOOD_TRIED, self_rating=3)


async def test_peak_week_9_is_exempt_from_cancellation(h):
    await _term_for_week(h, 9, 'test-week9')
    lesson = await _session_at(h, 23, 'n3')
    await _book_one(h, lesson)
    assert await _cancel_and_count(h) == []
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


async def test_two_bookings_keep_the_slot(h):
    await _term_for_week(h, 8, 'test-week8b')
    lesson = await _session_at(h, 23, 'n4')
    await _book_one(h, lesson)
    second = repo.Viewer(id=1002, role='mentee', lang='ru', name='Bob')
    async with h.sm() as s:
        await ensure_users(s, second.id)
        await svc.register(s, second, lesson.id, GOOD_QUESTION,
                           now=utcnow() - timedelta(hours=2), tried=GOOD_TRIED, self_rating=3)
        await s.commit()
    assert await _cancel_and_count(h) == []


async def test_unconfirmed_format_with_one_booking_warns_coordinator(h):
    """Не отменяем вслепую, но и не молчим: координатор получает просьбу подтвердить формат."""
    from bot.mentoring.reminders import check_once

    await _term_for_week(h, 8, 'format-warning')
    lesson = await _session_at(h, 23, 'format-warning', format=None)
    await _book_one(h, lesson)
    await check_once(h.bot, h.sm, SETTINGS)
    assert any('не подтверждён формат' in text for text in h.texts_to(COORD))
    async with h.sm() as s:
        assert (await s.get(MentoringSession, lesson.id)).status == 'scheduled'


async def test_confirmed_one_on_one_does_not_warn_about_format(h):
    from bot.mentoring.reminders import check_once

    await _term_for_week(h, 8, 'format-known')
    lesson = await _session_at(h, 23, 'format-known', format='one_on_one')
    await _book_one(h, lesson)
    await check_once(h.bot, h.sm, SETTINGS)
    assert not any('не подтверждён формат' in text for text in h.texts_to(COORD))


async def test_confirm_format_cannot_rewrite_completed_lesson(h):
    await _term_for_week(h, 8, 'format-completed')
    lesson = await _session_at(h, 23, 'format-completed', format=None)
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
        row.status = 'completed'
        await s.commit()
        with pytest.raises(svc.RuleError, match='не переписывается'):
            await svc.confirm_format(s, admin, lesson.id, 'intensive')


async def test_confirm_format_marks_scheduled_lesson(h):
    await _term_for_week(h, 8, 'format-confirm')
    lesson = await _session_at(h, 23, 'format-confirm', format=None)
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        updated = await svc.confirm_format(s, admin, lesson.id, 'intensive')
        await s.commit()
        assert updated.format == 'intensive' and updated.format_confirmed is True


async def test_clinic_coverage_ignores_unconfirmed_format(h):
    term = await _term_for_week(h, 8, 'clinic-unknown')
    lesson = await _session_at(h, 23, 'clinic-unknown', format=None)
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
        row.format, row.format_confirmed = 'syllabus_clinic', None
        await ensure_users(s, ALICE)
        s.add(SessionBooking(session_id=lesson.id, student_id=ALICE, attendance='present', status='active'))
        await s.flush()
        result = await svc.clinic_coverage(s, SETTINGS, term)
    assert result['students'] == 0 and result['registered'] == 0


async def test_clinic_coverage_counts_attendance_separately(h):
    term = await _term_for_week(h, 8, 'clinic-coverage')
    first = await _session_at(h, 23, 'clinic-first', format='syllabus_clinic')
    second = await _session_at(h, 23, 'clinic-second', format='syllabus_clinic')
    cancelled = await _session_at(h, 23, 'clinic-cancelled', format='syllabus_clinic')
    outside = await _session_at(h, 23, 'clinic-outside', format='syllabus_clinic')
    other = await _session_at(h, 23, 'clinic-other', format='intensive')
    async with h.sm() as s:
        await ensure_users(s, ALICE, 1002, 1003, 1004, 1005, 1006)
        (await s.get(MentoringSession, cancelled.id)).status = 'cancelled'
        outside_row = await s.get(MentoringSession, outside.id)
        outside_row.starts_at += timedelta(weeks=20)
        outside_row.ends_at += timedelta(weeks=20)
        s.add_all([
            SessionBooking(session_id=first.id, student_id=ALICE, attendance='present', status='active'),
            SessionBooking(session_id=second.id, student_id=ALICE, attendance='present', status='active'),
            SessionBooking(session_id=first.id, student_id=1002, attendance='unknown', status='active'),
            SessionBooking(session_id=first.id, student_id=1003, attendance='absent', status='active'),
            SessionBooking(session_id=first.id, student_id=1004, attendance='present', status='withdrawn'),
            SessionBooking(session_id=cancelled.id, student_id=1004, attendance='present', status='active'),
            SessionBooking(session_id=outside.id, student_id=1005, attendance='present', status='active'),
            SessionBooking(session_id=other.id, student_id=1006, attendance='present', status='active'),
        ])
        await s.flush()
        result = await svc.clinic_coverage(s, SETTINGS, term)
        assert result == {'sessions': 2, 'students': 1, 'registered': 3, 'marked': 2,
                          'cohort': term.cohort, 'percent': round(100 / term.cohort, 1)}
        term.cohort = 0
        assert (await svc.clinic_coverage(s, SETTINGS, term))['percent'] is None
        assert await svc.clinic_coverage(s, SETTINGS, None) == {
            'sessions': 0, 'students': 0, 'registered': 0, 'marked': 0, 'cohort': 0, 'percent': None}


# --------------------------------------------------------------- /term и /role

async def test_admin_sets_term_and_sees_week(h):
    monday = (_monday() - timedelta(weeks=3)).isoformat()
    await h.say(COORD, f'/term осень26 {monday} 11')
    assert 'Триместр' in h.texts_to(COORD)[-1]
    await h.say(COORD, '/term')
    assert 'неделя 4' in h.texts_to(COORD)[-1]


async def test_term_requires_monday(h):
    await h.say(COORD, '/term плохой 2026-09-08 11')
    assert 'понедельник' in h.texts_to(COORD)[-1]


async def test_admin_assigns_role_per_subject(h):
    monday = (_monday() - timedelta(weeks=3)).isoformat()
    await h.say(COORD, f'/term осень26 {monday} 11')
    await h.say(COORD, f'/addmentor {MENTOR}')
    async with h.sm() as s:
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        await s.commit()
        subject_id = topic.subject_id
    await h.say(COORD, f'/role @user{MENTOR} ведущий Calculus')
    async with h.sm() as s:
        role = await s.scalar(select(MentorRole).where(MentorRole.telegram_id == MENTOR))
    assert role is not None and role.role == 'lead' and role.subject_id == subject_id
    await h.say(COORD, '/roles')
    assert 'Как смотреть' in h.texts_to(COORD)[-1]
    await h.tap(COORD, 'rl:sub')
    assert any(b == f'rl:subv:{subject_id}' for b in h.buttons(h.last_to(COORD)))
    await h.tap(COORD, f'rl:subv:{subject_id}')
    last = h.texts_to(COORD)[-1]
    assert 'Ведущий' in last and f'@user{MENTOR}' in last


async def test_role_screens_by_person_and_picker(h):
    monday = (_monday() - timedelta(weeks=3)).isoformat()
    await h.say(COORD, f'/term осень26 {monday} 12')
    await h.say(COORD, f'/addmentor {MENTOR}')
    await h.say(COORD, f'/addmentor {COORD}')
    async with h.sm() as s:
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        await s.commit()

    # Назначение только кнопками: /role -> ментор -> предмет -> роль.
    await h.say(COORD, '/role')
    assert any(b == f'rl:men:{MENTOR}' for b in h.buttons(h.last_to(COORD)))
    await h.tap(COORD, f'rl:men:{MENTOR}')
    await h.tap(COORD, f'rl:sid:{MENTOR}:{topic.subject_id}')
    await h.tap(COORD, f'rl:set:{MENTOR}:{topic.subject_id}:respondent')
    assert 'Респондент' in h.texts_to(COORD)[-1]
    async with h.sm() as s:
        role = await s.scalar(select(MentorRole).where(MentorRole.telegram_id == MENTOR))
    assert role is not None and role.role == 'respondent'

    # Просмотр «по людям».
    await h.say(COORD, '/roles')
    await h.tap(COORD, 'rl:ppl:0')
    assert any(b == f'rl:prs:{MENTOR}' for b in h.buttons(h.last_to(COORD)))
    await h.tap(COORD, f'rl:prs:{MENTOR}')
    assert 'Респондент' in h.texts_to(COORD)[-1]


async def test_role_requires_known_subject(h):
    monday = (_monday() - timedelta(weeks=3)).isoformat()
    await h.say(COORD, f'/term осень26 {monday} 11')
    await h.say(COORD, f'/addmentor {MENTOR}')
    await h.say(COORD, f'/role @user{MENTOR} ведущий Несуществующий')
    assert 'не найден' in h.texts_to(COORD)[-1]


async def test_student_cannot_set_term_or_role(h):
    await h.say(ALICE, '/term осень26 2026-09-07 11')
    await h.say(ALICE, '/role @user1 ведущий Calculus')
    async with h.sm() as s:
        assert not list(await s.scalars(select(Term)))
        assert not list(await s.scalars(select(MentorRole)))
