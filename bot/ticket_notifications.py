"""Transactional new-ticket outbox; no Telegram/aiogram dependency.

The transport is injected, so routing, retries, leases and access checks can be
verified with a real SQL database without making any Telegram calls.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repo
from .config import Settings
from .db import Ticket, TicketNotification, conflict_insert, utcnow

log = logging.getLogger(__name__)
LEASE_SECONDS = 180


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "retry", "failed"]
    error: str | None = None
    retry_after: int | None = None


Sender = Callable[[Ticket, int], Awaitable[DeliveryResult]]


def recipients_for(settings: Settings, ticket: Ticket) -> set[int]:
    """Administrative routing; restricted complaints never go to coordinators."""
    if ticket.lead_only:
        return set(settings.lead_ids)
    if ticket.category == "serious":
        return set(settings.staff_ids)
    targets = set(settings.coordinator_ids)
    if settings.lead_gets_all or not targets:
        targets.update(settings.lead_ids)
    return targets


async def targets_for(s: AsyncSession, settings: Settings, ticket: Ticket) -> set[int]:
    targets = recipients_for(settings, ticket)
    if ticket.assigned_to:
        role = await repo.effective_role(s, settings, ticket.assigned_to)
        viewer = repo.Viewer(ticket.assigned_to, role, "ru", "")
        if viewer.is_staff and repo.can_view(viewer, ticket):
            targets.add(ticket.assigned_to)
    # A related mentor is not automatically an assignee. In particular, never
    # notify the subject of a complaint simply because their name was selected.
    return targets


async def enqueue_new_ticket(s: AsyncSession, settings: Settings, ticket: Ticket) -> None:
    """Idempotent and deliberately does NOT commit; the caller owns the transaction."""
    targets = await targets_for(s, settings, ticket)
    if not targets:
        # Retain an unrouted job: setting valid IDs and restarting can recover it.
        # No notification is ever sent to chat 0.
        log.error("Тикет %s: нет получателей; проверьте COORDINATOR_IDS / LEAD_IDS", ticket.code)
        targets = {0}
    rows = [dict(ticket_id=ticket.id, recipient_id=uid, status="pending", attempts=0,
                 due_at=ticket.created_at, last_error="no_recipients" if uid == 0 else None)
            for uid in sorted(targets)]
    await s.execute(conflict_insert(TicketNotification.__table__, s.get_bind().dialect.name)
                    .values(rows).on_conflict_do_nothing(index_elements=["ticket_id", "recipient_id"]))


def _due(now: datetime):
    return or_(
        and_(TicketNotification.status == "pending", TicketNotification.due_at <= now),
        and_(TicketNotification.status == "sending", TicketNotification.lease_until <= now),
    )


async def finish_delivery(s: AsyncSession, notification_id: int, token: str,
                          result: DeliveryResult, attempts: int, now: datetime) -> None:
    """A stale worker cannot overwrite a newer worker's receipt."""
    values = dict(status="sent" if result.status == "sent" else "failed",
                  due_at=None, lease_until=None, lease_token=None, last_error=result.error)
    if result.status == "retry":
        delay = max(1, result.retry_after or min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7)))
        values.update(status="pending", due_at=now + timedelta(seconds=delay))
    await s.execute(update(TicketNotification).where(
        TicketNotification.id == notification_id, TicketNotification.lease_token == token,
        TicketNotification.status == "sending").values(**values))


async def drain(sm: async_sessionmaker, settings: Settings, sender: Sender, *,
                ticket_id: int | None = None, recipient_id: int | None = None,
                now: datetime | None = None, limit: int = 30) -> int:
    """Deliver due jobs. Transient failures retry; access is rechecked on each attempt.

    `now` is a deterministic test clock; production uses a fresh clock per claim.
    Delivery is at least once: Telegram has no idempotency key for sendMessage.
    """
    scan_time = now or utcnow()
    clauses = [_due(scan_time)]
    if ticket_id is not None:
        clauses.append(TicketNotification.ticket_id == ticket_id)
    if recipient_id is not None:
        clauses.append(TicketNotification.recipient_id == recipient_id)
    async with sm() as s:
        ids = list(await s.scalars(select(TicketNotification.id).where(*clauses)
            .order_by(TicketNotification.id).limit(max(1, min(limit, 100)))))
    sent = 0
    for nid in ids:
        claim_time = now or utcnow()
        token = uuid.uuid4().hex
        async with sm() as s:
            claimed = await s.scalar(update(TicketNotification).where(
                TicketNotification.id == nid, _due(claim_time)).values(
                status="sending", attempts=TicketNotification.attempts + 1,
                lease_until=claim_time + timedelta(seconds=LEASE_SECONDS), lease_token=token
            ).returning(TicketNotification.id))
            if claimed is None:
                continue
            notification = await s.get(TicketNotification, nid)
            ticket = await s.get(Ticket, notification.ticket_id)
            targets = await targets_for(s, settings, ticket) if ticket else set()
            if notification.recipient_id == 0 and ticket:
                if targets:
                    await enqueue_new_ticket(s, settings, ticket)
                    notification.status, notification.due_at = "skipped", None
                    notification.last_error = None
                else:
                    notification.status = "pending"
                    notification.due_at = claim_time + timedelta(minutes=1)
                notification.lease_until, notification.lease_token = None, None
                await s.commit()
                continue
            if ticket is None or notification.recipient_id not in targets:
                notification.status, notification.due_at = "skipped", None
                notification.lease_until, notification.lease_token = None, None
                notification.last_error = "access_revoked"
                await s.commit()
                continue
            uid, attempts = notification.recipient_id, notification.attempts
            await s.commit()
        try:
            result = await sender(ticket, uid)
        except Exception as exc:
            # Do not let one broken delivery prevent all other staff alerts.
            # Never log the exception message, the ticket text or submitter ID.
            result = DeliveryResult("retry", type(exc).__name__[:64])
        async with sm() as s:
            await finish_delivery(s, nid, token, result, attempts, now or utcnow())
            await s.commit()
        if result.status == "sent":
            sent += 1
        else:
            log.warning("Тикет %s: уведомление сотруднику %s — %s (%s)",
                        ticket.code, uid, result.status, result.error or "send_error")
    return sent


async def retry_for_recipient(s: AsyncSession, settings: Settings, uid: int,
                              now: datetime | None = None) -> int:
    """An explicit /testnotify reactivates only this user's unsent jobs."""
    now = now or utcnow()
    count = 0
    rows = list(await s.scalars(select(TicketNotification).where(
        TicketNotification.recipient_id == uid,
        TicketNotification.status.in_(("failed", "pending")))))
    for notification in rows:
        ticket = await s.get(Ticket, notification.ticket_id)
        permitted = ticket and uid in await targets_for(s, settings, ticket)
        values = (dict(status="pending", due_at=now, last_error=None, attempts=0) if permitted else
                  dict(status="skipped", due_at=None, last_error="access_revoked"))
        # The worker may have claimed this row while we were checking access.
        # Never reset an in-flight or already delivered notification.
        changed = await s.scalar(update(TicketNotification).where(
            TicketNotification.id == notification.id,
            TicketNotification.status.in_(("failed", "pending"))).values(**values)
            .returning(TicketNotification.id))
        if changed is not None and permitted:
            count += 1
    return count


async def counts_for_recipient(s: AsyncSession, uid: int) -> dict[str, int]:
    rows = await s.execute(select(TicketNotification.status, func.count(TicketNotification.id))
        .where(TicketNotification.recipient_id == uid).group_by(TicketNotification.status))
    return dict(rows.all())
