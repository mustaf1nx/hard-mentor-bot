"""Слайс 2: форма ОС после занятия (Прил. 11) и KPI ментора (раздел 11)."""
from datetime import timedelta

from sqlalchemy import select

from bot import repo
from bot.db import Mentor, MentoringSession, SessionBooking, SessionFeedback, Term, utcnow
from bot.mentoring import service as svc
from tests.conftest import ensure_users, ALICE, BOB, COORD, MENTOR, SETTINGS

GOOD_QUESTION = 'Не понимаю, как выбрать подстановку в интеграле'
GOOD_TRIED = 'Перечитал лекцию, разобрал два примера из семинара, но на третьем шаге тупик.'


async def _lesson(h, hours_ago: int) -> MentoringSession:
    """Занятие в прошлом: через чат создать нельзя (валидатор запрещает), поэтому через сервис."""
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    moment = utcnow() - timedelta(hours=hours_ago)
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
            await s.flush()
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                          starts_at=moment, duration=60, location='Room 1',
                                          creation_key=f'fb{hours_ago}{moment.timestamp()}',
                                          now=moment - timedelta(hours=3))
        await s.commit()
        return lesson


async def _book(h, lesson, uid=ALICE) -> None:
    student = repo.Viewer(id=uid, role='mentee', lang='ru', name='Student')
    async with h.sm() as s:
        await ensure_users(s, student.id)
        await svc.register(s, student, lesson.id, GOOD_QUESTION,
                           now=lesson.starts_at - timedelta(hours=1), tried=GOOD_TRIED, self_rating=3)
        await s.commit()


async def _walk_feedback(h, uid=ALICE, clarity='4', overall='5', best='Разобрали мою задачу',
                         next_topics='Пределы'):
    await h.tap(uid, f'ms:fbc:{clarity}')
    await h.tap(uid, 'ms:fbp:partial')
    await h.tap(uid, 'ms:fbh:ok')
    await h.say(uid, best)
    await h.say(uid, next_topics)
    await h.tap(uid, f'ms:fbo:{overall}')


# ------------------------------------------------------------------ форма ОС

async def test_feedback_flow_saves_answers_and_axes(h):
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    notice = h.texts_to(ALICE)[-1]
    assert 'имя не показывается ментору' in notice
    assert 'комментарий может раскрыть' in notice
    assert 'связан с вашим аккаунтом в базе' in notice
    await _walk_feedback(h)
    async with h.sm() as s:
        row = (await s.scalars(select(SessionFeedback))).one()
    assert row.clarity == 4 and row.overall == 5
    assert row.progressed == 'partial' and row.honesty == 'ok'
    assert row.best == 'Разобрали мою задачу' and row.next_topics == 'Пределы'
    assert row.availability == 4 and row.competence == 5 and row.ethics == 5
    assert row.score == round((4 + 5 + 5) / 3, 2)


async def test_feedback_is_not_available_before_session_ends(h):
    lesson = await _lesson(h, hours_ago=-2)  # закончится через час
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    assert any('после окончания' in text for text in h.texts_to(ALICE)) or \
           any('после окончания' in a for a in h.alerts())
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionFeedback)))


async def test_only_participant_can_leave_feedback(h):
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson, ALICE)
    await h.tap(BOB, f'ms:fb:{lesson.id}')
    assert any('участник' in text for text in h.texts_to(BOB)) or any('участник' in a for a in h.alerts())
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionFeedback)))


async def test_second_feedback_overwrites_instead_of_duplicating(h):
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    await _walk_feedback(h, clarity='2', overall='3')
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    await _walk_feedback(h, clarity='5', overall='5')
    async with h.sm() as s:
        rows = list(await s.scalars(select(SessionFeedback)))
    assert len(rows) == 1 and rows[0].clarity == 5


async def test_feedback_requires_written_text(h):
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    await h.tap(ALICE, 'ms:fbc:3')
    await h.tap(ALICE, 'ms:fbp:no')
    await h.tap(ALICE, 'ms:fbh:violation')
    await h.say(ALICE, '   ')
    assert 'Комментарий обязателен' in h.texts_to(ALICE)[-1]
    await h.say(ALICE, 'Коротко')
    assert 'Слишком коротко' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionFeedback)))
    await h.say(ALICE, 'Разобрали мою задачу и стало понятнее')
    await h.tap(ALICE, 'ms:fbn:skip')
    await h.tap(ALICE, 'ms:fbo:1')
    async with h.sm() as s:
        row = (await s.scalars(select(SessionFeedback))).one()
    assert row.best == 'Разобрали мою задачу и стало понятнее'
    assert row.honesty == 'violation' and row.ethics == 1


async def test_feedback_skip_text_callback_is_rejected(h):
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    await h.tap(ALICE, 'ms:fbc:4')
    await h.tap(ALICE, 'ms:fbp:partial')
    await h.tap(ALICE, 'ms:fbh:ok')
    await h.tap(ALICE, 'ms:fbs:skip')
    assert any('Без текста' in alert for alert in h.alerts())
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionFeedback)))


async def test_feedback_prompt_is_sent_after_session(h):
    from bot.mentoring.reminders import check_once
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    h.clear()
    await check_once(h.bot, h.sm, SETTINGS)
    assert any('Как прошла менторка' in text for text in h.texts_to(ALICE))


# ----------------------------------------------------------------------- KPI

async def _term(h, week: int, name: str) -> Term:
    from zoneinfo import ZoneInfo
    from datetime import timezone
    local = utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(SETTINGS.timezone))
    monday = local.date() - timedelta(days=local.weekday())
    start = monday - timedelta(weeks=week - 1)
    async with h.sm() as s:
        term = Term(name=name, starts_on=start.isoformat(), weeks=11, active=True)
        s.add(term)
        await s.commit()
        await s.refresh(term)
    return term


async def test_kpi_counts_sessions_students_and_feedback(h):
    await _term(h, week=4, name='kpi-term')
    lesson = await _lesson(h, hours_ago=2)
    await _book(h, lesson)
    await h.tap(ALICE, f'ms:fb:{lesson.id}')
    await _walk_feedback(h, clarity='4', overall='4')
    async with h.sm() as s:
        await s.execute(
            MentoringSession.__table__.update()
            .where(MentoringSession.id == lesson.id).values(status='completed'))
        await s.commit()
        kpi = await svc.mentor_kpi(s, MENTOR, SETTINGS)
        await s.commit()
    assert kpi['completed'] == 1
    assert kpi['bookings'] == 1 and kpi['unique_students'] == 1
    assert kpi['feedback']['count'] == 1 and kpi['feedback']['score'] == 4.33
    assert kpi['term'] == 'kpi-term'
    assert svc.kpi_grade(kpi) == 'Ментор'


async def test_kpi_grade_uses_11_4_thresholds(h):
    base = {'completed': 5, 'materials': 1, 'feedback': {'score': 4.6}}
    assert 'Опорный' in svc.kpi_grade(base)
    base['feedback']['score'] = 4.0
    assert svc.kpi_grade(base) == 'Практикующий ментор'
    base['completed'] = 4
    assert svc.kpi_grade(base) == 'Ментор'


async def test_mentor_sees_own_kpi_and_admin_sees_list(h):
    await h.say(COORD, f'/addmentor {MENTOR}')
    await h.say(MENTOR, '/kpi')
    assert 'Мой KPI' in h.texts_to(MENTOR)[-1]
    await h.say(COORD, '/kpi')
    assert 'KPI' in h.texts_to(COORD)[-1]


async def test_student_cannot_open_kpi(h):
    await h.say(ALICE, '/kpi')
    assert 'только команде' in h.texts_to(ALICE)[-1]
