"""Session booking, preparation, attendance and material bank, independent of tickets."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import repo
from ..config import Settings
from ..db import LearningMaterial, MentoringSession, SessionBooking, Subject, Topic, TopicChecklist, User, utcnow
from ..mentoring import service as svc
from ..mentoring import ui
from ..repo import Viewer
from ..texts import MENU, menu_labels
from ..views import clip

entry_router = Router(name='mentoring-entry')
router = Router(name='mentoring')
MENU_TEXT = {label for item in MENU.values() for label in item.values()}
# Buttons from other modules must not become a new subject, a question or a filename.
DATA_TEXT = F.text & ~F.text.startswith('/') & ~F.text.in_(MENU_TEXT)


class MentoringForm(StatesGroup):
    subject = State()
    topic = State()
    mentor = State()
    start = State()
    duration = State()
    location = State()
    session_confirm = State()
    question = State()
    checklist = State()
    kind = State()
    visibility = State()
    document = State()
    cancel_reason = State()
    cancel_confirm = State()
    edit_value = State()
    edit_confirm = State()


def num(raw: str) -> int:
    if not raw.isdigit() or len(raw) > 19 or int(raw) > (1 << 63) - 1:
        raise svc.RuleError('Некорректная кнопка. Откройте /sessions ещё раз.')
    return int(raw)


async def form_is(state: FSMContext, *states: State) -> None:
    if await state.get_state() not in {x.state for x in states}:
        raise svc.RuleError('Этот шаг уже закрыт. Откройте нужное действие ещё раз; /cancel отменяет заполнение.')


async def session_data(sm, viewer: Viewer, sid: int, settings: Settings):
    async with sm() as s:
        lesson = await svc.get_session(s, sid)
        booking = await svc.own_booking(s, sid, viewer.id)
        if (lesson.status != 'scheduled' or lesson.starts_at <= utcnow()) and not (
                svc.can_manage(viewer, lesson) or booking and booking.status == 'active'):
            raise svc.RuleError('Это занятие недоступно. Откройте список актуальных менторок.')
        names = await svc.topic_names(s, lesson.topic_id)
        mentor = await s.get(User, lesson.mentor_id)
        count = len(await svc.active_bookings(s, sid))
    return ui.session_card(lesson, names, mentor.display_name if mentor else str(lesson.mentor_id), settings, count, booking, viewer)


async def show_session(target, sm, viewer, sid, settings):
    text, markup = await session_data(sm, viewer, sid, settings)
    await ui.show(target, text, markup)


async def browse_subjects(target, sm, mode: str, page: int):
    if mode not in ('schedule', 'bank'):
        raise svc.RuleError('Неизвестный раздел.')
    async with sm() as s:
        all_items = await svc.subjects(s)
    items, page = ui.page_slice(all_items, page)
    rows = [[ui.b(x.name, f'ms:topics:{mode}:{x.id}:0')] for x in items]
    if nav := ui.nav(len(all_items), page, f'ms:subjects:{mode}'):
        rows.append(nav)
    rows += [[ui.b('← Менторки', 'ms:home')]]
    text = '<b>Выберите предмет</b>' if items else 'Предметов пока нет. Координатор может создать занятие, а ментор — добавить материал.'
    await ui.show(target, text, ui.kb(*rows))


async def browse_topics(target, sm, mode: str, subject_id: int, page: int):
    if mode not in ('schedule', 'bank'):
        raise svc.RuleError('Неизвестный раздел.')
    async with sm() as s:
        subject = await s.get(Subject, subject_id)
        if subject is None:
            raise svc.RuleError('Предмет не найден.')
        all_items = await svc.topics(s, subject_id)
    items, page = ui.page_slice(all_items, page)
    rows = [[ui.b(x.name, f'ms:bank:{x.id}:0' if mode == 'bank' else f'ms:list:upcoming:{x.id}:0')] for x in items]
    if nav := ui.nav(len(all_items), page, f'ms:topics:{mode}:{subject_id}'):
        rows.append(nav)
    rows += [[ui.b('← Предметы', f'ms:subjects:{mode}:0')]]
    await ui.show(target, f'<b>{ui.e(subject.name, 100)}</b>\nВыберите тему.' if items else 'Тем пока нет.', ui.kb(*rows))


async def show_list(target, sm, viewer, settings, mode: str, topic_id: int, page: int):
    if mode not in ('upcoming', 'bookings', 'teaching', 'all'):
        raise svc.RuleError('Неизвестный список.')
    async with sm() as s:
        all_items = await svc.session_list(s, viewer, mode, topic_id)
        items, page = ui.page_slice(all_items, page)
        rows = []
        for x in items:
            names = await svc.topic_names(s, x.topic_id)
            label = f'#{x.id} · {svc.local_time(x.starts_at, settings)} · {clip(names[0] + ": " + names[1], 35)}'
            if x.status != 'scheduled':
                label = ('✅ ' if x.status == 'completed' else '❌ ') + label
            rows.append([ui.b(label, f'ms:view:{x.id}')])
    if nav := ui.nav(len(all_items), page, f'ms:list:{mode}:{topic_id}'):
        rows.append(nav)
    if topic_id:
        rows.append([ui.b('📚 Материалы этой темы', f'ms:bank:{topic_id}:0')])
    rows += [[ui.b('← Менторки', 'ms:home')]]
    title = {'upcoming': 'Доступные занятия', 'bookings': 'Мои записи', 'teaching': 'Мои занятия', 'all': 'Все занятия'}[mode]
    await ui.show(target, f'<b>{title}</b> · всего {len(all_items)}\n' + ('Выберите занятие.' if items else 'Пока пусто.'), ui.kb(*rows))


async def show_bank(target, sm, viewer, topic_id: int, page: int):
    async with sm() as s:
        names = await svc.topic_names(s, topic_id)
        all_items = await svc.material_list(s, viewer, topic_id)
        template = await s.get(TopicChecklist, topic_id)
    items, page = ui.page_slice(all_items, page)
    text = f'<b>Банк · {ui.e(names[0], 100)} · {ui.e(names[1], 120)}</b>\n'
    text += f'Доступных документов: {len(all_items)}. Чек-лист: {len(template.items) if template else 0} пунктов.'
    rows = []
    if template:
        rows += [[ui.b('☑️ Посмотреть чек-лист темы', f'ms:templateview:{topic_id}')]]
    for item in items:
        prefix = '📖 ' if item.kind == 'recap' else '📎 '
        if item.visibility == 'staff':
            prefix += '🔒 '
        rows += [[ui.b(prefix + clip(item.file_name, 50), f'ms:file:{item.id}')]]
    if nav := ui.nav(len(all_items), page, f'ms:bank:{topic_id}'):
        rows.append(nav)
    if viewer.is_staff:
        rows += [[ui.b('➕ Документ', f'ms:materialtopic:{topic_id}'), ui.b('☑️ Изменить чек-лист', f'ms:templatetopic:{topic_id}')]]
    rows += [[ui.b('📅 Занятия по теме', f'ms:list:upcoming:{topic_id}:0')], [ui.b('← Предметы', 'ms:subjects:bank:0')]]
    await ui.show(target, text, ui.kb(*rows))


async def draft_subjects(target, state, sm, page=0):
    await form_is(state, MentoringForm.subject)
    async with sm() as s:
        all_items = await svc.subjects(s)
    items, page = ui.page_slice(all_items, page)
    rows = [[ui.b(x.name, f'ms:picksub:{x.id}')] for x in items]
    if nav := ui.nav(len(all_items), page, 'ms:draftsub'):
        rows.append(nav)
    await ui.show(target, '<b>Предмет</b>\nВыберите существующий или напишите название нового.\n/cancel — отменить заполнение.', ui.kb(*rows) if rows else None)


async def draft_topics(target, state, sm, page=0):
    await form_is(state, MentoringForm.topic)
    data = await state.get_data()
    async with sm() as s:
        all_items = await svc.topics(s, data['subject_id']) if data.get('subject_id') else []
    items, page = ui.page_slice(all_items, page)
    rows = [[ui.b(x.name, f'ms:picktopic:{x.id}')] for x in items]
    if nav := ui.nav(len(all_items), page, 'ms:drafttopic'):
        rows.append(nav)
    await ui.show(target, f'<b>Тема · {ui.e(data["subject_name"], 100)}</b>\nВыберите тему или напишите новую.', ui.kb(*rows) if rows else None)


async def draft_mentors(target, state, sm, page=0):
    await form_is(state, MentoringForm.mentor)
    async with sm() as s:
        all_items = await repo.list_mentors(s)
    items, page = ui.page_slice(all_items, page)
    rows = [[ui.b(clip(x.display_name, 30) + (f' @{x.username}' if x.username else f' · {x.telegram_id}'), f'ms:pickmentor:{x.telegram_id}')] for x in items]
    if nav := ui.nav(len(all_items), page, 'ms:draftmentor'):
        rows.append(nav)
    await ui.show(target, '<b>Назначьте ментора</b>\nВыберите кнопкой или отправьте @username / Telegram ID.\n'
                  'Нет нужного человека? Добавьте через /addmentor, затем продолжите выбор.', ui.kb(*rows) if rows else None)


async def ask_kind(target, state):
    await state.set_state(MentoringForm.kind)
    await ui.show(target, 'Какой это документ?', ui.kb(
        [ui.b('📖 Рекап-гайд — до занятия', 'ms:kind:recap')],
        [ui.b('📎 Материал / конспект занятия', 'ms:kind:material')]))


async def ask_visibility(target, state):
    await state.set_state(MentoringForm.visibility)
    await ui.show(target, '<b>Кому доступен документ?</b>\nОтветы к мокам можно оставить только команде.', ui.kb(
        [ui.b('Студентам и команде', 'ms:visibility:students')],
        [ui.b('Только менторам и координаторам', 'ms:visibility:staff')]))


async def selected_topic(target, state, sm, topic_id):
    data = await state.get_data()
    async with sm() as s:
        names = await svc.topic_names(s, topic_id)
    await state.update_data(topic_id=topic_id, subject_name=names[0], topic_name=names[1])
    if data['operation'] == 'session':
        await state.set_state(MentoringForm.mentor)
        await draft_mentors(target, state, sm)
    elif data['operation'] == 'checklist':
        await state.set_state(MentoringForm.checklist)
        await ui.show(target, f'<b>Чек-лист · {ui.e(names[0], 100)} · {ui.e(names[1], 120)}</b>\n\n'
            f'Отправьте от 1 до {svc.MAX_ITEMS} пунктов, каждый с новой строки, до {svc.MAX_ITEM_LENGTH} символов на пункт.\n'
            'Например:\nЦикл for\nЦикл while\nВложенные циклы\n\n'
            'Новая версия заменит шаблон в банке, но не ответы к уже привязанным чек-листам занятий.')
    else:
        await ask_kind(target, state)


async def start_wizard(target, state, viewer, sm, operation: str, topic_id=0, session_id=0):
    if operation == 'session':
        svc.require_admin(viewer)
    elif operation in ('material', 'checklist'):
        svc.require_staff(viewer)
    else:
        raise svc.RuleError('Неизвестная операция.')
    if session_id:
        async with sm() as s:
            lesson = await svc.get_session(s, session_id)
            svc.require_manage(viewer, lesson)
            if lesson.status == 'cancelled':
                raise svc.RuleError('Занятие отменено. Документ можно добавить в банк отдельно.')
            topic_id = lesson.topic_id
    await state.clear()
    await state.update_data(operation=operation, token=uuid4().hex, session_id=session_id or None)
    if session_id:
        async with sm() as s:
            names = await svc.topic_names(s, topic_id)
        await state.update_data(topic_id=topic_id, subject_name=names[0], topic_name=names[1], kind='material')
        await ui.show(target, f'📎 Материалы занятия #{session_id}\nПредмет: {ui.e(names[0], 100)}\nТема: {ui.e(names[1], 120)}')
        await ask_visibility(target.message if isinstance(target, CallbackQuery) else target, state)
    elif topic_id:
        await selected_topic(target, state, sm, topic_id)
    else:
        await state.set_state(MentoringForm.subject)
        await draft_subjects(target, state, sm)


async def mentor_selected(target, state, viewer, sm, settings, mentor_id):
    svc.require_admin(viewer)
    await form_is(state, MentoringForm.mentor)
    async with sm() as s:
        if not await repo.is_mentor(s, mentor_id):
            raise svc.RuleError('Ментор не найден. Сначала добавьте его через /addmentor.')
        user = await s.get(User, mentor_id)
    data = await state.get_data()
    if data.get('operation') == 'edit':
        await state.update_data(value=mentor_id)
        await edit_preview(target, state, settings)
    else:
        await state.update_data(mentor_id=mentor_id, mentor_name=user.display_name)
        await state.set_state(MentoringForm.start)
        await ui.show(target, f'Введите дату и время начала в часовом поясе <b>{ui.e(settings.timezone, 60)}</b>.\n'
                      'Формат: ДД.ММ.ГГГГ ЧЧ:ММ.')


async def edit_preview(target, state, settings):
    data = await state.get_data()
    value = svc.local_time(datetime.fromisoformat(data['value']), settings) if data['field'] == 'start' else str(data['value'])
    await state.set_state(MentoringForm.edit_confirm)
    await ui.show(target, f'<b>Изменить занятие #{data["sid"]}?</b>\nНовое значение: {ui.e(value, 300)}\n'
                  'Участники получат уведомление, их записи сохранятся.', ui.kb(
                      [ui.b('Подтвердить изменение', f'ms:editconfirm:{data["token"]}')],
                      [ui.b('Не менять', 'ms:stop')]))


async def begin_question(target, state, viewer, sm, sid):
    async with sm() as s:
        lesson = await svc.get_session(s, sid)
        svc.require_open(lesson)
        if lesson.mentor_id == viewer.id:
            raise svc.RuleError('Вы ведёте это занятие.')
    await state.clear()
    await state.update_data(sid=sid)
    await state.set_state(MentoringForm.question)
    await ui.show(target, f'<b>Входной билет · занятие #{sid}</b>\n\n'
        'Ваше имя, вопрос и чек-лист увидят назначенный ментор и координаторы. Это не анонимное обращение.\n\n'
        'Напишите, что непонятно и что хотите разобрать, до 1500 символов. '
        'Если конкретного вопроса пока нет, отправьте «-».\n/cancel — не записываться.')


async def show_check(target, sm, viewer, booking_id, index=None):
    async with sm() as s:
        booking = await s.get(SessionBooking, booking_id)
        if booking is None or booking.student_id != viewer.id or booking.status != 'active':
            raise svc.RuleError('Ваш активный чек-лист не найден.')
        lesson = await svc.get_session(s, booking.session_id, lock=True)
        svc.require_open(lesson)
        await svc.attach_checklist(s, lesson)
        await s.commit()
    text, markup = ui.checklist_card(lesson, booking, index)
    await ui.show(target, text, markup)


async def show_people(target, sm, viewer, sid, page, *, attendance=False):
    async with sm() as s:
        lesson = await svc.get_session(s, sid)
        svc.require_manage(viewer, lesson)
        all_items = await svc.active_bookings(s, sid)
        items, page = ui.page_slice(all_items, page)
        users = await repo.users_by_ids(s, [x.student_id for x in items])
    lines = [f'<b>{"Посещаемость" if attendance else "Участники и вопросы"} · занятие #{sid}</b>',
             f'Активных записей: {len(all_items)}']
    rows = []
    for booking in items:
        name = users[booking.student_id].display_name if booking.student_id in users else str(booking.student_id)
        if attendance:
            label = f'{clip(name, 40)} · {ui.ATTENDANCE[booking.attendance]}'
            rows.append([ui.b(label, f'ms:person:{booking.id}:attendance')])
        else:
            complete = '✅' if svc.answered(lesson, booking) else '⬜'
            lines += [f'\n{complete} <b>{ui.e(name, 90)}</b>', ui.e(booking.question, 220)]
            rows.append([ui.b(clip(name, 45), f'ms:person:{booking.id}:detail')])
    prefix = f'ms:{"attendance" if attendance else "people"}:{sid}'
    if nav := ui.nav(len(all_items), page, prefix):
        rows.append(nav)
    if attendance and lesson.status == 'scheduled':
        rows.append([ui.b('🏁 Завершить после всех отметок', f'ms:complete:{sid}:ask')])
    rows.append([ui.b('← К занятию', f'ms:view:{sid}')])
    # Participant questions can contain many HTML-special characters; avoid overlong cards.
    text = '\n'.join(lines)
    if len(text) > 3600:
        lines = lines[:2] + ['Нажмите на участника, чтобы прочитать вопрос и ответы полностью.']
    await ui.show(target, '\n'.join(lines), ui.kb(*rows))


async def person_detail(callback, sm, viewer, bid, mode):
    async with sm() as s:
        booking = await s.get(SessionBooking, bid)
        if booking is None or booking.status != 'active':
            raise svc.RuleError('Участник не найден.')
        lesson = await svc.get_session(s, booking.session_id)
        svc.require_manage(viewer, lesson)
        user = await s.get(User, booking.student_id)
    header = f'<b>{ui.e(user.display_name, 128)}</b> · занятие #{lesson.id}'
    if mode == 'attendance':
        rows = [[ui.b(label, f'ms:mark:{bid}:{value}')] for value, label in ui.ATTENDANCE.items()]
        rows.append([ui.b('← Посещаемость', f'ms:attendance:{lesson.id}:0')])
        await ui.show(callback, header + f'\nСейчас: {ui.ATTENDANCE[booking.attendance]}', ui.kb(*rows))
        return
    lines = [header, f'Вопрос: {ui.e(booking.question, 1500)}', '\n<b>Самооценка по чек-листу</b>']
    for i, item in enumerate(lesson.checklist_items):
        label = svc.SCORES.get(booking.answers.get(str(i)), 'Нет ответа')
        lines += [f'{i + 1}. {ui.e(item, svc.MAX_ITEM_LENGTH)} — {label}']
    # Split the potentially long question separately; HTML entity boundaries stay intact.
    if len(lines[1]) > 3600:
        lines[1:2] = ['Вопрос:'] + [ui.e(booking.question[i:i + 400], 400) for i in range(0, len(booking.question), 400)]
    await ui.long_text(callback.message, '\n'.join(lines))
    await callback.message.answer('Вернуться к списку:', reply_markup=ui.kb([ui.b('← Участники', f'ms:people:{lesson.id}:0')]))


async def send_material(bot, sm, viewer, material_id):
    async with sm() as s:
        material = await svc.material_for(s, viewer, material_id)
        names = await svc.topic_names(s, material.topic_id)
    rows = [[ui.b('← Материалы темы', f'ms:bank:{material.topic_id}:0')]]
    if viewer.is_admin or viewer.is_staff and material.author_id == viewer.id:
        rows.insert(0, [ui.b('Убрать из банка', f'ms:archive:{material.id}:ask')])
    try:
        await bot.send_document(viewer.id, material.file_id,
            caption=f'📎 {ui.e(material.file_name, 100)}\n{ui.e(names[0], 80)} · {ui.e(names[1], 80)}',
            reply_markup=ui.kb(*rows))
    except TelegramAPIError:
        raise svc.RuleError('Документ сейчас не удалось отправить. Попробуйте снова; если ошибка повторяется, попросите автора загрузить его заново.')


async def show_report(target, sm, viewer, settings):
    svc.require_admin(viewer)
    async with sm() as s:
        data = await svc.stats(s)
        names = {r['lesson'].topic_id: await svc.topic_names(s, r['lesson'].topic_id) for r in data['rows']}
    rows = data['rows']
    completed = [r for r in rows if r['lesson'].status == 'completed']
    current = [r for r in rows if r['lesson'].status == 'scheduled']
    cancelled = [r for r in rows if r['lesson'].status == 'cancelled']
    active_rows = [r for r in rows if r['lesson'].status != 'cancelled']
    denominator = sum(r['present'] + r['absent'] for r in completed)
    present = sum(r['present'] for r in completed)
    percentage = f'{present / denominator:.0%}' if denominator else 'нет данных'
    unknown = sum(r['unknown'] for r in rows if r['lesson'].status != 'cancelled' and r['lesson'].ends_at <= utcnow())
    text = ('<b>Сводка менторок · за всё время</b>\n'
            f'Проведено: {len(completed)}\nЗапланировано / не закрыто: {len(current)}\nОтменено: {len(cancelled)}\n'
            f'Записей на неотменённые занятия: {sum(r["registered"] for r in active_rows)}\n'
            f'Посещаемость проведённых: {present}/{denominator} ({percentage})\n'
            f'Не отмечена посещаемость после окончания: {unknown}\n'
            f'Заполненных чек-листов: {sum(r["checklists"] for r in active_rows)}\n'
            f'Проведено без материалов: {sum(not r["has_materials"] for r in completed)}\n'
            f'Нулевых записей у незакрытых занятий: {sum(r["registered"] == 0 for r in current)}\n\n'
            'Ниже — файл со всеми занятиями. Данные пересчитываются при открытии, а не вводятся вручную.')
    await ui.show(target, text, ui.kb([ui.b('🗂 Все занятия', 'ms:list:all:0:0')], [ui.b('← Менторки', 'ms:home')]))
    lines = ['Сводка менторок — за всё время', f'Часовой пояс: {settings.timezone}',
             'Записи отменённых занятий указаны для истории, но исключены из общей посещаемости.', '']
    for r in sorted(rows, key=lambda row: row['lesson'].starts_at, reverse=True):
        lesson = r['lesson']
        subject, topic = names[lesson.topic_id]
        lines += [f'#{lesson.id} | {svc.local_time(lesson.starts_at, settings)} | {ui.STATUS[lesson.status]}',
                  f'{subject} | {topic} | mentor_id={lesson.mentor_id}',
                  f'Записано: {r["registered"]}; присутствовали: {r["present"]}; отсутствовали: {r["absent"]}; не отмечено: {r["unknown"]}',
                  f'Чек-листы: {str(r["checklists"]) + "/" + str(r["registered"]) if r["has_checklist"] else "не задан"}; '
                  f'материалы: {"да" if r["has_materials"] else "нет"}', '']
    message = target.message if isinstance(target, CallbackQuery) else target
    await message.answer_document(BufferedInputFile('\n'.join(lines).encode('utf-8'), filename='mentoring_report.txt'),
                                  caption='Сводка по занятиям, без имён и вопросов студентов.')


# Commands precede state-specific input handlers.
@entry_router.message(Command('sessions'))
@entry_router.message(F.text.in_(menu_labels('sessions')))
async def sessions_command(message: Message, state: FSMContext, viewer: Viewer):
    await state.clear()
    text, markup = ui.home(viewer)
    await message.answer(text, reply_markup=markup)


@entry_router.message(Command('materials'))
@entry_router.message(F.text.in_(menu_labels('materials')))
async def materials_command(message: Message, state: FSMContext, sm: async_sessionmaker):
    await state.clear()
    await browse_subjects(message, sm, 'bank', 0)


@entry_router.message(Command('mysessions'))
async def my_sessions_command(message: Message, state: FSMContext, viewer: Viewer,
                              settings: Settings, sm: async_sessionmaker):
    await state.clear()
    await show_list(message, sm, viewer, settings, 'teaching' if viewer.is_staff else 'bookings', 0, 0)


@entry_router.message(Command('newsession', 'addmaterial', 'setchecklist'))
async def new_command(message: Message, state: FSMContext, viewer: Viewer, sm: async_sessionmaker):
    operation = {'newsession': 'session', 'addmaterial': 'material', 'setchecklist': 'checklist'}[(message.text or message.caption or '').split()[0].split('@')[0][1:]]
    try:
        await start_wizard(message, state, viewer, sm, operation)
    except svc.RuleError as exc:
        await message.answer(ui.e(exc, 800))


@entry_router.message(Command('sessionreport'))
async def report_command(message: Message, viewer: Viewer, settings: Settings, sm: async_sessionmaker):
    try:
        await show_report(message, sm, viewer, settings)
    except svc.RuleError as exc:
        await message.answer(ui.e(exc, 800))


@router.callback_query(F.data.startswith('ms:'))
async def actions(callback: CallbackQuery, state: FSMContext, viewer: Viewer,
                  settings: Settings, sm: async_sessionmaker, bot: Bot):
    if not isinstance(callback.message, Message):
        await callback.answer('Откройте /sessions в личном чате.', show_alert=True)
        return
    await callback.answer()
    p = callback.data.split(':')
    try:
        op = p[1]
        if op in ('home', 'stop'):
            await state.clear()
            text, markup = ui.home(viewer)
            await ui.show(callback, text, markup)
        elif op == 'subjects':
            await state.clear()
            await browse_subjects(callback, sm, p[2], num(p[3]))
        elif op == 'topics':
            await state.clear()
            await browse_topics(callback, sm, p[2], num(p[3]), num(p[4]))
        elif op == 'list':
            await state.clear()
            await show_list(callback, sm, viewer, settings, p[2], num(p[3]), num(p[4]))
        elif op == 'view':
            await state.clear()
            await show_session(callback, sm, viewer, num(p[2]), settings)
        elif op == 'bank':
            await state.clear()
            await show_bank(callback, sm, viewer, num(p[2]), num(p[3]))
        elif op == 'wizard':
            await start_wizard(callback, state, viewer, sm, p[2])
        elif op in ('materialtopic', 'templatetopic'):
            await start_wizard(callback, state, viewer, sm, 'material' if op == 'materialtopic' else 'checklist', topic_id=num(p[2]))
        elif op == 'upload':
            await start_wizard(callback, state, viewer, sm, 'material', session_id=num(p[2]))
        elif op == 'draftsub':
            await draft_subjects(callback, state, sm, num(p[2]))
        elif op == 'drafttopic':
            await draft_topics(callback, state, sm, num(p[2]))
        elif op == 'draftmentor':
            svc.require_admin(viewer)
            await draft_mentors(callback, state, sm, num(p[2]))
        elif op == 'picksub':
            svc.require_staff(viewer)
            await form_is(state, MentoringForm.subject)
            async with sm() as s:
                subject = await s.get(Subject, num(p[2]))
                if subject is None:
                    raise svc.RuleError('Предмет не найден.')
            await state.update_data(subject_id=subject.id, subject_name=subject.name)
            await state.set_state(MentoringForm.topic)
            await draft_topics(callback, state, sm)
        elif op == 'picktopic':
            svc.require_staff(viewer)
            await form_is(state, MentoringForm.topic)
            data = await state.get_data()
            async with sm() as s:
                topic = await s.get(Topic, num(p[2]))
                if topic is None or topic.subject_id != data.get('subject_id'):
                    raise svc.RuleError('Выберите тему текущего предмета.')
            await selected_topic(callback, state, sm, topic.id)
        elif op == 'pickmentor':
            await mentor_selected(callback, state, viewer, sm, settings, num(p[2]))
        elif op == 'kind':
            svc.require_staff(viewer)
            await form_is(state, MentoringForm.kind)
            if p[2] not in ('recap', 'material'):
                raise svc.RuleError('Выберите тип кнопкой.')
            await state.update_data(kind=p[2])
            await ask_visibility(callback, state)
        elif op == 'visibility':
            svc.require_staff(viewer)
            await form_is(state, MentoringForm.visibility)
            if p[2] not in ('students', 'staff'):
                raise svc.RuleError('Выберите доступ кнопкой.')
            await state.update_data(visibility=p[2])
            await state.set_state(MentoringForm.document)
            await ui.show(callback, 'Отправьте один документ <b>PDF или DOCX</b> до 20 МиБ. Не фотографию.\n'
                          'Имя файла станет названием в банке. Публикация — без согласования.\n/cancel — отмена.')
        elif op == 'create':
            svc.require_admin(viewer)
            await form_is(state, MentoringForm.session_confirm)
            data = await state.get_data()
            if p[2] != data['token']:
                raise svc.RuleError('Это другой черновик.')
            async with sm() as s:
                lesson = await svc.create_session(s, viewer, settings, topic_id=data['topic_id'], mentor_id=data['mentor_id'],
                    starts_at=datetime.fromisoformat(data['starts_at']), duration=data['duration'], location=data['location'], creation_key=data['token'])
                await s.commit()
            await state.clear()
            await callback.message.answer(f'✅ Занятие #{lesson.id} создано. Ментор получит уведомление.')
            await show_session(callback.message, sm, viewer, lesson.id, settings)
        elif op == 'join':
            await begin_question(callback, state, viewer, sm, num(p[2]))
        elif op == 'leave':
            sid = num(p[2])
            if p[3] == 'ask':
                async with sm() as s:
                    booking = await svc.own_booking(s, sid, viewer.id)
                    if booking is None or booking.status != 'active':
                        raise svc.RuleError('Активной записи нет.')
                await ui.show(callback, f'Убрать вашу запись на занятие #{sid}?', ui.kb(
                    [ui.b('Да, убрать запись', f'ms:leave:{sid}:yes')], [ui.b('Сохранить запись', f'ms:view:{sid}')]))
            elif p[3] == 'yes':
                async with sm() as s:
                    await svc.withdraw(s, viewer, sid)
                    await s.commit()
                await show_session(callback, sm, viewer, sid, settings)
            else:
                raise svc.RuleError('Неизвестное действие.')
        elif op == 'check':
            await show_check(callback, sm, viewer, num(p[2]), None if p[3] == 'next' else num(p[3]))
        elif op == 'answer':
            async with sm() as s:
                lesson, booking = await svc.save_answer(s, viewer, num(p[2]), num(p[3]), num(p[4]))
                await s.commit()
            text, markup = ui.checklist_card(lesson, booking)
            await ui.show(callback, text, markup)
        elif op == 'refresh':
            async with sm() as s:
                lesson = await svc.refresh_checklist(s, viewer, num(p[2]))
                await s.commit()
            await callback.message.answer('Чек-лист из банка привязан к занятию.')
            await show_session(callback, sm, viewer, lesson.id, settings)
        elif op in ('people', 'attendance'):
            await show_people(callback, sm, viewer, num(p[2]), num(p[3]), attendance=op == 'attendance')
        elif op == 'person':
            if p[3] not in ('attendance', 'detail'):
                raise svc.RuleError('Неизвестный раздел участника.')
            await person_detail(callback, sm, viewer, num(p[2]), p[3])
        elif op == 'mark':
            async with sm() as s:
                lesson = await svc.set_attendance(s, viewer, num(p[2]), p[3])
                await s.commit()
            await show_people(callback, sm, viewer, lesson.id, 0, attendance=True)
        elif op == 'summary':
            async with sm() as s:
                lesson = await svc.get_session(s, num(p[2]))
                svc.require_manage(viewer, lesson)
                bookings = await svc.active_bookings(s, lesson.id)
            await ui.long_text(callback.message, ui.summary_text(lesson, bookings))
            await callback.message.answer('Вопросы и индивидуальные ответы — в списке участников.',
                reply_markup=ui.kb([ui.b('👥 Участники', f'ms:people:{lesson.id}:0')], [ui.b('← К занятию', f'ms:view:{lesson.id}')]))
        elif op == 'complete':
            sid = num(p[2])
            async with sm() as s:
                lesson = await svc.get_session(s, sid)
                svc.require_manage(viewer, lesson)
            if p[3] == 'ask':
                await ui.show(callback, 'Завершить занятие? Все участники должны быть отмечены в посещаемости.', ui.kb(
                    [ui.b('Завершить', f'ms:complete:{sid}:yes')], [ui.b('← К занятию', f'ms:view:{sid}')]))
            elif p[3] == 'yes':
                async with sm() as s:
                    await svc.complete_session(s, viewer, sid)
                    await s.commit()
                await callback.message.answer('✅ Занятие завершено. Добавьте материалы в PDF/DOCX.', reply_markup=ui.kb(
                    [ui.b('📎 Загрузить материалы', f'ms:upload:{sid}')]))
                await show_session(callback, sm, viewer, sid, settings)
            else:
                raise svc.RuleError('Неизвестное действие.')
        elif op == 'cancel':
            svc.require_admin(viewer)
            sid = num(p[2])
            async with sm() as s:
                lesson = await svc.get_session(s, sid)
                if lesson.status != 'scheduled':
                    raise svc.RuleError('Занятие уже закрыто.')
                count = len(await svc.active_bookings(s, sid))
            await state.clear()
            await state.update_data(sid=sid, token=uuid4().hex)
            await state.set_state(MentoringForm.cancel_reason)
            await ui.show(callback, f'Занятие #{sid}: записано {count}.\nВведите причину отмены до 400 символов '
                          '(например: «Нет записавшихся»). Она будет отправлена участникам.\n/cancel — не отменять занятие.')
        elif op == 'cancelconfirm':
            svc.require_admin(viewer)
            await form_is(state, MentoringForm.cancel_confirm)
            data = await state.get_data()
            if data['token'] != p[2]:
                raise svc.RuleError('Это другое подтверждение.')
            async with sm() as s:
                # Reconfirm when the participant count changed after the preview.
                lesson = await svc.get_session(s, data['sid'], lock=True)
                count = len(await svc.active_bookings(s, lesson.id))
                if count != data['count']:
                    await state.update_data(count=count)
                    await callback.message.answer(f'Количество записей изменилось: теперь {count}. Подтвердите отмену ещё раз.',
                        reply_markup=ui.kb([ui.b('Подтвердить отмену', f'ms:cancelconfirm:{data["token"]}')]))
                    return
                lesson = await svc.cancel_session(s, viewer, lesson.id, data['reason'])
                await s.commit()
            await state.clear()
            await callback.message.answer('Занятие отменено. Участники и ментор получат уведомление; напоминания о подготовке отключены.')
            await show_session(callback, sm, viewer, lesson.id, settings)
        elif op == 'edit':
            svc.require_admin(viewer)
            sid, field = num(p[2]), p[3]
            if field not in ('start', 'mentor', 'location'):
                raise svc.RuleError('Неизвестное изменение.')
            async with sm() as s:
                lesson = await svc.get_session(s, sid)
                svc.require_open(lesson)
            await state.clear()
            await state.update_data(operation='edit', sid=sid, field=field, token=uuid4().hex)
            if field == 'mentor':
                await state.set_state(MentoringForm.mentor)
                await draft_mentors(callback, state, sm)
            else:
                await state.set_state(MentoringForm.edit_value)
                await ui.show(callback, f'Введите новое время: ДД.ММ.ГГГГ ЧЧ:ММ ({ui.e(settings.timezone, 60)}).'
                              if field == 'start' else 'Введите новое место или ссылку, до 300 символов.')
        elif op == 'editconfirm':
            svc.require_admin(viewer)
            await form_is(state, MentoringForm.edit_confirm)
            data = await state.get_data()
            if p[2] != data['token']:
                raise svc.RuleError('Это другой черновик.')
            value = datetime.fromisoformat(data['value']) if data['field'] == 'start' else data['value']
            async with sm() as s:
                lesson = await svc.edit_session(s, viewer, data['sid'], data['field'], value)
                await s.commit()
            await state.clear()
            await callback.message.answer('Изменение сохранено. Участники получат уведомление.')
            await show_session(callback, sm, viewer, lesson.id, settings)
        elif op == 'templateview':
            async with sm() as s:
                template = await s.get(TopicChecklist, num(p[2]))
                if template is None:
                    raise svc.RuleError('Чек-лист пока не добавлен.')
            text = '<b>Чек-лист темы</b>\n' + '\n'.join(f'{i + 1}. {ui.e(x, svc.MAX_ITEM_LENGTH)}' for i, x in enumerate(template.items))
            text += '\n\nОтветить на чек-лист можно после записи на занятие.'
            await ui.long_text(callback.message, text)
        elif op == 'file':
            await send_material(bot, sm, viewer, num(p[2]))
        elif op == 'archive':
            async with sm() as s:
                material = await svc.material_for(s, viewer, num(p[2]))
                if not (viewer.is_admin or viewer.is_staff and material.author_id == viewer.id):
                    raise svc.RuleError('Удалять материал может автор или координатор.')
                if p[3] == 'yes':
                    await svc.archive_material(s, viewer, material.id)
                    await s.commit()
                    await callback.message.answer('Материал убран из банка. Уже полученные пользователями копии не удаляются.')
                elif p[3] == 'ask':
                    # A document message has no editable text: always send a new confirmation message.
                    await callback.message.answer(f'Убрать {ui.e(material.file_name, 200)} из банка?', reply_markup=ui.kb(
                        [ui.b('Да, убрать', f'ms:archive:{material.id}:yes')], [ui.b('Не убирать', f'ms:bank:{material.topic_id}:0')]))
                else:
                    raise svc.RuleError('Неизвестное действие.')
        elif op == 'report':
            await state.clear()
            await show_report(callback, sm, viewer, settings)
        else:
            raise svc.RuleError('Кнопка устарела. Откройте /sessions.')
    except svc.RuleError as exc:
        await callback.message.answer(ui.e(exc, 1000))
    except (IndexError, ValueError):
        await callback.message.answer('Некорректная кнопка. Откройте /sessions ещё раз.')


@router.message(MentoringForm.subject, DATA_TEXT)
@router.message(MentoringForm.topic, DATA_TEXT)
@router.message(MentoringForm.mentor, DATA_TEXT)
@router.message(MentoringForm.start, DATA_TEXT)
@router.message(MentoringForm.duration, DATA_TEXT)
@router.message(MentoringForm.location, DATA_TEXT)
@router.message(MentoringForm.checklist, DATA_TEXT)
@router.message(MentoringForm.question, DATA_TEXT)
@router.message(MentoringForm.cancel_reason, DATA_TEXT)
@router.message(MentoringForm.edit_value, DATA_TEXT)
async def form_text(message: Message, state: FSMContext, viewer: Viewer, settings: Settings,
                    sm: async_sessionmaker, bot: Bot):
    current, data = await state.get_state(), await state.get_data()
    text = (message.text or '').strip()
    try:
        if current == MentoringForm.question.state:
            async with sm() as s:
                lesson, booking = await svc.register(s, viewer, data['sid'], text)
                recaps = await svc.material_list(s, viewer, lesson.topic_id, recap_only=True)
                await s.commit()
            await state.clear()
            await message.answer('✅ Запись сохранена. Ниже — подготовка к занятию. '
                                 'Свой вопрос можно изменить в карточке занятия.')
            for material in recaps[:3]:
                try:
                    await send_material(bot, sm, viewer, material.id)
                except svc.RuleError:
                    await message.answer('Один из рекап-гайдов не удалось отправить. Он остаётся доступен в банке — попробуйте позже.')
            if not recaps:
                await message.answer('Рекап-гайд пока не добавлен. Это не мешает участвовать в занятии.')
            elif len(recaps) > 3:
                await message.answer('Остальные рекап-гайды доступны в банке.', reply_markup=ui.kb([ui.b('Банк темы', f'ms:bank:{lesson.topic_id}:0')]))
            if lesson.checklist_items:
                rendered, markup = ui.checklist_card(lesson, booking)
                await message.answer(rendered, reply_markup=markup)
            else:
                await message.answer('Чек-лист пока не добавлен. Его можно будет открыть из карточки занятия.')
                await show_session(message, sm, viewer, lesson.id, settings)
            return
        svc.require_staff(viewer)
        if current in (MentoringForm.mentor.state, MentoringForm.start.state, MentoringForm.duration.state,
                       MentoringForm.location.state, MentoringForm.cancel_reason.state, MentoringForm.edit_value.state):
            svc.require_admin(viewer)
        if current == MentoringForm.subject.state:
            name = svc.clean_name(text, 100)
            async with sm() as s:
                existing = await s.scalar(select(Subject).where(Subject.name_key == name.casefold()))
            await state.update_data(subject_name=existing.name if existing else name, subject_id=existing.id if existing else None)
            await state.set_state(MentoringForm.topic)
            await draft_topics(message, state, sm)
        elif current == MentoringForm.topic.state:
            async with sm() as s:
                topic = await svc.ensure_topic(s, data['subject_name'], text)
                await s.commit()
            await selected_topic(message, state, sm, topic.id)
        elif current == MentoringForm.mentor.state:
            async with sm() as s:
                mentors = await repo.list_mentors(s)
            found = [m for m in mentors if str(m.telegram_id) == text or m.username and m.username.casefold() == text.lstrip('@').casefold()]
            if len(found) != 1:
                raise svc.RuleError('Не удалось однозначно найти ментора. Выберите его кнопкой или укажите Telegram ID.')
            await mentor_selected(message, state, viewer, sm, settings, found[0].telegram_id)
        elif current == MentoringForm.start.state:
            starts = svc.parse_start(text, settings.timezone)
            await state.update_data(starts_at=starts.isoformat())
            await state.set_state(MentoringForm.duration)
            await message.answer('Длительность занятия в минутах: целое число от 15 до 360.')
        elif current == MentoringForm.duration.state:
            if not text.isdigit() or len(text) > 3 or not 15 <= int(text) <= 360:
                raise svc.RuleError('Длительность — целое число от 15 до 360 минут.')
            await state.update_data(duration=int(text))
            await state.set_state(MentoringForm.location)
            await message.answer('Введите место проведения или ссылку на онлайн-встречу, до 300 символов.')
        elif current == MentoringForm.location.state:
            location = svc.clean_name(text, 300)
            await state.update_data(location=location)
            await state.set_state(MentoringForm.session_confirm)
            await message.answer(f'<b>Создать занятие?</b>\n{ui.e(data["subject_name"], 100)} · {ui.e(data["topic_name"], 120)}\n'
                f'Ментор: {ui.e(data["mentor_name"], 128)}\n'
                f'{svc.local_time(datetime.fromisoformat(data["starts_at"]), settings)} ({ui.e(settings.timezone, 60)}), {data["duration"]} мин.\n'
                f'Место: {ui.e(location, 300)}', reply_markup=ui.kb(
                    [ui.b('✅ Создать', f'ms:create:{data["token"]}')], [ui.b('Не создавать', 'ms:stop')]))
        elif current == MentoringForm.checklist.state:
            items = svc.parse_items(text)
            async with sm() as s:
                await svc.put_checklist(s, viewer, data['topic_id'], items)
                await s.commit()
            await state.clear()
            await message.answer('✅ Шаблон чек-листа сохранён. У существующего занятия его можно привязать кнопкой '
                '«Обновить чек-лист из банка», пока студенты ещё не начали отвечать.', reply_markup=ui.kb(
                    [ui.b('Банк темы', f'ms:bank:{data["topic_id"]}:0')]))
        elif current == MentoringForm.cancel_reason.state:
            reason = svc.clean_name(text, 400)
            async with sm() as s:
                lesson = await svc.get_session(s, data['sid'])
                count = len(await svc.active_bookings(s, lesson.id))
            await state.update_data(reason=reason, count=count)
            await state.set_state(MentoringForm.cancel_confirm)
            await message.answer(f'<b>Отменить занятие #{lesson.id}?</b>\nЗаписано: {count}\nПричина: {ui.e(reason, 400)}',
                reply_markup=ui.kb([ui.b('Подтвердить отмену', f'ms:cancelconfirm:{data["token"]}')], [ui.b('Не отменять', 'ms:stop')]))
        elif current == MentoringForm.edit_value.state:
            value = svc.parse_start(text, settings.timezone).isoformat() if data['field'] == 'start' else svc.clean_name(text, 300)
            await state.update_data(value=value)
            await edit_preview(message, state, settings)
    except svc.RuleError as exc:
        await message.answer(ui.e(exc, 1000))


@router.message(MentoringForm.document, (~F.text) | DATA_TEXT)
async def material_document(message: Message, state: FSMContext, viewer: Viewer, sm: async_sessionmaker):
    try:
        svc.require_staff(viewer)
        doc = message.document
        if doc is None:
            raise svc.RuleError('Нужен документ PDF или DOCX. Отправьте как файл, не картинку; /cancel — отмена.')
        data = await state.get_data()
        async with sm() as s:
            material = await svc.add_material(s, viewer, topic_id=data['topic_id'], kind=data['kind'],
                visibility=data['visibility'], file_id=doc.file_id, file_unique_id=doc.file_unique_id,
                file_name=doc.file_name, file_size=doc.file_size, mime=doc.mime_type, session_id=data.get('session_id'))
            await s.commit()
        await state.clear()
        await message.answer('✅ Документ сохранён в банке. ' + ('Доступен студентам.' if material.visibility == 'students' else 'Доступен только команде.'),
            reply_markup=ui.kb([ui.b('Открыть банк темы', f'ms:bank:{material.topic_id}:0')],
                [ui.b('Добавить ещё документ', f'ms:upload:{material.session_id}' if material.session_id else f'ms:materialtopic:{material.topic_id}')]))
    except svc.RuleError as exc:
        await message.answer(ui.e(exc, 1000))


@router.message(MentoringForm.kind, DATA_TEXT)
@router.message(MentoringForm.visibility, DATA_TEXT)
@router.message(MentoringForm.session_confirm, DATA_TEXT)
@router.message(MentoringForm.cancel_confirm, DATA_TEXT)
@router.message(MentoringForm.edit_confirm, DATA_TEXT)
async def button_expected(message: Message):
    await message.answer('Выберите вариант кнопкой под предыдущим сообщением. /cancel — отменить заполнение.')
