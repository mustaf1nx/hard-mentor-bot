"""Reminder decisions and outbox generation, with no Telegram dependency."""
from __future__ import annotations
from datetime import datetime, timedelta
from html import escape
from sqlalchemy import select
from .. import repo
from ..config import Settings
from ..db import LearningMaterial, MentoringSession, MentoringNotification, SessionBooking
from . import service as svc


def b(label, data):
    return (label, data)


def kb(*rows):
    return list(rows)


def e(value, limit=500):
    return escape(str(value)[:limit])


async def generate_due(s, settings: Settings, now: datetime) -> None:
    """Never automatically cancel; alert coordinators about sessions with zero bookings."""
    lessons = list(await s.scalars(select(MentoringSession).where(MentoringSession.status == 'scheduled',
        MentoringSession.starts_at <= now + timedelta(hours=2))))
    for record in lessons:
        lesson = await svc.get_session(s, record.id, lock=True)
        if lesson.status != 'scheduled':
            continue
        bookings = await svc.active_bookings(s, lesson.id)
        if not await repo.is_mentor(s, lesson.mentor_id):
            for uid in settings.staff_ids:
                await svc.enqueue(s, lesson, uid, 'mentor_missing', now)
        if not bookings:
            for uid in settings.staff_ids:
                await svc.enqueue(s, lesson, uid, 'empty', now)
        elif lesson.ends_at <= now:
            # Lost mentor membership is reported to coordinators, never sent private student data to ex-staff.
            targets = {lesson.mentor_id} if await repo.is_mentor(s, lesson.mentor_id) else settings.staff_ids
            for uid in targets:
                await svc.enqueue(s, lesson, uid, 'attendance', now)


async def prepare(s, notification: MentoringNotification, settings: Settings, now: datetime):
    lesson = await svc.get_session(s, notification.session_id, lock=True)
    kind = notification.kind
    # Membership is checked live, including changes made directly in SQL.
    role = await repo.effective_role(s, settings, notification.recipient_id)
    staff = role in ('mentor', 'coordinator', 'lead')
    admin = notification.recipient_id in settings.staff_ids
    if kind.startswith('checklist_'):
        booking = await s.get(SessionBooking, notification.booking_id)
        if (lesson.status != 'scheduled' or lesson.starts_at <= now or booking is None
                or booking.student_id != notification.recipient_id or booking.status != 'active'):
            return None
        await svc.attach_checklist(s, lesson)
        if not lesson.checklist_items or svc.answered(lesson, booking):
            return None
        text = f'☑️ До менторки #{lesson.id} осталось немного времени. Заполните чек-лист — это поможет ментору подготовиться.'
        markup = kb([b('Заполнить чек-лист', f'ms:check:{booking.id}:next')], [b('К занятию', f'ms:view:{lesson.id}')])
    elif kind == 'empty':
        if not admin or lesson.status != 'scheduled' or await svc.active_bookings(s, lesson.id):
            return None
        text = f'⚠️ На менторку #{lesson.id} записано 0 человек. Отмену принимает координатор; автоматически занятие не отменяется.'
        markup = kb([b('Открыть занятие', f'ms:view:{lesson.id}')])
    elif kind == 'mentor_missing':
        if not admin or lesson.status != 'scheduled' or await repo.is_mentor(s, lesson.mentor_id):
            return None
        text = f'⚠️ Ментор занятия #{lesson.id} больше не в списке менторов. Проверьте занятие и назначьте ведущего; новые записи временно недоступны.'
        markup = kb([b('Открыть занятие', f'ms:view:{lesson.id}')])
    elif kind == 'attendance':
        if lesson.status != 'scheduled' or not staff or (not admin and lesson.mentor_id != notification.recipient_id):
            return None
        text = f'✅ Время менторки #{lesson.id} закончилось. Отметьте посещаемость, завершите занятие и загрузите материалы.'
        markup = kb([b('Посещаемость', f'ms:attendance:{lesson.id}:0')])
    elif kind == 'materials':
        if lesson.status != 'completed' or not staff or (not admin and lesson.mentor_id != notification.recipient_id):
            return None
        exists = await s.scalar(select(LearningMaterial.id).where(LearningMaterial.session_id == lesson.id,
            LearningMaterial.archived.is_(False)).limit(1))
        if exists:
            return None
        text = f'📎 Занятие #{lesson.id} завершено. Добавьте материалы в банк: PDF или DOCX, предмет и тема уже подставлены.'
        markup = kb([b('Загрузить материалы', f'ms:upload:{lesson.id}')])
    elif kind == 'assigned':
        if lesson.status != 'scheduled' or not staff or lesson.mentor_id != notification.recipient_id:
            return None
        text = f'🎓 Вам назначена менторка #{lesson.id}. В карточке доступны вопросы и чек-листы студентов.'
        markup = kb([b('Открыть занятие', f'ms:view:{lesson.id}')])
    elif kind == 'unassigned':
        if lesson.mentor_id == notification.recipient_id:
            return None
        text = f'ℹ️ У менторки #{lesson.id} изменён ведущий. Вы больше не назначены её ментором.'
        markup = None
    elif kind == 'changed':
        booking = await s.get(SessionBooking, notification.booking_id)
        if lesson.status != 'scheduled' or booking is None or booking.status != 'active':
            return None
        text = f'🕒 Данные менторки #{lesson.id} изменены. Ваша запись сохранена. Проверьте новое время, место и ведущего.'
        markup = kb([b('Посмотреть изменения', f'ms:view:{lesson.id}')])
    elif kind == 'cancelled':
        if lesson.status != 'cancelled':
            return None
        text = f'❌ Менторка #{lesson.id} отменена.\nПричина: {e(lesson.cancel_reason, 400)}'
        markup = kb([b('Другие занятия', 'ms:subjects:schedule:0')])
    else:
        return None
    subject, topic = await svc.topic_names(s, lesson.topic_id)
    text += f'\n\n{e(subject, 100)} · {e(topic, 120)}\n{svc.local_time(lesson.starts_at, settings)} ({e(settings.timezone, 60)})'
    return text, markup


