"""Read-only аудит копии PostgreSQL перед миграциями.

    python tools/audit_pg_copy.py --database-url "postgresql://…/copy"

Не выводит имена, вопросы и тексты. Только схема, количества и распределения.
Запускать на отдельной копии, не на production.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

EXPECTED_TABLES = (
    'users', 'mentors', 'mentor_subjects', 'mentor_topics', 'mentoring_sessions',
    'session_bookings', 'learning_materials', 'mentor_weekly_reports', 'tickets',
    'terms', 'mentor_roles', 'session_feedback', 'mentor_final_reports',
    'staff_members', 'responsibility_assignments', 'staff_history', 'session_format_changes',
)
EXPECTED_COLUMNS = {
    'mentoring_sessions': ('author_id', 'format', 'format_confirmed', 'term_id'),
    'session_bookings': ('tried', 'self_rating'),
    'session_format_changes': ('session_id', 'old_format', 'old_confirmed', 'new_format',
                               'changed_by', 'changed_at'),
    'learning_materials': ('cycle_stage', 'review_status', 'lecturer_name',
                           'reviewed_at', 'review_recorded_by', 'media_type'),
    'mentor_weekly_reports': ('insights', 'focus'),
}


def _out(value) -> str:
    return str(value)


async def table_names(conn) -> set[str]:
    rows = await conn.fetch(
        "select table_name from information_schema.tables where table_schema='public'")
    return {row['table_name'] for row in rows}


async def columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name=$1", table)
    return {row['column_name'] for row in rows}


async def scalar(conn, sql: str):
    return await conn.fetchval(sql)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-url', required=True)
    args = parser.parse_args()
    conn = await asyncpg.connect(args.database_url)
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        print('=== PostgreSQL copy audit (read-only) ===')
        print('server:', (await scalar(conn, 'select version()'))[:80])
        print('size:', await scalar(conn, 'select pg_size_pretty(pg_database_size(current_database()))'))
        has_alembic = 'alembic_version' in await table_names(conn)
        revision = await scalar(conn, 'select version_num from alembic_version limit 1') if has_alembic else None
        print('alembic_version:', revision if has_alembic else 'absent')

        tables = await table_names(conn)
        print('\n=== Tables ===')
        for table in EXPECTED_TABLES:
            print(f'{table}: {"present" if table in tables else "absent"}')

        print('\n=== Missing columns ===')
        missing = False
        for table, expected in EXPECTED_COLUMNS.items():
            if table not in tables:
                continue
            existing = await columns(conn, table)
            for column in expected:
                if column not in existing:
                    print(f'{table}.{column}: absent')
                    missing = True
        if not missing:
            print('none among known target columns')

        print('\n=== Row counts ===')
        for table in EXPECTED_TABLES:
            if table in tables:
                print(f'{table}: {await scalar(conn, f"select count(*) from {table}")}')

        if 'mentoring_sessions' in tables:
            print('\n=== Sessions ===')
            for row in await conn.fetch("select status, count(*) from mentoring_sessions group by 1 order by 1"):
                print('status', row['status'], row['count'])
            existing = await columns(conn, 'mentoring_sessions')
            if 'format' in existing:
                for row in await conn.fetch(
                        "select coalesce(format, '[NULL]') f, count(*) from mentoring_sessions group by 1 order by 1"):
                    print('format', row['f'], row['count'])
            if 'format_confirmed' in existing:
                for row in await conn.fetch(
                        "select coalesce(format_confirmed::text, '[NULL]'), count(*) "
                        "from mentoring_sessions group by 1 order by 1"):
                    print('format_confirmed', row[0], row[1])
            print('duplicate creation_key:',
                  await scalar(conn, "select count(*) from (select creation_key from mentoring_sessions "
                                     "group by 1 having count(*) > 1) d"))
            if 'term_id' in existing:
                print('term_id filled:', await scalar(
                    conn, "select count(*) from mentoring_sessions where term_id is not null"))
            if 'author_id' in existing:
                print('author_id filled:', await scalar(
                    conn, "select count(*) from mentoring_sessions where author_id is not null"))
            print('without topic:', await scalar(
                conn, "select count(*) from mentoring_sessions s left join mentor_topics t "
                      "on t.id=s.topic_id where t.id is null"))
            print('without mentor user:', await scalar(
                conn, "select count(*) from mentoring_sessions s left join users u "
                      "on u.telegram_id=s.mentor_id where u.telegram_id is null"))

        if 'session_bookings' in tables:
            print('\n=== Bookings ===')
            for row in await conn.fetch(
                    "select status, attendance, count(*) from session_bookings group by 1,2 order by 1,2"):
                print('status/attendance', row[0], row[1], row[2])
            existing = await columns(conn, 'session_bookings')
            if 'tried' in existing:
                print('tried filled:', await scalar(
                    conn, "select count(*) from session_bookings where tried is not null and tried <> ''"))
            if 'self_rating' in existing:
                print('self_rating filled:', await scalar(
                    conn, "select count(*) from session_bookings where self_rating is not null"))
            print('duplicate session/student:', await scalar(
                conn, "select count(*) from (select session_id, student_id from session_bookings "
                      "group by 1,2 having count(*) > 1) d"))

        if 'learning_materials' in tables:
            print('\n=== Materials ===')
            existing = await columns(conn, 'learning_materials')
            for column in ('review_status', 'cycle_stage'):
                if column in existing:
                    for row in await conn.fetch(
                            f"select coalesce({column}, '[NULL]'), count(*) from learning_materials "
                            "group by 1 order by 1"):
                        print(column, row[0], row[1])
            print('without author:', await scalar(
                conn, "select count(*) from learning_materials where author_id is null"))
            print('without session:', await scalar(
                conn, "select count(*) from learning_materials where session_id is null"))

        if 'users' in tables:
            print('\n=== Users ===')
            print('users:', await scalar(conn, 'select count(*) from users'))
            print('cohort filled:', await scalar(conn, 'select count(*) from users where cohort is not null'))
        if 'mentors' in tables:
            print('mentors:', await scalar(conn, 'select count(*) from mentors'))
        if 'mentor_weekly_reports' in tables:
            print('weekly reports:', await scalar(conn, 'select count(*) from mentor_weekly_reports'))
        if 'mentor_roles' in tables:
            print('\n=== Mentor roles ===')
            for row in await conn.fetch("select role, count(*) from mentor_roles group by 1 order by 1"):
                print(row['role'], row['count'])

        print('\n=== Done: only reads were executed ===')
        return 0
    finally:
        await conn.close()


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    raise SystemExit(asyncio.run(main()))
