"""Mentoring domain rules. No Telegram dependency; callers commit transactions.

All mutations lock the parent session on PostgreSQL, serializing registration,
rescheduling, cancellation, checklist edits and attendance for that session.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import repo
from ..config import Settings
from ..db import (LearningMaterial, Mentor, MentoringNotification, MentoringSession,
                  SessionBooking, Subject, Topic, TopicChecklist, User, conflict_insert, utcnow)
from ..repo import Viewer

PAGE_SIZE = 8
MAX_ITEMS = 20
MAX_ITEM_LENGTH = 110
MAX_FILE_BYTES = 20 * 1024 * 1024
SCORES = {2: 'Понимаю', 1: 'Есть вопросы', 0: 'Не понимаю'}


class RuleError(ValueError):
    """A message safe to display to a user (escape it before using HTML)."""


def clean_name(value: str, limit: int) -> str:
    value = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not value or len(value) > limit or any(unicodedata.category(c) == 'Cc' for c in value):
        raise RuleError(f'Введите название длиной от 1 до {limit} символов.')
    return value


def parse_items(text: str) -> list[str]:
    items = [re.sub(r'^(?:[-•]\s*|\d+[.)]\s+)', '', x.strip()) for x in text.splitlines() if x.strip()]
    if not 1 <= len(items) <= MAX_ITEMS:
        raise RuleError(f'Нужно от 1 до {MAX_ITEMS} пунктов, каждый с новой строки.')
    if any(not x or len(x.encode("utf-16-le")) // 2 > MAX_ITEM_LENGTH for x in items):
        raise RuleError(f'Один пункт — от 1 до {MAX_ITEM_LENGTH} символов.')
    if len({x.casefold() for x in items}) != len(items):
        raise RuleError('В чек-листе есть повторяющиеся пункты.')
    return items


def parse_start(text: str, tz_name: str, now: datetime | None = None) -> datetime:
    value = None
    for fmt in ('%d.%m.%Y %H:%M', '%Y-%m-%d %H:%M'):
        try:
            local = datetime.strptime(text.strip(), fmt)
            value = local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc).replace(tzinfo=None)
            break
        except ValueError:
            continue
    if value is None:
        raise RuleError('Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ, например 15.10.2026 18:30.')
    if value <= (now or utcnow()) + timedelta(minutes=5):
        raise RuleError('Начало должно быть больше чем через 5 минут.')
    return value


def local_time(value: datetime, settings: Settings) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.%Y %H:%M')


def validate_file(name: str | None, size: int | None, mime: str | None) -> str:
    name = PurePosixPath((name or '').replace('\\', '/')).name
    suffix = PurePosixPath(name).suffix.lower()
    expected = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    if suffix not in expected:
        raise RuleError('Отправьте документ PDF или DOCX, не фотографию и не картинку.')
    if size is not None and (size <= 0 or size > MAX_FILE_BYTES):
        raise RuleError('Размер документа должен быть от 1 байта до 20 МиБ.')
    if mime and mime not in (expected[suffix], 'application/octet-stream', 'application/zip' if suffix == '.docx' else expected[suffix]):
        raise RuleError('Тип документа не соответствует расширению PDF/DOCX.')
    return name[:200]


def require_admin(viewer: Viewer) -> None:
    if not viewer.is_admin:
        raise RuleError('Создавать, переносить и отменять занятия может координатор или руководитель.')


def require_staff(viewer: Viewer) -> None:
    if not viewer.is_staff:
        raise RuleError('Это действие доступно менторам и координаторам.')


def can_manage(viewer: Viewer, lesson: MentoringSession) -> bool:
    return viewer.is_admin or (viewer.is_staff and lesson.mentor_id == viewer.id)


def require_manage(viewer: Viewer, lesson: MentoringSession) -> None:
    if not can_manage(viewer, lesson):
        raise RuleError('У вас нет доступа к участникам и ответам этого занятия.')


async def get_session(s: AsyncSession, sid: int, *, lock: bool = False) -> MentoringSession:
    query = select(MentoringSession).where(MentoringSession.id == sid).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    lesson = await s.scalar(query)
    if lesson is None:
        raise RuleError('Занятие не найдено.')
    return lesson


def require_open(lesson: MentoringSession, now: datetime | None = None) -> None:
    if lesson.status != 'scheduled' or lesson.starts_at <= (now or utcnow()):
        raise RuleError('Запись и подготовка закрыты: занятие уже началось или отменено.')


async def ensure_topic(s: AsyncSession, subject_name: str, topic_name: str) -> Topic:
    subject_name, topic_name = clean_name(subject_name, 100), clean_name(topic_name, 120)
    dialect = s.get_bind().dialect.name
    await s.execute(conflict_insert(Subject.__table__, dialect).values(
        name=subject_name, name_key=subject_name.casefold()).on_conflict_do_nothing(index_elements=['name_key']))
    subject_id = await s.scalar(select(Subject.id).where(Subject.name_key == subject_name.casefold()))
    await s.execute(conflict_insert(Topic.__table__, dialect).values(
        subject_id=subject_id, name=topic_name, name_key=topic_name.casefold()).on_conflict_do_nothing(
            index_elements=['subject_id', 'name_key']))
    return await s.scalar(select(Topic).where(Topic.subject_id == subject_id, Topic.name_key == topic_name.casefold()))


async def topic_names(s: AsyncSession, topic_id: int) -> tuple[str, str]:
    row = (await s.execute(select(Subject.name, Topic.name).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == topic_id))).first()
    if row is None:
        raise RuleError('Тема не найдена.')
    return row[0], row[1]


async def subjects(s: AsyncSession) -> list[Subject]:
    return list(await s.scalars(select(Subject).order_by(Subject.name_key)))


async def topics(s: AsyncSession, subject_id: int) -> list[Topic]:
    return list(await s.scalars(select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.name_key)))


async def put_checklist(s: AsyncSession, viewer: Viewer, topic_id: int, items: list[str]) -> None:
    require_staff(viewer)
    parse_items('\n'.join(items))
    await topic_names(s, topic_id)
    ins = conflict_insert(TopicChecklist.__table__, s.get_bind().dialect.name)
    await s.execute(ins.values(topic_id=topic_id, items=items, author_id=viewer.id).on_conflict_do_update(
        index_elements=['topic_id'], set_={'items': items, 'author_id': viewer.id}))
    # Existing snapshots are intentionally immutable. New sessions/empty snapshots use the new template.


async def attach_checklist(s: AsyncSession, lesson: MentoringSession) -> None:
    if not lesson.checklist_items:
        template = await s.scalar(select(TopicChecklist).where(TopicChecklist.topic_id == lesson.topic_id).execution_options(populate_existing=True))
        if template:
            lesson.checklist_items = list(template.items)
            await s.flush()


async def refresh_checklist(s: AsyncSession, viewer: Viewer, sid: int, now: datetime | None = None) -> MentoringSession:
    lesson = await get_session(s, sid, lock=True)
    require_manage(viewer, lesson)
    require_open(lesson, now)
    bookings = list(await s.scalars(select(SessionBooking).where(SessionBooking.session_id == sid)))
    if any(b.answers for b in bookings):
        raise RuleError('Уже есть ответы студентов. Чек-лист этого занятия менять нельзя; новая версия будет у следующих занятий.')
    template = await s.scalar(select(TopicChecklist).where(TopicChecklist.topic_id == lesson.topic_id).execution_options(populate_existing=True))
    if template is None:
        raise RuleError('Сначала добавьте чек-лист этой темы в банк через /setchecklist.')
    lesson.checklist_items = list(template.items)
    return lesson


async def enqueue(s: AsyncSession, lesson: MentoringSession, uid: int, kind: str,
                  due_at: datetime, *, booking_id: int | None = None, key: str | None = None) -> None:
    key = key or f'{kind}:{lesson.id}:{lesson.revision}:{uid}'
    await s.execute(conflict_insert(MentoringNotification.__table__, s.get_bind().dialect.name).values(
        event_key=key, session_id=lesson.id, booking_id=booking_id, recipient_id=uid,
        kind=kind, due_at=due_at, status='pending', attempts=0).on_conflict_do_nothing(index_elements=['event_key']))


async def create_session(s: AsyncSession, viewer: Viewer, settings: Settings, *, topic_id: int,
                         mentor_id: int, starts_at: datetime, duration: int, location: str,
                         creation_key: str, now: datetime | None = None) -> MentoringSession:
    require_admin(viewer)
    existing = await s.scalar(select(MentoringSession).where(MentoringSession.creation_key == creation_key))
    if existing:
        if existing.created_by != viewer.id:
            raise RuleError('Недействительный черновик.')
        return existing
    now = now or utcnow()
    if starts_at <= now + timedelta(minutes=5) or not 15 <= duration <= 360:
        raise RuleError('Начало — позже чем через 5 минут, длительность — от 15 до 360 минут.')
    if not await repo.is_mentor(s, mentor_id):
        raise RuleError('Ментор больше не в списке. Сначала добавьте его через /addmentor.')
    await topic_names(s, topic_id)
    lesson = MentoringSession(creation_key=creation_key, topic_id=topic_id, mentor_id=mentor_id,
        created_by=viewer.id, starts_at=starts_at, ends_at=starts_at + timedelta(minutes=duration),
        location=clean_name(location, 300), status='scheduled', checklist_items=[], revision=1)
    s.add(lesson)
    await s.flush()
    await attach_checklist(s, lesson)
    await enqueue(s, lesson, mentor_id, 'assigned', now)
    return lesson


async def active_bookings(s: AsyncSession, sid: int) -> list[SessionBooking]:
    return list(await s.scalars(select(SessionBooking).where(SessionBooking.session_id == sid,
        SessionBooking.status == 'active').order_by(SessionBooking.id)))


async def own_booking(s: AsyncSession, sid: int, uid: int) -> SessionBooking | None:
    return await s.scalar(select(SessionBooking).where(SessionBooking.session_id == sid, SessionBooking.student_id == uid))


async def booking_reminders(s: AsyncSession, lesson: MentoringSession, booking: SessionBooking,
                            now: datetime) -> None:
    delta = lesson.starts_at - now
    stages = [('checklist_24', 24)] if delta > timedelta(hours=2) else []
    if delta > timedelta(minutes=10):
        stages += [('checklist_2', 2)]
    for kind, hours in stages:
        due = max(now + timedelta(minutes=5), lesson.starts_at - timedelta(hours=hours))
        await enqueue(s, lesson, booking.student_id, kind, due, booking_id=booking.id)
        # A student may withdraw and rejoin after the old reminder was skipped.
        await s.execute(update(MentoringNotification).where(
            MentoringNotification.event_key == f'{kind}:{lesson.id}:{lesson.revision}:{booking.student_id}',
            MentoringNotification.status == 'skipped').values(status='pending', due_at=due, attempts=0))


async def register(s: AsyncSession, viewer: Viewer, sid: int, question: str,
                   now: datetime | None = None) -> tuple[MentoringSession, SessionBooking]:
    now = now or utcnow()
    lesson = await get_session(s, sid, lock=True)
    require_open(lesson, now)
    if lesson.mentor_id == viewer.id:
        raise RuleError('Вы ведёте это занятие; записываться участником не нужно.')
    if not await repo.is_mentor(s, lesson.mentor_id):
        raise RuleError('Назначенный ментор больше не в списке. Координатор должен назначить другого.')
    question = question.strip()
    if not question or len(question) > 1500:
        raise RuleError('Напишите вопрос длиной до 1500 символов или «-», если вопросов пока нет.')
    await attach_checklist(s, lesson)
    booking = await own_booking(s, sid, viewer.id)
    if booking is None:
        booking = SessionBooking(session_id=sid, student_id=viewer.id, question=question,
                                 status='active', answers={}, attendance='unknown')
        s.add(booking)
        await s.flush()
    else:
        booking.question = question
        booking.status = 'active'
    await booking_reminders(s, lesson, booking, now)
    return lesson, booking


async def withdraw(s: AsyncSession, viewer: Viewer, sid: int, now: datetime | None = None) -> None:
    lesson = await get_session(s, sid, lock=True)
    require_open(lesson, now)
    booking = await own_booking(s, sid, viewer.id)
    if booking is None or booking.status != 'active':
        raise RuleError('У вас нет активной записи.')
    booking.status = 'withdrawn'


async def save_answer(s: AsyncSession, viewer: Viewer, booking_id: int, index: int, score: int,
                      now: datetime | None = None) -> tuple[MentoringSession, SessionBooking]:
    booking = await s.get(SessionBooking, booking_id)
    if booking is None or booking.student_id != viewer.id:
        raise RuleError('Это не ваш чек-лист.')
    lesson = await get_session(s, booking.session_id, lock=True)
    await s.refresh(booking)
    require_open(lesson, now)
    if booking.status != 'active' or score not in SCORES or not 0 <= index < len(lesson.checklist_items):
        raise RuleError('Чек-лист недоступен или кнопка устарела.')
    booking.answers = {**booking.answers, str(index): score}
    return lesson, booking


def answered(lesson: MentoringSession, booking: SessionBooking) -> bool:
    return bool(lesson.checklist_items) and all(booking.answers.get(str(i)) in SCORES for i in range(len(lesson.checklist_items)))


def checklist_summary(lesson: MentoringSession, bookings: list[SessionBooking]) -> list[tuple[str, dict]]:
    out = []
    for index, item in enumerate(lesson.checklist_items):
        counts = {0: 0, 1: 0, 2: 0, 'missing': 0}
        for booking in bookings:
            value = booking.answers.get(str(index))
            counts[value if value in SCORES else 'missing'] += 1
        out.append((item, counts))
    return out


async def set_attendance(s: AsyncSession, viewer: Viewer, booking_id: int, value: str,
                         now: datetime | None = None) -> MentoringSession:
    booking = await s.get(SessionBooking, booking_id)
    if booking is None:
        raise RuleError('Участник не найден.')
    lesson = await get_session(s, booking.session_id, lock=True)
    await s.refresh(booking)
    require_manage(viewer, lesson)
    if lesson.status == 'cancelled' or lesson.starts_at > (now or utcnow()):
        raise RuleError('Посещаемость отмечается после начала неотменённого занятия.')
    if booking.status != 'active' or value not in ('present', 'absent', 'unknown'):
        raise RuleError('Недопустимая отметка посещаемости.')
    booking.attendance = value
    return lesson


async def complete_session(s: AsyncSession, viewer: Viewer, sid: int,
                           now: datetime | None = None) -> MentoringSession:
    now = now or utcnow()
    lesson = await get_session(s, sid, lock=True)
    require_manage(viewer, lesson)
    if lesson.status == 'cancelled' or lesson.ends_at > now:
        raise RuleError('Завершить можно только закончившееся неотменённое занятие.')
    bookings = await active_bookings(s, sid)
    if not bookings:
        raise RuleError('Никто не записался. Попросите координатора отменить занятие.')
    if any(b.attendance == 'unknown' for b in bookings):
        raise RuleError('Сначала отметьте присутствие или отсутствие каждого участника.')
    lesson.status = 'completed'
    await enqueue(s, lesson, lesson.mentor_id, 'materials', now)
    return lesson


async def cancel_session(s: AsyncSession, viewer: Viewer, sid: int, reason: str,
                         now: datetime | None = None) -> MentoringSession:
    require_admin(viewer)
    lesson = await get_session(s, sid, lock=True)
    if lesson.status == 'completed':
        raise RuleError('Проведённое занятие нельзя отменить.')
    if lesson.status == 'cancelled':
        return lesson
    reason = clean_name(reason, 400)
    lesson.status, lesson.cancel_reason = 'cancelled', reason
    lesson.revision += 1
    for uid in {lesson.mentor_id} | {b.student_id for b in await active_bookings(s, sid)}:
        await enqueue(s, lesson, uid, 'cancelled', now or utcnow())
    return lesson


async def edit_session(s: AsyncSession, viewer: Viewer, sid: int, field: str, value,
                       now: datetime | None = None) -> MentoringSession:
    require_admin(viewer)
    now = now or utcnow()
    lesson = await get_session(s, sid, lock=True)
    require_open(lesson, now)
    old_mentor = lesson.mentor_id
    if field == 'mentor':
        if not await repo.is_mentor(s, int(value)):
            raise RuleError('Новый ментор не найден в списке /mentors.')
        booked = await own_booking(s, sid, int(value))
        if booked and booked.status == 'active':
            raise RuleError('Этот пользователь записан студентом. Сначала нужно отменить его запись.')
        lesson.mentor_id = int(value)
    elif field == 'start':
        if value <= now + timedelta(minutes=5):
            raise RuleError('Новая дата должна быть в будущем.')
        duration = lesson.ends_at - lesson.starts_at
        lesson.starts_at, lesson.ends_at = value, value + duration
    elif field == 'location':
        lesson.location = clean_name(value, 300)
    else:
        raise RuleError('Неизвестное изменение.')
    lesson.revision += 1
    await s.execute(update(MentoringNotification).where(MentoringNotification.session_id == sid,
        MentoringNotification.status == 'pending').values(status='skipped'))
    for b in await active_bookings(s, sid):
        await enqueue(s, lesson, b.student_id, 'changed', now, booking_id=b.id)
        await booking_reminders(s, lesson, b, now)
    await enqueue(s, lesson, lesson.mentor_id, 'assigned', now)
    if old_mentor != lesson.mentor_id:
        await enqueue(s, lesson, old_mentor, 'unassigned', now)
    return lesson


async def add_material(s: AsyncSession, viewer: Viewer, *, topic_id: int, kind: str,
                       visibility: str, file_id: str, file_unique_id: str, file_name: str,
                       file_size: int | None = None, mime: str | None = None,
                       session_id: int | None = None) -> LearningMaterial:
    require_staff(viewer)
    file_name = validate_file(file_name, file_size, mime)
    if kind not in ('recap', 'material') or visibility not in ('students', 'staff'):
        raise RuleError('Выберите тип материала и доступ кнопками.')
    if session_id:
        lesson = await get_session(s, session_id, lock=True)
        require_manage(viewer, lesson)
        if lesson.status == 'cancelled':
            raise RuleError('Материалы нельзя прикрепить к отменённому занятию. Добавьте их в банк отдельно.')
        topic_id = lesson.topic_id
    await topic_names(s, topic_id)
    existing = await s.scalar(select(LearningMaterial).where(LearningMaterial.topic_id == topic_id,
        LearningMaterial.session_id == session_id, LearningMaterial.file_unique_id == file_unique_id,
        LearningMaterial.kind == kind, LearningMaterial.visibility == visibility, LearningMaterial.archived.is_(False)))
    if existing:
        return existing
    material = LearningMaterial(topic_id=topic_id, session_id=session_id, author_id=viewer.id,
        kind=kind, visibility=visibility, file_id=file_id, file_unique_id=file_unique_id, file_name=file_name)
    s.add(material)
    await s.flush()
    return material


async def material_for(s: AsyncSession, viewer: Viewer, material_id: int) -> LearningMaterial:
    material = await s.get(LearningMaterial, material_id)
    if material is None or material.archived or (material.visibility == 'staff' and not viewer.is_staff):
        raise RuleError('Материал недоступен.')
    return material


async def archive_material(s: AsyncSession, viewer: Viewer, material_id: int) -> None:
    material = await material_for(s, viewer, material_id)
    if not viewer.is_admin and not (viewer.is_staff and material.author_id == viewer.id):
        raise RuleError('Убрать материал может его автор или координатор.')
    material.archived = True


async def material_list(s: AsyncSession, viewer: Viewer, topic_id: int, *, recap_only: bool = False) -> list[LearningMaterial]:
    query = select(LearningMaterial).where(LearningMaterial.topic_id == topic_id, LearningMaterial.archived.is_(False))
    if not viewer.is_staff:
        query = query.where(LearningMaterial.visibility == 'students')
    if recap_only:
        # Automatic preparation MUST never include team-only documents, even for staff acting as a student.
        query = query.where(LearningMaterial.kind == 'recap', LearningMaterial.visibility == 'students')
    return list(await s.scalars(query.order_by(LearningMaterial.id.desc())))


async def session_list(s: AsyncSession, viewer: Viewer, mode: str, topic_id: int = 0,
                       now: datetime | None = None) -> list[MentoringSession]:
    query = select(MentoringSession)
    if mode == 'all':
        require_admin(viewer)
    elif mode == 'teaching':
        require_staff(viewer)
        query = query.where(MentoringSession.mentor_id == viewer.id)
    elif mode == 'bookings':
        query = query.join(SessionBooking).where(SessionBooking.student_id == viewer.id, SessionBooking.status == 'active')
    else:
        query = query.where(MentoringSession.status == 'scheduled', MentoringSession.starts_at > (now or utcnow()))
    if topic_id:
        query = query.where(MentoringSession.topic_id == topic_id)
    return list(await s.scalars(query.order_by(MentoringSession.starts_at.desc() if mode in ('all','teaching','bookings') else MentoringSession.starts_at, MentoringSession.id)))


async def stats(s: AsyncSession) -> dict:
    lessons = list(await s.scalars(select(MentoringSession)))
    bookings = list(await s.scalars(select(SessionBooking).where(SessionBooking.status == 'active')))
    by_session: dict[int, list] = {}
    for b in bookings:
        by_session.setdefault(b.session_id, []).append(b)
    material_sids = set(await s.scalars(select(LearningMaterial.session_id).where(
        LearningMaterial.session_id.is_not(None), LearningMaterial.archived.is_(False))))
    rows = []
    for lesson in lessons:
        bs = by_session.get(lesson.id, [])
        rows.append({'lesson': lesson, 'registered': len(bs), 'present': sum(b.attendance == 'present' for b in bs),
            'absent': sum(b.attendance == 'absent' for b in bs), 'unknown': sum(b.attendance == 'unknown' for b in bs),
            'checklists': sum(answered(lesson, b) for b in bs), 'has_checklist': bool(lesson.checklist_items),
            'has_materials': lesson.id in material_sids})
    return {'rows': rows}
