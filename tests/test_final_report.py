"""Слайс 5: итоговый отчёт ментора (Прил. 4)."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from bot import repo
from bot.db import FinalReport, Mentor, MentoringSession, Term, User, utcnow
from bot.mentoring import service as svc
from tests.conftest import ALICE, COORD, MENTOR, SETTINGS

SUMMARY = ('За триместр группа заметно выросла: студенты перестали бояться интегралов, '
           'на разборах сами предлагали способ решения. Разобрали все типовые задачи мидтерма.')
DIFFICULTIES = 'Мало аудиторий после обеда, часть студентов не успевала к началу.'
PROPOSALS = 'Дать слоты на утро и заранее публиковать темы на две недели вперёд.'


def _local_today():
    """Бот считает недели в часовом поясе команды, а не в UTC."""
    return utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(SETTINGS.timezone)).date()


async def _term(h, *, weeks: int, weeks_ago: int, name: str) -> Term:
    monday = _local_today() - timedelta(days=_local_today().weekday())
    term = Term(name=name, starts_on=(monday - timedelta(weeks=weeks_ago)).isoformat(),
                weeks=weeks, active=True)
    async with h.sm() as s:
        s.add(term)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
        await s.commit()
        await s.refresh(term)
    return term


async def _submit(h, summary=SUMMARY, difficulties=DIFFICULTIES, proposals=PROPOSALS):
    await h.say(MENTOR, '/finalreport')
    await h.say(MENTOR, summary)
    await h.say(MENTOR, difficulties)
    await h.say(MENTOR, proposals)


async def test_mentor_submits_three_fields_with_auto_numbers(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-a')
    # одно проведённое занятие, чтобы в отчёте были живые цифры
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                          starts_at=utcnow() - timedelta(days=1), duration=60,
                                          location='Room 1', creation_key='fr1',
                                          now=utcnow() - timedelta(days=2))
        lesson.status = 'completed'
        await s.commit()
    await _submit(h)
    assert 'Итоговый отчёт сохранён' in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        report = (await s.scalars(select(FinalReport))).one()
    assert report.summary == SUMMARY
    assert report.slots == 1
    assert report.direction == 'hard'


async def test_short_summary_is_rejected(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-b')
    await h.say(MENTOR, '/finalreport')
    await h.say(MENTOR, 'нормально')
    assert 'минимум 40' in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(FinalReport)))


async def test_short_difficulties_are_rejected(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-c')
    await h.say(MENTOR, '/finalreport')
    await h.say(MENTOR, SUMMARY)
    await h.say(MENTOR, 'нет')
    assert 'минимум 10' in h.texts_to(MENTOR)[-1]


async def test_second_submission_overwrites(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-d')
    await _submit(h)
    await _submit(h, summary=SUMMARY + ' Уточнение: добавили разбор моков.')
    async with h.sm() as s:
        rows = list(await s.scalars(select(FinalReport)))
    assert len(rows) == 1 and 'Уточнение' in rows[0].summary


async def test_late_submission_is_marked(h):
    # Триместр ещё идёт, но дедлайн «за 3 дня до конца» уже прошёл.
    today = _local_today()
    term = Term(name='final-late', starts_on=(today - timedelta(days=20)).isoformat(),
                weeks=3, active=True)
    async with h.sm() as s:
        s.add(term)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
        await s.commit()
        await s.refresh(term)
    deadline = svc.final_report_deadline(term, SETTINGS)
    assert deadline < utcnow()
    await _submit(h)
    assert 'с опозданием' in h.texts_to(MENTOR)[-1]


async def test_student_cannot_submit(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-e')
    await h.say(ALICE, '/finalreport')
    assert 'команде программы' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(FinalReport)))


async def test_admin_sees_who_submitted_and_who_did_not(h):
    await _term(h, weeks=12, weeks_ago=2, name='final-f')
    async with h.sm() as s:
        s.add(User(telegram_id=900001, display_name='Демо Ментор', username='demo_mentor'))
        await s.flush()
        s.add(Mentor(telegram_id=900001))
        await s.commit()
    await _submit(h)
    await h.say(COORD, '/finalreports')
    text = h.texts_to(COORD)[-1]
    assert 'Итоговые отчёты' in text
    assert 'demo_mentor' in text
