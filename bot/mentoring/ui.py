"""Rendering for the mentoring module. User-controlled text is escaped at the boundary."""
from __future__ import annotations

from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Settings
from .. import clock
from ..db import MentoringSession, SessionBooking
from ..repo import Viewer
from ..views import clip, u16len
from . import service as svc

STATUS = {'scheduled': 'Запланировано', 'completed': 'Проведено', 'cancelled': 'Отменено'}
ATTENDANCE = {'unknown': 'Не отмечен', 'present': 'Присутствовал', 'absent': 'Отсутствовал'}


def e(text, limit=500) -> str:
    return escape(clip(str(text), limit))


def b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def page_slice(items, page: int):
    page = max(0, min(page, max(0, (len(items) - 1) // svc.PAGE_SIZE)))
    return items[page * svc.PAGE_SIZE:(page + 1) * svc.PAGE_SIZE], page


def nav(total: int, page: int, prefix: str) -> list:
    buttons = []
    if page:
        buttons.append(b('◀️', f'{prefix}:{page - 1}'))
    if (page + 1) * svc.PAGE_SIZE < total:
        buttons.append(b('▶️', f'{prefix}:{page + 1}'))
    return buttons


async def show(target: Message | CallbackQuery, text: str, markup=None) -> None:
    """Edit callback messages when possible. Ignore harmless repeated clicks."""
    if isinstance(target, CallbackQuery):
        if not isinstance(target.message, Message):
            return
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'not modified' not in str(exc).lower():
                await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def long_text(message: Message, text: str) -> None:
    """Split at whole lines, never in the middle of an HTML tag/entity."""
    lines, size = [], 0
    for line in text.splitlines():
        if lines and size + u16len(line) + 1 > 3700:
            await message.answer('\n'.join(lines))
            lines, size = [], 0
        lines.append(line)
        size += u16len(line) + 1
    if lines:
        await message.answer('\n'.join(lines))


def home(viewer: Viewer):
    rows = [[b('📅 Доступные занятия', 'ms:subjects:schedule:0')],
            [b('📝 Мои записи', 'ms:list:bookings:0:0')],
            [b('📚 Банк материалов', 'ms:subjects:bank:0')]]
    if viewer.mentor:
        rows += [[b('🎓 Веду / готовлю', 'ms:list:teaching:0:0')],
                 [b('📝 Сдать отчёт', 'ms:mentorreport:0')]]
    if viewer.is_admin:
        # Инструменты координатора живут на отдельном экране: в меню их было 12 штук подряд.
        rows += [[b('⚙️ Управление', 'ms:admin')]]
    text = ('<b>Менторки</b>\n\n'
            'Выберите предмет, тему и удобное занятие. Перед менторкой можно задать вопрос, '
            'заполнить чек-лист и прочитать рекап-гайд.\n\n'
            'Записи на занятия <b>именные</b>. Обращения и их анонимность — отдельный раздел /feedback.')
    return text, kb(*rows)


def admin_home():
    rows = [[b('➕ Создать занятие', 'ms:wizard:session')],
            [b('➕ Материал', 'ms:wizard:material'), b('☑️ Чек-лист темы', 'ms:wizard:checklist')],
            [b('📋 Отчёты менторов', 'ms:mentorreports:0:0')],
            [b('👥 Люди и назначения', 'dirl:list:0')],
            [b('🗂 Все занятия', 'ms:list:all:0:0')],
            [b('📊 Сводка', 'dash:overview')],
            [b('← Менторки', 'ms:home')]]
    text = ('<b>Управление</b>\n\n'
            'Инструменты координатора: занятия, материалы, отчёты менторов, люди и назначения, сводка.\n'
            'Студенческие и менторские разделы — на предыдущем экране.')
    return text, kb(*rows)


def _next_step(lesson: MentoringSession, viewer: Viewer, feedback: list,
               final_materials: int, attendance_ready: bool | None):
    """Подсказка Ведущему. Не выдаёт отсутствующие данные за выполненные."""
    if not (svc.can_manage(viewer, lesson) or viewer.is_admin):
        return None, None
    now = clock.now()
    if lesson.status == 'scheduled':
        if lesson.starts_at > now:
            return ('Следующий шаг: посмотрите участников и подготовьтесь к занятию.',
                    ('👥 Участники', f'ms:people:{lesson.id}:0'))
        if lesson.ends_at > now:
            return ('Занятие идёт. После него отметьте посещаемость — это откроет завершение.', None)
        if not attendance_ready:
            return ('Следующий шаг: отметьте посещаемость каждого участника.',
                    ('✅ Отметить посещаемость', f'ms:attendance:{lesson.id}:0'))
        return ('Посещаемость отмечена. Следующий шаг — завершить занятие.',
                ('🏁 Завершить занятие', f'ms:complete:{lesson.id}:ask'))
    if lesson.status != 'completed':
        return None, None
    if not final_materials:
        if viewer.is_admin or lesson.author_id == viewer.id:
            return ('Следующий шаг: загрузите итоговый материал и дождитесь методпроверки.',
                    ('📎 Итоговый материал', f'ms:upload:{lesson.id}'))
        return ('Итоговый материал ещё не загружен. Это делает Автор или координатор.', None)
    written = sum(1 for row in feedback or [] if svc.has_written_feedback(row))
    if not written:
        return ('Материал проверен, но письменного отзыва ещё нет — цикл не закрыт.', None)
    return ('Цикл закрыт: занятие проведено, итоговый материал проверен, письменный отзыв есть.', None)


def session_card(lesson: MentoringSession, names: tuple[str, str], mentor_name: str,
                 settings: Settings, count: int, booking: SessionBooking | None, viewer: Viewer,
                 feedback: list | None = None, final_materials: int = 0,
                 attendance_ready: bool | None = None):
    duration = int((lesson.ends_at - lesson.starts_at).total_seconds() // 60)
    lines = [f'<b>Менторка #{lesson.id}</b> · {STATUS[lesson.status]}',
             f'<b>{e(names[0], 100)} · {e(names[1], 120)}</b>',
             f'Формат: {e(svc.FORMAT_LABELS.get(lesson.format, "—") if lesson.format_confirmed else "не подтверждён", 60)}',
             f'🕒 {svc.local_time(lesson.starts_at, settings)} · {duration} мин.',
             f'Часовой пояс: {e(settings.timezone, 60)}',
             f'🎓 Ментор: {e(mentor_name, 128)}', f'📍 {e(lesson.location, 300)}']
    if count or svc.can_manage(viewer, lesson) or viewer.is_admin:
        lines.append(f'Записано: <b>{count}</b>')
    if lesson.status == 'cancelled':
        lines += [f'Причина: {e(lesson.cancel_reason or "—", 400)}']
    if lesson.status == 'completed' and (svc.can_manage(viewer, lesson) or viewer.is_admin):
        rows_feedback = feedback or []
        written = sum(svc.has_written_feedback(row) for row in rows_feedback)
        lines.append(f'Отзывы: {len(rows_feedback)} · письменных: {written}')
        if written:
            lines.append('Письменный отзыв получен.')
        elif rows_feedback:
            lines.append('Отзыв получен, но письменного комментария ещё нет.')
        else:
            lines.append('Отзывов пока нет.')
    next_hint, next_button = _next_step(lesson, viewer, feedback or [], final_materials,
                                        attendance_ready)
    if next_hint:
        lines.append('\n<b>Что дальше:</b> ' + next_hint)
    if not lesson.checklist_items:
        lines += ['Чек-лист пока не добавлен. Это не препятствует записи.']
    if booking and booking.status == 'active':
        progress = sum(booking.answers.get(str(i)) in svc.SCORES for i in range(len(lesson.checklist_items)))
        lines += ['✅ Вы записаны.', f'Ваш вопрос: {e(booking.question, 400)}',
                  f'Чек-лист: {progress}/{len(lesson.checklist_items)}',
                  f'Посещаемость: {ATTENDANCE[booking.attendance]}']
    rows = []
    if next_button:
        rows.append([b(next_button[0], next_button[1])])
    upcoming = lesson.status == 'scheduled' and lesson.starts_at > clock.now()
    ended = lesson.status != 'cancelled' and lesson.ends_at <= clock.now()
    one_on_one_taken = (lesson.format_confirmed is True and lesson.format == 'one_on_one'
                        and count >= 1 and not (booking and booking.status == 'active'))
    if one_on_one_taken:
        lines.append('Консультация 1-на-1: место занято — формат рассчитан на одного студента.')
    if ended and booking and booking.status == 'active':
        # Форма ОС доступна и без напоминания — из карточки занятия (Прил. 11).
        rows.append([b('📝 Оставить отзыв о занятии', f'ms:fb:{lesson.id}')])
    if upcoming and lesson.mentor_id != viewer.id and not one_on_one_taken:
        rows.append([b('✏️ Изменить вопрос' if booking and booking.status == 'active' else '📝 Записаться', f'ms:join:{lesson.id}')])
        if booking and booking.status == 'active':
            if lesson.checklist_items:
                rows.append([b('☑️ Заполнить / изменить чек-лист', f'ms:check:{booking.id}:next')])
            rows.append([b('Убрать мою запись', f'ms:leave:{lesson.id}:ask')])
    rows.append([b('📚 Материалы темы', f'ms:bank:{lesson.topic_id}:0')])
    if lesson.author_id == viewer.id and lesson.status != 'cancelled':
        rows.append([b('📎 Загрузить материал занятия', f'ms:upload:{lesson.id}')])
    if svc.can_manage(viewer, lesson) or viewer.is_admin:
        # Всё управление — за одной кнопкой: в карточке их было 15 подряд.
        rows.append([b('⚙️ Управление занятием', f'ms:manage:{lesson.id}')])
    rows += [[b('← Менторки', 'ms:home')]]
    return '\n'.join(lines), kb(*rows)


def manage_card(lesson: MentoringSession, viewer: Viewer, names: tuple[str, str],
                author_name: str | None = None):
    rows = []
    if lesson.status != 'cancelled':
        rows.append([b('👥 Участники и вопросы', f'ms:people:{lesson.id}:0')])
        rows.append([b('📊 Понимание тем', f'ms:summary:{lesson.id}')])
        rows.append([b('✅ Посещаемость', f'ms:attendance:{lesson.id}:0')])
        if viewer.is_admin or lesson.author_id == viewer.id:
            rows.append([b('📎 Загрузить материалы занятия', f'ms:upload:{lesson.id}')])
    if viewer.is_admin:
        rows.append([b('✍️ Назначить Автора', f'ms:authors:{lesson.id}:0')])
        if not lesson.format_confirmed and lesson.status == 'scheduled':
            rows.append([b('Подтвердить формат', f'ms:confirmformat:{lesson.id}')])
    if lesson.status == 'scheduled':
        rows.append([b('☑️ Обновить чек-лист из банка', f'ms:refresh:{lesson.id}')])
        rows.append([b('🏁 Завершить занятие', f'ms:complete:{lesson.id}:ask')])
    if viewer.is_admin and lesson.status == 'scheduled':
        rows.append([b('🕒 Перенести', f'ms:edit:{lesson.id}:start'),
                     b('📍 Место', f'ms:edit:{lesson.id}:location')])
        rows.append([b('🎓 Сменить ментора', f'ms:edit:{lesson.id}:mentor')])
        rows.append([b('❌ Отменить занятие', f'ms:cancel:{lesson.id}')])
    rows.append([b('← К занятию', f'ms:view:{lesson.id}')])
    text = (f'<b>Управление менторкой #{lesson.id}</b>\n'
            f'{e(names[0], 100)} · {e(names[1], 120)}\n'
            f'Статус: {STATUS[lesson.status]}\n'
            f"Автор: {e(author_name, 70) if author_name else ('не назначен' if lesson.author_id is None else '[__]')}\n\nЧто сделать?")
    return text, kb(*rows)


def checklist_card(lesson: MentoringSession, booking: SessionBooking, index: int | None = None,
                   summary: bool = False):
    items = lesson.checklist_items
    if not items:
        return 'Чек-лист этой темы пока не добавлен.', kb([b('← К занятию', f'ms:view:{lesson.id}')])
    answered_all = all(booking.answers.get(str(i)) in svc.SCORES for i in range(len(items)))
    if summary or (index is None and answered_all):
        done = sum(booking.answers.get(str(i)) in svc.SCORES for i in range(len(items)))
        head = (f'<b>Чек-лист занятия #{lesson.id}: отмечено {done} из {len(items)}</b>'
                if not answered_all else f'<b>Чек-лист занятия #{lesson.id} заполнен.</b>')
        lines = [head,
                 'Это самооценка, не тест и не оценка за предмет. Ответы помогут ментору подготовиться.',
                 'Любой пункт можно переоткрыть и изменить.']
        rows = []
        for i, item in enumerate(items):
            score = booking.answers.get(str(i))
            rows.append([b(f'{i + 1}. {clip(item, 32)} · {svc.SCORES.get(score, "—")}', f'ms:check:{booking.id}:{i}')])
        rows += [[b('← К занятию', f'ms:view:{lesson.id}')]]
        return '\n'.join(lines), kb(*rows)
    if index is None:
        index = next((i for i in range(len(items)) if booking.answers.get(str(i)) not in svc.SCORES), 0)
    if not 0 <= index < len(items):
        raise svc.RuleError('Пункт чек-листа не найден.')
    total = len(items)
    text = (f'<b>Чек-лист · пункт {index + 1}/{total}</b>\n\n{e(items[index], svc.MAX_ITEM_LENGTH)}\n\n'
            'Как вы понимаете эту тему? Здесь нет правильных или неправильных ответов.')
    rows = [[b(label, f'ms:answer:{booking.id}:{index}:{score}')] for score, label in svc.SCORES.items()]
    nav = []
    if index > 0:
        nav.append(b('← Предыдущий', f'ms:check:{booking.id}:{index - 1}'))
    if index + 1 < total:
        nav.append(b('Следующий →', f'ms:check:{booking.id}:{index + 1}'))
    if nav:
        rows.append(nav)
    if total > 1:
        rows.append([b('◀ К списку пунктов', f'ms:checklist:{booking.id}')])
    rows.append([b('← К занятию (чек-лист можно пропустить)', f'ms:view:{lesson.id}')])
    return text, kb(*rows)


def summary_text(lesson: MentoringSession, bookings: list[SessionBooking]) -> str:
    lines = [f'<b>Понимание тем · занятие #{lesson.id}</b>', f'Активных записей: {len(bookings)}',
             'П — понимаю; В — есть вопросы; Н — не понимаю; — не ответили.\n']
    for i, (item, counts) in enumerate(svc.checklist_summary(lesson, bookings), 1):
        lines += [f'{i}. {e(item, svc.MAX_ITEM_LENGTH)}',
                  f'П: {counts[2]} · В: {counts[1]} · Н: {counts[0]} · —: {counts["missing"]}']
    if not lesson.checklist_items:
        lines.append('Чек-лист не добавлен.')
    lines += ['\nЭто сводка самооценок студентов, а не результат проверки знаний.']
    return '\n'.join(lines)
