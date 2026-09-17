"""Маршрутизация (раздел 5 спека) и все исходящие уведомления.

Правило для логов: ID заявителей сюда не пишем никогда. ID сотрудников — можно.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import repo, views
from .config import Settings
from .db import Ticket
from .texts import status_name, t

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, *, staff: bool = False,
                    fallback: str | None = None, **kwargs) -> bool:
    """fallback — короткий текст без разметки на случай, если Telegram отверг основное сообщение.
    Уведомление о серьёзном обращении не должно пропасть из-за ошибки форматирования."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramBadRequest as exc:
        log.error("Telegram отверг сообщение (%s)", exc)
        if fallback:
            try:
                await bot.send_message(chat_id, fallback, parse_mode=None)
                return True
            except TelegramAPIError:
                pass
        return False
    except TelegramAPIError as exc:
        if staff:
            log.warning("Не удалось написать сотруднику %s: %s (он нажал /start?)", chat_id, exc)
        else:
            log.warning("Не удалось доставить уведомление заявителю: %s", type(exc).__name__)
        return False


def recipients_for(settings: Settings, ticket: Ticket) -> set[int]:
    """Кому уходит новое обращение."""
    if ticket.lead_only:
        return set(settings.lead_ids)  # жалоба на координатора — только руководителю
    if ticket.category == "serious":
        return set(settings.coordinator_ids | settings.lead_ids)
    out = set(settings.coordinator_ids)
    if settings.lead_gets_all:
        out |= settings.lead_ids
    if not out:  # координатор не настроен — не теряем обращение
        out |= settings.lead_ids
    return out


def watchers_for(settings: Settings, ticket: Ticket) -> set[int]:
    """Кого уведомлять о событиях по уже существующему тикету."""
    if ticket.assigned_to:
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


async def notify_new_ticket(bot: Bot, sm: async_sessionmaker, settings: Settings, ticket: Ticket) -> None:
    targets = recipients_for(settings, ticket)
    if not targets:
        log.error("Обращение %s некому отправить: проверьте COORDINATOR_IDS / LEAD_IDS", ticket.code)
    names = await _names_for(sm, ticket)
    praise_mentor = ticket.mentor_id if ticket.category == "praise" else None
    langs = await _staff_langs(sm, targets | ({praise_mentor} if praise_mentor else set()))

    for uid in targets:
        lang = langs[uid]
        if ticket.lead_only:
            header = t(lang, "new_ticket_lead_only")
        elif ticket.category == "serious":
            header = t(lang, "new_ticket_urgent")
        else:
            header = t(lang, "new_ticket")
        await safe_send(
            bot, uid, views.staff_card(ticket, [], lang, names, header=header),
            staff=True, reply_markup=views.open_ticket_kb(ticket, lang),
            fallback=f"New ticket {ticket.code} ({ticket.category}). Open: /ticket {ticket.code}",
        )
    if praise_mentor and praise_mentor not in targets:
        lang = langs[praise_mentor]
        await safe_send(
            bot, praise_mentor,
            views.staff_card(ticket, [], lang, names, header=t(lang, "praise_for_you")),
            staff=True,
        )


async def notify_assignee(bot: Bot, sm: async_sessionmaker, ticket: Ticket, assignee_id: int) -> None:
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
    targets = watchers_for(settings, ticket)
    langs = await _staff_langs(sm, targets)
    for uid in targets:
        lang = langs[uid]
        await safe_send(
            bot, uid, t(lang, "submitter_comment", code=ticket.code, text=escape(text)),
            staff=True, reply_markup=views.open_ticket_kb(ticket, lang),
        )
