"""Dispatcher integration tests. Requires requirements-dev.txt.

Run separately on SQLite or on a dedicated TEST_DATABASE_URL PostgreSQL database.
These tests use the genuine aiogram dispatcher but a fake Telegram session.
"""
from dataclasses import replace
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from bot import notify, ticket_notifications as outbox
from bot.db import Ticket, TicketNotification, utcnow
from tests.conftest import ALICE, COORD, LEAD, MENTOR, SETTINGS

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def rows(h):
    async with h.sm() as s:
        return list(await s.scalars(select(TicketNotification).order_by(TicketNotification.id)))


async def test_new_ticket_is_persisted_and_sent_with_open_button(h):
    await h.submit(ALICE, "question", "Как подготовиться к следующему занятию?")
    deliveries = await rows(h)
    assert {n.recipient_id for n in deliveries} == {COORD, LEAD}
    assert all(n.status == "sent" for n in deliveries)
    for uid in (COORD, LEAD):
        message = h.last_to(uid)
        assert message.disable_notification is False
        assert any(b.startswith("tk:") for b in h.buttons(message))


async def test_repeat_entrypoint_does_not_resend_delivered_ticket(h):
    await h.submit(ALICE, "suggestion", "Предлагаю сделать дополнительное занятие.")
    async with h.sm() as s:
        ticket = await s.scalar(select(Ticket))
    h.clear()
    await notify.notify_new_ticket(h.bot, h.sm, SETTINGS, ticket)
    assert not h.sent_to(COORD) and not h.sent_to(LEAD)


async def test_network_failure_queues_retry_without_blocking_other_staff(h, monkeypatch):
    original = h.session.make_request
    async def failure(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == COORD:
            raise TelegramNetworkError(method=method, message="temporary failure")
        return await original(bot, method, timeout)
    monkeypatch.setattr(h.session, "make_request", failure)
    await h.submit(ALICE, "suggestion", "Предлагаю дополнительное занятие по программированию.")
    by_uid = {n.recipient_id: n for n in await rows(h)}
    assert by_uid[COORD].status == "pending"
    assert by_uid[LEAD].status == "sent"
    monkeypatch.setattr(h.session, "make_request", original)
    async def sender(ticket, uid):
        return await notify._send_new_ticket(h.bot, h.sm, ticket, uid)
    assert await outbox.drain(h.sm, SETTINGS, sender, now=utcnow() + timedelta(minutes=2)) == 1
    assert all(n.status == "sent" for n in await rows(h))


async def test_confirmation_failure_does_not_lose_staff_notification(h, monkeypatch):
    original = h.session.make_request
    async def blocked_confirmation(bot, method, timeout=None):
        if (isinstance(method, SendMessage) and method.chat_id == ALICE
                and "HM-" in method.text):
            raise TelegramForbiddenError(method=method, message="bot blocked")
        return await original(bot, method, timeout)
    monkeypatch.setattr(h.session, "make_request", blocked_confirmation)
    with pytest.raises(TelegramForbiddenError):
        await h.submit(ALICE, "suggestion", "Предлагаю обсуждать сложные темы заранее.")
    assert all(n.status == "sent" for n in await rows(h))
    assert h.sent_to(COORD) and h.sent_to(LEAD)


async def test_bad_html_fallback_keeps_open_button(h, monkeypatch):
    original = h.session.make_request
    failed = False
    async def reject_html(bot, method, timeout=None):
        nonlocal failed
        if isinstance(method, SendMessage) and method.chat_id == COORD and not failed:
            failed = True
            raise TelegramBadRequest(method=method, message="can't parse entities")
        return await original(bot, method, timeout)
    monkeypatch.setattr(h.session, "make_request", reject_html)
    await h.submit(ALICE, "suggestion", "Предлагаю добавить примеры решений.")
    assert h.last_to(COORD).parse_mode is None
    assert h.last_to(COORD).disable_notification is False
    assert any(b.startswith("tk:") for b in h.buttons(h.last_to(COORD)))


async def test_self_test_has_no_broadcast_or_test_ticket(h):
    await h.say(COORD, "/testnotify")
    assert "Тестовое уведомление" in h.texts_to(COORD)[-1]
    assert not h.sent_to(LEAD) and not h.sent_to(MENTOR)
    assert h.last_to(COORD).disable_notification is False
    async with h.sm() as s:
        assert await s.scalar(select(Ticket.id)) is None


async def test_self_test_denies_regular_student(h):
    await h.say(ALICE, "/testnotify")
    assert "только команде" in h.texts_to(ALICE)[-1]
    assert not h.sent_to(COORD) and not h.sent_to(LEAD)


async def test_self_test_explains_lead_gets_all_false(h):
    settings = replace(SETTINGS, lead_gets_all=False)
    old = h.dp["settings"]
    h.dp["settings"] = settings
    try:
        await h.say(LEAD, "/testnotify")
        assert "LEAD_GETS_ALL=false" in h.texts_to(LEAD)[-1]
    finally:
        h.dp["settings"] = old
