"""Real SQLite/SQLAlchemy outbox checks, with an async-boundary adapter.

Run: python -m unittest tests.offline_ticket_notification_checks -v
No Telegram calls. The optional transport checks execute the production function
with injected API exception classes/UI stand-ins; they do not emulate aiogram's
Dispatcher or validate Telegram's remote service.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from bot import repo, ticket_notifications as queue
from bot.config import Settings
from bot.db import Base, Mentor, Note, Ticket, TicketNotification, User, init_db
from tests.offline_mentor_checks import EngineAdapter, run
from tests.offline_session_checks import Adapter

NOW = datetime(2026, 10, 6, 11, 28, 17)
COORD, LEAD, MENTOR, OTHER, ALICE = 10, 20, 30, 40, 1001
SETTINGS = Settings("test", "unused", coordinator_ids=frozenset({COORD}), lead_ids=frozenset({LEAD}))


class AsyncContext:
    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)

    async def __aenter__(self):
        return Adapter(self.session)

    async def __aexit__(self, kind, exc, tb):
        self.session.close()  # uncommitted work rolls back


class NotificationChecks(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        @event.listens_for(self.engine, "connect")
        def fk(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        run(init_db(EngineAdapter(self.engine)))
        self.sm = lambda: AsyncContext(self.engine)
        with Session(self.engine) as s:
            s.add_all(User(telegram_id=uid, display_name=f"Staff{uid}", username=f"user{uid}")
                      for uid in (COORD, LEAD, MENTOR, OTHER, ALICE))
            s.commit()
            s.add_all([Mentor(telegram_id=MENTOR), Mentor(telegram_id=OTHER)])
            s.commit()
        self.sent = []

    def tearDown(self):
        self.engine.dispose()

    def create(self, settings=SETTINGS, **changes):
        values = dict(category="question", text="Как подготовиться к следующему занятию?",
                      is_anonymous=False, submitter_id=ALICE, submitter_name="Alice", now=NOW,
                      notification_settings=settings)
        values.update(changes)
        with Session(self.engine, expire_on_commit=False) as s:
            return run(repo.create_ticket(Adapter(s), **values))

    def rows(self):
        with Session(self.engine) as s:
            return list(s.scalars(select(TicketNotification).order_by(TicketNotification.id)))

    def edit(self, **values):
        with Session(self.engine) as s:
            s.execute(update(TicketNotification).values(**values))
            s.commit()

    async def sender(self, ticket, uid):
        self.sent.append((ticket.id, uid))
        return queue.DeliveryResult("sent")

    def drain(self, sender=None, settings=SETTINGS, **kwargs):
        return run(queue.drain(self.sm, settings, sender or self.sender, now=kwargs.pop("now", NOW), **kwargs))

    def test_01_default_receivers_are_coordinators_and_leads(self):
        ticket = self.create()
        self.assertEqual({n.recipient_id for n in self.rows()}, {COORD, LEAD})
        self.assertEqual(self.drain(), 2)
        self.assertEqual(set(self.sent), {(ticket.id, COORD), (ticket.id, LEAD)})

    def test_02_explicit_legacy_setting_is_honoured(self):
        settings = replace(SETTINGS, lead_gets_all=False)
        ticket = self.create(settings=settings)
        self.assertEqual(queue.recipients_for(settings, ticket), {COORD})
        self.assertEqual({n.recipient_id for n in self.rows()}, {COORD})

    def test_03_environment_default_true_and_explicit_false(self):
        env = dict(BOT_TOKEN="42:test", DATABASE_URL="postgresql://localhost/db")
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(Settings.from_env().lead_gets_all)
        with patch.dict(os.environ, dict(env, LEAD_GETS_ALL="false"), clear=True):
            self.assertFalse(Settings.from_env().lead_gets_all)

    def test_04_restricted_complaint_only_to_lead(self):
        ticket = self.create(category="complaint", lead_only=True, assigned_to=MENTOR, mentor_id=MENTOR)
        self.assertEqual({n.recipient_id for n in self.rows()}, {LEAD})
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.sent, [(ticket.id, LEAD)])

    def test_05_serious_ticket_goes_to_both_even_when_lead_all_disabled(self):
        settings = replace(SETTINGS, lead_gets_all=False)
        self.create(settings=settings, category="serious")
        self.assertEqual({n.recipient_id for n in self.rows()}, {COORD, LEAD})

    def test_06_mentor_does_not_receive_complaint_about_them(self):
        self.create(category="complaint", mentor_id=MENTOR)
        self.drain()
        self.assertEqual({uid for _, uid in self.sent}, {COORD, LEAD})

    def test_07_praise_includes_active_assigned_mentor(self):
        self.create(category="praise", mentor_id=MENTOR, assigned_to=MENTOR, status="closed")
        self.assertEqual(self.drain(), 3)
        self.assertEqual({uid for _, uid in self.sent}, {COORD, LEAD, MENTOR})

    def test_08_inactive_or_unrelated_mentor_does_not_receive_ticket(self):
        self.create(assigned_to=ALICE, mentor_id=OTHER)
        self.drain()
        self.assertEqual({uid for _, uid in self.sent}, {COORD, LEAD})

    def test_09_one_person_with_two_roles_is_not_notified_twice(self):
        settings = replace(SETTINGS, coordinator_ids=frozenset({LEAD}))
        self.create(settings=settings)
        self.assertEqual(self.drain(settings=settings), 1)

    def test_10_lead_fallback_when_no_coordinator(self):
        settings = replace(SETTINGS, coordinator_ids=frozenset(), lead_gets_all=False)
        self.create(settings=settings)
        self.assertEqual(self.drain(settings=settings), 1)
        self.assertEqual(self.sent[0][1], LEAD)

    def test_11_idempotent_enqueue_and_no_repeat_of_sent_jobs(self):
        ticket = self.create()
        async def again():
            async with self.sm() as s:
                await queue.enqueue_new_ticket(s, SETTINGS, ticket)
                await s.commit()
        run(again())
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.drain(), 2)
        run(again())
        self.assertEqual(self.drain(), 0)
        self.assertEqual(len(self.sent), 2)

    def test_12_ticket_and_outbox_are_one_transaction(self):
        original = queue.enqueue_new_ticket
        async def fail_after_insert(s, settings, ticket):
            await original(s, settings, ticket)
            raise RuntimeError("simulated crash before commit")
        with patch.object(queue, "enqueue_new_ticket", fail_after_insert):
            with self.assertRaises(RuntimeError):
                self.create()
        with Session(self.engine) as s:
            self.assertEqual(s.scalar(select(func.count(Ticket.id))), 0)
            self.assertEqual(s.scalar(select(func.count(TicketNotification.id))), 0)

    def test_13_worker_recovers_after_commit_without_immediate_send(self):
        self.create()
        self.assertEqual({n.status for n in self.rows()}, {"pending"})
        # A newly constructed sessionmaker stands in for a new process.
        sm2 = lambda: AsyncContext(self.engine)
        sent = run(queue.drain(sm2, SETTINGS, self.sender, now=NOW + timedelta(minutes=1)))
        self.assertEqual(sent, 2)

    def test_14_transient_failure_does_not_block_other_recipients(self):
        self.create()
        async def fail_one(ticket, uid):
            if uid == COORD:
                raise OSError("sensitive external exception text")
            return await self.sender(ticket, uid)
        self.assertEqual(self.drain(sender=fail_one), 1)
        coord, lead = self.rows()
        self.assertEqual((coord.status, lead.status), ("pending", "sent"))
        self.assertEqual(coord.last_error, "OSError")
        self.assertEqual(coord.due_at, NOW + timedelta(seconds=30))
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=29)), 0)
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=31)), 1)

    def test_15_retry_after_is_respected(self):
        self.create()
        async def limited(ticket, uid):
            return queue.DeliveryResult("retry", "TelegramRetryAfter", 123)
        self.assertEqual(self.drain(sender=limited), 0)
        self.assertTrue(all(n.due_at == NOW + timedelta(seconds=123) for n in self.rows()))
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=122)), 0)
        self.assertEqual(self.drain(now=NOW + timedelta(seconds=124)), 2)

    def test_16_permanent_failure_waits_for_explicit_self_test(self):
        self.create()
        async def blocked(ticket, uid):
            return queue.DeliveryResult("failed", "TelegramForbiddenError")
        self.drain(sender=blocked)
        self.assertEqual(self.drain(now=NOW + timedelta(days=1)), 0)
        async def retry():
            async with self.sm() as s:
                count = await queue.retry_for_recipient(s, SETTINGS, COORD, NOW)
                await s.commit()
                return count
        self.assertEqual(run(retry()), 1)
        self.assertEqual(self.drain(), 1)
        self.assertEqual([(n.recipient_id, n.status) for n in self.rows()], [(COORD, "sent"), (LEAD, "failed")])

    def test_17_success_clears_transient_timestamps(self):
        self.create(is_anonymous=True)
        self.drain()
        for row in self.rows():
            self.assertIsNone(row.due_at)
            self.assertIsNone(row.lease_until)
            self.assertIsNone(row.lease_token)
            self.assertIsNone(row.last_error)

    def test_18_anonymous_identity_is_not_in_ticket_or_outbox(self):
        ticket = self.create(is_anonymous=True)
        self.assertIsNone(ticket.submitter_id)
        self.assertIsNone(ticket.submitter_name)
        self.assertEqual(ticket.created_at, NOW.replace(minute=0, second=0, microsecond=0))
        for row in self.rows():
            dump = " ".join(str(v) for k, v in vars(row).items() if not k.startswith("_"))
            self.assertNotIn(str(ALICE), dump)
            self.assertNotIn("Alice", dump)
            self.assertEqual(row.due_at, ticket.created_at)
        columns = set(TicketNotification.__table__.columns.keys())
        self.assertFalse({"submitter_id", "submitter_name", "text", "created_at", "sent_at"} & columns)

    def test_19_active_lease_prevents_duplicate_worker_send(self):
        self.create()
        self.edit(status="sending", lease_until=NOW + timedelta(seconds=90), lease_token="worker1")
        self.assertEqual(self.drain(), 0)
        self.assertEqual(self.sent, [])

    def test_20_expired_lease_is_recovered(self):
        self.create()
        self.edit(status="sending", lease_until=NOW - timedelta(seconds=1), lease_token="crashed")
        self.assertEqual(self.drain(), 2)
        self.assertTrue(all(n.status == "sent" for n in self.rows()))

    def test_21_two_concurrent_workers_do_not_double_send(self):
        self.create()
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()
            async def slow(ticket, uid):
                if uid == COORD:
                    started.set()
                    await release.wait()
                return await self.sender(ticket, uid)
            first = asyncio.create_task(queue.drain(self.sm, SETTINGS, slow, now=NOW))
            await started.wait()
            second = await queue.drain(self.sm, SETTINGS, self.sender, now=NOW)
            release.set()
            return await first, second
        self.assertEqual(sum(run(scenario())), 2)
        self.assertEqual(len(self.sent), len(set(self.sent)))

    def test_22_old_lease_receipt_cannot_overwrite_newer_worker(self):
        self.create()
        self.edit(status="sending", lease_token="newer", lease_until=NOW + timedelta(minutes=3))
        row = self.rows()[0]
        async def stale():
            async with self.sm() as s:
                await queue.finish_delivery(s, row.id, "older", queue.DeliveryResult("failed", "error"), 1, NOW)
                await s.commit()
        run(stale())
        current = self.rows()[0]
        self.assertEqual((current.status, current.lease_token), ("sending", "newer"))

    def test_23_removed_mentor_is_skipped_before_retry(self):
        self.create(category="praise", mentor_id=MENTOR, assigned_to=MENTOR, status="closed")
        with Session(self.engine) as s:
            s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR))
            # Legacy role must not grant access back.
            s.get(User, MENTOR).role = "mentor"
            s.commit()
        self.assertEqual(self.drain(), 2)
        mentor = next(n for n in self.rows() if n.recipient_id == MENTOR)
        self.assertEqual(mentor.status, "skipped")
        self.assertNotIn(MENTOR, {uid for _, uid in self.sent})

    def test_24_restricted_flag_change_rechecks_coordinator_access(self):
        ticket = self.create()
        with Session(self.engine) as s:
            s.get(Ticket, ticket.id).lead_only = True
            s.commit()
        self.assertEqual(self.drain(), 1)
        self.assertEqual(self.sent, [(ticket.id, LEAD)])

    def test_25_removed_leader_does_not_receive_old_queued_ticket(self):
        self.create()
        settings = replace(SETTINGS, lead_ids=frozenset())
        self.assertEqual(self.drain(settings=settings), 1)
        self.assertEqual({uid for _, uid in self.sent}, {COORD})

    def test_26_no_configured_recipients_can_recover_after_config_change(self):
        settings = replace(SETTINGS, lead_ids=frozenset(), coordinator_ids=frozenset())
        self.create(settings=settings)
        self.assertEqual([n.recipient_id for n in self.rows()], [0])
        self.assertEqual(self.drain(settings=settings), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.drain(settings=SETTINGS, now=NOW + timedelta(minutes=2)), 0)
        self.assertEqual(self.drain(settings=SETTINGS, now=NOW + timedelta(minutes=2)), 2)
        self.assertNotIn(0, {uid for _, uid in self.sent})

    def test_27_restricted_unrouted_ticket_never_falls_back_to_coordinator(self):
        settings = replace(SETTINGS, lead_ids=frozenset())
        self.create(settings=settings, category="complaint", lead_only=True)
        self.assertEqual(self.drain(settings=settings), 0)
        self.assertEqual(self.sent, [])

    def test_28_batch_limit_and_followup_drain(self):
        settings = replace(SETTINGS, coordinator_ids=frozenset(range(100, 145)), lead_ids=frozenset())
        self.create(settings=settings)
        self.assertEqual(self.drain(settings=settings, limit=30), 30)
        self.assertEqual(self.drain(settings=settings, limit=30), 15)
        self.assertEqual(len(self.sent), 45)

    def test_29_retry_backoff_does_not_exceed_one_hour(self):
        self.create()
        self.edit(attempts=500)
        async def temporary(ticket, uid):
            return queue.DeliveryResult("retry", "TelegramNetworkError")
        self.drain(sender=temporary)
        self.assertTrue(all(n.due_at == NOW + timedelta(hours=1) for n in self.rows()))

    def test_30_migration_is_non_destructive_and_no_historical_broadcast(self):
        ticket = self.create(settings=None)
        self.assertEqual(self.rows(), [])
        TicketNotification.__table__.drop(self.engine)
        run(init_db(EngineAdapter(self.engine)))
        run(init_db(EngineAdapter(self.engine)))
        with Session(self.engine) as s:
            self.assertIsNotNone(s.get(Ticket, ticket.id))
            self.assertEqual(set(s.scalars(select(Mentor.telegram_id))), {MENTOR, OTHER})
        self.assertEqual(self.rows(), [])

    def test_31_uniqueness_foreign_key_and_postgres_schema_compile(self):
        ticket = self.create()
        with Session(self.engine) as s:
            s.add(TicketNotification(ticket_id=ticket.id, recipient_id=COORD))
            with self.assertRaises(IntegrityError):
                s.commit()
        with Session(self.engine) as s:
            s.execute(delete(Ticket).where(Ticket.id == ticket.id))
            s.commit()
        self.assertEqual(self.rows(), [])
        sql = str(CreateTable(TicketNotification.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("BIGINT", sql)
        self.assertIn("UNIQUE (ticket_id, recipient_id)", sql)

    def test_32_retry_of_delivered_job_does_not_reset_receipt(self):
        self.create()
        self.drain()
        async def retry():
            async with self.sm() as s:
                count = await queue.retry_for_recipient(s, SETTINGS, COORD, NOW)
                await s.commit()
                return count
        self.assertEqual(run(retry()), 0)
        self.assertEqual(self.drain(), 0)

    def test_33_retry_command_obeys_revoked_access(self):
        ticket = self.create()
        self.edit(status="failed")
        with Session(self.engine) as s:
            s.get(Ticket, ticket.id).lead_only = True
            s.commit()
        async def retry():
            async with self.sm() as s:
                count = await queue.retry_for_recipient(s, SETTINGS, COORD, NOW)
                await s.commit()
                return count
        self.assertEqual(run(retry()), 0)
        self.assertEqual(self.rows()[0].status, "skipped")

    def test_34_transient_error_log_contains_no_raw_exception(self):
        self.create(is_anonymous=True)
        async def failure(ticket, uid):
            raise OSError("Alice 1001 private text")
        with self.assertLogs("bot.ticket_notifications", logging.WARNING) as captured:
            self.drain(sender=failure)
        output = " ".join(captured.output)
        self.assertNotIn("Alice", output)
        self.assertNotIn("1001", output)
        self.assertNotIn("private text", output)

    def test_35_startup_and_atomic_handler_are_wired(self):
        root = Path(__file__).parents[1]
        startup = (root / "bot/__main__.py").read_text()
        feedback = (root / "bot/handlers/feedback.py").read_text()
        self.assertIn("asyncio.create_task(notify.ticket_notification_loop(bot, sm, settings))", startup)
        self.assertIn("notification_settings=settings", feedback)
        tree = ast.parse(feedback)
        finish = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "finish")
        self.assertTrue(any(isinstance(n, ast.Try) and n.finalbody for n in ast.walk(finish)))

    def test_36_stats_are_scoped_to_the_recipient(self):
        self.create()
        async def stats():
            async with self.sm() as s:
                return await queue.counts_for_recipient(s, COORD), await queue.counts_for_recipient(s, ALICE)
        self.assertEqual(run(stats()), ({"pending": 1}, {}))


# Transport boundary checks. Compile only the original transport function and
# replace its unavailable external dependencies; never import a fake aiogram as
# though it were the real package.
class ApiError(Exception):
    pass
class BadRequest(ApiError):
    pass
class Forbidden(ApiError):
    pass
class RetryAfter(ApiError):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class TransportChecks(unittest.TestCase):
    def setUp(self):
        source = (Path(__file__).parents[1] / "bot/notify.py").read_text()
        tree = ast.parse(source)
        functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_new_ticket"]
        async def langs(sm, ids):
            return {uid: "ru" for uid in ids}
        async def names(sm, ticket):
            return {}
        async def no_sleep(delay):
            pass
        self.markup = {"open": "tk:1"}
        views = SimpleNamespace(staff_card=lambda *args, **kw: "<b>Ticket</b>",
                                open_ticket_kb=lambda *args: self.markup)
        ns = dict(asyncio=SimpleNamespace(sleep=no_sleep), _staff_langs=langs, _names_for=names,
                  views=views, t=lambda lang, key: key, DeliveryResult=queue.DeliveryResult,
                  TelegramAPIError=ApiError, TelegramBadRequest=BadRequest,
                  TelegramForbiddenError=Forbidden, TelegramRetryAfter=RetryAfter)
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + functions,
                            type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "bot/notify.py:_send_new_ticket", "exec"), ns)
        self.send = ns["_send_new_ticket"]
        self.ticket = SimpleNamespace(id=1, code="HM-0001", lead_only=False, category="question", mentor_id=None)
        self.calls = []

    def deliver(self, errors):
        async def send_message(*args, **kwargs):
            self.calls.append((args, kwargs))
            if errors:
                error = errors.pop(0)
                if error is not None:
                    raise error
        return run(self.send(SimpleNamespace(send_message=send_message), None, self.ticket, COORD))

    def test_01_normal_delivery_is_not_silent_and_has_open_button(self):
        self.assertEqual(self.deliver([]).status, "sent")
        options = self.calls[0][1]
        self.assertIs(options["disable_notification"], False)
        self.assertEqual(options["reply_markup"], self.markup)
        self.assertEqual(options["request_timeout"], 15)

    def test_02_html_fallback_keeps_button_and_notification(self):
        self.assertEqual(self.deliver([BadRequest("can't parse entities")]).status, "sent")
        self.assertEqual(len(self.calls), 2)
        options = self.calls[-1][1]
        self.assertIsNone(options["parse_mode"])
        self.assertIs(options["disable_notification"], False)
        self.assertEqual(options["reply_markup"], self.markup)

    def test_03_retry_after_is_returned_to_the_outbox(self):
        result = self.deliver([RetryAfter(72)])
        self.assertEqual((result.status, result.retry_after), ("retry", 72))

    def test_04_blocked_bot_is_permanent_until_retry_command(self):
        result = self.deliver([Forbidden("bot was blocked")])
        self.assertEqual((result.status, result.error), ("failed", "TelegramForbiddenError"))

    def test_05_unknown_chat_does_not_retry_formatting(self):
        result = self.deliver([BadRequest("Bad Request: chat not found")])
        self.assertEqual((result.status, result.error), ("failed", "chat_unavailable"))
        self.assertEqual(len(self.calls), 1)

    def test_06_transient_errors_retry(self):
        for error in [ApiError("network failed"), OSError("offline"), TimeoutError()]:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.deliver([error]).status, "retry")

    def test_07_fallback_rate_limit_also_retries(self):
        result = self.deliver([BadRequest("entities"), RetryAfter(15)])
        self.assertEqual((result.status, result.retry_after), ("retry", 15))

    def test_08_repeated_bad_request_is_recorded_not_raised(self):
        result = self.deliver([BadRequest("entities"), BadRequest("still invalid")])
        self.assertEqual(result.status, "failed")


if __name__ == "__main__":
    unittest.main()
