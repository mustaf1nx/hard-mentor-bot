"""Подготавливает отзыв для отдельного студента на локальном тестовом стенде."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
subprocess.run([sys.executable, str(ROOT / 'tools' / 'seed_demo.py'), *sys.argv[1:]], check=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from bot.config import normalize_db_url
from bot.db import MentoringSession, SessionBooking, User


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--admin', type=int, required=True)
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    url, opts = normalize_db_url(args.db)
    engine = create_async_engine(url, connect_args=opts)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        student = 100501
        if await session.get(User, student) is None:
            session.add(User(telegram_id=student, display_name='Демо Студент', username='demo_student_feedback'))
            await session.flush()
        lesson = await session.scalar(select(MentoringSession).where(MentoringSession.creation_key == 'demo-past'))
        session.add(SessionBooking(session_id=lesson.id, student_id=student, status='active',
                                   question='Не понимаю, как выбрать подстановку в интеграле',
                                   tried='Прочитал лекцию и разобрал два примера.',
                                   self_rating=2, attendance='present', answers={}))
        await session.commit()
    await engine.dispose()


asyncio.run(main())
