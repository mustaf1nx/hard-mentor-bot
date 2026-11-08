"""«Прод-подобные» данные: много менторов, занятий, записей, отзывов.

    python tools/seed_bulk.py --admin 100500 --mentors 30 --sessions 100

Нужны, чтобы увидеть то, чего не видно на трёх записях: обрезку сообщений,
пагинацию, «… и ещё N» в сводках, поведение дашборда на объёме.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from bot.config import normalize_db_url  # noqa: E402
from bot.db import (Base, LearningMaterial, Mentor, MentorRole, MentorWeeklyReport, MentoringSession,  # noqa: E402
                    SessionBooking, SessionFeedback, Subject, Term, Ticket, Topic, TopicChecklist, User, utcnow)

SUBJECTS = {
    "Calculus 1": ["Пределы", "Интегралы"],
    "Linear Algebra": ["Матрицы", "Метод Гаусса"],
    "Discrete Math": ["Множества", "Графы"],
    "ICT": ["Сети", "Базы данных"],
    "Programming": ["Массивы", "ООП"],
    "English": ["Academic writing", "Presentations"],
}
FORMATS = ["one_on_one", "intensive", "syllabus_clinic", "mock_review", "how_to_start"]
QUESTIONS = [
    "Не понимаю, как выбрать подстановку в интеграле",
    "Не сходится ответ в задаче на матрицы",
    "Путаюсь в определениях графа и дерева",
    "Не понимаю, как считается итоговая оценка",
    "Застрял на третьем шаге доказательства",
]
TRIED = "Прочитал лекцию, разобрал два примера из семинара, но на третьем шаге застрял."


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin", type=int, required=True)
    parser.add_argument("--mentors", type=int, default=30)
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./_sim.db"))
    args = parser.parse_args()

    random.seed(42)
    url, connect_args = normalize_db_url(args.db)
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    async with sm() as s:
        # люди
        students = list(range(600_001, 600_401))
        for uid in [args.admin, *students]:
            if await s.get(User, uid) is None:
                s.add(User(telegram_id=uid, display_name=f"Студент {uid % 1000}", username=f"student{uid}"))
        mentor_ids = list(range(500_001, 500_001 + args.mentors))
        for index, uid in enumerate(mentor_ids):
            if await s.get(User, uid) is None:
                s.add(User(telegram_id=uid, display_name=f"Ментор {index + 1}", username=f"mentor{index + 1}"))
            if await s.get(Mentor, uid) is None:
                s.add(Mentor(telegram_id=uid))
        if await s.get(User, args.admin) is None:
            s.add(User(telegram_id=args.admin, display_name="Координатор", username="coord"))
        await s.flush()

        # триместр
        local = now
        monday = local.date() - timedelta(days=local.weekday())
        term = await s.scalar(select(Term).where(Term.name == "bulk"))
        if term is None:
            term = Term(name="bulk", starts_on=(monday - timedelta(weeks=3)).isoformat(),
                        weeks=12, cohort=2500, active=True)
            s.add(term)
            await s.flush()

        # предметы, темы, чек-листы
        topics: dict[str, list[Topic]] = {}
        for name, topic_names in SUBJECTS.items():
            subject = await s.scalar(select(Subject).where(Subject.name_key == name.casefold()))
            if subject is None:
                subject = Subject(name=name, name_key=name.casefold())
                s.add(subject)
                await s.flush()
            topics[name] = []
            for topic_name in topic_names:
                topic = await s.scalar(select(Topic).where(Topic.subject_id == subject.id,
                                                           Topic.name_key == topic_name.casefold()))
                if topic is None:
                    topic = Topic(subject_id=subject.id, name=topic_name, name_key=topic_name.casefold())
                    s.add(topic)
                    await s.flush()
                    s.add(TopicChecklist(topic_id=topic.id, author_id=args.admin,
                                         items=["Понимаю определение", "Могу решить типовую задачу",
                                                "Вижу, где обычно теряют баллы"]))
                topics[name].append(topic)

        # роли: по 5 менторов на предмет, первый — Ведущий
        for index, uid in enumerate(mentor_ids):
            subject_name = list(SUBJECTS)[index % len(SUBJECTS)]
            subject = await s.scalar(select(Subject).where(Subject.name_key == subject_name.casefold()))
            role = "lead" if index < len(SUBJECTS) else ("author" if index % 3 == 2 else "respondent")
            exists = await s.scalar(select(MentorRole).where(
                MentorRole.telegram_id == uid, MentorRole.subject_id == subject.id,
                MentorRole.term_id == term.id))
            if exists is None:
                s.add(MentorRole(telegram_id=uid, subject_id=subject.id, term_id=term.id, role=role))

        # занятия: раскладываем по неделям ровно, иначе «68 слотов за неделю» — не данные, а шум
        made = 0
        existing = await s.scalar(select(MentoringSession.id).limit(1))
        if existing is None:
            weeks_span = [-3, -2, -1, 0, 1, 2, 3, 4, 5]
            for n in range(args.sessions):
                subject_name = list(SUBJECTS)[n % len(SUBJECTS)]
                topic = random.choice(topics[subject_name])
                mentor_id = mentor_ids[n % len(mentor_ids)]
                week = weeks_span[n % len(weeks_span)]
                day = (n // len(weeks_span)) % 5
                day_date = monday + timedelta(weeks=week, days=day)
                start = datetime.combine(day_date, datetime.min.time()).replace(
                    hour=random.choice([10, 14, 16, 18]), minute=0)
                status = "completed" if start < now else "scheduled"
                if status == "completed" and n % 11 == 0:
                    status = "cancelled"
                lesson = MentoringSession(
                    creation_key=f"bulk-{n}", topic_id=topic.id, mentor_id=mentor_id,
                    created_by=args.admin, starts_at=start, ends_at=start + timedelta(hours=1),
                    location=f"C1.{random.randint(1, 4)}.2{random.randint(10, 60)}",
                    status=status, revision=1, format=random.choice(FORMATS),
                    format_confirmed=True, term_id=term.id,
                    checklist_items=["Понимаю определение", "Могу решить типовую задачу"],
                    cancel_reason="Ведущий заболел" if status == "cancelled" else None)
                s.add(lesson)
                await s.flush()
                made += 1
                max_bookings = 1 if lesson.format == 'one_on_one' else 6
                for student in random.sample(students, random.randint(0, max_bookings)):
                    booking = SessionBooking(
                        session_id=lesson.id, student_id=student, question=random.choice(QUESTIONS),
                        status="active", answers={"0": 2, "1": 1},
                        attendance=random.choice(["present", "present", "absent", "unknown"]),
                        tried=TRIED, self_rating=random.randint(1, 5))
                    s.add(booking)
                    await s.flush()
                    if status == "completed" and random.random() < 0.6:
                        s.add(SessionFeedback(
                            session_id=lesson.id, mentor_id=mentor_id, student_id=student,
                            clarity=random.randint(2, 5), progressed=random.choice(["yes", "partial", "no"]),
                            honesty=random.choice(["ok"] * 9 + ["violation"]),
                            overall=random.randint(2, 5), best="Разобрали мою задачу",
                            next_topics="Пределы"))
                if status == "completed" and random.random() < 0.5:
                    s.add(LearningMaterial(topic_id=topic.id, session_id=lesson.id, author_id=mentor_id,
                                           file_id=f"f{n}", file_unique_id=f"u{n}",
                                           file_name=f"summary-{n}.pdf", kind="material",
                                           cycle_stage="final", review_status="approved"))

        # часть отчётов менторов
        week_start = (now - timedelta(days=now.weekday())).date().isoformat()
        for uid in mentor_ids[: max(1, args.mentors // 2)]:
            done = await s.scalar(select(MentorWeeklyReport).where(
                MentorWeeklyReport.mentor_id == uid, MentorWeeklyReport.week_start == week_start))
            if done is None:
                s.add(MentorWeeklyReport(mentor_id=uid, week_start=week_start,
                                         content="С чем трудно: подстановка и пределы.",
                                         insights="Подстановка и пределы — самая частая трудность.",
                                         focus="Разобрать два примера и дать чек-лист.",))

        # обращения: одно просроченное серьёзное
        if await s.scalar(select(Ticket.id).limit(1)) is None:
            s.add(Ticket(category="serious", text="Проверка просрочки SLA на объёме",
                         status="new", created_at=now - timedelta(hours=10)))
        await s.commit()

    await engine.dispose()
    print(f"Готово: менторов {args.mentors}, занятий {made}, триместр bulk, поток 2500")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
