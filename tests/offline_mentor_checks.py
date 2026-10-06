"""Offline core checks with real SQLite/SQLAlchemy and synchronous I/O adapters.

Run: python -m unittest tests.offline_mentor_checks -v
These exercise production migration/repository/parser functions WITHOUT Telegram,
aiogram, asyncpg or aiosqlite. The adapters only replace the async driver boundary;
this is not a replacement for the full dispatcher tests in test_mentors.py.
"""
from __future__ import annotations

import asyncio
import ast
import unittest
from pathlib import Path

from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bot import repo
from bot.config import Settings
from bot.db import Base, MENTORS_MIGRATION_KEY, Mentor, Meta, Note, Ticket, User, conflict_insert, init_db
from bot.mentor_refs import MAX_MENTOR_REFS, TooManyMentorRefs, parse_mentor_refs
from bot.texts import T


class SessionAdapter:
    def __init__(self, session):
        self.session = session

    def get_bind(self):
        return self.session.get_bind()

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self.session.scalar(*args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return self.session.scalars(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return self.session.execute(*args, **kwargs)

    async def commit(self):
        self.session.commit()


class ConnectionAdapter:
    def __init__(self, connection, fail_marker=False):
        self.connection, self.fail_marker = connection, fail_marker
        self.dialect = connection.dialect

    async def run_sync(self, callback):
        return callback(self.connection)

    async def scalar(self, *args, **kwargs):
        return self.connection.scalar(*args, **kwargs)

    async def execute(self, statement, *args, **kwargs):
        if self.fail_marker and getattr(statement, "is_insert", False) and statement.table.name == "meta":
            raise RuntimeError("simulated migration interruption")
        return self.connection.execute(statement, *args, **kwargs)


class TransactionAdapter:
    def __init__(self, engine, fail_marker):
        self.context = engine.begin()
        self.fail_marker = fail_marker

    async def __aenter__(self):
        return ConnectionAdapter(self.context.__enter__(), self.fail_marker)

    async def __aexit__(self, *args):
        return self.context.__exit__(*args)


class EngineAdapter:
    def __init__(self, engine, fail_marker=False):
        self.engine, self.fail_marker = engine, fail_marker

    def begin(self):
        return TransactionAdapter(self.engine, self.fail_marker)


def run(awaitable):
    return asyncio.run(awaitable)


class MentorCoreChecks(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        @event.listens_for(self.engine, "connect")
        def enable_fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        # Reproduce the old schema: no mentors table, existing users.role.
        Base.metadata.create_all(self.engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "mentors"])
        self.settings = Settings("test", "unused", coordinator_ids=frozenset({20, 10}), lead_ids=frozenset({10}))

    def tearDown(self):
        self.engine.dispose()

    def init(self):
        run(init_db(EngineAdapter(self.engine)))

    def seed(self, *users):
        with Session(self.engine) as s:
            s.add_all(users)
            s.commit()

    def members(self):
        with Session(self.engine) as s:
            return set(s.scalars(select(Mentor.telegram_id)))

    def edit(self, text, adding=True):
        with Session(self.engine, expire_on_commit=False) as s:
            return run(repo.edit_mentors(SessionAdapter(s), parse_mentor_refs(text), adding=adding,
                                         protected_ids=self.settings.staff_ids))

    def test_01_legacy_import_preserves_users_tickets_notes_and_meta(self):
        self.seed(User(telegram_id=1, username="old", role="mentor"), User(telegram_id=2, role="mentee"))
        with Session(self.engine) as s:
            ticket = Ticket(category="question", text="Existing ticket", submitter_id=2, assigned_to=1)
            s.add(ticket)
            s.flush()
            s.add(Note(ticket_id=ticket.id, text="Existing note"))
            s.add(Meta(key="last_weekly_report", value="2026-09-21"))
            s.commit()
        self.init()
        self.assertEqual(self.members(), {1})
        with Session(self.engine) as s:
            self.assertEqual(s.get(User, 1).role, "mentor")
            self.assertEqual(s.scalar(select(Ticket.text)), "Existing ticket")
            self.assertEqual(s.scalar(select(Note.text)), "Existing note")
            self.assertEqual(s.get(Meta, "last_weekly_report").value, "2026-09-21")
            self.assertEqual(s.get(Meta, MENTORS_MIGRATION_KEY).value, "done")

    def test_02_sql_removed_legacy_mentor_is_not_resurrected(self):
        self.seed(User(telegram_id=1, role="mentor"))
        self.init()
        with Session(self.engine) as s:
            s.execute(delete(Mentor))
            s.commit()
        self.init()
        self.init()
        self.assertEqual(self.members(), set())
        with Session(self.engine) as s:
            self.assertEqual(s.get(User, 1).role, "mentor")
            self.assertEqual(run(repo.effective_role(SessionAdapter(s), self.settings, 1)), "mentee")

    def test_03_migration_merges_preexisting_table_without_duplicate(self):
        self.seed(User(telegram_id=1, role="mentor"), User(telegram_id=2))
        Mentor.__table__.create(self.engine)
        self.seed(Mentor(telegram_id=1), Mentor(telegram_id=2))
        self.init()
        self.assertEqual(self.members(), {1, 2})

    def test_04_migration_failure_rolls_back_import_and_marker(self):
        self.seed(User(telegram_id=1, role="mentor"))
        Mentor.__table__.create(self.engine)
        with self.assertRaises(RuntimeError):
            run(init_db(EngineAdapter(self.engine, fail_marker=True)))
        self.assertEqual(self.members(), set())
        with Session(self.engine) as s:
            self.assertIsNone(s.get(Meta, MENTORS_MIGRATION_KEY))
        self.init()
        self.assertEqual(self.members(), {1})

    def test_05_bulk_normalization_duplicate_handles_ids_and_links(self):
        self.init()
        self.seed(User(telegram_id=1, username="one"), User(telegram_id=2, username="Two"),
                  User(telegram_id=3, username="three"))
        result = self.edit("\ufeff@ONE, 2; https://t.me/three\n1 @one")
        self.assertEqual(self.members(), {1, 2, 3})
        self.assertEqual(len(result.changed), 3)
        self.assertEqual(result.duplicates, 2)
        self.assertFalse(result.unknown or result.invalid or result.ambiguous)
        with Session(self.engine) as s:
            self.assertTrue(all(u.role == "mentee" for u in s.scalars(select(User))))

    def test_06_repeated_add_is_idempotent(self):
        self.init()
        self.seed(User(telegram_id=1, username="one"))
        self.edit("1")
        result = self.edit("@ONE")
        self.assertEqual(len(result.changed), 0)
        self.assertEqual(len(result.unchanged), 1)
        self.assertEqual(self.members(), {1})

    def test_07_unknown_invalid_and_ambiguous_skip_only_bad_refs(self):
        self.init()
        self.seed(User(telegram_id=1, username="one"), User(telegram_id=2, username="dupe"),
                  User(telegram_id=3, username="DUPE"))
        result = self.edit("@one @missing @DUPE -42 <bad> 0")
        self.assertEqual(self.members(), {1})
        self.assertEqual(result.unknown, ["@missing"])
        self.assertEqual(result.ambiguous, ["@DUPE"])
        self.assertEqual(result.invalid, ["-42", "<bad>", "0"])
        self.edit("2")
        self.assertEqual(self.members(), {1, 2})

    def test_08_thousand_entries_added_in_batches(self):
        self.init()
        self.seed(*(User(telegram_id=10000+i, username=f"person_{i}") for i in range(1000)))
        result = self.edit("\n".join(f"@person_{i}" for i in range(1000)))
        self.assertEqual(len(result.changed), 1000)
        self.assertEqual(len(self.members()), 1000)
        result = self.edit(" ".join(str(10000+i) for i in range(1000)), adding=False)
        self.assertEqual(len(result.changed), 1000)
        self.assertEqual(self.members(), set())

    def test_09_bulk_error_rolls_back_earlier_batch(self):
        self.init()
        self.seed(*(User(telegram_id=10000+i) for i in range(301)))
        class FailingSession(SessionAdapter):
            inserts = 0
            async def scalars(self, statement, *args, **kwargs):
                if getattr(statement, "is_insert", False):
                    self.inserts += 1
                    if self.inserts == 2:
                        raise RuntimeError("simulated second-batch failure")
                return await super().scalars(statement, *args, **kwargs)
        with self.assertRaises(RuntimeError):
            with Session(self.engine) as s:
                run(repo.edit_mentors(FailingSession(s), parse_mentor_refs(" ".join(str(10000+i) for i in range(301))), adding=True))
        self.assertEqual(self.members(), set())

    def test_10_env_priority_is_preserved(self):
        self.init()
        self.seed(User(telegram_id=10), User(telegram_id=20), User(telegram_id=30), User(telegram_id=40))
        self.edit("10 20 30")
        with Session(self.engine) as s:
            a = SessionAdapter(s)
            self.assertEqual(run(repo.effective_role(a, self.settings, 10)), "lead")
            self.assertEqual(run(repo.effective_role(a, self.settings, 20)), "coordinator")
            self.assertEqual(run(repo.effective_role(a, self.settings, 30)), "mentor")
            self.assertEqual(run(repo.effective_role(a, self.settings, 40)), "mentee")
        self.edit("10 20 30", adding=False)
        with Session(self.engine) as s:
            a = SessionAdapter(s)
            self.assertEqual(run(repo.effective_role(a, self.settings, 10)), "lead")
            self.assertEqual(run(repo.effective_role(a, self.settings, 20)), "coordinator")
            self.assertEqual(run(repo.effective_role(a, self.settings, 30)), "mentee")

    def test_11_direct_database_edits_take_effect_without_restart(self):
        self.init()
        self.seed(User(telegram_id=1, role="mentor"))
        with Session(self.engine) as s:
            self.assertFalse(run(repo.is_mentor(SessionAdapter(s), 1)))
        self.seed(Mentor(telegram_id=1))
        with Session(self.engine) as s:
            self.assertTrue(run(repo.is_mentor(SessionAdapter(s), 1)))
            s.execute(delete(Mentor))
            s.commit()
            self.assertFalse(run(repo.is_mentor(SessionAdapter(s), 1)))

    def test_12_remove_unassigns_open_only_and_preserves_admin_assignments(self):
        self.init()
        self.seed(User(telegram_id=1), User(telegram_id=20))
        self.edit("1 20")
        self.seed(Ticket(category="question", text="open", assigned_to=1, status="in_progress"),
                  Ticket(category="question", text="closed", assigned_to=1, status="closed"),
                  Ticket(category="question", text="admin", assigned_to=20, status="new"))
        result = self.edit("1 20", adding=False)
        self.assertEqual(result.unassigned, 1)
        with Session(self.engine) as s:
            rows = {t.text: t.assigned_to for t in s.scalars(select(Ticket))}
            self.assertEqual(rows, {"open": None, "closed": 1, "admin": 20})
            self.assertEqual(s.scalar(select(func.count()).select_from(User)), 2)

    def test_13_membership_foreign_key_and_delete_cascade(self):
        self.init()
        with self.assertRaises(IntegrityError):
            self.seed(Mentor(telegram_id=404))
        self.seed(User(telegram_id=1))
        self.seed(Mentor(telegram_id=1))
        with Session(self.engine) as s:
            s.execute(delete(User).where(User.telegram_id == 1))
            s.commit()
        self.assertEqual(self.members(), set())
        self.assertEqual(set(Mentor.__table__.columns.keys()), {"telegram_id"})

    def test_14_parser_limits_and_hostile_inputs(self):
        self.assertEqual(parse_mentor_refs(" \n\t,;;").total, 0)
        parsed = parse_mentor_refs("9223372036854775808 -1 0 @@one １２３ @one")
        self.assertEqual(len(parsed.invalid), 5)
        self.assertEqual([r.value for r in parsed.refs], ["one"])
        with self.assertRaises(TooManyMentorRefs):
            parse_mentor_refs(" ".join(["1"] * (MAX_MENTOR_REFS + 1)))
        self.assertEqual(parse_mentor_refs("9" * 20000).invalid, ["9" * 20000])

    def test_15_unknown_ids_never_fabricate_users(self):
        self.init()
        result = self.edit("100000 @nobody")
        self.assertEqual(result.unknown, ["100000", "@nobody"])
        with Session(self.engine) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(User)), 0)
        self.assertEqual(self.members(), set())

    def test_16_members_are_listed_from_new_table_and_stably_sorted(self):
        self.init()
        self.seed(User(telegram_id=1, display_name="beta"), User(telegram_id=2, display_name="alpha"),
                  User(telegram_id=3, display_name="alpha"), User(telegram_id=4, role="mentor"))
        self.edit("1 2 3")
        with Session(self.engine, expire_on_commit=False) as s:
            listed = run(repo.list_mentors(SessionAdapter(s)))
            self.assertEqual([u.telegram_id for u in listed], [2, 3, 1])

    def test_17_postgresql_statements_compile(self):
        stmt = conflict_insert(Mentor.__table__, "postgresql").from_select(
            ["telegram_id"], select(User.telegram_id).where(User.role == "mentor"),
        ).on_conflict_do_nothing(index_elements=["telegram_id"])
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.assertIn("INSERT INTO mentors", sql)
        self.assertIn("ON CONFLICT (telegram_id) DO NOTHING", sql)
        sql = str(conflict_insert(Mentor.__table__, "postgresql").values(telegram_id=1)
                  .on_conflict_do_nothing(index_elements=["telegram_id"]).returning(Mentor.telegram_id)
                  .compile(dialect=postgresql.dialect()))
        self.assertIn("RETURNING mentors.telegram_id", sql)
        with self.assertRaises(ValueError):
            conflict_insert(Mentor.__table__, "unsupported")

    def test_18_all_literal_translation_keys_exist_in_both_languages(self):
        root = Path(__file__).parents[1] / "bot"
        for file in root.rglob("*.py"):
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t":
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                        key = node.args[1].value
                        self.assertIn(key, T, f"{file}: {key}")
                        self.assertIn("ru", T[key])
                        self.assertIn("en", T[key])


if __name__ == "__main__":
    unittest.main(verbosity=2)
