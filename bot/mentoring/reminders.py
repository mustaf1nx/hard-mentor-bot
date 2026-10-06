"""Persistent outbox and restart-safe reminder scheduling; polling service, one replica.

Telegram cannot provide exactly-once delivery. A process crash after sending but
before saving the receipt may cause a single duplicate after the lease expires.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from ..db import MentoringNotification, utcnow
from . import ui
from .jobs import generate_due, prepare

log = logging.getLogger(__name__)


async def check_once(bot: Bot, sm: async_sessionmaker, settings: Settings,
                     now: datetime | None = None) -> int:
    now = now or utcnow()
    async with sm() as s:
        await generate_due(s, settings, now)
        # A crash leaves a short lease rather than permanently losing the notification.
        await s.execute(update(MentoringNotification).where(MentoringNotification.status == 'sending',
            MentoringNotification.lease_until <= now).values(status='pending', lease_until=None))
        await s.execute(update(MentoringNotification).where(MentoringNotification.status == 'pending',
            MentoringNotification.attempts >= 5).values(status='failed'))
        await s.commit()
        ids = list(await s.scalars(select(MentoringNotification.id).where(
            MentoringNotification.status == 'pending', MentoringNotification.due_at <= now,
            MentoringNotification.attempts < 5).order_by(MentoringNotification.due_at, MentoringNotification.id).limit(50)))
    sent = 0
    for nid in ids:
        async with sm() as s:
            claimed = await s.scalar(update(MentoringNotification).where(
                MentoringNotification.id == nid, MentoringNotification.status == 'pending').values(
                status='sending', attempts=MentoringNotification.attempts + 1,
                lease_until=now + timedelta(minutes=3)).returning(MentoringNotification.id))
            if claimed is None:
                continue
            n = await s.get(MentoringNotification, nid)
            message = await prepare(s, n, settings, now)
            if message is None:
                n.status, n.lease_until = 'skipped', None
                await s.commit()
                continue
            uid, attempts = n.recipient_id, n.attempts
            await s.commit()
        status, retry_at = 'sent', now
        try:
            markup = ui.kb(*[[ui.b(*button) for button in row] for row in message[1]]) if message[1] else None
            await bot.send_message(uid, message[0], reply_markup=markup)
            sent += 1
        except TelegramRetryAfter as exc:
            status, retry_at = 'pending', now + timedelta(seconds=max(exc.retry_after, 60))
        except (TelegramForbiddenError, TelegramBadRequest):
            status = 'failed'
        except (TelegramAPIError, OSError):
            status, retry_at = 'pending', now + timedelta(minutes=min(60, 2 ** attempts))
        if status == 'pending' and attempts >= 5:
            status = 'failed'
        async with sm() as s:
            await s.execute(update(MentoringNotification).where(MentoringNotification.id == nid).values(
                status=status, due_at=retry_at, lease_until=None))
            await s.commit()
        # Stay comfortably below Telegram bulk messaging limits.
        await asyncio.sleep(0.05)
    return sent


async def reminder_loop(bot: Bot, sm: async_sessionmaker, settings: Settings) -> None:
    while True:
        try:
            await check_once(bot, sm, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No student identifiers, answers or questions in logs.
            log.error('Ошибка цикла напоминаний менторок; повтор при следующей проверке.')
        await asyncio.sleep(60)
