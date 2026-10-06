"""Маршрутизация (раздел 5 спека) и все исходящие уведомления.

Правило для логов: ID заявителей сюда не пишем никогда. ID сотрудников — можно.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                TelegramForbiddenError, TelegramRetryAfter)
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import repo, ticket_notifications as outbox, views
from .config import Settings
from .db import Ticket
from .texts import status_name, t
from .ticket_notifications import DeliveryResult, recipients_for

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, *, staff: bool = False,
                    fallback: str | None = None, **kwargs) -> bool:
    """fallback — короткий текст без разметки на случай, если Telegram отверг основное сообщение.
    Уведомление о серьёзном обращении не должно пропасть из-за ошибки форматирования."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramBadRequest as exc:
        log.error("Telegram отверг сообщение (%s)", type(exc).__name__)
        if fallback:
            try:
                await bot.send_message(chat_id, fallback, parse_mode=None)
                return True
            except TelegramAPIError:
                pass
        return False
    except TelegramAPIError as exc:
        if staff:
            log.warning("Не удалось написать сотруднику %s: %s (он нажал /start?)", chat_id, type(exc).__name__)
        else:
            log.warning("Не удалось доставить уведомление заявителю: %s", type(exc).__name__)
        return False


async def watchers_for(sm: async_sessionmaker, settings: Settings, ticket: Ticket) -> set[int]:
    """Check live permissions, including memberships removed directly through SQL."""
    if ticket.assigned_to:
        async with sm() as session:
            role = await repo.effective_role(session, settings, ticket.assigned_to)
        viewer = repo.Viewer(ticket.assigned_to, role, "ru", "")
        if viewer.is_staff and repo.can_view(viewer, ticket):
            return {ticket.assigned_to}
    return recipients_for(settings, ticket)


async def _staff_langs(sm: async_sessionmaker, ids) -> dict[int, str]:
    async with sm() as session:
        users = await repo.users_by_ids(session, ids)
    return {i: (users[i].lang if i in users else "ru") for i in ids}


async def _names_for(sm: async_sessionmaker, ticket: Ticket) -> dict:
    ids = [i for i in (ticket.mentor_id, ticket.assigned_to) if i]
    async with sm() as session:
        return await repo.users_by_ids(session, ids)


async def _send_new_ticket(bot: Bot, sm: async_sessionmaker, ticket: Ticket,
                           uid: int) -> DeliveryResult:
    """Transport only: the outbox checks current authorization before calling us."""
    langs = await _staff_langs(sm, {uid})
    lang = langs[uid]
    names = await _names_for(sm, ticket)
    if ticket.lead_only:
        header = t(lang, "new_ticket_lead_only")
    elif ticket.category == "serious":
        header = t(lang, "new_ticket_urgent")
    elif ticket.category == "praise" and uid == ticket.mentor_id:
        header = t(lang, "praise_for_you")
    else:
        header = t(lang, "new_ticket")
    text = views.staff_card(ticket, [], lang, names, header=header)
    markup = views.open_ticket_kb(ticket, lang)
    short = (f"🔔 New ticket {ticket.code}. Open: /ticket {ticket.code}" if lang == "en" else
             f"🔔 Новое обращение {ticket.code}. Открыть: /ticket {ticket.code}")
    try:
        try:
            await bot.send_message(uid, text, reply_markup=markup, parse_mode="HTML",
                                   disable_notification=False, request_timeout=15)
        except TelegramBadRequest as exc:
            # Bad recipient IDs cannot be repaired by changing the text.
            message = str(exc).lower()
            if any(part in message for part in ("chat not found", "user is deactivated",
                                                  "bot can't initiate", "bot cannot initiate")):
                return DeliveryResult("failed", "chat_unavailable")
            # Keep the open button and audible-notification flag in the fallback.
            await bot.send_message(uid, short, reply_markup=markup, parse_mode=None,
                                   disable_notification=False, request_timeout=15)
        return DeliveryResult("sent")
    except TelegramRetryAfter as exc:
        return DeliveryResult("retry", "TelegramRetryAfter", max(1, int(exc.retry_after)))
    except TelegramForbiddenError:
        return DeliveryResult("failed", "TelegramForbiddenError")
    except TelegramBadRequest:
        return DeliveryResult("failed", "TelegramBadRequest")
    except (TelegramAPIError, OSError, TimeoutError) as exc:
        return DeliveryResult("retry", type(exc).__name__)
    finally:
        # Rate limiting from Telegram is handled above; this also spreads batches.
        await asyncio.sleep(0.05)


async def send_due_ticket_notifications(bot: Bot, sm: async_sessionmaker, settings: Settings,
                                         *, ticket_id: int | None = None,
                                         recipient_id: int | None = None) -> int:
    async def sender(ticket: Ticket, uid: int) -> DeliveryResult:
        return await _send_new_ticket(bot, sm, ticket, uid)
    return await outbox.drain(sm, settings, sender, ticket_id=ticket_id, recipient_id=recipient_id)


async def notify_new_ticket(bot: Bot, sm: async_sessionmaker, settings: Settings, ticket: Ticket) -> None:
    # The submission handler already enqueues atomically. This idempotent insert
    # keeps this entry point safe for other callers without resending sent jobs.
    async with sm() as session:
        current = await repo.get_ticket(session, ticket.id)
        if current is None:
            return
        await outbox.enqueue_new_ticket(session, settings, current)
        await session.commit()
    await send_due_ticket_notifications(bot, sm, settings, ticket_id=ticket.id)


async def ticket_notification_loop(bot: Bot, sm: async_sessionmaker, settings: Settings) -> None:
    while True:
        try:
            await send_due_ticket_notifications(bot, sm, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Ошибка очереди уведомлений тикетов (%s); будет повтор", type(exc).__name__)
        await asyncio.sleep(10)


async def test_notifications(bot: Bot, sm: async_sessionmaker, settings: Settings,
                              viewer: repo.Viewer) -> None:
    """Self-test only: no test ticket and no unsolicited broadcast to other staff."""
    if not viewer.is_staff:
        return
    async with sm() as session:
        counts = await outbox.counts_for_recipient(session, viewer.id)
    waiting = counts.get("pending", 0) + counts.get("sending", 0)
    failed = counts.get("failed", 0)
    if viewer.lang == "en":
        if viewer.role == "coordinator":
            routing = "You receive new tickets, except complaints restricted to leads."
        elif viewer.role == "lead":
            routing = ("You receive all new tickets." if settings.lead_gets_all or not settings.coordinator_ids else
                       "LEAD_GETS_ALL=false: ordinary tickets go to coordinators. Set it to true to receive all tickets.")
        else:
            routing = "You receive assigned tickets and praise addressed to you, not the whole queue."
        text = (f"🔔 <b>Test notification</b>\n\nRole: <b>{viewer.role}</b>\n{routing}\n\n"
                f"Waiting: {waiting}. Failed: {failed}.\n"
                "Your unsent ticket alerts will be retried; delivered ones will not be repeated.\n\n"
                "If this arrives without a sound/banner, check this chat's notifications and your device settings.")
    else:
        if viewer.role == "coordinator":
            routing = "Вам приходят новые тикеты, кроме жалоб, доступных только руководителям."
        elif viewer.role == "lead":
            routing = ("Вам приходят все новые тикеты." if settings.lead_gets_all or not settings.coordinator_ids else
                       "LEAD_GETS_ALL=false: обычные тикеты уходят координаторам. Для всех тикетов установите true.")
        else:
            routing = "Вам приходят назначенные обращения и адресованные вам благодарности, не вся очередь."
        role = {"coordinator": "координатор", "lead": "руководитель", "mentor": "ментор"}[viewer.role]
        text = (f"🔔 <b>Тестовое уведомление</b>\n\nВаша роль: <b>{role}</b>\n{routing}\n\n"
                f"Ожидают отправки: {waiting}. Не доставлены: {failed}.\n"
                "Неотправленные уведомления для вас будут повторены, доставленные — нет.\n\n"
                "Нет звука или баннера? Проверьте уведомления этого чата и настройки устройства.")
    delivered = await safe_send(bot, viewer.id, text, staff=True, disable_notification=False)
    if delivered:
        async with sm() as session:
            await outbox.retry_for_recipient(session, settings, viewer.id)
            await session.commit()
        await send_due_ticket_notifications(bot, sm, settings, recipient_id=viewer.id)


async def notify_assignee(bot: Bot, sm: async_sessionmaker, settings: Settings,
                          ticket: Ticket, assignee_id: int) -> None:
    if assignee_id not in await watchers_for(sm, settings, ticket):
        return
    langs = await _staff_langs(sm, {assignee_id})
    lang = langs[assignee_id]
    names = await _names_for(sm, ticket)
    async with sm() as session:
        notes = await repo.list_notes(session, ticket.id)
    await safe_send(
        bot, assignee_id,
        views.staff_card(ticket, notes, lang, names, header=t(lang, "assigned_to_you")),
        staff=True, reply_markup=views.open_ticket_kb(ticket, lang),
    )


async def _submitter_lang(sm: async_sessionmaker, ticket: Ticket) -> str:
    async with sm() as session:
        user = await repo.get_user(session, ticket.submitter_id)
    return user.lang if user else "ru"


async def notify_status_change(bot: Bot, sm: async_sessionmaker, ticket: Ticket) -> None:
    """Пуш заявителю при смене статуса (6.4). Анонимному писать некуда — и это правильно."""
    if ticket.is_anonymous or not ticket.submitter_id:
        return
    lang = await _submitter_lang(sm, ticket)
    await safe_send(bot, ticket.submitter_id,
                    t(lang, "status_changed", code=ticket.code, status=status_name(lang, ticket.status)))
    if ticket.status == "resolved" and ticket.category != "praise" and not ticket.satisfaction_rating:
        await safe_send(bot, ticket.submitter_id, t(lang, "ask_rating", code=ticket.code),
                        reply_markup=views.rating_kb(ticket.id))


async def notify_reply(bot: Bot, sm: async_sessionmaker, ticket: Ticket, text: str) -> bool | None:
    """True/False — доставлено или нет; None — тикет анонимный, доставлять некому."""
    if ticket.is_anonymous or not ticket.submitter_id:
        return None
    lang = await _submitter_lang(sm, ticket)
    return await safe_send(
        bot, ticket.submitter_id,
        t(lang, "reply_received", code=ticket.code, text=escape(text)),
        reply_markup=views.submitter_card_kb(ticket, lang),
    )


async def notify_submitter_comment(bot: Bot, sm: async_sessionmaker, settings: Settings,
                                   ticket: Ticket, text: str) -> None:
    targets = await watchers_for(sm, settings, ticket)
    langs = await _staff_langs(sm, targets)
    for uid in targets:
        lang = langs[uid]
        await safe_send(
            bot, uid, t(lang, "submitter_comment", code=ticket.code, text=escape(text)),
            staff=True, reply_markup=views.open_ticket_kb(ticket, lang),
        )
