"""Меню по роли и навигация чек-листа."""
from sqlalchemy import select

from bot.db import SessionBooking
from tests.conftest import COORD
from tests.test_sessions import book_through_chat, create_through_chat


def _buttons(h, uid):
    return h.buttons(h.last_to(uid))


async def test_coordinator_without_mentor_membership_sees_no_mentor_buttons(h):
    await h.say(COORD, '/sessions')
    buttons = _buttons(h, COORD)
    assert any(b.startswith('ms:subjects:schedule') for b in buttons)
    assert not any(b.startswith('ms:list:teaching') for b in buttons)
    assert not any(b.startswith('ms:mentorreport') for b in buttons)
    # управление координатору доступно всегда
    assert any(b.startswith('ms:admin') for b in buttons)


async def test_mentor_buttons_appear_after_adding_to_registry(h):
    await h.say(COORD, f'/addmentor {COORD}')
    await h.say(COORD, '/sessions')
    buttons = _buttons(h, COORD)
    assert any(b.startswith('ms:list:teaching') for b in buttons)
    assert any(b.startswith('ms:mentorreport') for b in buttons)


async def test_checklist_has_way_back_to_list_while_incomplete(h):
    lesson = await create_through_chat(h)
    booking = await book_through_chat(h, lesson)
    # чек-лист из двух пунктов: ни один не отмечен
    await h.tap(booking.student_id, f'ms:check:{booking.id}:0')
    buttons = _buttons(h, booking.student_id)
    assert any(b.startswith(f'ms:checklist:{booking.id}') for b in buttons), buttons
    await h.tap(booking.student_id, f'ms:checklist:{booking.id}')
    text = h.texts_to(booking.student_id)[-1]
    assert 'отмечено 0 из 2' in text


async def test_checklist_item_can_be_edited_after_completion(h):
    lesson = await create_through_chat(h)
    booking = await book_through_chat(h, lesson)
    uid = booking.student_id
    await h.tap(uid, f'ms:answer:{booking.id}:0:2')
    await h.tap(uid, f'ms:answer:{booking.id}:1:1')
    assert 'заполнен' in h.texts_to(uid)[-1]
    await h.tap(uid, f'ms:check:{booking.id}:1')
    await h.tap(uid, f'ms:answer:{booking.id}:1:0')
    async with h.sm() as s:
        row = await s.get(SessionBooking, booking.id)
    assert row.answers['1'] == 0
