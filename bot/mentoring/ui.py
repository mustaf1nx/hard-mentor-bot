"""Rendering for the mentoring module. User-controlled text is escaped at the boundary."""
from __future__ import annotations

from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Settings
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
    if viewer.is_staff:
        rows += [[b('🎓 Веду занятия', 'ms:list:teaching:0:0')],
                 [b('➕ Материал', 'ms:wizard:material'), b('☑️ Чек-лист темы', 'ms:wizard:checklist')]]
    if viewer.is_admin:
        rows += [[b('➕ Создать занятие', 'ms:wizard:session')],
                 [b('🗂 Все занятия', 'ms:list:all:0:0'), b('📊 Сводка', 'ms:report')]]
    text = ('<b>Менторки</b>\n\n'
            'Выберите предмет, тему и удобное занятие. Перед менторкой можно задать вопрос, '
            'заполнить чек-лист и прочитать рекап-гайд.\n\n'
            'Записи на занятия <b>именные</b>. Обращения и их анонимность — отдельный раздел /feedback.')
    return text, kb(*rows)


def session_card(lesson: MentoringSession, names: tuple[str, str], mentor_name: str,
                 settings: Settings, count: int, booking: SessionBooking | None, viewer: Viewer):
    duration = int((lesson.ends_at - lesson.starts_at).total_seconds() // 60)
    lines = [f'<b>Менторка #{lesson.id}</b> · {STATUS[lesson.status]}',
             f'<b>{e(names[0], 100)} · {e(names[1], 120)}</b>',
             f'🕒 {svc.local_time(lesson.starts_at, settings)} · {duration} мин.',
             f'Часовой пояс: {e(settings.timezone, 60)}',
             f'🎓 Ментор: {e(mentor_name, 128)}', f'📍 {e(lesson.location, 300)}',
             f'Записано: <b>{count}</b>']
    if lesson.status == 'cancelled':
        lines += [f'Причина: {e(lesson.cancel_reason or "—", 400)}']
    if not lesson.checklist_items:
        lines += ['Чек-лист пока не добавлен. Это не препятствует записи.']
    if booking and booking.status == 'active':
        progress = sum(booking.answers.get(str(i)) in svc.SCORES for i in range(len(lesson.checklist_items)))
        lines += ['✅ Вы записаны.', f'Ваш вопрос: {e(booking.question, 400)}',
                  f'Чек-лист: {progress}/{len(lesson.checklist_items)}',
                  f'Посещаемость: {ATTENDANCE[booking.attendance]}']
    rows = []
    from ..db import utcnow
    upcoming = lesson.status == 'scheduled' and lesson.starts_at > utcnow()
    if upcoming and lesson.mentor_id != viewer.id:
        rows.append([b('✏️ Изменить вопрос' if booking and booking.status == 'active' else '📝 Записаться', f'ms:join:{lesson.id}')])
        if booking and booking.status == 'active':
            if lesson.checklist_items:
                rows.append([b('☑️ Заполнить / изменить чек-лист', f'ms:check:{booking.id}:next')])
            rows.append([b('Убрать мою запись', f'ms:leave:{lesson.id}:ask')])
    rows.append([b('📚 Материалы темы', f'ms:bank:{lesson.topic_id}:0')])
    if svc.can_manage(viewer, lesson):
        rows += [[b('👥 Участники и вопросы', f'ms:people:{lesson.id}:0')],
                 [b('📊 Понимание тем', f'ms:summary:{lesson.id}')]]
        if lesson.status != 'cancelled':
            rows += [[b('✅ Посещаемость', f'ms:attendance:{lesson.id}:0')],
                     [b('📎 Загрузить материалы занятия', f'ms:upload:{lesson.id}')]]
        if lesson.status == 'scheduled':
            rows += [[b('☑️ Обновить чек-лист из банка', f'ms:refresh:{lesson.id}')],
                     [b('🏁 Завершить занятие', f'ms:complete:{lesson.id}:ask')]]
    if viewer.is_admin and lesson.status == 'scheduled':
        rows += [[b('🕒 Перенести', f'ms:edit:{lesson.id}:start'), b('📍 Место', f'ms:edit:{lesson.id}:location')],
                 [b('🎓 Сменить ментора', f'ms:edit:{lesson.id}:mentor')],
                 [b('❌ Отменить занятие', f'ms:cancel:{lesson.id}')]]
    rows += [[b('← Менторки', 'ms:home')]]
    return '\n'.join(lines), kb(*rows)


def checklist_card(lesson: MentoringSession, booking: SessionBooking, index: int | None = None):
    items = lesson.checklist_items
    if not items:
        return 'Чек-лист этой темы пока не добавлен.', kb([b('← К занятию', f'ms:view:{lesson.id}')])
    if index is None:
        index = next((i for i in range(len(items)) if booking.answers.get(str(i)) not in svc.SCORES), None)
    if index is None:
        lines = [f'<b>Чек-лист занятия #{lesson.id} заполнен.</b>',
                 'Это самооценка, не тест и не оценка за предмет. Ответы помогут ментору подготовиться.']
        rows = []
        for i, item in enumerate(items):
            rows.append([b(f'{i + 1}. {clip(item, 35)} · {svc.SCORES[booking.answers[str(i)]]}', f'ms:check:{booking.id}:{i}')])
        rows += [[b('← К занятию', f'ms:view:{lesson.id}')]]
        return '\n'.join(lines), kb(*rows)
    if not 0 <= index < len(items):
        raise svc.RuleError('Пункт чек-листа не найден.')
    text = (f'<b>Чек-лист · пункт {index + 1}/{len(items)}</b>\n\n{e(items[index], svc.MAX_ITEM_LENGTH)}\n\n'
            'Как вы понимаете эту тему? Здесь нет правильных или неправильных ответов.')
    rows = [[b(label, f'ms:answer:{booking.id}:{index}:{score}')] for score, label in svc.SCORES.items()]
    rows += [[b('← К занятию', f'ms:view:{lesson.id}')]]
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
