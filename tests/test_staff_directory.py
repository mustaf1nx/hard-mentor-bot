"""Единый реестр стаффа: люди, профили, назначения и история."""
from datetime import timedelta

from sqlalchemy import select

from bot import repo
from bot.db import (FinalReport, Mentor, MentorRole, MentorWeeklyReport, MentoringSession,
                    ResponsibilityAssignment, StaffHistory, StaffMember, Subject, Term, utcnow)
from bot.mentoring import service as svc
from tests.conftest import ALICE, COORD, LEAD, MENTOR, SETTINGS, ensure_users

GOOD_QUESTION = 'Не понимаю, как выбрать подстановку в интеграле'


def _buttons(h):
    return h.buttons(h.last_to(COORD))


def _by_prefix(h, prefix: str) -> str:
    return next(b for b in _buttons(h) if b.startswith(prefix))


async def _seed(h):
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        for uid in (MENTOR, LEAD):
            if await s.get(Mentor, uid) is None:
                s.add(Mentor(telegram_id=uid))
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        term = Term(name='staff-term', starts_on=(utcnow().date() - timedelta(weeks=2)).isoformat(),
                    weeks=12, active=True)
        s.add(term)
        await s.flush()
        s.add(MentorRole(telegram_id=MENTOR, subject_id=topic.subject_id,
                         term_id=term.id, role='lead'))
        admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
        lesson = await svc.create_session(
            s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
            starts_at=utcnow() + timedelta(days=3), duration=60, location='Room 1',
            creation_key='staff-session', format='one_on_one')
        s.add(MentorWeeklyReport(mentor_id=MENTOR, week_start='2026-11-02',
                                 content='Отчёт', insights='Инсайт', focus='Фокус'))
        s.add(FinalReport(mentor_id=MENTOR, term_id=term.id, direction='hard',
                          summary='Итог', difficulties='Трудно', proposals='Предложение'))
        await s.commit()
        return topic, term, lesson


async def test_staff_list_shows_mentor_and_admins_without_fake_data(h):
    await _seed(h)
    await h.say(COORD, '/staff')
    text = h.texts_to(COORD)[-1]
    assert 'Люди и назначения' in text
    assert 'User300' in text and 'ментор' in text
    assert 'User100' in text and 'администратор школы' in text
    assert 'не заполнено' in text
    assert any(b.startswith('dirl:view:300:') for b in _buttons(h))
    await h.say(LEAD, '/staff')
    assert 'Люди и назначения' in h.texts_to(LEAD)[-1]


async def test_student_cannot_open_or_change_directory(h):
    await _seed(h)
    await h.say(ALICE, '/staff')
    assert 'координатору' in h.texts_to(ALICE)[-1]
    h.clear()
    await h.tap(ALICE, 'dirl:list:0')
    assert any('администрации' in alert for alert in h.alerts())
    async with h.sm() as s:
        assert not list(await s.scalars(select(StaffMember)))


async def test_card_shows_roles_sessions_and_reports(h):
    await _seed(h)
    await h.say(COORD, '/staff')
    await h.tap(COORD, _by_prefix(h, 'dirl:view:300:'))
    text = h.texts_to(COORD)[-1]
    assert 'User300' in text
    assert 'Ведущий — Calculus' in text and 'staff-term' in text, text
    assert 'Занятия: Ведущий 1' in text
    assert 'недельных 1' in text and 'итоговых 1' in text
    assert 'не назначено' in text


async def test_unlinked_history_is_visible_but_not_assignable(h):
    async with h.sm() as s:
        s.add(StaffHistory(display_name='Вышедший человек', username='left_person',
                           team='hard', title='Hard-ментор', status='left',
                           source='staff_registry_7.11.json'))
        await s.commit()
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:unlinked:0')
    assert 'История без аккаунта' in h.texts_to(COORD)[-1]
    await h.tap(COORD, _by_prefix(h, 'dirl:unlinkedview:'))
    assert 'назначить этого человека нельзя' in h.texts_to(COORD)[-1]


async def test_profile_status_preview_and_save(h):
    await _seed(h)
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:edit:300:0')
    await h.tap(COORD, 'dirl:field:300:status:0')
    await h.tap(COORD, 'dirl:preview:300:status:active:0')
    preview = h.texts_to(COORD)[-1]
    assert 'Было: не заполнено' in preview and 'Станет: Активен' in preview
    await h.tap(COORD, 'dirl:apply:300:status:active:0')
    card = h.texts_to(COORD)[-1]
    assert 'Изменение сохранено' in card and 'Статус: Активен' in card
    async with h.sm() as s:
        assert (await s.get(StaffMember, MENTOR)).status == 'active'


async def test_profile_team_and_title(h):
    await _seed(h)
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:edit:300:0')
    await h.tap(COORD, 'dirl:field:300:team:0')
    await h.tap(COORD, 'dirl:preview:300:team:hard:0')
    assert 'Станет: Группа Hard-менторства' in h.texts_to(COORD)[-1]
    await h.tap(COORD, 'dirl:apply:300:team:hard:0')
    await h.tap(COORD, 'dirl:title:300:0')
    await h.say(COORD, 'Старший ментор')
    assert 'Станет: Старший ментор' in h.texts_to(COORD)[-1]
    await h.tap(COORD, _by_prefix(h, 'dirl:applytitle:'))
    async with h.sm() as s:
        member = await s.get(StaffMember, MENTOR)
        assert member.team == 'hard' and member.title == 'Старший ментор'


async def test_assignment_change_keeps_history_and_end_frees_zone(h):
    await _seed(h)
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:assign:zone:schedule')
    await h.tap(COORD, 'dirl:assign:pick:schedule:0:300')
    assert 'Сейчас: не назначен' in h.texts_to(COORD)[-1]
    await h.tap(COORD, 'dirl:assign:apply:schedule:0:300')
    async with h.sm() as s:
        active = list(await s.scalars(select(ResponsibilityAssignment).where(
            ResponsibilityAssignment.ended_at.is_(None))))
        assert len(active) == 1 and active[0].person_id == MENTOR

    await h.tap(COORD, 'dirl:assign:zone:schedule')
    await h.tap(COORD, 'dirl:assign:pick:schedule:0:200')
    assert 'Станет: User200' in h.texts_to(COORD)[-1]
    await h.tap(COORD, 'dirl:assign:apply:schedule:0:200')
    async with h.sm() as s:
        rows = list(await s.scalars(select(ResponsibilityAssignment).order_by(
            ResponsibilityAssignment.id)))
        assert len(rows) == 2
        assert rows[0].person_id == MENTOR and rows[0].ended_at is not None
        assert rows[1].person_id == LEAD and rows[1].ended_at is None

    await h.tap(COORD, 'dirl:history:300:0')
    history = h.texts_to(COORD)[-1]
    assert 'Расписание' in history and '→' in history

    await h.tap(COORD, 'dirl:end:0')
    await h.tap(COORD, _by_prefix(h, 'dirl:end:ask:'))
    assert 'Завершить назначение?' in h.texts_to(COORD)[-1]
    await h.tap(COORD, _by_prefix(h, 'dirl:end:apply:'))
    async with h.sm() as s:
        assert not list(await s.scalars(select(ResponsibilityAssignment).where(
            ResponsibilityAssignment.ended_at.is_(None))))
        assert await s.scalar(select(MentoringSession).where(
            MentoringSession.creation_key == 'staff-session')) is not None
        assert await s.scalar(select(MentorWeeklyReport).where(
            MentorWeeklyReport.mentor_id == MENTOR)) is not None


async def test_subject_assignment_uses_subject(h):
    topic, term, _lesson = await _seed(h)
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:assign:zone:subject')
    await h.tap(COORD, f'dirl:assign:subject:{topic.subject_id}:0')
    await h.tap(COORD, f'dirl:assign:pick:subject:{topic.subject_id}:300')
    await h.tap(COORD, f'dirl:assign:apply:subject:{topic.subject_id}:300')
    async with h.sm() as s:
        row = await s.scalar(select(ResponsibilityAssignment).where(
            ResponsibilityAssignment.zone == 'subject'))
        assert row is not None and row.subject_id == topic.subject_id and row.person_id == MENTOR


async def test_add_person_unknown_then_known(h):
    await _seed(h)
    await h.say(ALICE, '/start')
    await h.say(COORD, '/staff')
    await h.tap(COORD, 'dirl:add')
    await h.say(COORD, '999999')
    assert 'не найден' in h.texts_to(COORD)[-1]
    await h.say(COORD, '@user1001')
    assert 'Добавлен в реестр' in h.texts_to(COORD)[-1]
    async with h.sm() as s:
        assert (await s.get(StaffMember, ALICE)).status == 'active'


async def test_long_people_list_is_paginated(h):
    await _seed(h)
    async with h.sm() as s:
        await ensure_users(s, *range(2000, 2020))
        for uid in range(2000, 2020):
            s.add(StaffMember(telegram_id=uid, status='active'))
        await s.commit()
    await h.say(COORD, '/staff')
    assert 'Страница 1/' in h.texts_to(COORD)[-1]
    await h.tap(COORD, _by_prefix(h, 'dirl:list:1'))
    assert 'Страница 2/' in h.texts_to(COORD)[-1]
