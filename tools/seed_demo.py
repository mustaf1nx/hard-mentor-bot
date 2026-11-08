"""Демо-данные для локальной проверки бота. Ничего не удаляет, повторный запуск безопасен.

    python tools/seed_demo.py --admin 123456789

Создаёт: триместр (текущая неделя = 4), шесть предметов с темами, роли,
прошедшее занятие с вами в роли студента (чтобы пришло приглашение на форму ОС)
и будущее занятие, где вы Ведущий.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from bot.config import Settings, normalize_db_url  # noqa: E402
from bot.db import (Base, Mentor, MentorRole, MentoringSession, SessionBooking, Subject, Term,  # noqa: E402
                    Topic, TopicChecklist, User, utcnow)

SUBJECTS = {
    "Calculus 1": ["Пределы", "Интегралы"],
    "Linear Algebra": ["Матрицы", "Метод Гаусса"],
    "Discrete Math": ["Множества", "Графы"],
    "ICT": ["Сети", "Базы данных"],
    "Programming": ["Массивы", "ООП"],
    "English": ["Academic writing"],
}
FAKE_MENTOR = 900001
FAKE_STUDENT = 900002


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin", type=int, required=True,
                        help="ваш Telegram ID — станет координатором и ментором (/myid)")
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./local.db"))
    args = parser.parse_args()

    url, connect_args = normalize_db_url(args.db)
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = utcnow()
    async with engine.begin() as conn:
        pass
    from sqlalchemy.ext.asyncio import async_sessionmaker
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async with sm() as s:
        for uid, name, username in ((args.admin, "Вы (координатор)", "me"),
                                    (FAKE_MENTOR, "Демо Ментор", "demo_mentor"),
                                    (FAKE_STUDENT, "Демо Студент", "demo_student")):
            if await s.get(User, uid) is None:
                s.add(User(telegram_id=uid, display_name=name, username=username))
        await s.flush()

        for uid in (args.admin, FAKE_MENTOR):
            if await s.get(Mentor, uid) is None:
                s.add(Mentor(telegram_id=uid))
        await s.flush()

        local = now.replace(tzinfo=__import__("datetime").timezone.utc)
        monday = local.date() - timedelta(days=local.weekday())
        term_start = monday - timedelta(weeks=3)  # сегодня — 4-я неделя
        term = await s.scalar(select(Term).where(Term.name == "demo"))
        if term is None:
            term = Term(name="demo", starts_on=term_start.isoformat(), weeks=12, active=True, cohort=2500)
            s.add(term)
            await s.flush()

        topics: dict[str, Topic] = {}
        for subject_name, topic_names in SUBJECTS.items():
            subject = await s.scalar(select(Subject).where(Subject.name_key == subject_name.casefold()))
            if subject is None:
                subject = Subject(name=subject_name, name_key=subject_name.casefold())
                s.add(subject)
                await s.flush()
            for topic_name in topic_names:
                key = topic_name.casefold()
                topic = await s.scalar(select(Topic).where(Topic.subject_id == subject.id,
                                                           Topic.name_key == key))
                if topic is None:
                    topic = Topic(subject_id=subject.id, name=topic_name, name_key=key)
                    s.add(topic)
                    await s.flush()
                    s.add(TopicChecklist(topic_id=topic.id, author_id=args.admin,
                                         items=["Понимаю определение", "Могу решить типовую задачу"]))
                topics[topic_name] = topic

        calculus = topics["Интегралы"]
        if not await s.scalar(select(MentorRole).where(MentorRole.telegram_id == args.admin,
                                                       MentorRole.term_id == term.id)):
            s.add(MentorRole(telegram_id=args.admin,
                             subject_id=await s.scalar(select(Subject.id).where(
                                 Subject.name_key == "calculus 1")),
                             term_id=term.id, role="lead"))

        past_key, future_key = "demo-past", "demo-future"
        past = await s.scalar(select(MentoringSession).where(MentoringSession.creation_key == past_key))
        if past is None:
            past = MentoringSession(creation_key=past_key, topic_id=calculus.id, mentor_id=FAKE_MENTOR,
                                    created_by=args.admin, starts_at=now - timedelta(days=2, hours=1),
                                    ends_at=now - timedelta(days=2), location="C1.3.233 (демо)",
                                    status="completed", checklist_items=["Понимаю определение"],
                                    revision=1, format="syllabus_clinic", format_confirmed=True,
                                    term_id=term.id)
            s.add(past)
            await s.flush()
            # Записан отдельный демо-студент, а не администратор: роли в сценариях не смешиваются.
            s.add(SessionBooking(session_id=past.id, student_id=FAKE_STUDENT,
                                 question="Не понимаю, как выбрать подстановку в интеграле",
                                 status="active", answers={}, attendance="present",
                                 tried="Перечитал лекцию, разобрал два примера из семинара",
                                 self_rating=2))

        future = await s.scalar(select(MentoringSession).where(MentoringSession.creation_key == future_key))
        if future is None:
            s.add(MentoringSession(creation_key=future_key, topic_id=calculus.id, mentor_id=args.admin,
                                   created_by=args.admin, starts_at=now + timedelta(days=3),
                                   ends_at=now + timedelta(days=3, hours=1),
                                   location="C1.3.233 (демо)", status="scheduled",
                                   checklist_items=["Понимаю определение"], revision=1,
                                   format="one_on_one", format_confirmed=True, term_id=term.id))

        # Занятие, на которое можно записаться: ведёт демо-ментор, вы — студент.
        book_key = "demo-book"
        if await s.scalar(select(MentoringSession).where(MentoringSession.creation_key == book_key)) is None:
            s.add(MentoringSession(creation_key=book_key, topic_id=topics["Пределы"].id,
                                   mentor_id=FAKE_MENTOR, created_by=args.admin,
                                   starts_at=now + timedelta(days=4),
                                   ends_at=now + timedelta(days=4, hours=1),
                                   location="C1.3.233 (демо)", status="scheduled",
                                   checklist_items=["Понимаю определение", "Могу решить типовую задачу"],
                                   revision=1, format="one_on_one", format_confirmed=True, term_id=term.id))
        await s.commit()

    await engine.dispose()
    print("Готово. Демо-данные созданы (повторный запуск ничего не дублирует).\n")
    print(f"  координатор/ментор: {args.admin}")
    print("  триместр: demo, сегодня 4-я неделя (не пиковая — автоотмена работает)")
    print("  6 предметов по 1–2 темы, у вас роль «Ведущий» по Calculus 1")
    print("  прошедшее занятие 2 дня назад, вы в нём записаны → придёт приглашение на форму ОС")
    print("  будущее занятие через 3 дня, вы Ведущий\n")
    print("Дальше:")
    print("  $env:BOT_TOKEN=\"...\"")
    print(f"  $env:COORDINATOR_IDS=\"{args.admin}\"")
    print("  $env:DATABASE_URL=\"sqlite+aiosqlite:///./local.db\"")
    print("  .venv\\Scripts\\python -m bot")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
