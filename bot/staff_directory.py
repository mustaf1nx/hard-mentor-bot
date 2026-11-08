"""Единый реестр стаффа: люди, профиль, зоны ответственности и история.

Домен не зависит от Telegram. Источники истины не дублируются:
- имя и Telegram — `users`;
- членство в менторах — `mentors`;
- роль и предмет — `mentor_roles`;
- занятия/материалы/отчёты — существующие таблицы.
Новые таблицы хранят только то, чего там нет: статус участия, контур,
должность и историю назначений по трём зонам.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import clock, repo
from .config import Settings
from .db import (FinalReport, LearningMaterial, Mentor, MentorRole, MentorWeeklyReport,
                 MentoringSession, RESPONSIBILITY_ZONES, ResponsibilityAssignment, StaffMember,
                 Subject, Term, User)
from .mentoring import service as svc

STATUS_LABELS = {
    'active': 'Активен',
    'paused': 'Приостановлен',
    'left': 'Вышел',
}
TEAM_LABELS = {
    'hard': 'Группа Hard-менторства',
    'hard_leadership': 'Руководство Hard',
    'school_leadership': 'Общее руководство школы',
}
ZONE_LABELS = {
    'subject': 'Предмет',
    'schedule': 'Расписание',
    'materials': 'Материалы',
}
ROLE_LABELS = svc.ROLE_LABELS


@dataclass
class PersonSummary:
    telegram_id: int
    name: str
    username: str | None
    user_exists: bool
    status: str | None
    team: str | None
    is_mentor: bool
    is_admin: bool
    active_assignments: int


def display_name(user: User | None, telegram_id: int) -> str:
    if user is None:
        return f'ID {telegram_id}'
    return user.display_name or f'ID {telegram_id}'


def contact(user: User | None, telegram_id: int) -> str:
    if user is None:
        return f'<code>{telegram_id}</code> · профиль не создан'
    if user.username:
        return f'@{user.username} · <code>{telegram_id}</code>'
    return f'<code>{telegram_id}</code>'


def status_label(value: str | None) -> str:
    return STATUS_LABELS.get(value or '', 'не заполнено')


def team_label(value: str | None) -> str:
    return TEAM_LABELS.get(value or '', 'не заполнено')


def zone_label(value: str) -> str:
    return ZONE_LABELS.get(value, value)


def require_admin(viewer: repo.Viewer) -> None:
    if not viewer.is_admin:
        raise svc.RuleError('Раздел доступен только администрации школы менторства.')


async def _active_assignments(s: AsyncSession, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await s.execute(
        select(ResponsibilityAssignment.person_id, func.count())
        .where(ResponsibilityAssignment.person_id.in_(ids),
               ResponsibilityAssignment.ended_at.is_(None))
        .group_by(ResponsibilityAssignment.person_id))
    return {person_id: count for person_id, count in rows.all()}


async def list_people(s: AsyncSession, settings: Settings) -> list[PersonSummary]:
    """Люди раздела: профили стаффа, менторы и администраторы из env.

    Студенты сюда не попадают. Отсутствующий профиль не выдумываем:
    показываем ID и «не заполнено», пока администратор не внесёт сведения.
    """
    staff_ids = set(await s.scalars(select(StaffMember.telegram_id)))
    mentor_ids = set(await s.scalars(select(Mentor.telegram_id)))
    ids = sorted(staff_ids | mentor_ids | set(settings.staff_ids))
    users = await repo.users_by_ids(s, ids)
    members = {m.telegram_id: m for m in await s.scalars(
        select(StaffMember).where(StaffMember.telegram_id.in_(ids)))}
    counts = await _active_assignments(s, ids)
    out = []
    for uid in ids:
        user = users.get(uid)
        member = members.get(uid)
        out.append(PersonSummary(
            telegram_id=uid,
            name=display_name(user, uid),
            username=user.username if user else None,
            user_exists=user is not None,
            status=member.status if member else None,
            team=member.team if member else None,
            is_mentor=uid in mentor_ids,
            is_admin=uid in settings.staff_ids,
            active_assignments=counts.get(uid, 0),
        ))
    return sorted(out, key=lambda p: (p.name.casefold(), p.telegram_id))


async def _roles(s: AsyncSession, uid: int) -> list[dict]:
    rows = await s.execute(
        select(MentorRole, Subject, Term)
        .join(Subject, Subject.id == MentorRole.subject_id)
        .join(Term, Term.id == MentorRole.term_id)
        .where(MentorRole.telegram_id == uid)
        .order_by(Term.starts_on.desc(), Subject.name_key))
    return [{'role': role.role, 'role_label': ROLE_LABELS.get(role.role, role.role),
             'subject': subject.name, 'term': term.name, 'term_id': term.id}
            for role, subject, term in rows.all()]


async def _assignments(s: AsyncSession, uid: int) -> list[dict]:
    rows = await s.execute(
        select(ResponsibilityAssignment, Subject)
        .outerjoin(Subject, Subject.id == ResponsibilityAssignment.subject_id)
        .where(ResponsibilityAssignment.person_id == uid)
        .order_by(ResponsibilityAssignment.started_at.desc(), ResponsibilityAssignment.id.desc()))
    return [{'row': assignment, 'zone': assignment.zone, 'zone_label': zone_label(assignment.zone),
             'subject': subject.name if subject else None,
             'started_at': assignment.started_at, 'ended_at': assignment.ended_at}
            for assignment, subject in rows.all()]


async def _assignment_rows(s: AsyncSession, query) -> list[dict]:
    rows = await s.execute(query)
    return [{'row': assignment, 'zone': assignment.zone, 'zone_label': zone_label(assignment.zone),
             'subject': subject.name if subject else None,
             'person_id': assignment.person_id,
             'started_at': assignment.started_at, 'ended_at': assignment.ended_at}
            for assignment, subject in rows.all()]


async def person_card(s: AsyncSession, settings: Settings, uid: int) -> dict:
    user = await s.get(User, uid)
    member = await s.get(StaffMember, uid)
    mentor = await repo.is_mentor(s, uid)
    roles = await _roles(s, uid)
    assignments = await _assignments(s, uid)
    current = [a for a in assignments if a['ended_at'] is None]
    history = [a for a in assignments if a['ended_at'] is not None]
    sessions_as_mentor = await s.scalar(select(func.count()).select_from(MentoringSession).where(
        MentoringSession.mentor_id == uid)) or 0
    sessions_as_author = await s.scalar(select(func.count()).select_from(MentoringSession).where(
        MentoringSession.author_id == uid)) or 0
    materials = await s.scalar(select(func.count()).select_from(LearningMaterial).where(
        LearningMaterial.author_id == uid, LearningMaterial.archived.is_(False))) or 0
    weekly = await s.scalar(select(func.count()).select_from(MentorWeeklyReport).where(
        MentorWeeklyReport.mentor_id == uid)) or 0
    final = await s.scalar(select(func.count()).select_from(FinalReport).where(
        FinalReport.mentor_id == uid)) or 0
    recent_sessions = list(await s.scalars(
        select(MentoringSession)
        .where(or_(MentoringSession.mentor_id == uid, MentoringSession.author_id == uid))
        .order_by(MentoringSession.starts_at.desc()).limit(5)))
    weekly_reports = list(await s.scalars(
        select(MentorWeeklyReport).where(MentorWeeklyReport.mentor_id == uid)
        .order_by(MentorWeeklyReport.week_start.desc()).limit(5)))
    return {
        'uid': uid, 'user': user, 'member': member, 'mentor': mentor,
        'is_admin': uid in settings.staff_ids,
        'roles': roles, 'current': current, 'history': history,
        'sessions_as_mentor': int(sessions_as_mentor),
        'sessions_as_author': int(sessions_as_author),
        'materials': int(materials), 'weekly_reports': int(weekly), 'final_reports': int(final),
        'recent_sessions': recent_sessions, 'weekly_report_rows': weekly_reports,
    }


async def overview(s: AsyncSession, settings: Settings) -> dict:
    subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
    active = await _assignment_rows(
        s, select(ResponsibilityAssignment, Subject)
        .outerjoin(Subject, Subject.id == ResponsibilityAssignment.subject_id)
        .where(ResponsibilityAssignment.ended_at.is_(None))
        .order_by(ResponsibilityAssignment.zone, Subject.name_key))
    people = {}
    ids = [a['person_id'] for a in active]
    if ids:
        people = await repo.users_by_ids(s, ids)
    for item in active:
        user = people.get(item['person_id'])
        item['person_name'] = display_name(user, item['person_id'])
        item['person_username'] = user.username if user else None
    by_zone = {zone: [] for zone in ZONE_LABELS}
    for item in active:
        by_zone.setdefault(item['zone'], []).append(item)
    subject_rows = []
    for subject in subjects:
        found = next((a for a in by_zone.get('subject', [])
                      if a['subject'] == subject.name), None)
        subject_rows.append({'subject': subject, 'assignment': found})
    return {
        'schedule': by_zone.get('schedule', [None])[0] if by_zone.get('schedule') else None,
        'materials': by_zone.get('materials', [None])[0] if by_zone.get('materials') else None,
        'subjects': subject_rows,
        'all_active': active,
    }


async def add_person(s: AsyncSession, viewer: repo.Viewer, ref: str) -> tuple[StaffMember, bool]:
    require_admin(viewer)
    user = await repo.find_user(s, ref.strip())
    if user is None:
        raise svc.RuleError('Пользователь не найден. Пусть сначала нажмёт /start, затем повторите.')
    member = await s.get(StaffMember, user.telegram_id)
    if member is not None:
        return member, False
    now = clock.now()
    member = StaffMember(telegram_id=user.telegram_id, status='active', team=None, title=None,
                         created_at=now, updated_at=now, updated_by=viewer.id)
    s.add(member)
    await s.flush()
    return member, True


async def _member_for_edit(s: AsyncSession, viewer: repo.Viewer, uid: int) -> StaffMember:
    require_admin(viewer)
    if await s.get(User, uid) is None:
        raise svc.RuleError('Профиль не создан: человек ещё не нажимал /start.')
    member = await s.get(StaffMember, uid)
    if member is None:
        now = clock.now()
        member = StaffMember(telegram_id=uid, status=None, team=None, title=None,
                             created_at=now, updated_at=now, updated_by=viewer.id)
        s.add(member)
        await s.flush()
    return member


async def set_status(s: AsyncSession, viewer: repo.Viewer, uid: int, value: str) -> tuple[str | None, str | None]:
    require_admin(viewer)
    if value not in STATUS_LABELS:
        raise svc.RuleError('Выберите статус кнопкой.')
    member = await _member_for_edit(s, viewer, uid)
    old = member.status
    member.status = value
    member.updated_at = clock.now()
    member.updated_by = viewer.id
    return old, value


async def set_team(s: AsyncSession, viewer: repo.Viewer, uid: int, value: str) -> tuple[str | None, str | None]:
    require_admin(viewer)
    if value not in TEAM_LABELS:
        raise svc.RuleError('Выберите контур кнопкой.')
    member = await _member_for_edit(s, viewer, uid)
    old = member.team
    member.team = value
    member.updated_at = clock.now()
    member.updated_by = viewer.id
    return old, value


async def set_title(s: AsyncSession, viewer: repo.Viewer, uid: int, value: str) -> tuple[str | None, str | None]:
    require_admin(viewer)
    title = ' '.join(value.split())
    if len(title) > 120:
        raise svc.RuleError('Должность — до 120 символов.')
    member = await _member_for_edit(s, viewer, uid)
    old = member.title
    member.title = title or None
    member.updated_at = clock.now()
    member.updated_by = viewer.id
    return old, member.title


async def _is_staff_person(s: AsyncSession, settings: Settings, uid: int) -> bool:
    if uid in settings.staff_ids or await s.get(StaffMember, uid) is not None:
        return True
    return await repo.is_mentor(s, uid)


async def assign(s: AsyncSession, viewer: repo.Viewer, settings: Settings, zone: str,
                 subject_id: int | None, person_id: int, now: datetime | None = None) -> tuple[dict | None, dict]:
    """Назначает ответственного. Старое активное назначение завершается, не удаляется."""
    require_admin(viewer)
    now = now or clock.now()
    if zone not in RESPONSIBILITY_ZONES:
        raise svc.RuleError('Неизвестная зона ответственности.')
    if zone == 'subject':
        if not subject_id:
            raise svc.RuleError('Для зоны «Предмет» выберите предмет.')
        if await s.get(Subject, subject_id) is None:
            raise svc.RuleError('Предмет не найден.')
    elif subject_id:
        raise svc.RuleError('Для расписания и материалов предмет не указывается.')
    person = await s.get(User, person_id)
    if person is None:
        raise svc.RuleError('Человек не найден: пусть сначала нажмёт /start.')
    if not await _is_staff_person(s, settings, person_id):
        raise svc.RuleError('Назначить можно только участника стаффа или ментора.')
    member = await s.get(StaffMember, person_id)
    if member is not None and member.status == 'left':
        raise svc.RuleError('Этот человек отмечен как вышедший. Сначала измените статус.')

    criteria = [ResponsibilityAssignment.zone == zone,
                ResponsibilityAssignment.ended_at.is_(None)]
    if zone == 'subject':
        criteria.append(ResponsibilityAssignment.subject_id == subject_id)
    current = await s.scalar(select(ResponsibilityAssignment).where(*criteria).with_for_update())
    if current is not None and current.person_id == person_id:
        raise svc.RuleError('Этот человек уже ответственный в этой зоне.')
    if current is not None:
        current.ended_at = now
        current.ended_by = viewer.id
        await s.flush()
    created = ResponsibilityAssignment(
        person_id=person_id, zone=zone, subject_id=subject_id if zone == 'subject' else None,
        started_at=now, ended_at=None, created_by=viewer.id, ended_by=None)
    s.add(created)
    await s.flush()
    old = None
    if current is not None:
        old = {'person_id': current.person_id, 'started_at': current.started_at,
               'zone': current.zone, 'subject_id': current.subject_id}
    new = {'person_id': person_id, 'started_at': now, 'zone': zone,
           'subject_id': subject_id if zone == 'subject' else None}
    return old, new


async def end_assignment(s: AsyncSession, viewer: repo.Viewer, assignment_id: int,
                         now: datetime | None = None) -> ResponsibilityAssignment:
    require_admin(viewer)
    now = now or clock.now()
    assignment = await s.get(ResponsibilityAssignment, assignment_id)
    if assignment is None or assignment.ended_at is not None:
        raise svc.RuleError('Активное назначение не найдено: возможно, его уже завершили.')
    assignment.ended_at = now
    assignment.ended_by = viewer.id
    return assignment


async def current_assignment(s: AsyncSession, zone: str, subject_id: int | None = None) -> dict | None:
    query = (select(ResponsibilityAssignment, Subject)
             .outerjoin(Subject, Subject.id == ResponsibilityAssignment.subject_id)
             .where(ResponsibilityAssignment.zone == zone,
                    ResponsibilityAssignment.ended_at.is_(None)))
    if zone == 'subject':
        query = query.where(ResponsibilityAssignment.subject_id == subject_id)
    row = (await s.execute(query)).first()
    if row is None:
        return None
    assignment, subject = row
    return {'row': assignment, 'zone': assignment.zone, 'zone_label': zone_label(assignment.zone),
            'subject': subject.name if subject else None,
            'person_id': assignment.person_id, 'started_at': assignment.started_at}


async def person_sessions(s: AsyncSession, uid: int) -> list[MentoringSession]:
    return list(await s.scalars(
        select(MentoringSession)
        .where(or_(MentoringSession.mentor_id == uid, MentoringSession.author_id == uid))
        .order_by(MentoringSession.starts_at.desc())))


async def weekly_reports(s: AsyncSession, uid: int) -> list[MentorWeeklyReport]:
    return list(await s.scalars(
        select(MentorWeeklyReport).where(MentorWeeklyReport.mentor_id == uid)
        .order_by(MentorWeeklyReport.week_start.desc())))


async def assignment_history(s: AsyncSession, uid: int) -> list[dict]:
    return await _assignments(s, uid)
