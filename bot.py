import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from database import SessionLocal, Application, init_db

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


class ApplicationForm(StatesGroup):
    full_name = State()
    course = State()
    subject = State()
    comment = State()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я бот для заявок на хард-ментора.\n\n"
        "Если по нужному тебе предмету пока нет хард-ментора, "
        "оставь заявку — админы постараются найти для тебя ментора.\n\n"
        "Как тебя зовут? (Фамилия Имя)"
    )
    await state.set_state(ApplicationForm.full_name)


@dp.message(ApplicationForm.full_name)
async def process_full_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await message.answer("На каком курсе ты учишься? (например: 1 курс)")
    await state.set_state(ApplicationForm.course)


@dp.message(ApplicationForm.course)
async def process_course(message: Message, state: FSMContext):
    await state.update_data(course=message.text.strip())
    await message.answer("По какому предмету нужен хард-ментор?")
    await state.set_state(ApplicationForm.subject)


@dp.message(ApplicationForm.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await message.answer(
        "Хочешь что-то добавить к заявке? (например, что именно непонятно)\n"
        "Если нет — просто напиши «-»"
    )
    await state.set_state(ApplicationForm.comment)


@dp.message(ApplicationForm.comment)
async def process_comment(message: Message, state: FSMContext):
    data = await state.update_data(comment=message.text.strip())

    db = SessionLocal()
    try:
        application = Application(
            telegram_id=message.from_user.id,
            telegram_username=message.from_user.username,
            full_name=data["full_name"],
            course=data["course"],
            subject=data["subject"],
            comment=None if data["comment"] == "-" else data["comment"],
        )
        db.add(application)
        db.commit()
    finally:
        db.close()

    await message.answer(
        "Спасибо! Заявка принята ✅\n"
        f"Предмет: <b>{data['subject']}</b>\n"
        "Мы сообщим тебе, как только найдём хард-ментора.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
