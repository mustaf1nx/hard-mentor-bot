"""Административный раздел «Люди и назначения».

Только отрисовка и навигация. Доменные правила и доступ — в
`bot.staff_directory`; проверка прав выполняется на сервере в каждом
обработчике, а не только скрытием кнопок.
"""
from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import repo, staff_directory as directory
from ..config import Settings
from ..db import RESPONSIBILITY_ZONES, StaffHistory, Subject, User
from ..mentoring import service as svc
from ..mentoring import ui as mui
from ..texts import t

router = Router(name="directory")
PAGE = 8


class DirectoryForm(StatesGroup):
    add_person = State()
    title = State()


def _page(items, page: int):
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = min(max(0, page), pages - 1)
    return items[page * PAGE:(page + 1) * PAGE], page, pages


def _date(value) -> str:
    return value.strftime('%d.%m.%Y') if value else '—'


def _role_lines(roles: list[dict]) -> list[str]:
    lines = [f"• {r['role_label']} — {mui.e(r['subject'], 80)} · {mui.e(r['term'], 40)}"
             for r in roles[:8]]
    if len(roles) > 8:
        lines.append(f"… и ещё {len(roles) - 8}")
    return lines


def _zone_line(item: dict | None) -> str:
    if item is None:
        return 'не назначен'
    name = item.get('person_name') or f"ID {item.get('person_id')}"
    if item.get('person_username'):
        name = f"@{item['person_username']}"
    subject = f" · {mui.e(item['subject'], 80)}" if item.get('subject') else ''
    return f"{mui.e(name, 60)}{subject} · с {_date(item.get('started_at'))}"


async def _person_name(s, uid: int) -> str:
    user = await s.get(User, uid)
    return directory.display_name(user, uid)


def _people_text(people: list[directory.PersonSummary], page: int) -> str:
    items, page, pages = _page(people, page)
    lines = [f"<b>Люди и назначения · {len(people)}</b>",
             f"Страница {page + 1}/{pages}",
             "Профиль, роли и фактические назначения. Незаполненное отмечено явно."]
    if not items:
        lines.append("\nВ разделе пока нет людей. Добавьте человека или ментора через /addmentor.")
        return '\n'.join(lines)
    for p in items:
        marks = []
        if p.is_mentor:
            marks.append('ментор')
        if p.is_admin:
            marks.append('администратор школы')
        tail = f" · {', '.join(marks)}" if marks else ''
        details = []
        if p.status:
            details.append(directory.status_label(p.status))
        if p.team:
            details.append(directory.team_label(p.team))
        if p.active_assignments:
            details.append(f"зон: {p.active_assignments}")
        summary = ' · '.join(details) if details else 'не заполнено'
        lines.append(f"\n<b>{mui.e(p.name, 70)}</b>{tail}\n{summary}")
    return '\n'.join(lines)


def _people_kb(people: list[directory.PersonSummary], page: int):
    items, page, _ = _page(people, page)
    rows = [[mui.b(mui.clip(p.name, 45), f'dirl:view:{p.telegram_id}:{page}')] for p in items]
    if nav := mui.nav(len(people), page, 'dirl:list'):
        rows.append(nav)
    rows.append([mui.b('👥 Зоны ответственности', 'dirl:zones')])
    rows.append([mui.b('🗂 История без аккаунта', 'dirl:unlinked:0')])
    rows.append([mui.b('➕ Добавить человека', 'dirl:add')])
    rows.append([mui.b('← Управление', 'ms:admin')])
    return mui.kb(*rows)


def _card_text(card: dict) -> str:
    user = card['user']
    member = card['member']
    uid = card['uid']
    lines = [f"<b>{mui.e(directory.display_name(user, uid), 100)}</b>",
             directory.contact(user, uid),
             f"Статус: {directory.status_label(member.status if member else None)}",
             f"Контур: {directory.team_label(member.team if member else None)}",
             f"Должность: {mui.e(member.title, 120) if member and member.title else 'не заполнено'}",
             f"В реестре менторов: {'да' if card['mentor'] else 'нет'}",
             f"Администратор школы: {'да' if card['is_admin'] else 'нет'}"]
    lines.append('\n<b>Роль и предметы</b>')
    roles = _role_lines(card['roles'])
    lines += roles or ['не заполнено']
    lines.append('\n<b>Текущие зоны ответственности</b>')
    if card['current']:
        for item in card['current']:
            subject = f" · {mui.e(item['subject'], 80)}" if item['subject'] else ''
            lines.append(f"• {item['zone_label']}{subject} · с {_date(item['started_at'])}")
    else:
        lines.append('не назначено')
    lines.append('\n<b>Связанные данные в боте</b>')
    lines.append(f"Занятия: Ведущий {card['sessions_as_mentor']} · Автор {card['sessions_as_author']}")
    lines.append(f"Материалы: {card['materials']} · отчёты: недельных {card['weekly_reports']}, "
                 f"итоговых {card['final_reports']}")
    if card['recent_sessions']:
        lines.append('Последние занятия:')
        for lesson in card['recent_sessions'][:3]:
            lines.append(f"· #{lesson.id} · {_date(lesson.starts_at)}")
    if not user:
        lines.append('\nПрофиль не создан: человек ещё не нажимал /start.')
    else:
        lines.append('\nИсточник: users, mentors, mentor_roles и связанные записи бота. '
                     'Незаполненные поля не означают, что человек не работал.')
    return '\n'.join(lines)


def _card_kb(uid: int, page: int):
    return mui.kb(
        [mui.b('✏️ Изменить профиль', f'dirl:edit:{uid}:{page}')],
        [mui.b('📜 История зон', f'dirl:history:{uid}:{page}'),
         mui.b('📅 Занятия', f'dirl:sessions:{uid}:{page}')],
        [mui.b('📝 Отчёты', f'dirl:reports:{uid}:{page}')],
        [mui.b('➕ Назначить зону', 'dirl:assign')],
        [mui.b('← Люди', f'dirl:list:{page}')],
    )


def _edit_text(card: dict, page: int) -> str:
    member = card['member']
    return '\n'.join([
        f"<b>Профиль · {mui.e(directory.display_name(card['user'], card['uid']), 100)}</b>",
        f"Статус: {directory.status_label(member.status if member else None)}",
        f"Контур: {directory.team_label(member.team if member else None)}",
        f"Должность: {mui.e(member.title, 120) if member and member.title else 'не заполнено'}",
        '',
        'Изменения сохраняются только после подтверждения. Имя и Telegram берутся из профиля Telegram.',
    ])


def _edit_kb(uid: int, page: int):
    return mui.kb(
        [mui.b('Статус участия', f'dirl:field:{uid}:status:{page}')],
        [mui.b('Контур команды', f'dirl:field:{uid}:team:{page}')],
        [mui.b('Должность', f'dirl:title:{uid}:{page}')],
        [mui.b('← Карточка', f'dirl:view:{uid}:{page}')],
    )


def _field_kb(uid: int, field: str, page: int):
    if field == 'status':
        rows = [[mui.b(label, f'dirl:preview:{uid}:status:{key}:{page}')]
                for key, label in directory.STATUS_LABELS.items()]
    elif field == 'team':
        rows = [[mui.b(label, f'dirl:preview:{uid}:team:{key}:{page}')]
                for key, label in directory.TEAM_LABELS.items()]
    else:
        raise svc.RuleError('Неизвестное поле профиля.')
    rows.append([mui.b('← Профиль', f'dirl:edit:{uid}:{page}')])
    return mui.kb(*rows)


def _preview_text(uid: int, field: str, value: str, old: str | None) -> str:
    labels = directory.STATUS_LABELS if field == 'status' else directory.TEAM_LABELS
    return '\n'.join([
        '<b>Подтвердите изменение</b>',
        f"Человек: <code>{uid}</code>",
        f"Поле: {'статус участия' if field == 'status' else 'контур команды'}",
        f"Было: {labels.get(old or '', 'не заполнено')}",
        f"Станет: {labels.get(value, value)}",
        '',
        'После сохранения карточка обновится. История назначений не меняется.',
    ])


def _zones_text(data: dict) -> str:
    lines = ['<b>Зоны ответственности</b>',
             'Первая версия: предмет, расписание, материалы. Пустая зона показана как «не назначен».',
             '', f"<b>Расписание</b>: {_zone_line(data['schedule'])}",
             f"<b>Материалы</b>: {_zone_line(data['materials'])}", '', '<b>Предметы</b>']
    if not data['subjects']:
        lines.append('Предметов в боте пока нет.')
    for row in data['subjects']:
        lines.append(f"• {mui.e(row['subject'].name, 80)} — {_zone_line(row['assignment'])}")
    return '\n'.join(lines)


def _zones_kb():
    return mui.kb(
        [mui.b('👥 Люди', 'dirl:list:0'), mui.b('➕ Назначить', 'dirl:assign')],
        [mui.b('🏁 Завершить назначение', 'dirl:end:0')],
        [mui.b('← Управление', 'ms:admin')],
    )


def _assign_zones_kb():
    return mui.kb(
        [mui.b('Предмет', 'dirl:assign:zone:subject')],
        [mui.b('Расписание', 'dirl:assign:zone:schedule')],
        [mui.b('Материалы', 'dirl:assign:zone:materials')],
        [mui.b('← Зоны', 'dirl:zones')],
    )


def _subjects_text(subjects: list, page: int):
    items, page, pages = _page(subjects, page)
    lines = ['<b>Предмет для ответственного</b>', f"Страница {page + 1}/{pages}"]
    if not items:
        lines.append('Предметов в боте пока нет.')
    else:
        for subject, assignment in items:
            mark = ' — есть ответственный' if assignment else ' — не назначен'
            lines.append(f"• {mui.e(subject.name, 80)}{mark}")
    return '\n'.join(lines), page, pages


def _subjects_kb(subjects: list, page: int):
    items, page, _ = _page(subjects, page)
    rows = [[mui.b(mui.clip(subject.name, 45), f'dirl:assign:subject:{subject.id}:{page}')]
            for subject, _assignment in items]
    if nav := mui.nav(len(subjects), page, 'dirl:assign:subjects'):
        rows.append(nav)
    rows.append([mui.b('← Зоны', 'dirl:zones')])
    return mui.kb(*rows)


def _pick_text(zone: str, subject, people: list, page: int):
    items, page, pages = _page(people, page)
    title = f"Ответственный: {directory.zone_label(zone)}"
    if subject is not None:
        title += f" · {subject.name}"
    lines = [f"<b>{mui.e(title, 120)}</b>", f"Страница {page + 1}/{pages}",
             'Выберите человека из стаффа или менторов.']
    for p in items:
        status = directory.status_label(p.status)
        lines.append(f"• {mui.e(p.name, 70)} — {status}")
    if not items:
        lines.append('Нет доступных людей. Добавьте человека или ментора.')
    return '\n'.join(lines), page, pages


def _pick_kb(zone: str, sid: int, people: list, page: int):
    items, page, _ = _page(people, page)
    rows = [[mui.b(mui.clip(p.name, 45), f'dirl:assign:pick:{zone}:{sid}:{p.telegram_id}')]
            for p in items]
    prefix = f'dirl:assign:people:{zone}:{sid}'
    if nav := mui.nav(len(people), page, prefix):
        rows.append(nav)
    rows.append([mui.b('← Зоны', 'dirl:zones')])
    return mui.kb(*rows)


def _assign_preview_text(zone: str, subject, old, new_name: str) -> str:
    lines = ['<b>Подтвердите назначение</b>',
             f"Зона: {directory.zone_label(zone)}"]
    if subject is not None:
        lines.append(f"Предмет: {mui.e(subject.name, 100)}")
    if old is None:
        lines.append('Сейчас: не назначен')
    else:
        old_name = old.get('person_name') or f"ID {old.get('person_id')}"
        lines.append(f"Сейчас: {mui.e(old_name, 70)} · с {_date(old.get('started_at'))}")
        lines.append('Текущее назначение будет завершено; история сохранится.')
    lines.append(f"Станет: {mui.e(new_name, 70)}")
    return '\n'.join(lines)


def _end_text(items: list[dict], page: int):
    page_items, page, pages = _page(items, page)
    lines = ['<b>Завершить назначение</b>', f"Страница {page + 1}/{pages}",
             'Завершённое назначение остаётся в истории.']
    if not page_items:
        lines.append('\nАктивных назначений нет.')
    for item in page_items:
        subject = f" · {mui.e(item['subject'], 80)}" if item['subject'] else ''
        name = item.get('person_name') or f"ID {item['person_id']}"
        lines.append(f"\n#{item['row'].id} · {item['zone_label']}{subject}\n"
                     f"{mui.e(name, 70)} · с {_date(item['started_at'])}")
    return '\n'.join(lines), page, pages


def _end_kb(items: list[dict], page: int):
    page_items, page, _ = _page(items, page)
    rows = [[mui.b(f"Завершить #{item['row'].id}", f'dirl:end:ask:{item["row"].id}:{page}')]
            for item in page_items]
    if nav := mui.nav(len(items), page, 'dirl:end'):
        rows.append(nav)
    rows.append([mui.b('← Зоны', 'dirl:zones')])
    return mui.kb(*rows)


def _history_text(uid: int, rows: list[dict], page: int):
    items, page, pages = _page(rows, page)
    lines = [f'<b>История зон · <code>{uid}</code></b>', f"Страница {page + 1}/{pages}"]
    if not items:
        lines.append('Назначений в боте нет.')
    for item in items:
        subject = f" · {mui.e(item['subject'], 80)}" if item['subject'] else ''
        end = 'сейчас' if item['ended_at'] is None else _date(item['ended_at'])
        lines.append(f"• {item['zone_label']}{subject} · {_date(item['started_at'])} → {end}")
    return '\n'.join(lines)


def _sessions_text(uid: int, rows: list, page: int):
    items, page, pages = _page(rows, page)
    lines = [f'<b>Занятия · <code>{uid}</code></b>', f"Страница {page + 1}/{pages}"]
    if not items:
        lines.append('Связанных занятий в боте нет.')
    for lesson in items:
        lines.append(f"• #{lesson.id} · {_date(lesson.starts_at)} · {mui.STATUS.get(lesson.status, lesson.status)}")
    return '\n'.join(lines)


def _unlinked_text(rows: list[StaffHistory], page: int) -> str:
    items, page, pages = _page(rows, page)
    lines = ['<b>История без аккаунта</b>', f"Страница {page + 1}/{pages}",
             'Вышедшие люди сохраняются без Telegram ID. Назначить их нельзя: в боте нет учётной записи.']
    if not items:
        lines.append('\nЗаписей пока нет.')
    for row in items:
        lines.append(f"\n<b>{mui.e(row.display_name, 100)}</b>"
                     + (f" · @{mui.e(row.username, 60)}" if row.username else '')
                     + f"\n{directory.status_label(row.status)} · "
                       f"{directory.team_label(row.team)}"
                     + (f" · {mui.e(row.title, 120)}" if row.title else ''))
    return '\n'.join(lines)


def _unlinked_kb(rows: list[StaffHistory], page: int):
    items, page, _ = _page(rows, page)
    buttons = [[mui.b(mui.clip(row.display_name, 45), f'dirl:unlinkedview:{row.id}')]
               for row in items]
    if nav := mui.nav(len(rows), page, 'dirl:unlinked'):
        buttons.append(nav)
    buttons.append([mui.b('← Люди', 'dirl:list:0')])
    return mui.kb(*buttons)


async def _show_card(callback: CallbackQuery, sm, settings: Settings, uid: int, page: int,
                     prefix: str = '') -> None:
    async with sm() as s:
        card = await directory.person_card(s, settings, uid)
    text = (prefix + '\n\n' if prefix else '') + _card_text(card)
    await mui.show(callback, text, _card_kb(uid, page))


async def _show_list(callback: CallbackQuery, sm, settings, page: int) -> None:
    async with sm() as s:
        people = await directory.list_people(s, settings)
    _, page, _ = _page(people, page)
    await mui.show(callback, _people_text(people, page), _people_kb(people, page))


async def _show_zones(callback: CallbackQuery, sm, settings, prefix: str = '') -> None:
    async with sm() as s:
        data = await directory.overview(s, settings)
    text = (prefix + '\n\n' if prefix else '') + _zones_text(data)
    await mui.show(callback, text, _zones_kb())


async def _route(callback: CallbackQuery, state: FSMContext, viewer: repo.Viewer,
                 settings: Settings, sm: async_sessionmaker) -> None:
    directory.require_admin(viewer)
    data = callback.data or ''
    parts = data.split(':')
    op = parts[1] if len(parts) > 1 else ''

    if op == 'list':
        await _show_list(callback, sm, settings, int(parts[2]) if len(parts) > 2 else 0)
        return

    if op == 'view':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        await _show_card(callback, sm, settings, uid, page)
        return

    if op == 'zones':
        await _show_zones(callback, sm, settings)
        return

    if op == 'edit':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        async with sm() as s:
            card = await directory.person_card(s, settings, uid)
        await mui.show(callback, _edit_text(card, page), _edit_kb(uid, page))
        return

    if op == 'field':
        uid, field = int(parts[2]), parts[3]
        page = int(parts[4]) if len(parts) > 4 else 0
        if field not in ('status', 'team'):
            raise svc.RuleError('Неизвестное поле профиля.')
        await mui.show(callback, '<b>Выберите значение</b>', _field_kb(uid, field, page))
        return

    if op == 'preview':
        uid, field, value = int(parts[2]), parts[3], parts[4]
        page = int(parts[5]) if len(parts) > 5 else 0
        async with sm() as s:
            card = await directory.person_card(s, settings, uid)
        old = (card['member'].status if field == 'status' and card['member'] else
               card['member'].team if field == 'team' and card['member'] else None)
        await mui.show(callback, _preview_text(uid, field, value, old),
                       mui.kb([mui.b('Подтвердить', f'dirl:apply:{uid}:{field}:{value}:{page}')],
                              [mui.b('← Профиль', f'dirl:edit:{uid}:{page}')]))
        return

    if op == 'apply':
        uid, field, value = int(parts[2]), parts[3], parts[4]
        page = int(parts[5]) if len(parts) > 5 else 0
        async with sm() as s:
            if field == 'status':
                await directory.set_status(s, viewer, uid, value)
            elif field == 'team':
                await directory.set_team(s, viewer, uid, value)
            else:
                raise svc.RuleError('Неизвестное поле профиля.')
            await s.commit()
        await _show_card(callback, sm, settings, uid, page, '✅ Изменение сохранено.')
        return

    if op == 'title':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        await state.clear()
        await state.update_data(uid=uid, page=page)
        await state.set_state(DirectoryForm.title)
        await mui.show(callback, 'Введите должность или роль в команде, до 120 символов.\n'
                                 '«-» очищает поле. /cancel — отмена.')
        return

    if op == 'applytitle':
        token = parts[2] if len(parts) > 2 else ''
        stored = await state.get_data()
        if not token or token != stored.get('title_token'):
            raise svc.RuleError('Черновик устарел. Откройте профиль заново.')
        uid, page, title = stored['uid'], stored.get('page', 0), stored.get('pending_title', '')
        async with sm() as s:
            await directory.set_title(s, viewer, uid, title)
            await s.commit()
        await state.clear()
        await _show_card(callback, sm, settings, uid, page, '✅ Должность сохранена.')
        return

    if op == 'add':
        await state.clear()
        await state.set_state(DirectoryForm.add_person)
        await mui.show(callback, 'Отправьте @username или Telegram ID человека.\n'
                                 'Человек должен хотя бы раз нажать /start. /cancel — отмена.')
        return

    if op == 'unlinked':
        page = int(parts[2]) if len(parts) > 2 else 0
        async with sm() as s:
            rows = list(await s.scalars(select(StaffHistory).order_by(StaffHistory.display_name)))
        await mui.show(callback, _unlinked_text(rows, page), _unlinked_kb(rows, page))
        return

    if op == 'unlinkedview':
        row_id = int(parts[2])
        async with sm() as s:
            row = await s.get(StaffHistory, row_id)
        if row is None:
            raise svc.RuleError('Запись не найдена.')
        text = '\n'.join([
            f"<b>{mui.e(row.display_name, 100)}</b>",
            f"Username: @{mui.e(row.username, 60)}" if row.username else 'Username: не заполнено',
            f"Статус: {directory.status_label(row.status)}",
            f"Контур: {directory.team_label(row.team)}",
            f"Должность: {mui.e(row.title, 120) if row.title else 'не заполнено'}",
            f"Источник: {mui.e(row.source, 200) if row.source else 'не заполнено'}",
            '',
            'Учётной записи в Telegram-боте нет. Профиль сохранён для истории; '
            'назначить этого человека нельзя, пока он не нажмёт /start.',
        ])
        await mui.show(callback, text, mui.kb([mui.b('← История', 'dirl:unlinked:0')]))
        return

    if op == 'assign':
        if len(parts) == 2:
            await mui.show(callback, '<b>Кому назначить ответственность?</b>', _assign_zones_kb())
            return
        sub = parts[2]
        if sub == 'zone':
            zone = parts[3]
            if zone not in RESPONSIBILITY_ZONES:
                raise svc.RuleError('Неизвестная зона.')
            if zone == 'subject':
                async with sm() as s:
                    subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
                    data = await directory.overview(s, settings)
                pairs = [(subject, next((row['assignment'] for row in data['subjects']
                                         if row['subject'].id == subject.id), None))
                         for subject in subjects]
                text, page, _ = _subjects_text(pairs, 0)
                await mui.show(callback, text, _subjects_kb(pairs, page))
            else:
                async with sm() as s:
                    people = [p for p in await directory.list_people(s, settings)
                              if p.user_exists and p.status != 'left']
                text, page, _ = _pick_text(zone, None, people, 0)
                await mui.show(callback, text, _pick_kb(zone, 0, people, page))
            return
        if sub == 'subjects':
            page = int(parts[3]) if len(parts) > 3 else 0
            async with sm() as s:
                subjects = list(await s.scalars(select(Subject).order_by(Subject.name_key)))
                data = await directory.overview(s, settings)
            pairs = [(subject, next((row['assignment'] for row in data['subjects']
                                     if row['subject'].id == subject.id), None))
                     for subject in subjects]
            text, page, _ = _subjects_text(pairs, page)
            await mui.show(callback, text, _subjects_kb(pairs, page))
            return
        if sub == 'subject':
            sid, page = int(parts[3]), int(parts[4]) if len(parts) > 4 else 0
            async with sm() as s:
                subject = await s.get(Subject, sid)
                if subject is None:
                    raise svc.RuleError('Предмет не найден.')
                people = [p for p in await directory.list_people(s, settings)
                          if p.user_exists and p.status != 'left']
            text, page, _ = _pick_text('subject', subject, people, page)
            await mui.show(callback, text, _pick_kb('subject', sid, people, page))
            return
        if sub == 'people':
            zone = parts[3]
            sid = int(parts[4]) if len(parts) > 4 else 0
            page = int(parts[5]) if len(parts) > 5 else 0
            async with sm() as s:
                subject = await s.get(Subject, sid) if sid else None
                people = [p for p in await directory.list_people(s, settings)
                          if p.user_exists and p.status != 'left']
            text, page, _ = _pick_text(zone, subject, people, page)
            await mui.show(callback, text, _pick_kb(zone, sid, people, page))
            return
        if sub == 'pick':
            zone, sid, uid = parts[3], int(parts[4]), int(parts[5])
            async with sm() as s:
                subject = await s.get(Subject, sid) if sid else None
                old = await directory.current_assignment(s, zone, sid if zone == 'subject' else None)
                old_user = await s.get(User, old['person_id']) if old else None
                if old:
                    old['person_name'] = directory.display_name(old_user, old['person_id'])
                    old['person_username'] = old_user.username if old_user else None
                new_user = await s.get(User, uid)
                new_name = directory.display_name(new_user, uid)
            await mui.show(callback, _assign_preview_text(zone, subject, old, new_name),
                           mui.kb([mui.b('Подтвердить', f'dirl:assign:apply:{zone}:{sid}:{uid}')],
                                  [mui.b('← Зоны', 'dirl:zones')]))
            return
        if sub == 'apply':
            zone, sid, uid = parts[3], int(parts[4]), int(parts[5])
            async with sm() as s:
                old, new = await directory.assign(s, viewer, settings, zone,
                                                  sid if zone == 'subject' else None, uid)
                await s.commit()
                name = await _person_name(s, uid)
            await _show_zones(callback, sm, settings, f'✅ Назначено: {mui.e(name, 70)}.')
            return
        raise svc.RuleError('Неизвестное действие назначения.')

    if op == 'end':
        if len(parts) == 2 or parts[2].isdigit():
            page = int(parts[2]) if len(parts) > 2 else 0
            async with sm() as s:
                data = await directory.overview(s, settings)
            items = data['all_active']
            text, page, _ = _end_text(items, page)
            await mui.show(callback, text, _end_kb(items, page))
            return
        if parts[2] == 'ask':
            aid, page = int(parts[3]), int(parts[4]) if len(parts) > 4 else 0
            async with sm() as s:
                data = await directory.overview(s, settings)
            item = next((row for row in data['all_active'] if row['row'].id == aid), None)
            if item is None:
                raise svc.RuleError('Назначение не найдено или уже завершено.')
            subject = f" · {mui.e(item['subject'], 80)}" if item['subject'] else ''
            text = ('<b>Завершить назначение?</b>\n'
                    f"{item['zone_label']}{subject}\n"
                    f"{mui.e(item.get('person_name', ''), 70)} · с {_date(item['started_at'])}\n\n"
                    'Запись останется в истории; текущая зона станет свободной.')
            await mui.show(callback, text, mui.kb(
                [mui.b('Завершить', f'dirl:end:apply:{aid}:{page}')],
                [mui.b('← Зоны', 'dirl:zones')]))
            return
        if parts[2] == 'apply':
            aid, page = int(parts[3]), int(parts[4]) if len(parts) > 4 else 0
            async with sm() as s:
                await directory.end_assignment(s, viewer, aid)
                await s.commit()
            await _show_zones(callback, sm, settings, '✅ Назначение завершено.')
            return
        raise svc.RuleError('Неизвестное действие завершения.')

    if op == 'history':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        async with sm() as s:
            rows = await directory.assignment_history(s, uid)
        rows_kb = []
        if nav := mui.nav(len(rows), page, f'dirl:history:{uid}'):
            rows_kb.append(nav)
        rows_kb.append([mui.b('← Карточка', f'dirl:view:{uid}:{page}')])
        await mui.show(callback, _history_text(uid, rows, page), mui.kb(*rows_kb))
        return

    if op == 'sessions':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        async with sm() as s:
            rows = await directory.person_sessions(s, uid)
        items, page, _ = _page(rows, page)
        rows_kb = [[mui.b(f"#{lesson.id} · {_date(lesson.starts_at)}", f'ms:view:{lesson.id}')]
                   for lesson in items]
        if nav := mui.nav(len(rows), page, f'dirl:sessions:{uid}'):
            rows_kb.append(nav)
        rows_kb.append([mui.b('← Карточка', f'dirl:view:{uid}:{page}')])
        await mui.show(callback, _sessions_text(uid, rows, page), mui.kb(*rows_kb))
        return

    if op == 'reports':
        uid, page = int(parts[2]), int(parts[3]) if len(parts) > 3 else 0
        async with sm() as s:
            card = await directory.person_card(s, settings, uid)
            weekly = await directory.weekly_reports(s, uid)
        items, page, pages = _page(weekly, page)
        lines = [f'<b>Отчёты · <code>{uid}</code></b>', f"Страница {page + 1}/{pages}",
                 f"Недельных: {card['weekly_reports']} · итоговых: {card['final_reports']}"]
        if not weekly:
            lines.append('Недельных отчётов в боте нет.')
        else:
            lines.append('Недельные отчёты:')
        rows = [[mui.b(f"{report.week_start}", f'ms:mentorreportview:{report.id}')]
                for report in items]
        if nav := mui.nav(len(weekly), page, f'dirl:reports:{uid}'):
            rows.append(nav)
        rows.append([mui.b('← Карточка', f'dirl:view:{uid}:{page}')])
        await mui.show(callback, '\n'.join(lines), mui.kb(*rows))
        return

    raise svc.RuleError('Кнопка устарела. Откройте /staff.')


@router.message(Command("staff"))
async def cmd_staff(message: Message, viewer: repo.Viewer, settings: Settings,
                    sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, 'admin_only'))
        return
    async with sm() as s:
        people = await directory.list_people(s, settings)
    await message.answer(_people_text(people, 0), reply_markup=_people_kb(people, 0))


@router.callback_query(F.data.startswith('dirl:'))
async def cb_directory(callback: CallbackQuery, state: FSMContext, viewer: repo.Viewer,
                       settings: Settings, sm: async_sessionmaker) -> None:
    try:
        await _route(callback, state, viewer, settings, sm)
    except svc.RuleError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    except IntegrityError:
        await callback.answer('Назначение уже изменилось. Откройте зоны заново.', show_alert=True)
        return
    await callback.answer()


@router.message(DirectoryForm.add_person, F.text)
async def add_person_text(message: Message, state: FSMContext, viewer: repo.Viewer,
                          settings: Settings, sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, 'admin_only'))
        return
    try:
        async with sm() as s:
            member, created = await directory.add_person(s, viewer, message.text or '')
            await s.commit()
            uid = member.telegram_id
        await state.clear()
        prefix = '✅ Добавлен в реестр.' if created else 'Этот человек уже в реестре.'
        async with sm() as s:
            card = await directory.person_card(s, settings, uid)
        await message.answer(prefix + '\n\n' + _card_text(card), reply_markup=_card_kb(uid, 0))
    except svc.RuleError as exc:
        await message.answer(mui.e(exc, 1000))


@router.message(DirectoryForm.title, F.text)
async def title_text(message: Message, state: FSMContext, viewer: repo.Viewer,
                     settings: Settings, sm: async_sessionmaker) -> None:
    if not viewer.is_admin:
        await message.answer(t(viewer.lang, 'admin_only'))
        return
    data = await state.get_data()
    uid, page = data.get('uid'), data.get('page', 0)
    if not uid:
        await state.clear()
        await message.answer('Черновик устарел. Откройте профиль заново.')
        return
    value = (message.text or '').strip()
    if len(value) > 120:
        await message.answer('Должность — до 120 символов. /cancel — отмена.')
        return
    title = '' if value == '-' else value
    async with sm() as s:
        card = await directory.person_card(s, settings, uid)
    old = card['member'].title if card['member'] else None
    token = uuid4().hex
    await state.update_data(title_token=token, pending_title=title)
    await message.answer(
        '\n'.join(['<b>Подтвердите изменение должности</b>',
                   f"Человек: <code>{uid}</code>",
                   f"Было: {mui.e(old, 120) if old else 'не заполнено'}",
                   f"Станет: {mui.e(title, 120) if title else 'не заполнено'}"]),
        reply_markup=mui.kb([mui.b('Подтвердить', f'dirl:applytitle:{token}')],
                            [mui.b('← Карточка', f'dirl:view:{uid}:{page}')]))
