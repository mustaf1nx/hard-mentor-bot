"""Еженедельная сводка (6.7) и напоминания о просроченном SLA (6.3, 6.5).

Отчёт — только агрегаты: по нему нельзя выйти на конкретного анонимного автора.
Координатор получает версию без тикетов lead_only, руководитель — полную.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import repo
from .config import SLA_HOURS, Settings
from .db import CATEGORIES, Ticket, utcnow
from .notify import _staff_langs, safe_send, watchers_for
from .texts import cat_name, fmt_age, t
from .views import open_ticket_kb

log = logging.getLogger(__name__)


def _within_sla(ticket: Ticket) -> bool | None:
    hours = SLA_HOURS.get(ticket.category)
    if hours is None or ticket.first_response_at is None:
        return None
    return ticket.first_response_at - ticket.created_at <= timedelta(hours=hours)


async def build_weekly_report(sm: async_sessionmaker, lang: str, include_lead_only: bool,
                              now: datetime | None = None) -> str:
    now = now or utcnow()
    week_start, prev_start = now - timedelta(days=7), now - timedelta(days=14)
    async with sm() as session:
        this_week = await repo.tickets_between(session, week_start, now + timedelta(seconds=1))
        prev_week = await repo.tickets_between(session, prev_start, week_start)
        still_open = await repo.open_tickets(session)
    if not include_lead_only:
        this_week = [x for x in this_week if not x.lead_only]
        prev_week = [x for x in prev_week if not x.lead_only]
        still_open = [x for x in still_open if not x.lead_only]

    lines = [t(lang, "report_title", start=f"{week_start:%d.%m}", end=f"{now:%d.%m}")]
    if not include_lead_only:
        lines.append(t(lang, "report_scope_coord"))

    lines += ["", t(lang, "report_volume")]
    for cat in CATEGORIES:
        cur = sum(1 for x in this_week if x.category == cat)
        prev = sum(1 for x in prev_week if x.category == cat)
        lines.append(f"{cat_name(lang, cat)}: {cur} / {prev}")
    lines.append(f"<b>{t(lang, 'report_total')}: {len(this_week)} / {len(prev_week)}</b>")

    lines += ["", t(lang, "report_median")]
    for cat in CATEGORIES:
        if SLA_HOURS.get(cat) is None:
            continue
        deltas = [(x.first_response_at - x.created_at).total_seconds()
                  for x in this_week if x.category == cat and x.first_response_at]
        value = fmt_age(lang, median(deltas)) if deltas else t(lang, "report_nodata")
        lines.append(f"{cat_name(lang, cat)}: {value}")
    judged = [r for r in (_within_sla(x) for x in this_week) if r is not None]
    if judged:
        lines.append(f"{t(lang, 'report_within_sla')}: {round(100 * sum(judged) / len(judged))}%")

    lines += ["", t(lang, "report_overdue")]
    overdue = sorted((x for x in still_open if repo.is_overdue(x, now)), key=lambda x: x.created_at)
    if not overdue:
        lines.append(t(lang, "report_none"))
    for x in overdue[:15]:
        lines.append(f"{x.code} · {cat_name(lang, x.category)} · {fmt_age(lang, (now - x.created_at).total_seconds())}")
    if len(overdue) > 15:
        lines.append(f"… +{len(overdue) - 15}")

    lines.append("")
    if this_week:
        anon = sum(1 for x in this_week if x.is_anonymous)
        lines.append(f"{t(lang, 'report_anon')}: {round(100 * anon / len(this_week))}% ({anon}/{len(this_week)})")
    else:
        lines.append(f"{t(lang, 'report_anon')}: {t(lang, 'report_nodata')}")
    ratings = [x.satisfaction_rating for x in this_week if x.satisfaction_rating]
    avg = f"{sum(ratings) / len(ratings):.1f} ({len(ratings)})" if ratings else t(lang, "report_nodata")
    lines.append(f"{t(lang, 'report_rating')}: {avg}")
    return "\n".join(lines)


async def send_weekly_report(bot: Bot, sm: async_sessionmaker, settings: Settings) -> None:
    langs = await _staff_langs(sm, settings.staff_ids)
    for uid in settings.staff_ids:
        text = await build_weekly_report(sm, langs[uid], include_lead_only=uid in settings.lead_ids)
        await safe_send(bot, uid, text, staff=True)


async def weekly_report_loop(bot: Bot, sm: async_sessionmaker, settings: Settings) -> None:
    """Раз в 5 минут проверяем, не пора ли слать отчёт. Дата последней отправки лежит в БД,
    поэтому редеплой на Railway не приводит ни к дублям, ни к пропуску."""
    tz = ZoneInfo(settings.timezone)
    while True:
        try:
            local = datetime.now(tz)
            if local.weekday() == settings.report_weekday and local.hour >= settings.report_hour:
                today = local.date().isoformat()
                async with sm() as session:
                    last = await repo.meta_get(session, "last_weekly_report")
                if last != today:
                    async with sm() as session:
                        await repo.meta_set(session, "last_weekly_report", today)
                    await send_weekly_report(bot, sm, settings)
                    log.info("Еженедельный отчёт отправлен")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле еженедельного отчёта")
        await asyncio.sleep(300)


async def check_sla_once(bot: Bot, sm: async_sessionmaker, settings: Settings,
                         now: datetime | None = None) -> int:
    now = now or utcnow()
    async with sm() as session:
        overdue = await repo.overdue_unalerted(session, now)
        for ticket in overdue:
            ticket.sla_alerted = True
        await session.commit()
    for ticket in overdue:
        targets = set(watchers_for(settings, ticket))
        if ticket.lead_only:
            targets = set(settings.lead_ids)
        else:
            targets |= settings.lead_ids  # просрочка — это эскалация руководителю
            if ticket.category == "serious":
                targets |= settings.coordinator_ids
        langs = await _staff_langs(sm, targets)
        for uid in targets:
            lang = langs[uid]
            await safe_send(
                bot, uid,
                t(lang, "sla_alert", code=ticket.code, cat=cat_name(lang, ticket.category),
                  age=fmt_age(lang, (now - ticket.created_at).total_seconds())),
                staff=True, reply_markup=open_ticket_kb(ticket, lang),
            )
    return len(overdue)


async def sla_watch_loop(bot: Bot, sm: async_sessionmaker, settings: Settings) -> None:
    while True:
        try:
            await check_sla_once(bot, sm, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле контроля SLA")
        await asyncio.sleep(600)
