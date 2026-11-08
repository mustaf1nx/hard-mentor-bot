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

from .. import clock, repo
from ..config import Settings
from ..db import (FinalReport, LearningMaterial, Mentor, MentorRole, MentorWeeklyReport, MentoringNotification, MentoringSession,
                  SessionBooking, SessionFeedback, SessionFormatChange, Subject, Term, Topic, TopicChecklist, User,
                  conflict_insert)
from ..repo import Viewer

PAGE_SIZE = 8
MAX_ITEMS = 20
MAX_ITEM_LENGTH = 110
MAX_FILE_BYTES = 20 * 1024 * 1024
SCORES = {2: 'Понимаю', 1: 'Есть вопросы', 0: 'Не понимаю'}

ROLE_LABELS = {'lead': 'Ведущий', 'respondent': 'Респондент', 'author': 'Автор'}

# Форматы занятий п. 8.1.2 — подписи для экранов и отчётов.
FORMAT_LABELS = {
    'syllabus_clinic': 'Syllabus Clinic',
    'how_to_start': 'How to start assignment',
    'weekly_errors': 'Ошибки недели',
    'emergency_hours': 'Academic Emergency Hours',
    'intensive': 'Групповой интенсив',
    'one_on_one': 'Консультация 1-на-1',
    'async_answers': 'Асинхронные ответы',
    'mock_review': 'Разбор Mock-экзамена',
    'cheat_sheet': 'Cheat Sheets & Calculator',
}

# Норматив плотности слотов по неделям (п. 8.1.5).
WEEK_PLAN = {3: 3, 5: 10, 7: 4, 8: 4, 9: 8, 10: 12}
DEFAULT_WEEK_PLAN = 4
PROGRESSED_LABELS = {'yes': 'Да, теперь знаю как', 'partial': 'Частично, остались вопросы',
                     'no': 'Нет, ясности не прибавилось'}
HONESTY_LABELS = {'ok': 'Помогал понять метод', 'violation': 'Пытался дать готовое решение'}

# Пиковые недели: правило «меньше двух записей → отмена» не действует (п. 8.1.3).
PEAK_WEEKS = (5, 9, 10)

# Формулировки из п. 8.1.4, которые отклоняются автоматически.
_OFFLOAD = (
    re.compile(r'\b(сделай|сделайте|реши|решите|выполни|выполните)\s+(за меня|мне)\b', re.I),
    re.compile(r'объясни(те)?\s+вс[её]\s+с\s+нуля', re.I),
    re.compile(r'\b(скинь|скиньте|дай|дайте)\s+(готов(ое|ые)|решени)', re.I),
)


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
    if value <= (now or clock.now()) + timedelta(minutes=5):
        raise RuleError('Начало должно быть больше чем через 5 минут.')
    return value


def local_time(value: datetime, settings: Settings) -> str:
    return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.%Y %H:%M')


def validate_file(name: str | None, size: int | None, mime: str | None) -> str:
    name = PurePosixPath((name or '').replace('\\', '/')).name
    suffix = PurePosixPath(name).suffix.lower()
    expected = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    if suffix not in expected:
        shown = name or 'файл без имени'
        raise RuleError(f'Файл «{shown[:80]}» не поддерживается. Принимаются PDF, DOCX или фотография заметок. Если это презентация, экспортируйте её в PDF.')
    if size is not None and (size <= 0 or size > MAX_FILE_BYTES):
        raise RuleError('Размер документа должен быть от 1 байта до 20 МиБ.')
    if mime and mime not in (expected[suffix], 'application/octet-stream', 'application/zip' if suffix == '.docx' else expected[suffix]):
        raise RuleError('Тип документа не соответствует расширению PDF/DOCX.')
    return name[:200]


def validate_photo(name: str | None, size: int | None, mime: str | None) -> str:
    """Фото заметок: Telegram уже сжимает его в JPEG; содержание не анализируем."""
    if size is not None and (size <= 0 or size > MAX_FILE_BYTES):
        raise RuleError('Размер фотографии должен быть от 1 байта до 20 МиБ.')
    if mime and not mime.startswith('image/'):
        raise RuleError('Это не фотография. Пришлите фото, PDF или DOCX.')
    clean = PurePosixPath((name or '').replace('\\', '/')).name
    if not clean:
        clean = 'photo.jpg'
    return clean[:200]


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
    if lesson.status != 'scheduled' or lesson.starts_at <= (now or clock.now()):
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
                         creation_key: str, format: str | None = None,
                         now: datetime | None = None) -> MentoringSession:
    require_admin(viewer)
    if format is not None and format not in FORMAT_LABELS:
        raise RuleError('Неизвестный формат занятия.')
    existing = await s.scalar(select(MentoringSession).where(MentoringSession.creation_key == creation_key))
    if existing:
        if existing.created_by != viewer.id:
            raise RuleError('Недействительный черновик.')
        return existing
    now = now or clock.now()
    if starts_at <= now + timedelta(minutes=5) or not 15 <= duration <= 360:
        raise RuleError('Начало — позже чем через 5 минут, длительность — от 15 до 360 минут.')
    if not await repo.is_mentor(s, mentor_id):
        raise RuleError('Ментор больше не в списке. Сначала добавьте его через /addmentor.')
    await topic_names(s, topic_id)
    term = await active_term(s, settings, starts_at)
    lesson = MentoringSession(creation_key=creation_key, topic_id=topic_id, mentor_id=mentor_id,
        created_by=viewer.id, starts_at=starts_at, ends_at=starts_at + timedelta(minutes=duration),
        location=clean_name(location, 300), status='scheduled', checklist_items=[], revision=1,
        format=format or 'one_on_one', format_confirmed=True if format is not None else None,
        term_id=term.id if term else None)
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


def offload_reason(*texts: str) -> str | None:
    """Формулировки, которые п. 8.1.4 велит отклонять автоматически."""
    joined = ' '.join(t for t in texts if t)
    for pattern in _OFFLOAD:
        match = pattern.search(joined)
        if match:
            return match.group(0)
    return None


def validate_tried(text: str) -> str:
    """«Что вы уже предприняли самостоятельно» (Прил. 5, п. 6)."""
    tried = ' '.join((text or '').split())
    if tried.casefold() in {'', '-', '—', '.', 'нет', 'ничего', 'не знаю', 'не пробовал', 'не пробовала'}:
        raise RuleError('Опишите, что уже пробовали. «Ничего» и «не знаю» не принимаются: '
                        'что прочитали, что написали, где именно застряли.')
    if len(tried) < 30:
        raise RuleError('Слишком коротко: минимум 30 символов о том, что уже пробовали.')
    if len(tried) > 1500:
        raise RuleError('Описание попыток — до 1500 символов.')
    return tried


def validate_ticket(tried: str, self_rating: int | None) -> tuple[str, int]:
    """Входной билет по п. 8.1.4 / Прил. 5: что пробовал сам и самооценка 1–5."""
    tried = validate_tried(tried)
    if self_rating not in (1, 2, 3, 4, 5):
        raise RuleError('Выберите самооценку понимания темы от 1 до 5.')
    return tried, int(self_rating)


def require_entry_window(lesson: MentoringSession, now: datetime | None = None) -> None:
    """Запись на слот закрывается за 24 часа (п. 8.1.4)."""
    moment = now or clock.now()
    if lesson.starts_at - moment <= timedelta(hours=24):
        raise RuleError('Запись закрыта: до начала меньше 24 часов. Входной билет подаётся '
                        'не позднее чем за сутки до занятия (п. 8.1.4).')


async def one_on_one_taken(s: AsyncSession, lesson: MentoringSession, *,
                           exclude_student_id: int | None = None) -> bool:
    """Подтверждённая консультация 1-на-1 рассчитана на одного студента.

    Неподтверждённый формат не ограничиваем: мы не знаем, 1-на-1 это или группа.
    """
    if lesson.format_confirmed is not True or lesson.format != 'one_on_one':
        return False
    query = select(func.count()).select_from(SessionBooking).where(
        SessionBooking.session_id == lesson.id, SessionBooking.status == 'active')
    if exclude_student_id is not None:
        query = query.where(SessionBooking.student_id != exclude_student_id)
    return int(await s.scalar(query) or 0) >= 1


async def register(s: AsyncSession, viewer: Viewer, sid: int, question: str,
                   now: datetime | None = None, *, tried: str = "",
                   self_rating: int | None = None) -> tuple[MentoringSession, SessionBooking]:
    now = now or clock.now()
    lesson = await get_session(s, sid, lock=True)
    require_open(lesson, now)
    if lesson.mentor_id == viewer.id:
        raise RuleError('Вы ведёте это занятие; записываться участником не нужно.')
    if not await repo.is_mentor(s, lesson.mentor_id):
        raise RuleError('Назначенный ментор больше не в списке. Координатор должен назначить другого.')
    if await one_on_one_taken(s, lesson, exclude_student_id=viewer.id):
        raise RuleError('Консультация 1-на-1 рассчитана на одного студента. '
                        'На это занятие уже записался студент; выберите другое.')
    question = question.strip()
    if not question or len(question) > 1500:
        raise RuleError('Напишите вопрос длиной до 1500 символов или «-», если вопросов пока нет.')
    if len(question) < 20:
        raise RuleError('В чём затруднение — минимум 20 символов: тема и что именно не получается.')
    bad = offload_reason(question, tried)
    if bad:
        raise RuleError(f'«{bad}» не принимается: опишите конкретное затруднение и свои попытки. '
                        'Заявки вида «объясните всё с нуля» и «сделайте за меня» отклоняются (п. 8.1.4).')
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
    if tried or self_rating is not None:
        booking.tried, booking.self_rating = validate_ticket(tried, self_rating)
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
    if lesson.status == 'cancelled' or lesson.starts_at > (now or clock.now()):
        raise RuleError('Посещаемость отмечается после начала неотменённого занятия.')
    if booking.status != 'active' or value not in ('present', 'absent', 'unknown'):
        raise RuleError('Недопустимая отметка посещаемости.')
    booking.attendance = value
    return lesson


async def complete_session(s: AsyncSession, viewer: Viewer, sid: int,
                           now: datetime | None = None) -> MentoringSession:
    now = now or clock.now()
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
    return await apply_cancel(s, lesson, reason, now)


async def apply_cancel(s: AsyncSession, lesson: MentoringSession, reason: str,
                       now: datetime | None = None) -> MentoringSession:
    """Единая отмена: вручную координатором и автоматически по правилу 24 часов."""
    if lesson.status == 'completed':
        raise RuleError('Проведённое занятие нельзя отменить.')
    if lesson.status == 'cancelled':
        return lesson
    reason = clean_name(reason, 400)
    lesson.status, lesson.cancel_reason = 'cancelled', reason
    lesson.revision += 1
    for uid in {lesson.mentor_id} | {b.student_id for b in await active_bookings(s, lesson.id)}:
        await enqueue(s, lesson, uid, 'cancelled', now or clock.now())
    return lesson


async def edit_session(s: AsyncSession, viewer: Viewer, sid: int, field: str, value,
                       now: datetime | None = None) -> MentoringSession:
    require_admin(viewer)
    now = now or clock.now()
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
                       session_id: int | None = None, cycle_stage: str | None = None,
                       media_type: str = 'document') -> LearningMaterial:
    require_staff(viewer)
    if media_type not in ('document', 'photo'):
        raise RuleError('Неизвестный тип материала.')
    if media_type == 'photo':
        file_name = validate_photo(file_name, file_size, mime)
    else:
        file_name = validate_file(file_name, file_size, mime)
    if kind not in ('recap', 'material') or visibility not in ('students', 'staff'):
        raise RuleError('Выберите тип материала и доступ кнопками.')
    if (session_id and cycle_stage not in ('preparation', 'final')) or (not session_id and cycle_stage is not None):
        raise RuleError('Выберите назначение материала занятия.')
    if session_id:
        lesson = await get_session(s, session_id, lock=True)
        if not (viewer.is_admin or lesson.author_id == viewer.id):
            raise RuleError('Загружать материалы занятия может назначенный Автор или координатор.')
        if lesson.status == 'cancelled':
            raise RuleError('Материалы нельзя прикрепить к отменённому занятию. Добавьте их в банк отдельно.')
        topic_id = lesson.topic_id
    await topic_names(s, topic_id)
    existing = await s.scalar(select(LearningMaterial).where(
        LearningMaterial.topic_id == topic_id, LearningMaterial.session_id == session_id,
        LearningMaterial.file_unique_id == file_unique_id, LearningMaterial.kind == kind,
        LearningMaterial.visibility == visibility, LearningMaterial.cycle_stage == cycle_stage,
        LearningMaterial.media_type == media_type, LearningMaterial.review_status == 'pending',
        LearningMaterial.archived.is_(False)))
    if existing:
        return existing
    material = LearningMaterial(topic_id=topic_id, session_id=session_id, author_id=viewer.id,
        kind=kind, visibility=visibility, file_id=file_id, file_unique_id=file_unique_id,
        file_name=file_name, media_type=media_type, cycle_stage=cycle_stage,
        review_status='pending')
    s.add(material)
    await s.flush()
    return material


async def material_for(s: AsyncSession, viewer: Viewer, material_id: int) -> LearningMaterial:
    material = await s.get(LearningMaterial, material_id)
    if material is None or material.archived or (not viewer.is_staff and
            (material.visibility == 'staff' or material.review_status == 'pending')):
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
        query = query.where(LearningMaterial.visibility == 'students',
                            or_(LearningMaterial.review_status.is_(None), LearningMaterial.review_status == 'approved'))
    if recap_only:
        # Automatic preparation MUST never include team-only documents, even for staff acting as a student.
        query = query.where(LearningMaterial.kind == 'recap', LearningMaterial.visibility == 'students',
                            or_(LearningMaterial.review_status.is_(None), LearningMaterial.review_status == 'approved'))
    return list(await s.scalars(query.order_by(LearningMaterial.id.desc())))


async def assign_author(s: AsyncSession, viewer: Viewer, sid: int, author_id: int) -> None:
    require_admin(viewer)
    lesson = await get_session(s, sid, lock=True)
    if not await repo.is_mentor(s, author_id):
        raise RuleError('Автор должен быть добавлен как ментор.')
    lesson.author_id = author_id


async def confirm_format(s: AsyncSession, viewer: Viewer, sid: int, fmt: str) -> MentoringSession:
    """Фиксирует фактический формат будущего занятия.

    Для прошедшего занятия используйте `confirm_past_format`: это отдельное
    действие с отдельной записью в истории.
    """
    require_admin(viewer)
    if fmt not in FORMAT_LABELS:
        raise RuleError('Неизвестный формат занятия.')
    lesson = await get_session(s, sid, lock=True)
    if lesson.status != 'scheduled':
        raise RuleError('Формат прошедшего или отменённого занятия не переписывается.')
    _record_format_change(s, lesson, viewer, fmt)
    return lesson


async def confirm_past_format(s: AsyncSession, viewer: Viewer, sid: int, fmt: str,
                              now: datetime | None = None) -> MentoringSession:
    """Администратор явно указывает формат уже проведённого занятия.

    Это не автоподстановка: без явного выбора формат остаётся неизвестным.
    Отменённые занятия не исправляются — это отдельное решение владельца.
    """
    require_admin(viewer)
    if fmt not in FORMAT_LABELS:
        raise RuleError('Неизвестный формат занятия.')
    lesson = await get_session(s, sid, lock=True)
    if lesson.status == 'cancelled':
        raise RuleError('Формат отменённого занятия не исправляется.')
    if lesson.status != 'completed':
        raise RuleError('Это действие только для проведённого занятия.')
    if lesson.format_confirmed is True:
        raise RuleError('Формат уже подтверждён; повторно не переписываем.')
    _record_format_change(s, lesson, viewer, fmt, now=now)
    return lesson


def _record_format_change(s: AsyncSession, lesson: MentoringSession, viewer: Viewer,
                          fmt: str, now: datetime | None = None) -> None:
    """Сохраняет старое значение и автора до изменения самого занятия."""
    s.add(SessionFormatChange(
        session_id=lesson.id, old_format=lesson.format, old_confirmed=lesson.format_confirmed,
        new_format=fmt, changed_by=viewer.id, changed_at=now or clock.now()))
    lesson.format = fmt
    lesson.format_confirmed = True


def has_written_feedback(feedback: SessionFeedback) -> bool:
    """Письменный отзыв — непустой текст, а не только оценки.

    Пустые строки и пробелы текстом не считаются. Комментарий другого занятия
    сюда не попадает: проверка вызывается для конкретной записи SessionFeedback.
    """
    return bool((feedback.best or '').strip() or (feedback.next_topics or '').strip())


async def record_material_review(s: AsyncSession, viewer: Viewer, material_id: int,
                                 lecturer_name: str, reviewed_at: datetime) -> None:
    require_admin(viewer)
    material = await s.get(LearningMaterial, material_id, with_for_update=True)
    if material is None or material.archived or material.review_status != 'pending':
        raise RuleError('Материал недоступен или уже проверен.')
    lecturer_name = clean_name(lecturer_name, 200)
    if reviewed_at > clock.now():
        raise RuleError('Дата проверки не может быть в будущем.')
    material.lecturer_name = lecturer_name
    material.reviewed_at = reviewed_at
    material.review_recorded_by = viewer.id
    material.review_status = 'approved'


async def session_list(s: AsyncSession, viewer: Viewer, mode: str, topic_id: int = 0,
                       now: datetime | None = None) -> list[MentoringSession]:
    query = select(MentoringSession)
    if mode == 'all':
        require_admin(viewer)
    elif mode == 'teaching':
        require_staff(viewer)
        query = query.where(or_(MentoringSession.mentor_id == viewer.id, MentoringSession.author_id == viewer.id))
    elif mode == 'bookings':
        query = query.join(SessionBooking).where(SessionBooking.student_id == viewer.id, SessionBooking.status == 'active')
    else:
        query = query.where(MentoringSession.status == 'scheduled', MentoringSession.starts_at > (now or clock.now()))
    if topic_id:
        query = query.where(MentoringSession.topic_id == topic_id)
    return list(await s.scalars(query.order_by(MentoringSession.starts_at.desc() if mode in ('all','teaching','bookings') else MentoringSession.starts_at, MentoringSession.id)))


def report_week_key(settings: Settings, shift: int = 0, now: datetime | None = None) -> str:
    """Monday of the requested local week as YYYY-MM-DD. shift=-1 is previous week."""
    base = (now or clock.now()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
    monday = (base - timedelta(days=base.weekday()) + timedelta(weeks=shift)).date()
    return monday.isoformat()


def report_week_bounds(week_start: str, settings: Settings) -> tuple[datetime, datetime]:
    try:
        day = datetime.strptime(week_start, '%Y-%m-%d')
    except ValueError as exc:
        raise RuleError('Некорректная неделя отчёта.') from exc
    local_start = day.replace(tzinfo=ZoneInfo(settings.timezone))
    start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=7)


def report_week_label(week_start: str) -> str:
    start = datetime.strptime(week_start, '%Y-%m-%d')
    end = start + timedelta(days=6)
    return f'{start:%d.%m.%Y}–{end:%d.%m.%Y}'


async def save_mentor_report(s: AsyncSession, viewer: Viewer, week_start: str, content: str) -> MentorWeeklyReport:
    if not await repo.is_mentor(s, viewer.id):
        raise RuleError('Сдавать отчёт может пользователь, добавленный в список менторов.')
    content = content.strip()
    if not 10 <= len(content) <= 3500:
        raise RuleError('Отчёт должен быть от 10 до 3500 символов.')
    existing = await s.scalar(select(MentorWeeklyReport).where(
        MentorWeeklyReport.mentor_id == viewer.id, MentorWeeklyReport.week_start == week_start
    ).with_for_update())
    if existing:
        existing.content = content
        existing.updated_at = clock.now()
        return existing
    report = MentorWeeklyReport(mentor_id=viewer.id, week_start=week_start, content=content,
                                created_at=clock.now(), updated_at=clock.now())
    s.add(report)
    await s.flush()
    return report


async def save_mentor_report_structured(s: AsyncSession, viewer: Viewer, week_start: str,
                                        insights: str, focus: str) -> MentorWeeklyReport:
    """Еженедельный отчёт по п. 9.3: цифры считает бот, ментор пишет смысл.

    Поле «что провёл» бот уже показал цифрами, поэтому руками — только инсайты и фокус.
    """
    if not await repo.is_mentor(s, viewer.id):
        raise RuleError('Сдавать отчёт может пользователь, добавленный в список менторов.')
    insights, focus = ' '.join(insights.split()), ' '.join(focus.split())
    if len(insights) < 20:
        raise RuleError('«С чем именно было трудно» — минимум 20 символов. Опишите, где спотыкались студенты.')
    if len(focus) < 10:
        raise RuleError('«Фокус на следующую неделю» — минимум 10 символов.')
    if len(insights) > 2000 or len(focus) > 1000:
        raise RuleError('Инсайты — до 2000 символов, фокус — до 1000.')
    content = f'С чем трудно: {insights}\nФокус на неделю: {focus}'
    existing = await s.scalar(select(MentorWeeklyReport).where(
        MentorWeeklyReport.mentor_id == viewer.id, MentorWeeklyReport.week_start == week_start
    ).with_for_update())
    if existing:
        existing.content, existing.insights, existing.focus = content, insights, focus
        existing.updated_at = clock.now()
        return existing
    report = MentorWeeklyReport(mentor_id=viewer.id, week_start=week_start, content=content,
                                insights=insights, focus=focus, created_at=clock.now(), updated_at=clock.now())
    s.add(report)
    await s.flush()
    return report


async def mentor_report_list(s: AsyncSession, viewer: Viewer, *, week_start: str | None = None) -> list[MentorWeeklyReport]:
    require_admin(viewer)
    q = select(MentorWeeklyReport)
    if week_start:
        q = q.where(MentorWeeklyReport.week_start == week_start)
    return list(await s.scalars(q.order_by(MentorWeeklyReport.week_start.desc(), MentorWeeklyReport.updated_at.desc(), MentorWeeklyReport.id.desc())))


async def missing_weekly_reports(s: AsyncSession, week_start: str) -> list[int]:
    """Менторы, не сдавшие отчёт за неделю (п. 9.2, вс 23:59)."""
    mentors = set(await s.scalars(select(Mentor.telegram_id)))
    done = set(await s.scalars(select(MentorWeeklyReport.mentor_id).where(
        MentorWeeklyReport.week_start == week_start)))
    return sorted(mentors - done)


# -------------------------------------------- итоговый отчёт (Прил. 4)

def final_report_deadline(term: Term, settings: Settings) -> datetime:
    """За 3 дня до конца триместра (Прил. 4)."""
    _, end = term_bounds(term, settings)
    return end - timedelta(days=3)


async def final_stats(s: AsyncSession, mentor_id: int, term: Term, settings: Settings) -> dict:
    """Цифры для Прил. 4 — считает бот, ментор их не переписывает."""
    start, end = term_bounds(term, settings)
    lessons = list(await s.scalars(select(MentoringSession).where(
        MentoringSession.mentor_id == mentor_id,
        MentoringSession.starts_at >= start, MentoringSession.starts_at < end)))
    ids = [x.id for x in lessons]
    students = 0
    if ids:
        students = len(set(await s.scalars(select(SessionBooking.student_id).where(
            SessionBooking.session_id.in_(ids), SessionBooking.status == 'active'))))
    materials = await s.scalar(select(func.count()).select_from(LearningMaterial).where(
        LearningMaterial.author_id == mentor_id, LearningMaterial.archived.is_(False))) or 0
    return {
        'slots': sum(x.status == 'completed' for x in lessons),
        'scheduled': sum(x.status == 'scheduled' for x in lessons),
        'cancelled': sum(x.status == 'cancelled' for x in lessons),
        'materials': int(materials),
        'audience_total': students,
    }


async def save_final_report(s: AsyncSession, viewer: Viewer, term: Term, settings: Settings, *,
                            role: str | None, summary: str, difficulties: str, proposals: str,
                            now: datetime | None = None) -> FinalReport:
    if not await repo.is_mentor(s, viewer.id):
        raise RuleError('Итоговый отчёт сдаёт ментор из реестра /mentors.')
    summary = ' '.join(summary.split())
    difficulties = ' '.join(difficulties.split())
    proposals = ' '.join(proposals.split())
    if len(summary) < 40:
        raise RuleError('Резюме — минимум 40 символов (Прил. 4: 4–6 предложений о динамике).')
    if len(difficulties) < 10:
        raise RuleError('«Сложности и барьеры» — минимум 10 символов.')
    if len(proposals) < 10:
        raise RuleError('«Предложения по улучшению» — минимум 10 символов.')
    if max(len(summary), len(difficulties), len(proposals)) > 3000:
        raise RuleError('Каждое поле — до 3000 символов.')
    stats = await final_stats(s, viewer.id, term, settings)
    moment = now or clock.now()
    existing = await s.scalar(select(FinalReport).where(
        FinalReport.mentor_id == viewer.id, FinalReport.term_id == term.id).with_for_update())
    values = dict(direction='hard', role=role, slots=stats['slots'], materials=stats['materials'],
                  audience_total=stats['audience_total'], audience_first=stats['audience_total'],
                  summary=summary, difficulties=difficulties, proposals=proposals, updated_at=moment)
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        return existing
    report = FinalReport(mentor_id=viewer.id, term_id=term.id, created_at=moment, **values)
    s.add(report)
    await s.flush()
    return report


async def final_report_for(s: AsyncSession, mentor_id: int, term_id: int) -> FinalReport | None:
    return await s.scalar(select(FinalReport).where(
        FinalReport.mentor_id == mentor_id, FinalReport.term_id == term_id))


async def final_reports_overview(s: AsyncSession, term: Term, settings: Settings) -> dict:
    mentors = set(await s.scalars(select(Mentor.telegram_id)))
    reports = list(await s.scalars(select(FinalReport).where(FinalReport.term_id == term.id)))
    done = {r.mentor_id: r for r in reports}
    names: dict[int, str] = {}
    everyone = mentors | set(done)
    if everyone:
        for user in await s.scalars(select(User).where(User.telegram_id.in_(everyone))):
            names[user.telegram_id] = f"@{user.username}" if user.username else user.display_name
    deadline = final_report_deadline(term, settings)
    return {
        'deadline': deadline,
        'submitted': [(names.get(uid, str(uid)), report) for uid, report in sorted(done.items())],
        'missing': [names.get(uid, str(uid)) for uid in sorted(mentors - set(done))],
        'late': [names.get(uid, str(uid)) for uid, report in done.items() if report.updated_at > deadline],
    }


async def mentor_week_stats(s: AsyncSession, mentor_id: int, week_start: str, settings: Settings) -> dict:
    start, end = report_week_bounds(week_start, settings)
    lessons = list(await s.scalars(select(MentoringSession).where(
        MentoringSession.mentor_id == mentor_id, MentoringSession.starts_at >= start, MentoringSession.starts_at < end
    ).order_by(MentoringSession.starts_at)))
    ids = [x.id for x in lessons]
    bookings = []
    material_sids: set[int] = set()
    if ids:
        bookings = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id.in_(ids), SessionBooking.status == 'active')))
        material_sids = set(await s.scalars(select(LearningMaterial.session_id).where(
            LearningMaterial.session_id.in_(ids), LearningMaterial.archived.is_(False), LearningMaterial.session_id.is_not(None))))
    by_sid: dict[int, list[SessionBooking]] = {}
    for b in bookings:
        by_sid.setdefault(b.session_id, []).append(b)
    completed = [x for x in lessons if x.status == 'completed']
    completed_ids = {x.id for x in completed}
    completed_bookings = [b for b in bookings if b.session_id in completed_ids]
    present = sum(b.attendance == 'present' for b in completed_bookings)
    absent = sum(b.attendance == 'absent' for b in completed_bookings)
    unknown = sum(b.attendance == 'unknown' for b in completed_bookings)
    return {
        'lessons': lessons,
        'completed': len(completed),
        'cancelled': sum(x.status == 'cancelled' for x in lessons),
        'scheduled': sum(x.status == 'scheduled' for x in lessons),
        'bookings': len(bookings),
        'present': present,
        'absent': absent,
        'unknown': unknown,
        'with_materials': sum(x.id in material_sids for x in completed),
        'completed_without_materials': sum(x.id not in material_sids for x in completed),
    }


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


# ------------------------------------------------------------------ триместры

def term_week(term: Term, moment: datetime) -> int | None:
    """Номер недели триместра для момента времени, либо None вне триместра."""
    try:
        start = datetime.strptime(term.starts_on, '%Y-%m-%d').date()
    except ValueError:
        return None
    week = (moment.date() - start).days // 7 + 1
    return week if 1 <= week <= term.weeks else None


def is_peak_week(week: int | None) -> bool:
    return week in PEAK_WEEKS


def _local(moment: datetime, settings: Settings) -> datetime:
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))


async def active_term(s: AsyncSession, settings: Settings, now: datetime | None = None) -> Term | None:
    moment = _local(now or clock.now(), settings)
    for term in await s.scalars(select(Term).where(Term.active.is_(True)).order_by(Term.starts_on.desc())):
        if term_week(term, moment) is not None:
            return term
    return None


async def week_now(s: AsyncSession, settings: Settings,
                   now: datetime | None = None) -> tuple[Term | None, int | None]:
    term = await active_term(s, settings, now)
    if term is None:
        return None, None
    return term, term_week(term, _local(now or clock.now(), settings))


def term_bounds(term: Term, settings: Settings) -> tuple[datetime, datetime]:
    start_local = datetime.strptime(term.starts_on, '%Y-%m-%d').replace(tzinfo=ZoneInfo(settings.timezone))
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(weeks=term.weeks)


async def clinic_coverage(s: AsyncSession, settings: Settings, term: Term | None) -> dict:
    """Охват Syllabus Clinic (п. 11.3): уникальные присутствовавшие / поток.

    students и percent отражают явку; registered — активные записи. Студент,
    посетивший несколько занятий, считается один раз. Отменённые занятия и
    отозванные записи не учитываются. Без триместра или размера потока доля неизвестна.
    """
    if term is None:
        return {'sessions': 0, 'students': 0, 'registered': 0, 'marked': 0,
                'cohort': 0, 'percent': None}
    start, end = term_bounds(term, settings)
    ids = list(await s.scalars(select(MentoringSession.id).where(
        MentoringSession.format == 'syllabus_clinic', MentoringSession.format_confirmed.is_(True),
        MentoringSession.status != 'cancelled',
        MentoringSession.starts_at >= start, MentoringSession.starts_at < end)))
    students = registered = marked = 0
    if ids:
        bookings = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id.in_(ids), SessionBooking.status == 'active')))
        registered = len({booking.student_id for booking in bookings})
        marked = len({booking.student_id for booking in bookings if booking.attendance in ('present', 'absent')})
        students = len({booking.student_id for booking in bookings if booking.attendance == 'present'})
    cohort = term.cohort or 0
    return {'sessions': len(ids), 'students': students, 'registered': registered, 'marked': marked,
            'cohort': cohort,
            'percent': round(100 * students / cohort, 1) if cohort else None}


def week_plan(week: int | None) -> int:
    """Норматив слотов на неделю по п. 8.1.5."""
    if week is None:
        return DEFAULT_WEEK_PLAN
    return WEEK_PLAN.get(week, DEFAULT_WEEK_PLAN)


# --------------------------------------------- отмена по правилу 24 часов

async def low_demand_candidates(s: AsyncSession, settings: Settings,
                                now: datetime | None = None) -> list[tuple[MentoringSession, int, bool | None]]:
    """Слоты в ближайшие 24 часа: (занятие, активных записей, действует ли исключение).

    `None` в третьем поле — триместр не задан, и тогда автоматическая отмена
    не выполняется: лучше не отменить ничего, чем снести пиковую неделю.
    """
    moment = now or clock.now()
    lessons = list(await s.scalars(select(MentoringSession).where(
        MentoringSession.status == 'scheduled',
        MentoringSession.starts_at > moment,
        MentoringSession.starts_at <= moment + timedelta(hours=24),
    ).order_by(MentoringSession.starts_at)))
    out = []
    for lesson in lessons:
        count = await s.scalar(select(func.count()).select_from(SessionBooking).where(
            SessionBooking.session_id == lesson.id, SessionBooking.status == 'active'))
        term = await active_term(s, settings, lesson.starts_at)
        week = term_week(term, _local(lesson.starts_at, settings)) if term else None
        out.append((lesson, int(count or 0), None if term is None else is_peak_week(week)))
    return out


def minimum_bookings(lesson: MentoringSession) -> int:
    """Минимум записей, при котором слот сохраняется.

    Для подтверждённого 1-на-1 достаточно одного студента. Неподтверждённый
    формат не считается 1-на-1: миграция 0006 могла проставить старым записям
    `one_on_one` без фактического решения (уточнение владельца 7.11.2026).
    """
    if lesson.format_confirmed is True and lesson.format == 'one_on_one':
        return 1
    return 2


async def autocancel_low_demand(s: AsyncSession, settings: Settings,
                                now: datetime | None = None) -> list[MentoringSession]:
    """Отменяет слоты с низким спросом с учётом формата и пиковых недель.

    Если формат не подтверждён, порог не применяется: единственный бесспорный
    случай — полное отсутствие записей. Иначе групповой слот с одной записью
    мог бы отмениться, а 1-на-1 — остаться без предупреждения.
    """
    cancelled = []
    for lesson, count, exempt in await low_demand_candidates(s, settings, now):
        if exempt is not False:
            continue
        if lesson.format_confirmed is True:
            if count >= minimum_bookings(lesson):
                continue
        elif count != 0:
            continue
        reason = ('Автоматически: нет записей за 24 часа до начала.'
                  if count == 0 else
                  'Автоматически: меньше двух записей за 24 часа до начала (п. 8.1.3).')
        await apply_cancel(s, lesson, reason, now)
        cancelled.append(lesson)
    return cancelled


# ------------------------------------------------------------- роли менторов

async def set_mentor_role(s: AsyncSession, viewer: Viewer, mentor_id: int, subject_id: int,
                          term_id: int, role: str) -> MentorRole:
    require_admin(viewer)
    if role not in ROLE_LABELS:
        raise RuleError('Роль: lead (Ведущий), respondent (Респондент) или author (Автор).')
    if not await repo.is_mentor(s, mentor_id):
        raise RuleError('Сначала добавьте ментора через /addmentor.')
    if await s.get(Subject, subject_id) is None:
        raise RuleError('Предмет не найден.')
    ins = conflict_insert(MentorRole.__table__, s.get_bind().dialect.name)
    await s.execute(ins.values(telegram_id=mentor_id, subject_id=subject_id, term_id=term_id,
                               role=role, created_at=clock.now()).on_conflict_do_update(
        index_elements=['telegram_id', 'subject_id', 'term_id'], set_={'role': role}))
    return await s.scalar(select(MentorRole).where(
        MentorRole.telegram_id == mentor_id, MentorRole.subject_id == subject_id,
        MentorRole.term_id == term_id))


async def roles_of(s: AsyncSession, mentor_id: int, term_id: int | None = None) -> list[tuple[str, str]]:
    """[(предмет, роль)] — роль привязана к предмету, у человека их может быть несколько."""
    query = (select(Subject.name, MentorRole.role).join(Subject, Subject.id == MentorRole.subject_id)
             .where(MentorRole.telegram_id == mentor_id))
    if term_id is not None:
        query = query.where(MentorRole.term_id == term_id)
    return [tuple(row) for row in (await s.execute(query.order_by(Subject.name_key))).all()]


async def create_term(s: AsyncSession, viewer: Viewer, name: str, starts_on: str, weeks: int,
                      cohort: int = 2500) -> Term:
    require_admin(viewer)
    name = clean_name(name, 60)
    try:
        start = datetime.strptime(starts_on, '%Y-%m-%d').date()
    except ValueError as exc:
        raise RuleError('Дата старта — в формате ГГГГ-ММ-ДД, например 2026-09-07.') from exc
    if start.weekday() != 0:
        raise RuleError('Дата старта должна быть понедельником первой недели триместра.')
    if not 1 <= weeks <= 30:
        raise RuleError('Число недель — от 1 до 30.')
    if not 0 <= cohort <= 100000:
        raise RuleError('Размер потока — от 0 до 100000.')
    existing = await s.scalar(select(Term).where(Term.name == name))
    if existing:
        existing.starts_on, existing.weeks, existing.active = starts_on, weeks, True
        existing.cohort = cohort
        return existing
    term = Term(name=name, starts_on=starts_on, weeks=weeks, active=True, cohort=cohort)
    s.add(term)
    await s.flush()
    return term


# ------------------------------------------- форма ОС после занятия (Прил. 11)

async def save_feedback(s: AsyncSession, viewer: Viewer, sid: int, *, clarity: int, progressed: str,
                        honesty: str, overall: int, best: str = '', next_topics: str = '',
                        now: datetime | None = None) -> SessionFeedback:
    """Студент-участник оценивает прошедшее занятие. Повторная отправка перезаписывает."""
    moment = now or clock.now()
    lesson = await get_session(s, sid, lock=True)
    if lesson.status == 'cancelled':
        raise RuleError('Занятие отменено — обратную связь оставить нельзя.')
    if lesson.ends_at > moment:
        raise RuleError('Обратная связь доступна после окончания занятия.')
    if clarity not in (1, 2, 3, 4, 5) or overall not in (1, 2, 3, 4, 5):
        raise RuleError('Оценка — от 1 до 5.')
    if progressed not in PROGRESSED_LABELS or honesty not in HONESTY_LABELS:
        raise RuleError('Выберите один из предложенных вариантов.')
    booking = await own_booking(s, sid, viewer.id)
    if booking is None or booking.status != 'active':
        raise RuleError('Обратную связь оставляет участник этого занятия.')
    best, next_topics = best.strip()[:500], next_topics.strip()[:500]
    existing = await s.scalar(select(SessionFeedback).where(
        SessionFeedback.session_id == sid, SessionFeedback.student_id == viewer.id))
    if existing:
        existing.clarity, existing.progressed, existing.honesty = clarity, progressed, honesty
        existing.overall, existing.best, existing.next_topics = overall, best, next_topics
        return existing
    feedback = SessionFeedback(session_id=sid, mentor_id=lesson.mentor_id, student_id=viewer.id,
                               clarity=clarity, progressed=progressed, honesty=honesty, overall=overall,
                               best=best, next_topics=next_topics, created_at=moment)
    s.add(feedback)
    await s.flush()
    return feedback


async def feedback_for_session(s: AsyncSession, sid: int) -> list[SessionFeedback]:
    return list(await s.scalars(select(SessionFeedback).where(SessionFeedback.session_id == sid)
                                .order_by(SessionFeedback.id)))


async def feedback_summary(s: AsyncSession, *, mentor_id: int | None = None,
                           since: datetime | None = None, until: datetime | None = None) -> dict:
    query = select(SessionFeedback)
    if mentor_id is not None:
        query = query.where(SessionFeedback.mentor_id == mentor_id)
    if since is not None:
        query = query.where(SessionFeedback.created_at >= since)
    if until is not None:
        query = query.where(SessionFeedback.created_at < until)
    rows = list(await s.scalars(query))
    if not rows:
        return {'count': 0, 'written': 0, 'availability': None, 'competence': None, 'ethics': None,
                'score': None, 'progressed': {}, 'violations': 0}
    progressed: dict[str, int] = {}
    for row in rows:
        progressed[row.progressed] = progressed.get(row.progressed, 0) + 1
    availability = round(sum(r.availability for r in rows) / len(rows), 2)
    competence = round(sum(r.competence for r in rows) / len(rows), 2)
    ethics = round(sum(r.ethics for r in rows) / len(rows), 2)
    return {
        'count': len(rows),
        'written': sum(has_written_feedback(r) for r in rows),
        'availability': availability,
        'competence': competence,
        'ethics': ethics,
        # Итог — среднее трёх осей (п. 11.2), а не среднее по отзывам.
        'score': round((availability + competence + ethics) / 3, 2),
        'progressed': progressed,
        'violations': sum(r.honesty == 'violation' for r in rows),
    }


# ------------------------------------------------- KPI ментора (раздел 11)

async def mentor_kpi(s: AsyncSession, mentor_id: int, settings: Settings,
                     now: datetime | None = None) -> dict:
    moment = now or clock.now()
    term = await active_term(s, settings, moment)
    query = select(MentoringSession).where(MentoringSession.mentor_id == mentor_id)
    start = end = None
    if term is not None:
        start, end = term_bounds(term, settings)
        query = query.where(MentoringSession.starts_at >= start, MentoringSession.starts_at < end)
    lessons = list(await s.scalars(query.order_by(MentoringSession.starts_at)))
    ids = [x.id for x in lessons]
    bookings = []
    if ids:
        bookings = list(await s.scalars(select(SessionBooking).where(
            SessionBooking.session_id.in_(ids), SessionBooking.status == 'active')))
    completed = [x for x in lessons if x.status == 'completed']
    materials = await s.scalar(select(func.count()).select_from(LearningMaterial).where(
        LearningMaterial.author_id == mentor_id, LearningMaterial.archived.is_(False)))
    feedback = await feedback_summary(s, mentor_id=mentor_id, since=start, until=end)
    reports = list(await s.scalars(select(MentorWeeklyReport).where(MentorWeeklyReport.mentor_id == mentor_id)))
    on_time = sum(r.created_at <= report_deadline(r.week_start, settings) for r in reports)
    return {
        'term': term.name if term else None,
        'lessons': len(lessons),
        'completed': len(completed),
        'cancelled': sum(x.status == 'cancelled' for x in lessons),
        'scheduled': sum(x.status == 'scheduled' for x in lessons),
        'bookings': len(bookings),
        'unique_students': len({b.student_id for b in bookings}),
        'present': sum(b.attendance == 'present' for b in bookings),
        'materials': int(materials or 0),
        'feedback': feedback,
        'reports': len(reports),
        'reports_on_time': on_time,
        'reports_late': len(reports) - on_time,
    }


def kpi_grade(kpi: dict) -> str:
    """Грейд по п. 11.4. «Опорный» требует оценки потока — только как кандидат."""
    score = (kpi['feedback']['score'] or 0)
    if kpi['completed'] >= 5 and kpi['materials'] >= 1:
        return ('Опорный ментор (кандидат): материал используется потоком — проверяет Главный ментор'
                if score >= 4.5 else 'Практикующий ментор')
    return 'Ментор'


def report_deadline(week_start: str, settings: Settings) -> datetime:
    """Дедлайн еженедельного отчёта — 23:59 воскресенья той же недели (п. 9.2)."""
    day = datetime.strptime(week_start, '%Y-%m-%d')
    end_local = day.replace(hour=23, minute=59, tzinfo=ZoneInfo(settings.timezone)) + timedelta(days=6)
    return end_local.astimezone(timezone.utc).replace(tzinfo=None)
