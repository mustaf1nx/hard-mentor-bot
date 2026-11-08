"""Слайс 4: сводка для администрации."""
from datetime import timedelta

import pytest

from sqlalchemy import select

from bot import dashboard, repo
from bot.db import (LearningMaterial, Mentor, MentoringSession, SessionBooking, SessionFeedback,
                    SessionFormatChange, Term, Topic, utcnow)
from bot.mentoring import service as svc
from tests.conftest import ensure_users, ALICE, COORD, MENTOR, SETTINGS

GOOD_QUESTION = 'Не понимаю, как выбрать подстановку в интеграле'
GOOD_TRIED = 'Перечитал лекцию, разобрал два примера из семинара, но на третьем шаге тупик.'


async def _seed_past_session(h, with_feedback=True, with_material=True, *,
                             key='dash1', days_ago=1, written_feedback=True) -> MentoringSession:
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    moment = utcnow() - timedelta(days=days_ago)
    async with h.sm() as s:
        await ensure_users(s, ALICE)
        if await s.get(Mentor, MENTOR) is None:
            s.add(Mentor(telegram_id=MENTOR))
            await s.flush()
        topic = await svc.ensure_topic(s, 'Calculus', 'Integrals')
        lesson = await svc.create_session(s, admin, SETTINGS, topic_id=topic.id, mentor_id=MENTOR,
                                          starts_at=moment - timedelta(hours=1), duration=60,
                                          location='Room 1', creation_key=key,
                                          now=moment - timedelta(hours=3))
        student = repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice')
        _, booking = await svc.register(s, student, lesson.id, GOOD_QUESTION,
                                     now=moment - timedelta(hours=2), tried=GOOD_TRIED, self_rating=3)
        await svc.set_attendance(s, admin, booking.id, 'present', now=moment)
        await svc.complete_session(s, admin, lesson.id, now=moment)
        if with_feedback:
            await svc.save_feedback(s, student, lesson.id, clarity=4, progressed='partial',
                                    honesty='ok', overall=5, now=moment,
                                    best='Стало понятнее, как выбирать подстановку.' if written_feedback else '',
                                    next_topics='Повторить метод.' if written_feedback else '')
        if with_material:
            s.add(LearningMaterial(topic_id=topic.id, session_id=lesson.id, author_id=MENTOR,
                                   file_id='f1', file_unique_id='u1', file_name='guide.pdf', kind='material',
                                   cycle_stage='final', review_status='approved'))
        await s.commit()
        return lesson


async def _seed_lesson_for_subject(h, subject: str, key: str, mentor_id: int) -> MentoringSession:
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    async with h.sm() as s:
        await ensure_users(s, ALICE, mentor_id)
        if await s.get(Mentor, mentor_id) is None:
            s.add(Mentor(telegram_id=mentor_id))
            await s.flush()
        topic = await svc.ensure_topic(s, subject, f'{subject} topic')
        lesson = await svc.create_session(
            s, admin, SETTINGS, topic_id=topic.id, mentor_id=mentor_id,
            starts_at=utcnow() + timedelta(hours=23), duration=60, location='Room 1',
            creation_key=key, now=utcnow(), format='intensive')
        await s.commit()
        return lesson


async def test_overview_counts_and_cycle_on_empty_base(h):
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    assert data['lessons'] == [] and data['cycle'] == {}
    text = dashboard.overview_text(data)
    assert 'Сводка' in text and 'триместр не задан' in text
    assert 'Главные проблемы' in text and 'Цикл' in text
    assert data['kpi']['reports_total'] == data['kpi']['reports_done'] == 0
    assert 'реестр менторов в боте пуст' in dashboard.kpi_text(data)
    assert '1 из 1' not in dashboard.kpi_text(data)


async def test_cycle_closes_when_material_and_feedback_exist(h):
    await _seed_past_session(h, with_feedback=True, with_material=True)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    row = data['cycle']['Calculus']
    assert row['feedback'] is True and row['bank'] is True
    assert row['closed'] is True
    assert data['cycle_closed'] == 1
    assert '✓' in dashboard.cycle_text(data)


async def test_cycle_shows_gap_without_material(h):
    await _seed_past_session(h, with_feedback=True, with_material=False)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    assert data['cycle']['Calculus']['closed'] is False
    assert data['without_materials']
    assert 'Проведены без материалов' in dashboard.debts_text(data)


async def test_cycle_cannot_combine_material_and_feedback_from_different_lessons(h):
    await _seed_past_session(h, with_material=False, key='feedback-only')
    await _seed_past_session(h, with_feedback=False, key='material-only')
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    row = data['cycle']['Calculus']
    assert row['closed'] is False
    assert row['closed_sessions'] == 0
    assert row['completed_sessions'] == 2
    assert data['cycle_closed'] == 0


async def test_one_closed_cycle_does_not_hide_another_incomplete_lesson(h):
    await _seed_past_session(h, key='closed')
    await _seed_past_session(h, with_material=False, key='open')
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    row = data['cycle']['Calculus']
    assert row['closed'] is False
    assert (row['closed_sessions'], row['completed_sessions']) == (1, 2)
    assert '1/2' in dashboard.cycle_text(data)


@pytest.mark.parametrize('archived', [False, True])
async def test_cycle_requires_active_material_attached_to_this_lesson(h, archived):
    lesson = await _seed_past_session(h, with_material=False)
    async with h.sm() as s:
        s.add(LearningMaterial(topic_id=lesson.topic_id,
                               session_id=lesson.id if archived else None,
                               author_id=MENTOR, file_id='file', file_unique_id='unique',
                               file_name='guide.pdf', kind='material', archived=archived))
        await s.commit()
        data = await dashboard.overview(s, SETTINGS)
    assert data['cycle']['Calculus']['closed'] is False
    assert data['cycle']['Calculus']['closed_sessions'] == 0


async def test_cycle_does_not_use_lessons_from_another_term(h):
    await _seed_past_session(h, key='previous-term', days_ago=100)
    await _seed_past_session(h, key='this-term', with_material=False)
    async with h.sm() as s:
        monday = utcnow().date() - timedelta(days=utcnow().date().weekday(), weeks=1)
        s.add(Term(name='current', starts_on=monday.isoformat(), weeks=12, active=True))
        await s.commit()
        data = await dashboard.overview(s, SETTINGS)
    row = data['cycle']['Calculus']
    assert row['closed'] is False
    assert (row['closed_sessions'], row['completed_sessions']) == (0, 1)


@pytest.mark.parametrize('week', [5, 9, 10])
async def test_peak_week_low_demand_does_not_claim_automatic_cancellation(h, week):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, week, f'peak-{week}')
    lesson = await _session_at(h, 23, f'peak-{week}')
    async with h.sm() as s:
        lesson = await s.get(MentoringSession, lesson.id)
        lesson.format = 'intensive'
        await s.commit()
        data = await dashboard.overview(s, SETTINGS)
    warnings = [text for _, text, _ in data['attention'] if f'#{lesson.id} ·' in text]
    assert len(warnings) == 1
    assert 'пиковая неделя' in warnings[0]
    assert 'отменится автоматически' not in warnings[0]


async def test_regular_week_low_demand_still_warns_of_automatic_cancellation(h):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, 8, 'regular-week')
    lesson = await _session_at(h, 23, 'regular-week')
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    assert any(f'#{lesson.id} ·' in text and 'отменится автоматически' in text
               for _, text, _ in data['attention'])


async def test_one_on_one_with_one_booking_has_no_low_demand_warning(h):
    from tests.test_ticket_and_terms import _book_one, _session_at, _term_for_week

    await _term_for_week(h, 8, 'one-on-one')
    lesson = await _session_at(h, 23, 'one-on-one', format='one_on_one')
    await _book_one(h, lesson)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    assert all(candidate.id != lesson.id for candidate, _, _ in data['low_demand'])


async def test_unconfirmed_format_is_reported_without_false_cancellation(h):
    from tests.test_ticket_and_terms import _book_one, _session_at, _term_for_week

    await _term_for_week(h, 8, 'legacy-format')
    lesson = await _session_at(h, 23, 'legacy-format', format=None)
    await _book_one(h, lesson)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    assert any(candidate.id == lesson.id for candidate, _, _ in data['low_demand'])
    warning = next(text for _, text, _ in data['attention'] if f'#{lesson.id} ·' in text)
    assert 'формат не подтверждён' in warning
    assert 'отменится автоматически' not in warning


async def test_dashboard_shows_week_of_term(h):
    async with h.sm() as s:
        today = utcnow().date()
        monday = today - timedelta(days=today.weekday())
        s.add(Term(name='dash-term', starts_on=(monday - timedelta(weeks=2)).isoformat(),
                   weeks=12, active=True))
        await s.commit()
        data = await dashboard.overview(s, SETTINGS)
    assert data['week'] == 3
    assert 'неделя 3' in dashboard.overview_text(data)


async def test_csv_export_has_header_and_rows(h):
    await _seed_past_session(h)
    async with h.sm() as s:
        content = await dashboard.export_csv(s, SETTINGS)
    text = content.decode('utf-8-sig')
    assert text.splitlines()[0].startswith('id;дата;статус')
    assert 'Calculus' in text and 'проведено' in text


async def test_admin_opens_dashboard_and_blocks(h):
    await _seed_past_session(h)
    await h.say(COORD, '/dashboard')
    assert 'Сводка' in h.texts_to(COORD)[-1]
    assert any(b.startswith('dash:cycle') for b in h.buttons(h.last_to(COORD)))
    await h.tap(COORD, 'dash:cycle')
    assert 'Цикл по предметам' in h.texts_to(COORD)[-1]
    await h.tap(COORD, 'dash:debts')
    assert 'Долги' in h.texts_to(COORD)[-1]


async def test_student_cannot_open_dashboard(h):
    await h.say(ALICE, '/dashboard')
    assert 'координатору' in h.texts_to(ALICE)[-1]
    await h.tap(ALICE, 'dash:cycle')
    assert any('координатору' in alert for alert in h.alerts())


async def test_unconfirmed_formats_list_and_context_back(h):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, 8, 'unconfirmed-list')
    lesson = await _session_at(h, 23, 'unconfirmed-list', format=None)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:unconfirmed')
    text = h.texts_to(COORD)[-1]
    assert 'Неподтверждённые форматы' in text
    assert 'format_confirmed=NULL' in text
    assert '1-на-1' in text
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    card = h.texts_to(COORD)[-1]
    assert f'#{lesson.id}' in card and 'формат не подтверждён' in card
    confirm = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:confirm:'))
    await h.tap(COORD, confirm)
    assert 'Сначала будет предпросмотр' in h.texts_to(COORD)[-1]
    fmt_button = next(b for b in h.buttons(h.last_to(COORD))
                      if b.startswith('dash:previewfmt:'))
    await h.tap(COORD, fmt_button)
    assert 'Было:' in h.texts_to(COORD)[-1]
    save = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:setfmt:'))
    await h.tap(COORD, save)
    assert any('Формат подтверждён.' in text for text in h.texts_to(COORD))
    back = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:unconfirmed:'))
    await h.tap(COORD, back)
    assert 'Занятий с неподтверждённым форматом нет.' in h.texts_to(COORD)[-1]


async def test_filters_empty_result_and_reset(h):
    from tests.test_ticket_and_terms import _term_for_week

    await _term_for_week(h, 8, 'empty-filter')
    await _seed_lesson_for_subject(h, 'Calculus', 'empty-filter-calc', 300)
    async with h.sm() as s:
        empty = await svc.ensure_topic(s, 'Empty subject', 'No lessons')
        await s.commit()
        empty_subject = (await s.get(Topic, empty.id)).subject_id
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:sessions')
    filters = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filters)
    pick = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pick:subject:'))
    await h.tap(COORD, pick)
    choice = next(b for b in h.buttons(h.last_to(COORD))
                  if b.startswith(f'dash:pickv:subject:{empty_subject}:'))
    await h.tap(COORD, choice)
    assert 'Найдено занятий: 0' in h.texts_to(COORD)[-1]
    to_list = next(b for b in h.buttons(h.last_to(COORD))
                   if b == f'dash:sessions:0:a:{empty_subject}:0')
    await h.tap(COORD, to_list)
    assert 'По выбранным фильтрам занятий нет' in h.texts_to(COORD)[-1]
    reset = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:sessions:0:a:0:0'))
    await h.tap(COORD, reset)
    assert 'Менторки' in h.texts_to(COORD)[-1] and 'По выбранным фильтрам' not in h.texts_to(COORD)[-1]


async def test_unconfirmed_page_and_back_keep_page(h):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, 8, 'paged-unconfirmed')
    for i in range(8):
        await _session_at(h, 23, f'paged-{i}', format=None)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:unconfirmed')
    assert 'страница 1/2' in h.texts_to(COORD)[-1]
    await h.tap(COORD, next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:unconfirmed:1:')))
    assert 'страница 2/2' in h.texts_to(COORD)[-1]
    lesson = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson)
    back = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:unconfirmed:1:'))
    await h.tap(COORD, back)
    assert 'страница 2/2' in h.texts_to(COORD)[-1]


async def test_callback_data_fits_telegram_limit(h):
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:sessions')
    assert all(len(data.encode()) <= 64 for data in h.buttons(h.last_to(COORD)))
    filters = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filters)
    assert all(len(data.encode()) <= 64 for data in h.buttons(h.last_to(COORD)))
    pick = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pick:subject:'))
    await h.tap(COORD, pick)
    assert all(len(data.encode()) <= 64 for data in h.buttons(h.last_to(COORD)))


async def test_admin_filters_by_mentor_and_period(h):
    from tests.test_ticket_and_terms import _term_for_week

    await _term_for_week(h, 8, 'mentor-filter')
    await _seed_lesson_for_subject(h, 'Calculus', 'mentor-filter-calc', 300)
    await _seed_lesson_for_subject(h, 'Linear Algebra', 'mentor-filter-la', 400)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:sessions')
    filters = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filters)
    pick = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pick:mentor:'))
    await h.tap(COORD, pick)
    choice = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pickv:mentor:400:'))
    await h.tap(COORD, choice)
    assert 'Ведущий: @user400' in h.texts_to(COORD)[-1]
    to_list = next(b for b in h.buttons(h.last_to(COORD))
                   if b == 'dash:sessions:0:a:0:400')
    await h.tap(COORD, to_list)
    assert 'Linear Algebra' in h.texts_to(COORD)[-1]
    filters = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filters)
    period = next(b for b in h.buttons(h.last_to(COORD))
                  if b.startswith('dash:setf:period:p:'))
    await h.tap(COORD, period)
    assert 'Период: прошедшие' in h.texts_to(COORD)[-1]
    to_list = next(b for b in h.buttons(h.last_to(COORD))
                   if b == 'dash:sessions:0:p:0:400')
    await h.tap(COORD, to_list)
    assert 'По выбранным фильтрам занятий нет' in h.texts_to(COORD)[-1]


async def test_mentor_card_shows_written_feedback_status(h):
    from bot.mentoring import ui

    lesson = await _seed_past_session(h, with_feedback=True, with_material=True,
                                      written_feedback=False)
    async with h.sm() as s:
        row = await s.scalar(select(SessionFeedback).where(SessionFeedback.session_id == lesson.id))
    admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
    text, _ = ui.session_card(lesson, ('Calculus', 'Integrals'), 'Mentor', SETTINGS,
                              1, None, admin, feedback=[row])
    assert 'письменного комментария ещё нет' in text
    assert 'Отзывы: 1' in text


async def test_scores_only_feedback_does_not_close_cycle(h):
    lesson = await _seed_past_session(h, with_feedback=True, with_material=True,
                                      written_feedback=False)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
        text = dashboard.lesson_text(data, next(x for x in data['lessons'] if x.id == lesson.id))
    assert data['feedback'][lesson.id] == 1
    assert data['written_feedback'].get(lesson.id, 0) == 0
    assert data['cycle']['Calculus']['closed'] is False
    assert 'письменного комментария ещё нет' in text
    assert 'Письменный отзыв получен' not in text


async def test_whitespace_feedback_does_not_close_cycle(h):
    lesson = await _seed_past_session(h, with_feedback=True, with_material=True,
                                      written_feedback=False)
    async with h.sm() as s:
        row = await s.scalar(select(SessionFeedback).where(SessionFeedback.session_id == lesson.id))
        row.best = '   '
        row.next_topics = '\n\t'
        await s.commit()
        data = await dashboard.overview(s, SETTINGS)
    assert data['written_feedback'].get(lesson.id, 0) == 0
    assert data['cycle']['Calculus']['closed'] is False


async def test_feedback_from_another_session_does_not_close_cycle(h):
    await _seed_past_session(h, with_feedback=True, with_material=True, key='written-other')
    await _seed_past_session(h, with_feedback=False, with_material=True, key='no-written-other')
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
    row = data['cycle']['Calculus']
    assert (row['closed_sessions'], row['completed_sessions']) == (1, 2)
    assert row['closed'] is False


async def test_written_feedback_closes_cycle_and_is_counted(h):
    await _seed_past_session(h, with_feedback=True, with_material=True)
    async with h.sm() as s:
        data = await dashboard.overview(s, SETTINGS)
        summary = await svc.feedback_summary(s)
    assert data['cycle']['Calculus']['closed'] is True
    assert summary['written'] == 1


async def test_confirm_past_format_updates_clinic_and_keeps_history(h):
    from tests.test_ticket_and_terms import _term_for_week

    await _term_for_week(h, 8, 'past-format')
    lesson = await _seed_past_session(h, with_feedback=True, with_material=True, key='past-format')
    async with h.sm() as s:
        term = await svc.active_term(s, SETTINGS)
        term.cohort = 10
        await s.commit()
        before = await dashboard.overview(s, SETTINGS)
        assert before['kpi']['clinic']['sessions'] == 0
        admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
        updated = await svc.confirm_past_format(s, admin, lesson.id, 'syllabus_clinic')
        await s.commit()
        assert updated.format_confirmed is True and updated.format == 'syllabus_clinic'
        after = await dashboard.overview(s, SETTINGS)
        assert after['kpi']['clinic']['sessions'] == 1
        assert after['kpi']['clinic']['students'] == 1
        assert after['kpi']['clinic']['percent'] == 10.0
        change = await s.scalar(select(SessionFormatChange).where(
            SessionFormatChange.session_id == lesson.id))
        assert change.changed_by == COORD and change.old_format == 'one_on_one'
        assert change.old_confirmed is None


async def test_confirm_past_format_rejects_student_cancelled_and_repeat(h):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, 8, 'past-format-guards')
    lesson = await _seed_past_session(h, key='past-format-guards')
    student = repo.Viewer(id=ALICE, role='mentee', lang='ru', name='Alice')
    async with h.sm() as s:
        with pytest.raises(svc.RuleError):
            await svc.confirm_past_format(s, student, lesson.id, 'intensive')
        await s.rollback()
        admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
        await svc.confirm_past_format(s, admin, lesson.id, 'intensive')
        await s.commit()
        with pytest.raises(svc.RuleError):
            await svc.confirm_past_format(s, admin, lesson.id, 'weekly_errors')
        await s.rollback()

    cancelled = await _session_at(h, 23, 'cancelled-format', format=None)
    async with h.sm() as s:
        admin = repo.Viewer(id=COORD, role='coordinator', lang='ru', name='Coord')
        await svc.cancel_session(s, admin, cancelled.id, 'тест')
        await s.commit()
        with pytest.raises(svc.RuleError):
            await svc.confirm_past_format(s, admin, cancelled.id, 'intensive')
        await s.rollback()


async def test_completed_unconfirmed_format_can_be_confirmed_with_preview(h):
    lesson = await _seed_past_session(h, with_feedback=True, with_material=True)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:unconfirmed')
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    confirm = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:confirm:'))
    assert len(confirm.encode()) <= 64
    await h.tap(COORD, confirm)
    fmt = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:previewfmt:'))
    assert len(fmt.encode()) <= 64
    await h.tap(COORD, fmt)
    preview = h.texts_to(COORD)[-1]
    assert 'Было:' in preview and 'Станет:' in preview
    save = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:setfmt:'))
    await h.tap(COORD, save)
    assert any('Формат подтверждён.' in text for text in h.texts_to(COORD))
    card = h.texts_to(COORD)[-1]
    assert 'Формат подтвердил:' in card
    async with h.sm() as s:
        row = await s.get(MentoringSession, lesson.id)
        assert row.format_confirmed is True
        change = (await s.execute(select(SessionFormatChange).where(
            SessionFormatChange.session_id == lesson.id))).scalar_one()
        assert change.changed_by == COORD and change.old_confirmed is None


async def test_back_from_week_subject_and_debts(h):
    await _seed_past_session(h, with_feedback=False, with_material=False)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:week')
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    await h.tap(COORD, next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:week:')))
    assert 'Занятия недели' in h.texts_to(COORD)[-1]

    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:cycle')
    await h.tap(COORD, next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:subject:')))
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    await h.tap(COORD, next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:subject:')))
    assert 'Calculus' in h.texts_to(COORD)[-1]

    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:debts')
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    await h.tap(COORD, next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:debts:')))
    assert 'Долги' in h.texts_to(COORD)[-1]


async def test_subject_filter_keeps_subject_context(h):
    await _seed_past_session(h)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:cycle')
    subject_link = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:subject:'))
    await h.tap(COORD, subject_link)
    filters = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filters)
    period = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:setf:period:m:'))
    await h.tap(COORD, period)
    to_list = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:subject:'))
    await h.tap(COORD, to_list)
    assert 'Calculus' in h.texts_to(COORD)[-1]
    assert 'Предмет ·' not in h.texts_to(COORD)[-1]


async def test_missing_source_shows_safe_path(h):
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:subject:99999:0')
    assert 'Предмет больше недоступен' in h.texts_to(COORD)[-1]
    assert 'dash:sessions' in h.buttons(h.last_to(COORD))
    await h.tap(COORD, 'dash:lesson:99999:s:0:a:0:0')
    assert 'Занятие не найдено' in h.texts_to(COORD)[-1]


async def test_attention_lesson_back_preserves_section_and_page(h):
    from tests.test_ticket_and_terms import _session_at, _term_for_week

    await _term_for_week(h, 8, 'attention-back')
    await _session_at(h, 23, 'attention-back', format=None)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:attention')
    lesson_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:lesson:'))
    await h.tap(COORD, lesson_button)
    back = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:attention:'))
    await h.tap(COORD, back)
    assert 'Требует внимания' in h.texts_to(COORD)[-1]


async def test_admin_manages_lesson_from_dashboard_card_without_booking(h):
    """Администратор открывает управление занятием из сводки, не будучи записан студентом."""
    lesson = await _seed_past_session(h, key='dash-manage')
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:sessions')
    card = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith(f'dash:lesson:{lesson.id}:'))
    await h.tap(COORD, card)
    assert f'ms:manage:{lesson.id}' in h.buttons(h.last_to(COORD))
    await h.tap(COORD, f'ms:manage:{lesson.id}')
    assert f'ms:authors:{lesson.id}:0' in h.buttons(h.last_to(COORD))


async def test_admin_filters_by_subject_and_resets(h):
    from tests.test_ticket_and_terms import _term_for_week

    await _term_for_week(h, 8, 'filters')
    await _seed_lesson_for_subject(h, 'Calculus', 'filter-calc', 300)
    await _seed_lesson_for_subject(h, 'Linear Algebra', 'filter-la', 400)
    await h.say(COORD, '/dashboard')
    await h.tap(COORD, 'dash:sessions')
    filter_button = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:filters:'))
    await h.tap(COORD, filter_button)
    assert 'Фильтры' in h.texts_to(COORD)[-1]
    subject_pick = next(b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pick:subject:'))
    await h.tap(COORD, subject_pick)
    subject_choices = [b for b in h.buttons(h.last_to(COORD)) if b.startswith('dash:pickv:subject:')]
    assert subject_choices
    await h.tap(COORD, subject_choices[0])
    assert 'Предмет:' in h.texts_to(COORD)[-1]
    reset = next(b for b in h.buttons(h.last_to(COORD))
                 if b.startswith('dash:sessions:0:a:0:0'))
    await h.tap(COORD, reset)
    assert 'Предмет: все предметы' in h.texts_to(COORD)[-1]

