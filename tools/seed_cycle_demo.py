"""Тестовые данные для полного цикла занятия (только _sim.db)."""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
subprocess.run([sys.executable, str(ROOT / 'tools' / 'seed_demo.py'), *sys.argv[1:]], check=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import normalize_db_url
from bot.db import MentoringSession, Mentor, Term, Topic, User, utcnow

ADMIN = 100500
STUDENT = 900002
LEAD = 900001
AUTHOR = 900003


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--admin', type=int, default=ADMIN)
    parser.add_argument('--db', default='sqlite+aiosqlite:///./_sim.db')
    args = parser.parse_args()
    url, connect_args = normalize_db_url(args.db)
    engine = create_async_engine(url, connect_args=connect_args)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        if await s.get(User, AUTHOR) is None:
            s.add(User(telegram_id=AUTHOR, display_name='Демо Автор', username='demo_author'))
            await s.flush()
        if await s.get(Mentor, AUTHOR) is None:
            s.add(Mentor(telegram_id=AUTHOR))
            await s.flush()
        term = await s.scalar(select(Term).where(Term.active.is_(True)))
        topic = await s.scalar(select(Topic).where(Topic.name_key == 'интегралы'))
        lesson = await s.scalar(select(MentoringSession).where(
            MentoringSession.creation_key == 'cycle-demo'))
        now = utcnow()
        if lesson is None:
            lesson = MentoringSession(
                creation_key='cycle-demo', topic_id=topic.id, mentor_id=LEAD,
                author_id=AUTHOR, created_by=args.admin, starts_at=now + timedelta(days=3),
                ends_at=now + timedelta(days=3, hours=1), location='C1.3.233 (цикл)',
                status='scheduled', checklist_items=['Понимаю определение'],
                revision=1, format='weekly_errors', format_confirmed=True,
                term_id=term.id)
            s.add(lesson)
            await s.flush()
        lesson.status = 'scheduled'
        lesson.starts_at = now + timedelta(days=3)
        lesson.ends_at = now + timedelta(days=3, hours=1)
        await s.commit()
        print('cycle lesson', lesson.id, 'student will book in the scenario')
    await engine.dispose()


asyncio.run(main())
