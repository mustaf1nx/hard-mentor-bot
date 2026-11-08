"""Проверка миграций на трёх сценариях. Запуск: python tools/check_migrations.py"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Консоль Windows по умолчанию cp1252 — без этого русские строки падают.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv" / "Scripts"
ALEMBIC = VENV / "alembic.exe"
DB = ROOT / "_alembic_test.db"

OLD_SCHEMA = """
CREATE TABLE users (telegram_id BIGINT PRIMARY KEY);
CREATE TABLE mentor_subjects (id INTEGER PRIMARY KEY, name TEXT, name_key TEXT);
CREATE TABLE mentor_topics (id INTEGER PRIMARY KEY, subject_id INTEGER, name TEXT, name_key TEXT);
CREATE TABLE mentoring_sessions (
    id INTEGER PRIMARY KEY, creation_key TEXT, topic_id INTEGER, mentor_id BIGINT,
    created_by BIGINT, starts_at DATETIME, ends_at DATETIME, location TEXT,
    status TEXT, cancel_reason TEXT, checklist_items JSON, revision INTEGER);
CREATE TABLE session_bookings (
    id INTEGER PRIMARY KEY, session_id INTEGER, student_id BIGINT, question TEXT,
    status TEXT, answers JSON, attendance TEXT);
CREATE TABLE learning_materials (
    id INTEGER PRIMARY KEY, topic_id INTEGER, session_id INTEGER, author_id BIGINT,
    file_id TEXT, file_unique_id TEXT, file_name TEXT, kind TEXT, visibility TEXT,
    archived BOOLEAN, cycle_stage TEXT);
"""


def run_alembic(*args: str) -> tuple[int, str]:
    env = dict(os.environ, DATABASE_URL=f"sqlite+aiosqlite:///{DB.as_posix()}")
    result = subprocess.run([str(ALEMBIC), *args], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout + result.stderr).strip()


def tables() -> list[str]:
    con = sqlite3.connect(DB)
    try:
        return sorted(r[0] for r in con.execute("select name from sqlite_master where type='table'"))
    finally:
        con.close()


def columns(table: str) -> list[str]:
    con = sqlite3.connect(DB)
    try:
        return sorted(r[1] for r in con.execute(f"pragma table_info({table})"))
    finally:
        con.close()


def indexes() -> list[str]:
    con = sqlite3.connect(DB)
    try:
        return sorted(r[0] for r in con.execute(
            "select name from sqlite_master where type='index' and name not like 'sqlite_autoindex%'"))
    finally:
        con.close()


def revision() -> str:
    con = sqlite3.connect(DB)
    try:
        return con.execute("select version_num from alembic_version").fetchone()[0]
    finally:
        con.close()


def expected_head() -> str:
    """Голова цепочки: ревизия, на которую никто не ссылается как на down_revision."""
    import re

    pairs = []
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision\s*=\s*["\']([^"\']+)', text, re.M)
        down = re.search(r'^down_revision\s*=\s*(?:["\']([^"\']+)["\']|None)', text, re.M)
        if rev:
            pairs.append((rev.group(1), down.group(1) if down and down.group(1) else None))
    down_values = {d for _, d in pairs if d}
    heads = [r for r, _ in pairs if r not in down_values]
    return heads[0] if len(heads) == 1 else "?"


def fresh_db(sql: str = "") -> None:
    if DB.exists():
        DB.unlink()
    if sql:
        con = sqlite3.connect(DB)
        con.executescript(sql)
        con.commit()
        con.close()


def check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if not cond and detail else ""))
    return cond


def scenario_empty() -> bool:
    print("\n[1] Пустая база -> upgrade head")
    fresh_db()
    code, out = run_alembic("upgrade", "head")
    head = expected_head()
    ok = check("upgrade прошёл", code == 0, out[-300:])
    ok &= check("таблица terms создана", "terms" in tables())
    ok &= check("таблица mentor_roles создана", "mentor_roles" in tables())
    ok &= check("таблица session_feedback создана", "session_feedback" in tables())
    ok &= check(f"ревизия {head}", revision() == head, revision())
    return ok


def scenario_old_schema() -> bool:
    print("\n[2] Старая схема (как в проде) -> stamp 0001 -> upgrade head")
    fresh_db(OLD_SCHEMA)
    before = columns("session_bookings")
    code, out = run_alembic("stamp", "0001")
    ok = check("stamp 0001", code == 0, out[-300:])
    ok &= check("колонок ticket ещё нет", "tried" not in before and "self_rating" not in before, str(before))
    code, out = run_alembic("upgrade", "head")
    ok &= check("upgrade прошёл", code == 0, out[-300:])
    ok &= check("session_bookings.tried добавлен", "tried" in columns("session_bookings"), str(columns("session_bookings")))
    ok &= check("session_bookings.self_rating добавлен", "self_rating" in columns("session_bookings"))
    ok &= check("mentoring_sessions.term_id добавлен", "term_id" in columns("mentoring_sessions"))
    ok &= check("mentoring_sessions.author_id добавлен", "author_id" in columns("mentoring_sessions"))
    ok &= check("mentoring_sessions.format_confirmed добавлен", "format_confirmed" in columns("mentoring_sessions"))
    ok &= check("learning_materials.cycle_stage добавлен", "cycle_stage" in columns("learning_materials"))
    ok &= check("learning_materials.media_type добавлен", "media_type" in columns("learning_materials"))
    ok &= check("learning_materials.review_status добавлен", "review_status" in columns("learning_materials"))
    ok &= check("staff_members создана", "staff_members" in tables())
    ok &= check("responsibility_assignments создана", "responsibility_assignments" in tables())
    ok &= check("staff_history создана", "staff_history" in tables())
    ok &= check("session_format_changes создана", "session_format_changes" in tables())
    ok &= check("частичный индекс активного предмета создан", "uq_resp_subject_active" in indexes())
    ok &= check("terms создана", "terms" in tables())
    return ok


def scenario_idempotent() -> bool:
    print("\n[3] Повторный upgrade head — ничего не должно сломаться")
    head = expected_head()
    code, out = run_alembic("upgrade", "head")
    ok = check("повторный upgrade прошёл", code == 0, out[-300:])
    ok &= check(f"ревизия всё ещё {head}", revision() == head, revision())
    return ok


def scenario_create_all_then_upgrade() -> bool:
    print("\n[4] Схема создана create_all (так живут тесты) -> upgrade head")
    fresh_db()
    sys.path.insert(0, str(ROOT))
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from bot.db import Base

    async def create() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{DB.as_posix()}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    ok = check("create_all создал mentor_roles", "mentor_roles" in tables())
    ok &= check("create_all создал staff_members", "staff_members" in tables())
    ok &= check("create_all создал responsibility_assignments", "responsibility_assignments" in tables())
    ok &= check("create_all создал staff_history", "staff_history" in tables())
    ok &= check("create_all создал session_format_changes", "session_format_changes" in tables())
    code, out = run_alembic("upgrade", "head")
    ok &= check("upgrade поверх create_all прошёл", code == 0, out[-400:])
    ok &= check("колонки на месте", "tried" in columns("session_bookings"))
    return ok


if __name__ == "__main__":
    if not ALEMBIC.exists():
        print("alembic не установлен в .venv — pip install alembic")
        raise SystemExit(2)
    results = [scenario_empty(), scenario_old_schema(), scenario_idempotent(), scenario_create_all_then_upgrade()]
    if DB.exists():
        DB.unlink()
    print("\nИТОГ:", "все сценарии прошли" if all(results) else "есть падения")
    raise SystemExit(0 if all(results) else 1)
