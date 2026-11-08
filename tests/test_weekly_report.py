"""Слайс 3: еженедельный отчёт по п. 9.3 — цифры от бота, смысл от ментора."""
from datetime import timedelta

from sqlalchemy import select

from bot.db import Mentor, MentorWeeklyReport, MentoringSession, utcnow
from bot.mentoring import service as svc
from tests.conftest import ALICE, COORD, MENTOR, SETTINGS

INSIGHTS = 'Студенты путают подстановку и замену переменной, на третьем шаге бросают.'
FOCUS = 'Разобрать два примера с подстановкой и дать чек-лист шагов.'


async def _setup_mentor(h):
    await h.say(COORD, f'/addmentor {MENTOR}')


async def _open_report(h):
    await h.say(MENTOR, '/mentorreport')
    await h.tap(MENTOR, 'ms:mentorreport:0')


async def test_report_splits_into_insights_and_focus(h):
    await _setup_mentor(h)
    await _open_report(h)
    assert 'Шаг 1 из 2' in h.texts_to(MENTOR)[-1]
    await h.say(MENTOR, INSIGHTS)
    assert 'Шаг 2 из 2' in h.texts_to(MENTOR)[-1]
    await h.say(MENTOR, FOCUS)
    assert 'Отчёт ментора сохранён' in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        report = (await s.scalars(select(MentorWeeklyReport))).one()
    assert report.insights == INSIGHTS
    assert report.focus == FOCUS
    assert INSIGHTS in report.content and FOCUS in report.content


async def test_report_shows_numbers_computed_by_bot(h):
    await _setup_mentor(h)
    # Занятие ментора на этой неделе, чтобы в сводке были цифры.
    admin = __import__("bot.repo", fromlist=["Viewer"]).Viewer(
        id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                 starts_at=utcnow() + timedelta(days=1), duration=60,
                                 location='Room 1', creation_key='wr1', now=utcnow())
        await s.commit()
    await _open_report(h)
    text = h.texts_to(MENTOR)[-1]
    assert 'Проведено занятий' in text and 'Активных записей' in text
    assert 'переписывать их не нужно' in text


async def test_short_insights_are_rejected(h):
    await _setup_mentor(h)
    await _open_report(h)
    await h.say(MENTOR, 'не знаю')
    assert 'минимум 20' in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(MentorWeeklyReport)))


async def test_short_focus_is_rejected(h):
    await _setup_mentor(h)
    await _open_report(h)
    await h.say(MENTOR, INSIGHTS)
    await h.say(MENTOR, 'ок')
    assert 'минимум 10' in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(MentorWeeklyReport)))


async def test_second_submission_overwrites(h):
    await _setup_mentor(h)
    await _open_report(h)
    await h.say(MENTOR, INSIGHTS)
    await h.say(MENTOR, FOCUS)
    await _open_report(h)
    await h.say(MENTOR, INSIGHTS + ' Уточнение: ещё пределы.')
    await h.say(MENTOR, 'Повторить пределы и подстановку.')
    async with h.sm() as s:
        rows = list(await s.scalars(select(MentorWeeklyReport)))
    assert len(rows) == 1 and 'Уточнение' in rows[0].insights


async def test_student_cannot_submit_report(h):
    await h.say(ALICE, '/mentorreport')
    assert 'списка менторов' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(MentorWeeklyReport)))
        assert await s.get(Mentor, MENTOR) is None
