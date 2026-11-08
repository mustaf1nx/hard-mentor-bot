"""Сводка для администрации: общее состояние менторства без чтения таблиц.

Шесть блоков из спецификации: где мы, менторки, цикл по предметам, люди,
обращения, долги. Плюс выгрузка CSV для отчётности наверх.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from . import clock
from .db import (CATEGORIES, LearningMaterial, Mentor, MentorRole, MentorWeeklyReport, MentoringSession,
                 SessionBooking, SessionFeedback, SessionFormatChange, Subject, Ticket, Topic, User, utcnow)
from .mentoring import service as svc
from .mentoring.ui import e

OPEN_STATUSES = ("new", "in_review", "in_progress")
PAGE_SIZE = 6

# Коды для callback_data: у Telegram лимит 64 байта, поэтому блок и период
# кодируются одной буквой, а не словом.
PERIOD_LABELS = {
    'a': 'весь триместр',
    'd': 'сегодня',
    'w': 'текущая неделя',
    'm': '30 дней до/после',
    'p': 'прошедшие',
    'f': 'предстоящие',
}
BLOCK_CODES = {
    'sessions': 's', 'week': 'w', 'debts': 'd', 'attention': 'a',
    'unconfirmed': 'u', 'subject': 'b', 'cycle': 'c', 'overview': 'o',
}
CODE_BLOCKS = {code: block for block, code in BLOCK_CODES.items()}
FILTER_BLOCKS = ('sessions', 'week', 'debts', 'unconfirmed', 'subject')


@dataclass(frozen=True)
class DashContext:
    """Откуда открыт список/карточка и какие фильтры выбраны."""
    block: str = 'sessions'
    page: int = 0
    period: str = 'a'
    subject_id: int = 0
    mentor_id: int = 0

    def suffix(self) -> str:
        return f'{self.period}:{self.subject_id}:{self.mentor_id}'


def _block_code(block: str) -> str:
    return BLOCK_CODES.get(block, 's')


def _anchor(ctx: DashContext) -> int:
    return ctx.subject_id if ctx.block == 'subject' else 0


def list_cb(block: str, ctx: DashContext) -> str:
    """Ссылка на список с сохранением страницы и фильтров."""
    if block == 'subject':
        return f'dash:subject:{ctx.subject_id}:{ctx.page}:{ctx.suffix()}'
    return f'dash:{block}:{ctx.page}:{ctx.suffix()}'


def lesson_cb(lesson_id: int, ctx: DashContext) -> str:
    return f'dash:lesson:{lesson_id}:{_block_code(ctx.block)}:{ctx.page}:{ctx.suffix()}'


def filters_cb(block: str, ctx: DashContext) -> str:
    return f'dash:filters:{_block_code(block)}:{_anchor(ctx)}:{ctx.page}:{ctx.suffix()}'


def reset_cb(block: str, ctx: DashContext) -> str:
    return list_cb(block, replace(ctx, page=0, period='a',
                                  subject_id=ctx.subject_id if block == 'subject' else 0,
                                  mentor_id=0))


def setf_cb(field: str, value: str, block: str, ctx: DashContext) -> str:
    return (f'dash:setf:{field}:{value}:{_block_code(block)}:{_anchor(ctx)}:'
            f'{ctx.page}:{ctx.suffix()}')


def pick_cb(field: str, block: str, ctx: DashContext, picker_page: int = 0) -> str:
    return (f'dash:pick:{field}:{_block_code(block)}:{_anchor(ctx)}:{ctx.page}:'
            f'{ctx.suffix()}:{picker_page}')


def pickv_cb(field: str, value: int, block: str, ctx: DashContext) -> str:
    return (f'dash:pickv:{field}:{value}:{_block_code(block)}:{_anchor(ctx)}:'
            f'{ctx.page}:{ctx.suffix()}')


def confirm_cb(lesson_id: int, ctx: DashContext) -> str:
    return f'dash:confirm:{lesson_id}:{_block_code(ctx.block)}:{ctx.page}:{ctx.suffix()}'


def previewfmt_cb(lesson_id: int, fmt: str, ctx: DashContext) -> str:
    return (f'dash:previewfmt:{lesson_id}:{fmt}:{_block_code(ctx.block)}:{ctx.page}:'
            f'{ctx.suffix()}')


def setfmt_cb(lesson_id: int, fmt: str, ctx: DashContext) -> str:
    return (f'dash:setfmt:{lesson_id}:{fmt}:{_block_code(ctx.block)}:{ctx.page}:'
            f'{ctx.suffix()}')


def ctx_from_parts(parts: list[str], start: int, *, block: str,
                   page: int = 0) -> DashContext:
    """Разбирает хвост callback: period:subject:mentor; мусор → безопасный default."""
    period = parts[start] if len(parts) > start and parts[start] in PERIOD_LABELS else 'a'
    try:
        subject_id = int(parts[start + 1]) if len(parts) > start + 1 else 0
        mentor_id = int(parts[start + 2]) if len(parts) > start + 2 else 0
    except ValueError:
        subject_id = mentor_id = 0
    return DashContext(block=block, page=max(0, page), period=period,
                       subject_id=max(0, subject_id), mentor_id=max(0, mentor_id))


def period_label(ctx: DashContext) -> str:
    return PERIOD_LABELS.get(ctx.period, PERIOD_LABELS['a'])


def has_filters(ctx: DashContext) -> bool:
    return ctx.period != 'a' or ctx.subject_id != 0 or ctx.mentor_id != 0


def subject_label(data: dict, subject_id: int) -> str:
    if not subject_id:
        return 'все предметы'
    for name, row in data['cycle'].items():
        if row['subject_id'] == subject_id:
            return name
    return f'предмет #{subject_id}'


def mentor_label(data: dict, mentor_id: int) -> str:
    return data['session_names'].get(mentor_id, f'ID {mentor_id}') if mentor_id else 'все ведущие'


def filter_summary(data: dict, ctx: DashContext) -> str:
    parts = [f'Период: {period_label(ctx)}']
    if ctx.block != 'subject':
        parts.append(f'Предмет: {subject_label(data, ctx.subject_id)}')
    parts.append(f'Ведущий: {mentor_label(data, ctx.mentor_id)}')
    return ' · '.join(parts)



async def _sessions_in(s: AsyncSession, settings: Settings, term) -> list[MentoringSession]:
    query = select(MentoringSession)
    if term is not None:
        start, end = svc.term_bounds(term, settings)
        query = query.where(MentoringSession.starts_at >= start, MentoringSession.starts_at < end)
    return list(await s.scalars(query.order_by(MentoringSession.starts_at)))


async def _per_session(s: AsyncSession, ids: list[int]) -> tuple[dict, dict, dict, dict]:
    if not ids:
        return {}, {}, {}, {}
    booked: dict[int, int] = {}
    for sid, count in (await s.execute(
            select(SessionBooking.session_id, func.count()).where(
                SessionBooking.session_id.in_(ids), SessionBooking.status == 'active'
            ).group_by(SessionBooking.session_id))).all():
        booked[sid] = count
    feedback: dict[int, int] = {}
    written: dict[int, int] = {}
    for row in await s.scalars(select(SessionFeedback).where(SessionFeedback.session_id.in_(ids))):
        feedback[row.session_id] = feedback.get(row.session_id, 0) + 1
        if svc.has_written_feedback(row):
            written[row.session_id] = written.get(row.session_id, 0) + 1
    materials: dict[int, int] = {}
    for sid, count in (await s.execute(
            select(LearningMaterial.session_id, func.count()).where(
                LearningMaterial.session_id.in_(ids), LearningMaterial.archived.is_(False),
                LearningMaterial.cycle_stage == 'final', LearningMaterial.review_status == 'approved'
            ).group_by(LearningMaterial.session_id))).all():
        materials[sid] = count
    return booked, feedback, written, materials


async def overview(s: AsyncSession, settings: Settings, now: datetime | None = None) -> dict:
    moment = now or clock.now()
    term, week = await svc.week_now(s, settings, moment)
    lessons = await _sessions_in(s, settings, term)
    ids = [x.id for x in lessons]
    booked, feedback, written, materials = await _per_session(s, ids)

    completed = [x for x in lessons if x.status == 'completed']
    cancelled = [x for x in lessons if x.status == 'cancelled']
    scheduled = [x for x in lessons if x.status == 'scheduled']
    with_feedback = sum(1 for x in completed if written.get(x.id))
    without_materials = [x for x in completed if not materials.get(x.id)]

    tickets_open = await s.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.status.in_(OPEN_STATUSES))) or 0
    tickets_overdue = 0
    overdue_ids: list[int] = []
    sla = {"serious": 4, "complaint": 48, "question": 24, "suggestion": 48, "praise": None}
    for ticket in await s.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES))):
        hours = sla.get(ticket.category)
        if hours and (moment - ticket.created_at) > timedelta(hours=hours):
            tickets_overdue += 1
            overdue_ids.append(ticket.id)

    mentors_ids = set(await s.scalars(select(Mentor.telegram_id)))
    missing_reports = await svc.missing_weekly_reports(s, svc.report_week_key(settings, 0, moment))
    names: dict[int, str] = {}
    if missing_reports:
        for user in await s.scalars(select(User).where(User.telegram_id.in_(missing_reports))):
            names[user.telegram_id] = f"@{user.username}" if user.username else user.display_name
    with_roles = set(await s.scalars(select(MentorRole.telegram_id).where(
        MentorRole.term_id == term.id))) if term else set()
    week_start = svc.report_week_key(settings, 0, moment)
    week_from, week_until = svc.report_week_bounds(week_start, settings)
    week_lessons = [x for x in lessons if week_from <= x.starts_at < week_until]
    busy: dict[int, int] = {}
    week_scheduled = 0
    for lesson in lessons:
        in_week = week_from <= lesson.starts_at < week_until
        if lesson.status != 'cancelled' and in_week:
            # норматив п. 8.1.5 — про все слоты недели, а не про оставшиеся
            week_scheduled += 1
            busy[lesson.mentor_id] = busy.get(lesson.mentor_id, 0) + 1
    overloaded = [uid for uid, count in busy.items() if count > 3]

    cycle = await cycle_by_subject(s, term, settings)
    clinic = await svc.clinic_coverage(s, settings, term)
    feedback_stats = await _feedback_stats(s, ids)
    attendance_by_session = {}
    present = 0
    seen = 0
    if ids:
        for booking in await s.scalars(select(SessionBooking).where(
                SessionBooking.session_id.in_(ids), SessionBooking.status == 'active')):
            attendance = attendance_by_session.setdefault(booking.session_id, {'present': 0, 'marked': 0})
            if booking.attendance == 'present':
                present += 1
                attendance['present'] += 1
            if booking.attendance in ('present', 'absent'):
                seen += 1
                attendance['marked'] += 1

    # Слоты, которые скоро отменятся сами (п. 8.1.3), и неизвестный формат.
    # Блок «требует внимания» ограничен триместром: иначе кнопка ведёт в занятие,
    # которого нет в текущем срезе сводки.
    term_start, term_end = svc.term_bounds(term, settings) if term is not None else (None, None)
    low_demand = []
    for lesson, count, exempt in await svc.low_demand_candidates(s, settings, moment):
        if term is not None and not (term_start <= lesson.starts_at < term_end):
            continue
        if lesson.format_confirmed is True:
            low = count < svc.minimum_bookings(lesson)
        else:
            # Неподтверждённый формат: 0 записей — отмена бесспорна; 1 запись —
            # порог применить нельзя, нужна фиксация формата, а не догадка.
            low = count <= 1
        if low:
            low_demand.append((lesson, count, exempt))

    reports_total = len(mentors_ids)
    kpi = {
        'reports_done': reports_total - len(missing_reports),
        'reports_total': reports_total,
        'score': feedback_stats['score'],
        'reviews': feedback_stats['count'],
        'reviews_written': feedback_stats.get('written', 0),
        'clinic': clinic,
        'attendance': (present, seen),
        'materials_done': len(completed) - len(without_materials),
        'completed': len(completed),
        'plan': svc.week_plan(week),
        'planned_now': week_scheduled,
    }
    attention = _attention(low_demand, completed, written, without_materials,
                           missing_reports, names, overdue_ids)
    topic_ids = {x.topic_id for x in lessons}
    topic_names = {}
    subject_names = {}
    subject_ids = {}
    if topic_ids:
        for topic, subject in (await s.execute(select(Topic, Subject).join(
                Subject, Subject.id == Topic.subject_id).where(Topic.id.in_(topic_ids)))).all():
            topic_names[topic.id] = topic.name
            subject_names[topic.id] = subject.name
            subject_ids[topic.id] = subject.id
    material_status = {}
    if ids:
        for material in await s.scalars(select(LearningMaterial).where(
                LearningMaterial.session_id.in_(ids), LearningMaterial.archived.is_(False))):
            counts = material_status.setdefault(material.session_id, {'preparation': 0, 'other': 0})
            if material.cycle_stage == 'preparation' and material.review_status == 'approved':
                counts['preparation'] += 1
            elif material.cycle_stage != 'final' or material.review_status != 'approved':
                counts['other'] += 1
    session_names = {}
    format_changes = {}
    if ids:
        for change in await s.scalars(select(SessionFormatChange).where(
                SessionFormatChange.session_id.in_(ids)).order_by(
                SessionFormatChange.changed_at, SessionFormatChange.id)):
            format_changes[change.session_id] = change
    person_ids = {uid for x in lessons for uid in (x.mentor_id, x.author_id) if uid is not None}
    person_ids |= {change.changed_by for change in format_changes.values()}
    if person_ids:
        for user in await s.scalars(select(User).where(User.telegram_id.in_(person_ids))):
            session_names[user.telegram_id] = f'@{user.username}' if user.username else user.display_name
    week_ids = [x.id for x in week_lessons]
    week_completed = [x for x in week_lessons if x.status == 'completed']
    week_bookings = list(await s.scalars(select(SessionBooking).where(
        SessionBooking.session_id.in_(week_ids), SessionBooking.status == 'active'))) if week_ids else []
    week_summary = {
        'completed': len(week_completed),
        'scheduled': sum(x.status == 'scheduled' for x in week_lessons),
        'cancelled': sum(x.status == 'cancelled' for x in week_lessons),
        'bookings': len(week_bookings),
        'marked': sum(x.attendance in ('present', 'absent') for x in week_bookings),
        'present': sum(x.attendance == 'present' for x in week_bookings),
        'closed': sum(bool(written.get(x.id) and materials.get(x.id)) for x in week_completed),
        'feedback': await _feedback_stats(s, [x.id for x in week_completed]),
    }

    return {
        'term': term, 'week': week, 'moment': moment,
        'moment_local': (moment.replace(tzinfo=timezone.utc)
                         .astimezone(ZoneInfo(settings.timezone)).strftime('%d.%m.%Y %H:%M')),
        'timezone': settings.timezone,
        'week_start': week_start, 'week_lessons': week_lessons, 'week_summary': week_summary,
        'subject_names': subject_names, 'topic_names': topic_names, 'subject_ids': subject_ids,
        'material_status': material_status, 'session_names': session_names,
        'lessons': lessons, 'completed': completed, 'cancelled': cancelled, 'scheduled': scheduled,
        'booked': booked, 'feedback': feedback, 'written_feedback': written, 'materials': materials,
        'format_changes': format_changes,
        'attendance_by_session': attendance_by_session,
        'with_feedback': with_feedback,
        'without_materials': without_materials,
        'unique_students': len(set(await s.scalars(select(SessionBooking.student_id).where(
            SessionBooking.session_id.in_(ids), SessionBooking.status == 'active'))) if ids else set()),
        'tickets_open': tickets_open, 'tickets_overdue': tickets_overdue,
        'mentors': len(mentors_ids), 'mentors_without_roles': len(mentors_ids - with_roles),
        'missing_reports': missing_reports,
        'missing_report_names': [names.get(uid, str(uid)) for uid in missing_reports],
        'overloaded': overloaded,
        'cycle': cycle,
        'cycle_closed': sum(1 for row in cycle.values() if row['closed']),
        'kpi': kpi, 'attention': attention, 'low_demand': low_demand,
        'unconfirmed': [x for x in lessons if x.format_confirmed is not True],
    }


async def _feedback_stats(s: AsyncSession, ids: list[int]) -> dict:
    if not ids:
        return {'count': 0, 'written': 0, 'score': None}
    rows = list(await s.scalars(select(SessionFeedback).where(SessionFeedback.session_id.in_(ids))))
    if not rows:
        return {'count': 0, 'written': 0, 'score': None}
    return {'count': len(rows), 'written': sum(svc.has_written_feedback(r) for r in rows),
            'score': round(sum(r.score for r in rows) / len(rows), 2)}


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f'{n} {one}'
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f'{n} {few}'
    return f'{n} {many}'


def _attention(low_demand, completed, written, without_materials, missing_reports, names,
               overdue_ids) -> list[tuple[str, str, str | None]]:
    """Список «что требует решения»: уровень, текст, куда вести."""
    items: list[tuple[str, str, str | None]] = []
    for lesson, count, exempt in low_demand:
        if count == 0:
            tail = ('триместр не задан — автоотмена выключена' if exempt is None
                    else 'пиковая неделя — автоотмена не применяется (п. 8.1.3)' if exempt
                    else 'отменится автоматически за 24 ч')
        elif lesson.format_confirmed is not True:
            tail = 'формат не подтверждён — автоотмена выключена, подтвердите формат'
        elif exempt is None:
            tail = 'триместр не задан — автоотмена выключена'
        elif exempt:
            tail = 'пиковая неделя — автоотмена не применяется (п. 8.1.3)'
        else:
            tail = 'отменится автоматически за 24 ч'
        if lesson.format_confirmed is not True and count > 0:
            level = 'yellow'
        else:
            level = 'red' if exempt is False and count < svc.minimum_bookings(lesson) else 'yellow'
        items.append((level, f'#{lesson.id} · {lesson.starts_at:%d.%m %H:%M} — записей {count}, {tail}',
                      f'ms:view:{lesson.id}'))
    for sid in overdue_ids:
        items.append(('red', f'обращение #{sid} просрочено по SLA', '/queue'))
    no_feedback = [x for x in completed if not written.get(x.id)]
    if no_feedback:
        items.append(('yellow', f'{_plural(len(no_feedback), "занятие", "занятия", "занятий")} '
                                'без письменного отзыва', 'dash:debts'))
    if without_materials:
        items.append(('yellow', f'{_plural(len(without_materials), "занятие", "занятия", "занятий")} '
                                'без подтверждённого итогового материала', 'dash:debts'))
    if missing_reports:
        items.append(('yellow', f'Не сдали отчёт за неделю: {len(missing_reports)}', 'dash:people'))
    return items


async def cycle_by_subject(s: AsyncSession, term, settings: Settings) -> dict:
    """Цикл каждого проведённого занятия: его материал в Банке и минимум один отзыв.

    Статус completed выставляется после отметки явки. Предварительный материал
    и работа Респондента в чатах не представлены отдельными признаками занятия.
    """
    subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
    out: dict[str, dict] = {}
    for subject in subjects:
        topic_ids = list(await s.scalars(select(Topic.id).where(Topic.subject_id == subject.id)))
        lesson_query = select(MentoringSession).where(MentoringSession.topic_id.in_(topic_ids)) if topic_ids \
            else None
        if lesson_query is not None and term is not None:
            start, end = svc.term_bounds(term, settings)
            lesson_query = lesson_query.where(
                MentoringSession.starts_at >= start, MentoringSession.starts_at < end)
        lessons = list(await s.scalars(lesson_query)) if lesson_query is not None else []
        completed = [x for x in lessons if x.status == 'completed']
        ids = [x.id for x in lessons]
        _, feedback, written, materials = await _per_session(s, ids)
        closed_ids = [x.id for x in completed if materials.get(x.id) and written.get(x.id)]
        has_material = bool(completed) and all(materials.get(x.id) for x in completed)
        out[subject.name] = {
            'subject_id': subject.id,
            'material': has_material,
            'lesson': bool(lessons),
            'held': bool(completed),
            'feedback': bool(completed) and all(written.get(x.id) for x in completed),
            'bank': has_material,
            'closed': bool(completed) and len(closed_ids) == len(completed),
            'closed_sessions': len(closed_ids),
            'completed_sessions': len(completed),
        }
    return out


# ------------------------------------------------------------------ экраны

def _tick(value: bool) -> str:
    return '✅' if value else '—'


def overview_text(data: dict) -> str:
    term, week = data['term'], data['week']
    head = f"<b>Сводка · {e(term.name, 40)}, неделя {week}</b>" if term else \
        "<b>Сводка · триместр не задан</b>"
    start = datetime.strptime(data['week_start'], '%Y-%m-%d')
    end = start + timedelta(days=6)
    lines = [head, f"Неделя {start:%d.%m}–{end:%d.%m.%Y}",
             f"Данные бота на {data['moment_local']} · {e(data['timezone'], 40)}",
             "Показаны только сведения, внесённые в бот.", "\n<b>Итоги недели</b>"]
    summary = data['week_summary']
    if data['week_lessons']:
        lines.append(f"Проведено {summary['completed']} · в расписании {summary['scheduled']}"
                     f" · отменено {summary['cancelled']}")
        lines.append(f"Активных записей: {summary['bookings']}; явка отмечена у {summary['marked']},"
                     f" пришли {summary['present']}")
        lines.append(f"Цикл закрыт: {summary['closed']} из {summary['completed']} проведённых занятий")
        stats = summary['feedback']
        lines.append(f"Оценка: {stats['score']} по {stats['count']} отзывам; "
                     f"письменных: {stats.get('written', 0)}" if stats['count']
                     else "Отзывов о проведённых занятиях за неделю пока нет.")
    else:
        lines.append("В боте нет занятий за эту неделю. Фактическую работу вне бота это не подтверждает.")
        lines.append("Цикл: нет проведённых занятий для оценки.")
    items = data['attention']
    red = sum(1 for level, _, _ in items if level == 'red')
    lines.append(f"\n<b>Главные проблемы · {len(items)}</b>" + (f" · 🔴 {red}" if red else ""))
    lines.append("Занятия за триместр, отчёты за неделю и текущие обращения." if term
                 else "Занятия всей базы, отчёты за неделю и текущие обращения.")
    lines.append(f"Неподтверждённых форматов: {len(data.get('unconfirmed', []))} — "
                 "список всех таких занятий в отдельном разделе.")
    if items:
        icon = {'red': '🔴', 'yellow': '🟡'}
        # Сначала красное: иначе срочное может не попасть в первые три строки.
        for level, text, _ in sorted(items, key=lambda item: item[0] != 'red')[:3]:
            lines.append(f"{icon.get(level, '•')} {e(text, 350)}")
        if len(items) > 3:
            lines.append(f"Ещё {len(items) - 3} — кнопка «Требует внимания».")
    else:
        lines.append("По данным бота проблем не найдено.")
    lines.append("\nПодробности: занятия недели, цикл, люди и KPI — кнопками ниже.")
    if term is None:
        lines.append("\n⚠️ Триместр не задан — цифры по всей базе. Задайте: /term")
    return "\n".join(lines)


def _scope(data: dict) -> str:
    return 'за триместр' if data['term'] else 'по всей базе · триместр не задан'


def _local_date(data: dict, moment: datetime) -> str:
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(data['timezone'])).strftime('%d.%m %H:%M')


def _format_label(lesson: MentoringSession) -> str:
    if lesson.format_confirmed is not True:
        return '[__] — формат не подтверждён'
    return svc.FORMAT_LABELS.get(lesson.format, '[__]')


def _local_day(data: dict, moment: datetime):
    return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(data['timezone'])).date()


def _filtered_lessons(data: dict, ctx: DashContext, base: list) -> list:
    """Период, предмет и ведущий. Фильтры не меняют правила автоотмены."""
    lessons = list(base)
    now = data['moment']
    if ctx.period == 'w':
        week_ids = {x.id for x in data['week_lessons']}
        lessons = [x for x in lessons if x.id in week_ids]
    elif ctx.period == 'd':
        today = _local_day(data, now)
        lessons = [x for x in lessons if _local_day(data, x.starts_at) == today]
    elif ctx.period == 'm':
        lessons = [x for x in lessons
                   if abs((x.starts_at - now).total_seconds()) <= 30 * 24 * 3600]
    elif ctx.period == 'p':
        lessons = [x for x in lessons if x.starts_at < now]
    elif ctx.period == 'f':
        lessons = [x for x in lessons if x.starts_at >= now]
    if ctx.subject_id:
        lessons = [x for x in lessons if data['subject_ids'].get(x.topic_id) == ctx.subject_id]
    if ctx.mentor_id:
        lessons = [x for x in lessons if x.mentor_id == ctx.mentor_id]
    return lessons


def mentor_options(data: dict) -> list[tuple[int, str]]:
    ids = sorted({x.mentor_id for x in data['lessons']})
    return [(uid, data['session_names'].get(uid, f'ID {uid}')) for uid in ids]


def subject_options(data: dict) -> list[tuple[int, str]]:
    return sorted((row['subject_id'], name) for name, row in data['cycle'].items())


def page_items(data: dict, block: str, subject_id: int | None = None,
               ctx: DashContext | None = None) -> list:
    """Единый порядок строк для текста и кнопок; ничего не обрезается навсегда."""
    ctx = ctx or DashContext(block=block, subject_id=subject_id or 0)
    if block == 'attention':
        return data['attention']
    if block == 'people':
        return data['missing_report_names']
    if block == 'cycle':
        return list(data['cycle'].items())
    base = data['week_lessons'] if block == 'week' else data['lessons']
    if block == 'debts':
        base = [x for x in data['completed'] if not data['feedback'].get(x.id)
                or not data['materials'].get(x.id)]
    if block == 'unconfirmed':
        base = [x for x in data['lessons'] if x.format_confirmed is not True]
    if block == 'subject':
        subject_id = subject_id if subject_id is not None else ctx.subject_id
        base = [x for x in base if data['subject_ids'].get(x.topic_id) == subject_id]
    lessons = _filtered_lessons(data, ctx, base)
    return sorted(lessons, key=lambda x: (x.starts_at, x.id), reverse=True)


def page_slice(items: list, page: int) -> tuple[list, int, int]:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages


def sessions_text(data: dict, page: int = 0, *, block: str = 'sessions',
                  subject_id: int | None = None, ctx: DashContext | None = None) -> str:
    ctx = ctx or DashContext(block=block, subject_id=subject_id or 0)
    items = page_items(data, block, subject_id, ctx)
    lessons, page, pages = page_slice(items, page)
    titles = {
        'week': 'Занятия недели',
        'sessions': 'Менторки',
        'unconfirmed': 'Неподтверждённые форматы',
        'debts': 'Долги по занятиям',
    }
    title = titles.get(block, 'Занятия')
    if block == 'subject':
        title = next((name for name, row in data['cycle'].items()
                      if row['subject_id'] == subject_id), 'Предмет')
    lines = [f"<b>{e(title, 100)} · {len(items)}</b>",
             'Текущая неделя' if block == 'week' else _scope(data),
             f"Время: {e(data['timezone'], 40)} · страница {page + 1}/{pages}"]
    if block == 'unconfirmed':
        lines.append('Показаны все занятия с format_confirmed=NULL, а не только ближайшие 24 часа.')
        lines.append('Старое значение «1-на-1» не считается подтверждённым форматом.')
    lines.append(filter_summary(data, ctx))
    for lesson in lessons:
        mark = {'completed': '✅', 'cancelled': '❌', 'scheduled': '🕒'}[lesson.status]
        booked = data['booked'].get(lesson.id, 0)
        fb = data['feedback'].get(lesson.id, 0)
        wf = data['written_feedback'].get(lesson.id, 0)
        mats = data['materials'].get(lesson.id, 0)
        fmt = _format_label(lesson)
        subject = data['subject_names'].get(lesson.topic_id, '[__]')
        lines.append(f"\n{mark} #{lesson.id} · {_local_date(data, lesson.starts_at)} · {e(subject, 55)}\n"
                     f"     {e(fmt, 40)}\n"
                     f"     записей {booked} · отзывов {fb} (письменных {wf}) · "
                     f"итогов после проверки {mats}")
        if block == 'unconfirmed':
            lines.append('     формат не подтверждён; автоподтверждения нет.')
    if not lessons:
        if has_filters(ctx):
            lines.append("По выбранным фильтрам занятий нет. Измените период, предмет или ведущего "
                         "либо сбросьте фильтры.")
        elif block == 'unconfirmed':
            lines.append("Занятий с неподтверждённым форматом нет.")
        else:
            lines.append("В боте нет занятий в этом разделе.")
    else:
        lines.append("\nВыберите занятие ниже, чтобы увидеть его цикл.")
    return "\n".join(lines)


def cycle_text(data: dict, page: int = 0) -> str:
    items, page, pages = page_slice(page_items(data, 'cycle'), page)
    rows = []
    for name, row in items:
        if row['completed_sessions']:
            rows.append(
                f"• {e(name, 60)} — закрыто {row['closed_sessions']}/{row['completed_sessions']}; "
                f"занятия {'✓' if row['held'] else '·'}, "
                f"письменный отзыв {'✓' if row['feedback'] else '·'}, "
                f"итог в Банке {'✓' if row['bank'] else '·'}")
        else:
            rows.append(f"• {e(name, 60)} — в этом триместре проведённых занятий нет")
    lines = ["<b>Цикл по предметам</b>",
             "Цикл — закрыто / проведено; нужны свой проверенный итоговый материал и ≥1 письменный отзыв.",
             "✓ — условие выполнено · — пока нет.",
             _scope(data), f"Страница {page + 1}/{pages}", "",
             "\n".join(rows) if rows else "В боте нет предметов.",
             "Выберите предмет ниже, чтобы открыть занятия."]
    return "\n".join(lines)


def people_text(data: dict, page: int = 0) -> str:
    names, page, pages = page_slice(page_items(data, 'people'), page)
    lines = ["<b>Люди</b>", f"В реестре менторов: {data['mentors']}",
             f"Без роли на этот триместр: {data['mentors_without_roles']}" if data['term']
             else "Роли: триместр не задан.",
             f"Перегруз (больше 3 слотов за неделю): {len(data['overloaded'])}"]
    if data['missing_reports']:
        deadline = datetime.strptime(data['week_start'], '%Y-%m-%d') + timedelta(days=6)
        lines.append(f"\n<b>Ещё не сдали отчёт за эту неделю ({len(data['missing_reports'])}):</b>")
        lines.append(f"Срок: {deadline:%d.%m} 23:59 · {e(data['timezone'], 40)} (п. 9.2)")
        lines.append(f"Страница {page + 1}/{pages}")
        lines += [f"· {e(name, 60)}" for name in names]
    elif data['mentors']:
        lines.append(f"\nЗа эту неделю отчёт сдали все: {data['mentors']}. "
                     f"Страница 1/1.")
    else:
        lines.append("Реестр менторов в боте пуст; полнота команды не подтверждена.")
    return "\n".join(lines)


def debts_text(data: dict, page: int = 0, *, ctx: DashContext | None = None) -> str:
    ctx = ctx or DashContext(block='debts')
    items = page_items(data, 'debts', ctx=ctx)
    lessons, page, pages = page_slice(items, page)
    lines = ["<b>Долги по занятиям</b>", _scope(data),
             f"Письменные отзывы: {data['with_feedback']} из {len(data['completed'])} проведённых",
             f"Проведены без материалов с подтверждённым итоговым этапом и методпроверкой: {len(data['without_materials'])}",
             f"Незакрытых занятий: {len(items)} · страница {page + 1}/{pages}"]
    lines.append(filter_summary(data, ctx))
    for lesson in lessons:
        gaps = []
        if not data['materials'].get(lesson.id):
            gaps.append('нет подтверждённого итогового материала')
        if not data['written_feedback'].get(lesson.id):
            gaps.append('нет письменного отзыва')
        subject = data['subject_names'].get(lesson.topic_id, '[__]')
        lines.append(f"\n#{lesson.id} · {_local_date(data, lesson.starts_at)} · {e(subject, 55)}\n"
                     + ', '.join(gaps))
    if not items:
        lines.append("По выбранным фильтрам долгов нет. Измените или сбросьте фильтры."
                     if has_filters(ctx) else
                     "По внесённым занятиям долгов нет." if data['completed']
                     else "В боте нет проведённых занятий для проверки.")
    return "\n".join(lines)


def attention_text(data: dict, page: int = 0) -> str:
    items, page, pages = page_slice(page_items(data, 'attention'), page)
    lines = [f"<b>Требует внимания · {len(data['attention'])}</b>",
             f"Страница {page + 1}/{pages}",
             "Занятия: " + _scope(data) + "; отчёты за неделю; текущие обращения."]
    for level, text, _ in items:
        lines.append(f"\n{'🔴' if level == 'red' else '🟡'} {e(text, 350)}")
    if not items:
        lines.append("По данным бота проблем не найдено.")
    return '\n'.join(lines)


def kpi_text(data: dict) -> str:
    kpi = data['kpi']
    lines = ['<b>KPI против цели</b>', 'Занятия и оценки: ' + _scope(data),
             'Ориентиры — по проекту Положения о Школе менторства.']
    lines.append(f"\nОтчёты за неделю: {kpi['reports_done']} из {kpi['reports_total']} — цель 100% (п. 9.2)"
                 if kpi['reports_total'] else '\nОтчёты: реестр менторов в боте пуст.')
    lines.append(f"Оценка: {kpi['score']} по {kpi['reviews']} отзывам — цель ≥4,0 (п. 11.3); "
                 f"письменных: {kpi.get('reviews_written', 0)}"
                 if kpi['reviews'] else 'Оценка: отзывов в боте пока нет.')
    clinic = kpi['clinic']
    if data['term']:
        if clinic['sessions']:
            lines.append(f"Syllabus Clinic: слотов {clinic['sessions']}; записались {clinic['registered']}; "
                         f"явка отмечена у {clinic['marked']}, пришли {clinic['students']}")
            if clinic['registered'] and not clinic['marked']:
                lines.append('Охват: явка не отмечена — [__].')
            elif clinic['cohort']:
                lines.append(f"Охват: {clinic['percent']}% из {clinic['cohort']} — цель ≥60% (п. 11.3)")
            else:
                lines.append('Охват: размер потока не задан.')
        else:
            lines.append('Syllabus Clinic: подтверждённых слотов в этом триместре нет — охват не считается.')
        lines.append(f"Слотов на неделе: {kpi['planned_now']} — ориентир ~{kpi['plan']} (п. 8.1.5)")
    else:
        lines.append('Syllabus Clinic и недельный ориентир: сначала задайте триместр /term.')
    lines.append(f"Итоговые материалы после методпроверки: у {kpi['materials_done']} из {kpi['completed']} проведённых занятий")
    return '\n'.join(lines)


def section_page(data: dict, block: str, page: int = 0, subject_id: int | None = None,
                 ctx: DashContext | None = None) -> dict:
    """Текст, ссылки и границы страницы административного раздела."""
    ctx = ctx or DashContext(block=block, page=page, subject_id=subject_id or 0)
    if block == 'subject' and subject_id is None:
        subject_id = ctx.subject_id
    if block == 'subject' and subject_id is not None:
        ctx = replace(ctx, block=block, subject_id=subject_id)
    items, page, pages = page_slice(page_items(data, block, subject_id, ctx), page)
    ctx = replace(ctx, block=block, page=page)
    builders = {'cycle': cycle_text, 'people': people_text, 'attention': attention_text}
    if block == 'debts':
        text = debts_text(data, page, ctx=ctx)
    elif block in builders:
        text = builders[block](data, page)
    else:
        text = sessions_text(data, page, block=block, subject_id=subject_id, ctx=ctx)
    links = []
    if block in ('sessions', 'week', 'debts', 'subject', 'unconfirmed'):
        links = [(f"Занятие #{x.id} · {_local_date(data, x.starts_at)}", lesson_cb(x.id, ctx))
                 for x in items]
    elif block == 'cycle':
        links = [(name[:55], f"dash:subject:{row['subject_id']}:0") for name, row in items]
    elif block == 'attention':
        for _, label, target in items:
            if target and target.startswith('ms:view:'):
                lesson_id = int(target.rsplit(':', 1)[1])
                links.append((f"Занятие #{lesson_id}", lesson_cb(
                    lesson_id, replace(ctx, block='attention', page=page))))
            elif target and target.startswith('dash:'):
                links.append(('Люди и отчёты' if target == 'dash:people' else 'Открыть долги',
                              f'{target}:0:{ctx.suffix()}'))
            elif target == '/queue':
                links.append(('Обращения', 'dash:tickets'))
        links = list(dict.fromkeys(links))
    return {'text': text, 'links': links, 'page': page, 'pages': pages, 'ctx': ctx}


def filter_panel_text(data: dict, ctx: DashContext) -> str:
    block_title = {
        'sessions': 'Все занятия',
        'week': 'Занятия недели',
        'debts': 'Долги по занятиям',
        'unconfirmed': 'Неподтверждённые форматы',
        'subject': subject_label(data, ctx.subject_id),
    }.get(ctx.block, 'Занятия')
    lines = [f"<b>Фильтры · {e(block_title, 80)}</b>",
             f"Период: {period_label(ctx)}"]
    if ctx.block != 'subject':
        lines.append(f"Предмет: {subject_label(data, ctx.subject_id)}")
    lines.append(f"Ведущий: {mentor_label(data, ctx.mentor_id)}")
    lines.append(f"Найдено занятий: {len(page_items(data, ctx.block, ctx=ctx))}")
    lines.append('\nВыберите фильтр кнопками ниже. Сброс вернёт все занятия.')
    return '\n'.join(lines)


def picker_options(data: dict, field: str) -> list[tuple[int, str]]:
    if field == 'subject':
        return subject_options(data)
    if field == 'mentor':
        return mentor_options(data)
    return []


def picker_text(data: dict, field: str, ctx: DashContext, picker_page: int = 0) -> str:
    options = picker_options(data, field)
    items, page, pages = page_slice(options, picker_page)
    title = 'Выберите предмет' if field == 'subject' else 'Выберите ведущего'
    current = subject_label(data, ctx.subject_id) if field == 'subject' else mentor_label(data, ctx.mentor_id)
    lines = [f"<b>{title}</b>", f"Сейчас: {current}", f"Страница {page + 1}/{pages}"]
    if not items:
        lines.append('\nВ текущем срезе нет вариантов для выбора.')
    return '\n'.join(lines)


def lesson_text(data: dict, lesson: MentoringSession) -> str:
    subject = data['subject_names'].get(lesson.topic_id, '[__]')
    topic = data['topic_names'].get(lesson.topic_id, '[__]')
    state = {'completed': 'Проведено', 'cancelled': 'Отменено', 'scheduled': 'В расписании'}[lesson.status]
    attendance = data['attendance_by_session'].get(lesson.id, {'present': 0, 'marked': 0})
    feedback = data['feedback'].get(lesson.id, 0)
    written = data['written_feedback'].get(lesson.id, 0)
    materials = data['materials'].get(lesson.id, 0)
    change = data['format_changes'].get(lesson.id)
    booked = data['booked'].get(lesson.id, 0)
    material_state = data['material_status'].get(lesson.id, {'preparation': 0, 'other': 0})
    author = data['session_names'].get(lesson.author_id, '[__] — не назначен')
    mentor = data['session_names'].get(lesson.mentor_id, '[__]')
    lines = [f"<b>Занятие #{lesson.id} · {e(subject, 70)}</b>", e(topic, 100),
             f"{_local_date(data, lesson.starts_at)} · {e(data['timezone'], 40)} · {state}",
             f"{e(_format_label(lesson), 50)} · {e(lesson.location, 100)}",
             f"Ведущий: {e(mentor, 70)} · Автор: {e(author, 70)}",
             f"\nАктивных записей: {booked}",
             f"Явка отмечена: {attendance['marked']} из {booked}; пришли {attendance['present']}"]
    if change is not None:
        old = svc.FORMAT_LABELS.get(change.old_format or '', '[__]')
        old_state = 'подтверждён' if change.old_confirmed else 'не подтверждён'
        by = data['session_names'].get(change.changed_by, f'ID {change.changed_by}')
        lines.append(f"Формат подтвердил: {e(by, 70)} · {_local_date(data, change.changed_at)} "
                     f"(было: {e(old, 40)}, {old_state})")
    if lesson.status == 'completed':
        lines += [f"\nИтоговых материалов после методпроверки: {materials} · "
                  f"отзывов: {feedback} (письменных: {written})",
                  f"Подготовительных после методпроверки: {material_state['preparation']}",
                  f"Прочих файлов без подтверждённого итога: {material_state['other']}"]
        if written:
            lines.append('Письменный отзыв получен.')
        elif feedback:
            lines.append('Отзыв получен, но письменного комментария ещё нет.')
        else:
            lines.append('Отзывов пока нет.')
        lines.append('Цикл закрыт: занятие проведено, есть его итоговый материал после методпроверки '
                     'и минимум один письменный отзыв.'
                     if materials and written else 'Цикл не закрыт: ' + ', '.join(
                         gap for value, gap in (
                             (materials, 'нет подтверждённого итогового материала'),
                             (written, 'нет письменного отзыва')) if not value) + '.')
    elif lesson.status == 'scheduled':
        lines.append('\nМатериалы и отзывы появятся после занятия. Цикл ещё не оценивается.')
    else:
        lines.append('\nОтменённое занятие не входит в цикл.')
    if lesson.cancel_reason:
        lines.append('Причина отмены: ' + e(lesson.cancel_reason, 400))
    lines.append('\nИсточник: сведения этого занятия в боте. Наличие подготовительного файла не подтверждает выдачу студентам до занятия. Респондент в чатах не блокирует цикл.')
    return '\n'.join(lines)


STATUS_RU = {'scheduled': 'в расписании', 'completed': 'проведено', 'cancelled': 'отменено'}


async def export_csv(s: AsyncSession, settings: Settings, now: datetime | None = None) -> bytes:
    """CSV по занятиям триместра — для отчётности наверх."""
    data = await overview(s, settings, now)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=';')
    writer.writerow(['id', 'дата', 'статус', 'формат', 'предмет', 'тема', 'ментор', 'записей',
                     'отзывов', 'письменных', 'материалов', 'причина отмены'])
    for lesson in data['lessons']:
        subject, topic = await svc.topic_names(s, lesson.topic_id)
        mentor = await s.get(User, lesson.mentor_id)
        writer.writerow([
            lesson.id, lesson.starts_at.strftime('%Y-%m-%d %H:%M'),
            STATUS_RU.get(lesson.status, lesson.status),
            _format_label(lesson),
            subject, topic,
            (mentor.username or mentor.display_name) if mentor else '[__]',
            data['booked'].get(lesson.id, 0), data['feedback'].get(lesson.id, 0),
            data['written_feedback'].get(lesson.id, 0),
            data['materials'].get(lesson.id, 0), lesson.cancel_reason or '',
        ])
    return buffer.getvalue().encode('utf-8-sig')
