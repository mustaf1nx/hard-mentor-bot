"""Full dispatcher tests. Require requirements-dev.txt and a working async driver."""
from datetime import timedelta

from aiogram.methods import SendDocument
from aiogram.types import Document
from sqlalchemy import delete, select

from bot import repo
from bot.db import LearningMaterial, Mentor, MentoringSession, SessionBooking, Ticket, Topic, utcnow
from bot.mentoring import service as svc
from bot.mentoring.reminders import check_once
from tests.conftest import ALICE, BOB, COORD, LEAD, MENTOR, SETTINGS


async def create_through_chat(h):
    await h.say(COORD, f'/addmentor {MENTOR}')
    await h.say(MENTOR, '/setchecklist')
    await h.say(MENTOR, 'Calculus')
    await h.say(MENTOR, 'Integrals')
    await h.say(MENTOR, 'Подстановка\nПо частям')
    await h.say(COORD, '/newsession')
    await h.say(COORD, 'Calculus')
    await h.say(COORD, 'Integrals')
    await h.tap(COORD, f'ms:pickmentor:{MENTOR}')
    await h.say(COORD, svc.local_time(utcnow() + timedelta(days=4), SETTINGS))
    await h.say(COORD, '60')
    await h.say(COORD, 'Room 101')
    confirmation = next(x for x in h.buttons(h.last_to(COORD)) if x.startswith('ms:create:'))
    await h.tap(COORD, confirmation)
    async with h.sm() as s:
        lesson = (await s.scalars(select(MentoringSession))).one()
    return lesson


async def book_through_chat(h, lesson, uid=ALICE):
    await h.tap(uid, f'ms:join:{lesson.id}')
    await h.say(uid, 'Как выбрать подстановку?')
    async with h.sm() as s:
        booking = await svc.own_booking(s, lesson.id, uid)
    assert booking is not None and booking.status == 'active'
    return booking


async def test_coordinator_creation_student_booking_checklist_and_mentor_summary(h):
    lesson = await create_through_chat(h)
    booking = await book_through_chat(h, lesson)
    await h.tap(ALICE, f'ms:answer:{booking.id}:0:2')
    await h.tap(ALICE, f'ms:answer:{booking.id}:1:1')
    assert 'заполнен' in h.texts_to(ALICE)[-1]
    await h.tap(MENTOR, f'ms:summary:{lesson.id}')
    assert any('В: 1' in text for text in h.texts_to(MENTOR))
    await h.tap(MENTOR, f'ms:person:{booking.id}:detail')
    assert any('Как выбрать подстановку?' in text for text in h.texts_to(MENTOR))


async def test_sessions_are_separate_from_feedback_tickets(h):
    lesson = await create_through_chat(h)
    await book_through_chat(h, lesson)
    async with h.sm() as s:
        assert not list(await s.scalars(select(Ticket)))
    await h.submit(BOB, 'question', 'Можно узнать правила программы?')
    async with h.sm() as s:
        assert len(list(await s.scalars(select(Ticket)))) == 1
        assert len(list(await s.scalars(select(SessionBooking)))) == 1


async def test_new_menu_interrupts_old_ticket_comment_without_saving_it(h):
    await h.submit(ALICE, 'question', 'Можно узнать правила программы?')
    async with h.sm() as s:
        ticket = (await s.scalars(select(Ticket))).one()
    await h.tap(ALICE, f'cm:{ticket.id}')
    await h.say(ALICE, '🗓 Менторки')
    assert 'Входной' not in h.texts_to(ALICE)[-1]
    assert 'Менторки' in h.texts_to(ALICE)[-1]
    async with h.sm() as s:
        assert not await repo.list_notes(s, ticket.id)


async def test_new_menu_interrupts_staff_reply(h):
    await h.submit(ALICE, 'question', 'Можно узнать правила программы?')
    async with h.sm() as s:
        ticket = (await s.scalars(select(Ticket))).one()
    await h.tap(COORD, f'rp:{ticket.id}')
    await h.say(COORD, '📚 Банк материалов')
    async with h.sm() as s:
        assert not await repo.list_notes(s, ticket.id)


async def test_student_cannot_create_read_roster_mark_attendance_or_view_report(h):
    lesson = await create_through_chat(h)
    booking = await book_through_chat(h, lesson)
    await h.say(BOB, '/newsession')
    h.clear()
    for data in (f'ms:people:{lesson.id}:0', f'ms:person:{booking.id}:detail',
                 f'ms:mark:{booking.id}:present', 'ms:report'):
        await h.tap(BOB, data)
    assert not any('Как выбрать подстановку?' in t for t in h.texts_to(BOB))
    async with h.sm() as s:
        assert (await s.get(SessionBooking, booking.id)).attendance == 'unknown'


async def test_partial_checklist_survives_memory_storage_reset(h):
    lesson = await create_through_chat(h)
    booking = await book_through_chat(h, lesson)
    await h.tap(ALICE, f'ms:answer:{booking.id}:0:1')
    h.dp.storage.storage.clear()
    await h.tap(ALICE, f'ms:check:{booking.id}:next')
    assert 'пункт 2/2' in h.texts_to(ALICE)[-1]


async def test_bulk_mentors_still_work_and_removed_mentor_cannot_read_roster(h):
    lesson = await create_through_chat(h)
    await book_through_chat(h, lesson)
    await h.say(COORD, f'/delmentor {MENTOR}')
    h.clear()
    await h.tap(MENTOR, f'ms:people:{lesson.id}:0')
    assert not any('Как выбрать подстановку?' in text for text in h.texts_to(MENTOR))
    await h.say(COORD, f'/addmentor {MENTOR} {LEAD}')
    async with h.sm() as s:
        assert await repo.is_mentor(s, MENTOR)
        assert await repo.is_mentor(s, LEAD)


async def test_material_document_flow_and_private_access(h):
    lesson = await create_through_chat(h)
    await h.tap(MENTOR, f'ms:upload:{lesson.id}')
    await h.tap(MENTOR, 'ms:visibility:staff')
    await h.say(MENTOR, document=Document(file_id='doc1',file_unique_id='uniq1',file_name='answers.pdf',
                                         file_size=1024,mime_type='application/pdf'))
    async with h.sm() as s:
        material = (await s.scalars(select(LearningMaterial))).one()
        assert material.topic_id == lesson.topic_id and material.session_id == lesson.id
    h.clear()
    await h.tap(ALICE, f'ms:file:{material.id}')
    assert not any(isinstance(c,SendDocument) for c in h.sent_to(ALICE))
    await h.tap(MENTOR, f'ms:file:{material.id}')
    assert any(isinstance(c,SendDocument) for c in h.sent_to(MENTOR))


async def test_feedback_menu_not_swallowed_by_document_upload(h):
    lesson = await create_through_chat(h)
    await h.tap(MENTOR, f'ms:upload:{lesson.id}')
    await h.tap(MENTOR, 'ms:visibility:students')
    await h.say(MENTOR, '✍️ Оставить обращение')
    assert any(x.startswith('cat:') for x in h.buttons(h.last_to(MENTOR)))


async def test_cancellation_confirmation_and_persistent_notifications(h):
    lesson = await create_through_chat(h)
    await book_through_chat(h, lesson)
    await h.tap(COORD, f'ms:cancel:{lesson.id}')
    await h.say(COORD, 'Координатор отменил занятие')
    confirmation = next(x for x in h.buttons(h.last_to(COORD)) if x.startswith('ms:cancelconfirm:'))
    await h.tap(COORD, confirmation)
    async with h.sm() as s:
        assert (await s.get(MentoringSession,lesson.id)).status == 'cancelled'
    h.clear()
    await check_once(h.bot,h.sm,SETTINGS)
    assert any('отменена' in text for text in h.texts_to(ALICE))
    assert not any('Заполните чек-лист' in text for text in h.texts_to(ALICE))
    h.clear()
    await check_once(h.bot,h.sm,SETTINGS)
    assert h.texts_to(ALICE) == []


async def test_registration_during_cancellation_requires_reconfirmation(h):
    lesson = await create_through_chat(h)
    await h.tap(COORD, f'ms:cancel:{lesson.id}')
    await h.say(COORD, 'Нет записавшихся')
    confirmation = next(x for x in h.buttons(h.last_to(COORD)) if x.startswith('ms:cancelconfirm:'))
    await book_through_chat(h, lesson)
    await h.tap(COORD, confirmation)
    async with h.sm() as s:
        assert (await s.get(MentoringSession,lesson.id)).status == 'scheduled'
    assert 'теперь 1' in h.texts_to(COORD)[-1]


async def test_stale_and_malformed_buttons_do_not_mutate_database(h):
    lesson = await create_through_chat(h)
    for data in ('ms:answer', 'ms:mark:-2:present', 'ms:pickmentor:300', 'ms:create:stale', 'ms:topics:invalid:1:0'):
        await h.tap(ALICE,data)
    async with h.sm() as s:
        assert not list(await s.scalars(select(SessionBooking)))
        assert (await s.get(MentoringSession,lesson.id)).status == 'scheduled'
