"""Real SQLAlchemy/SQLite core checks; only the async driver boundary is adapted.

Run: python -m unittest tests.offline_session_checks -v
This does NOT substitute for Telegram dispatcher tests or a PostgreSQL runtime test.
"""
from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from bot import repo
from bot.config import Settings
from bot.db import (Base, LearningMaterial, Mentor, MentoringNotification, MentoringSession,
                    SessionBooking, Subject, Topic, TopicChecklist, User, init_db)
from bot.mentoring import jobs, service as svc
from tests.offline_mentor_checks import EngineAdapter, SessionAdapter, run

NOW = datetime(2026, 10, 2, 9, 0)
SETTINGS = Settings('test', 'unused', coordinator_ids=frozenset({10}), lead_ids=frozenset({20}))
ADMIN = repo.Viewer(10, 'coordinator', 'ru', 'Coordinator')
LEAD = repo.Viewer(20, 'lead', 'ru', 'Lead')
MENTOR = repo.Viewer(30, 'mentor', 'ru', 'Mentor')
OTHER = repo.Viewer(40, 'mentor', 'ru', 'Other mentor')
ALICE = repo.Viewer(100, 'mentee', 'ru', 'Alice')
BOB = repo.Viewer(200, 'mentee', 'ru', 'Bob')


class Adapter(SessionAdapter):
    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def refresh(self, obj):
        self.session.refresh(obj)


class SessionChecks(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        @event.listens_for(self.engine, 'connect')
        def fk(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        run(init_db(EngineAdapter(self.engine)))
        self.s = Session(self.engine, expire_on_commit=False)
        self.a = Adapter(self.s)
        self.s.add_all(User(telegram_id=v.id, display_name=v.name) for v in (ADMIN, LEAD, MENTOR, OTHER, ALICE, BOB))
        self.s.commit()
        self.s.add_all([Mentor(telegram_id=30), Mentor(telegram_id=40)])
        self.s.commit()
        self.topic = run(svc.ensure_topic(self.a, 'Calculus', 'Integrals'))
        run(svc.put_checklist(self.a, MENTOR, self.topic.id, ['Подстановка', 'По частям']))
        self.s.commit()
        self.counter = 0

    def tearDown(self):
        self.s.close()
        self.engine.dispose()

    def create(self, **kwargs):
        self.counter += 1
        values = dict(topic_id=self.topic.id, mentor_id=30, starts_at=NOW + timedelta(days=2),
                      duration=60, location='Room 101', creation_key=f'draft{self.counter}', now=NOW)
        values.update(kwargs)
        lesson = run(svc.create_session(self.a, ADMIN, SETTINGS, **values))
        self.s.commit()
        return lesson

    def book(self, lesson, viewer=ALICE, question='Как применять подстановку?', now=NOW):
        result = run(svc.register(self.a, viewer, lesson.id, question, now))
        self.s.commit()
        return result[1]

    def answer(self, booking, i=0, score=2, viewer=ALICE, now=NOW):
        result = run(svc.save_answer(self.a, viewer, booking.id, i, score, now))
        self.s.commit()
        return result[1]

    def notes(self, kind=None):
        query = select(MentoringNotification)
        if kind:
            query = query.where(MentoringNotification.kind == kind)
        return list(self.s.scalars(query))

    def test_01_creation_has_mentor_topic_snapshot_and_persistent_assignment(self):
        lesson = self.create()
        self.assertEqual(lesson.checklist_items, ['Подстановка', 'По частям'])
        self.assertEqual(lesson.ends_at - lesson.starts_at, timedelta(hours=1))
        self.assertEqual(len(self.notes('assigned')), 1)
        self.assertEqual(self.notes('assigned')[0].recipient_id, 30)

    def test_02_only_admin_creates_sessions(self):
        with self.assertRaises(svc.RuleError):
            run(svc.create_session(self.a, MENTOR, SETTINGS, topic_id=self.topic.id, mentor_id=30,
                starts_at=NOW + timedelta(days=2), duration=60, location='x', creation_key='no', now=NOW))
        self.assertEqual(self.s.scalar(select(func.count()).select_from(MentoringSession)), 0)

    def test_03_names_normalize_without_duplicate_topics(self):
        again = run(svc.ensure_topic(self.a, '  CALCULUS ', ' integrals '))
        self.assertEqual(again.id, self.topic.id)
        self.assertEqual(self.s.scalar(select(func.count()).select_from(Topic)), 1)
        other = run(svc.ensure_topic(self.a, 'Physics', 'Integrals'))
        self.assertNotEqual(other.id, self.topic.id)

    def test_04_invalid_creation_and_missing_mentor(self):
        for kwargs in ({'starts_at': NOW}, {'duration': 0}, {'duration': 361}, {'mentor_id': 100}, {'location': ''}):
            with self.assertRaises(svc.RuleError):
                self.create(**kwargs)
            self.s.rollback()

    def test_05_idempotent_creation_token(self):
        first = self.create(creation_key='same')
        second = self.create(creation_key='same')
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.s.scalar(select(func.count()).select_from(MentoringSession)), 1)
        self.assertEqual(len(self.notes('assigned')), 1)

    def test_06_registration_is_unique_and_question_can_change(self):
        lesson = self.create()
        booking = self.book(lesson)
        again = self.book(lesson, question='А по частям?')
        self.assertEqual(booking.id, again.id)
        self.assertEqual(again.question, 'А по частям?')
        self.assertEqual(self.s.scalar(select(func.count()).select_from(SessionBooking)), 1)
        self.assertEqual(len(self.notes('checklist_24')), 1)
        self.assertEqual(len(self.notes('checklist_2')), 1)

    def test_07_no_booking_after_start_cancel_or_by_assigned_mentor(self):
        lesson = self.create()
        with self.assertRaises(svc.RuleError):
            self.book(lesson, MENTOR)
        with self.assertRaises(svc.RuleError):
            self.book(lesson, now=lesson.starts_at)
        run(svc.cancel_session(self.a, ADMIN, lesson.id, 'Нет записей', NOW))
        self.s.commit()
        with self.assertRaises(svc.RuleError):
            self.book(lesson)

    def test_08_absent_materials_and_template_never_block_booking(self):
        other = run(svc.ensure_topic(self.a, 'Python', 'Loops'))
        lesson = self.create(topic_id=other.id)
        booking = self.book(lesson)
        self.assertEqual(lesson.checklist_items, [])
        self.assertEqual(booking.status, 'active')
        self.assertFalse(svc.answered(lesson, booking))

    def test_09_late_template_is_attached_without_reregistering(self):
        other = run(svc.ensure_topic(self.a, 'Python', 'Loops'))
        lesson = self.create(topic_id=other.id)
        booking = self.book(lesson)
        run(svc.put_checklist(self.a, MENTOR, other.id, ['for', 'while']))
        self.s.commit()
        run(svc.attach_checklist(self.a, lesson))
        self.s.commit()
        self.assertEqual(lesson.checklist_items, ['for', 'while'])
        self.answer(booking)

    def test_10_each_answer_is_persisted_and_resumable(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking, 0, 1)
        self.s.expire_all()
        reloaded = self.s.get(SessionBooking, booking.id)
        self.assertEqual(reloaded.answers, {'0': 1})
        self.assertFalse(svc.answered(lesson, reloaded))
        self.answer(booking, 1, 0)
        self.assertTrue(svc.answered(lesson, booking))
        self.answer(booking, 0, 2)
        self.assertEqual(booking.answers['0'], 2)

    def test_11_other_student_cannot_answer_another_booking(self):
        booking = self.book(self.create())
        with self.assertRaises(svc.RuleError):
            self.answer(booking, viewer=BOB)
        self.assertEqual(booking.answers, {})

    def test_12_checklist_score_index_and_state_validation(self):
        lesson = self.create()
        booking = self.book(lesson)
        for index, score in ((999, 2), (-1, 2), (0, 4)):
            with self.assertRaises(svc.RuleError):
                self.answer(booking, index, score)
        with self.assertRaises(svc.RuleError):
            self.answer(booking, now=lesson.starts_at)
        run(svc.withdraw(self.a, ALICE, lesson.id, NOW))
        self.s.commit()
        with self.assertRaises(svc.RuleError):
            self.answer(booking)

    def test_13_changing_template_never_relabels_historical_answers(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking, 0, 1)
        run(svc.put_checklist(self.a, OTHER, self.topic.id, ['Новая тема']))
        self.s.commit()
        self.assertEqual(lesson.checklist_items, ['Подстановка', 'По частям'])
        with self.assertRaises(svc.RuleError):
            run(svc.refresh_checklist(self.a, MENTOR, lesson.id, NOW))
        next_lesson = self.create()
        self.assertEqual(next_lesson.checklist_items, ['Новая тема'])

    def test_14_summary_counts_missing_separately_from_not_understanding(self):
        lesson = self.create()
        first = self.book(lesson)
        second = self.book(lesson, BOB)
        self.answer(first, 0, 0)
        self.answer(second, 0, 1, viewer=BOB)
        counts = svc.checklist_summary(lesson, [first, second])
        self.assertEqual(counts[0][1], {0: 1, 1: 1, 2: 0, 'missing': 0})
        self.assertEqual(counts[1][1]['missing'], 2)

    def test_15_rejoin_preserves_answers_and_no_duplicate_bookings(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking)
        run(svc.withdraw(self.a, ALICE, lesson.id, NOW))
        self.s.commit()
        self.assertEqual(run(svc.active_bookings(self.a, lesson.id)), [])
        new = self.book(lesson)
        self.assertEqual(new.id, booking.id)
        self.assertEqual(new.answers, {'0': 2})
        self.assertEqual(len(run(svc.active_bookings(self.a, lesson.id))), 1)

    def test_16_only_assigned_mentor_or_admin_can_manage(self):
        lesson = self.create()
        for viewer in (MENTOR, ADMIN, LEAD):
            self.assertTrue(svc.can_manage(viewer, lesson))
        for viewer in (OTHER, ALICE, repo.Viewer(30, 'mentee', 'ru', 'Ex mentor')):
            self.assertFalse(svc.can_manage(viewer, lesson))

    def test_17_attendance_requires_started_session_and_current_assignee(self):
        lesson = self.create()
        booking = self.book(lesson)
        with self.assertRaises(svc.RuleError):
            run(svc.set_attendance(self.a, MENTOR, booking.id, 'present', NOW))
        with self.assertRaises(svc.RuleError):
            run(svc.set_attendance(self.a, OTHER, booking.id, 'present', lesson.starts_at))
        run(svc.set_attendance(self.a, MENTOR, booking.id, 'present', lesson.starts_at))
        self.s.commit()
        self.assertEqual(booking.attendance, 'present')

    def test_18_complete_requires_ended_session_and_all_attendance(self):
        lesson = self.create()
        booking = self.book(lesson)
        with self.assertRaises(svc.RuleError):
            run(svc.complete_session(self.a, MENTOR, lesson.id, NOW))
        with self.assertRaises(svc.RuleError):
            run(svc.complete_session(self.a, MENTOR, lesson.id, lesson.ends_at))
        run(svc.set_attendance(self.a, MENTOR, booking.id, 'present', lesson.ends_at))
        run(svc.complete_session(self.a, MENTOR, lesson.id, lesson.ends_at))
        self.s.commit()
        self.assertEqual(lesson.status, 'completed')
        self.assertEqual(len(self.notes('materials')), 1)
        run(svc.complete_session(self.a, MENTOR, lesson.id, lesson.ends_at))
        self.s.commit()
        self.assertEqual(len(self.notes('materials')), 1)

    def test_19_empty_session_cannot_be_counted_as_completed(self):
        lesson = self.create()
        with self.assertRaises(svc.RuleError):
            run(svc.complete_session(self.a, ADMIN, lesson.id, lesson.ends_at))
        self.assertEqual(lesson.status, 'scheduled')

    def test_20_manual_cancel_admin_only_preserves_history_and_notifies(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking)
        with self.assertRaises(svc.RuleError):
            run(svc.cancel_session(self.a, MENTOR, lesson.id, 'Нет людей', NOW))
        run(svc.cancel_session(self.a, ADMIN, lesson.id, 'Перенос программы', NOW))
        self.s.commit()
        self.assertEqual(lesson.status, 'cancelled')
        self.assertEqual(booking.answers, {'0': 2})
        self.assertEqual({n.recipient_id for n in self.notes('cancelled')}, {30, 100})
        self.assertEqual(len(run(svc.active_bookings(self.a, lesson.id))), 1)
        self.assertIsNone(run(jobs.prepare(self.a, self.notes('checklist_24')[0], SETTINGS, NOW)))

    def test_21_empty_warning_is_idempotent_and_never_auto_cancels(self):
        lesson = self.create(starts_at=NOW + timedelta(hours=1))
        run(jobs.generate_due(self.a, SETTINGS, NOW))
        self.s.commit()
        run(jobs.generate_due(self.a, SETTINGS, NOW))
        self.s.commit()
        self.assertEqual(lesson.status, 'scheduled')
        self.assertEqual({n.recipient_id for n in self.notes('empty')}, {10, 20})
        self.assertEqual(len(self.notes('empty')), 2)
        self.assertIsNotNone(run(jobs.prepare(self.a, self.notes('empty')[0], SETTINGS, NOW)))
        self.book(lesson)
        self.assertIsNone(run(jobs.prepare(self.a, self.notes('empty')[0], SETTINGS, NOW)))

    def test_22_filled_and_withdrawn_checklists_suppress_reminders(self):
        lesson = self.create()
        booking = self.book(lesson)
        notification = self.notes('checklist_24')[0]
        self.assertIsNotNone(run(jobs.prepare(self.a, notification, SETTINGS, NOW)))
        self.answer(booking, 0, 2)
        self.answer(booking, 1, 1)
        self.assertIsNone(run(jobs.prepare(self.a, notification, SETTINGS, NOW)))
        booking.answers = {}
        run(svc.withdraw(self.a, ALICE, lesson.id, NOW))
        self.s.commit()
        self.assertIsNone(run(jobs.prepare(self.a, notification, SETTINGS, NOW)))

    def test_23_last_minute_booking_does_not_queue_two_immediate_reminders(self):
        lesson = self.create(starts_at=NOW + timedelta(minutes=40))
        self.book(lesson)
        self.assertEqual(len(self.notes('checklist_24')), 0)
        self.assertEqual(len(self.notes('checklist_2')), 1)
        lesson2 = self.create(starts_at=NOW + timedelta(minutes=8))
        self.book(lesson2)
        self.assertEqual(len([n for n in self.notes() if n.session_id == lesson2.id and n.kind.startswith('checklist')]), 0)

    def test_24_reschedule_preserves_bookings_answers_and_changes_reminder_due_dates(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking)
        new_time = lesson.starts_at + timedelta(days=1)
        run(svc.edit_session(self.a, ADMIN, lesson.id, 'start', new_time, NOW))
        self.s.commit()
        self.assertEqual(booking.answers, {'0': 2})
        self.assertEqual(lesson.starts_at, new_time)
        self.assertEqual(lesson.ends_at, new_time + timedelta(hours=1))
        pending = [n for n in self.notes('checklist_24') if n.status == 'pending']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].due_at, new_time - timedelta(hours=24))
        self.assertEqual(len(self.notes('changed')), 1)

    def test_25_reassign_removes_old_mentor_access_and_keeps_history(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking)
        run(svc.edit_session(self.a, ADMIN, lesson.id, 'mentor', OTHER.id, NOW))
        self.s.commit()
        self.assertFalse(svc.can_manage(MENTOR, lesson))
        self.assertTrue(svc.can_manage(OTHER, lesson))
        self.assertEqual(booking.answers, {'0': 2})
        self.assertIsNone(run(jobs.prepare(self.a, self.notes('assigned')[0], SETTINGS, NOW)))
        self.assertEqual(len(self.notes('unassigned')), 1)

    def test_26_removed_mentor_keeps_session_history_and_cannot_receive_assignment(self):
        lesson = self.create()
        self.s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR.id))
        self.s.commit()
        self.assertIsNotNone(self.s.get(MentoringSession, lesson.id))
        self.assertIsNone(run(jobs.prepare(self.a, self.notes('assigned')[0], SETTINGS, NOW)))
        with self.assertRaises(svc.RuleError):
            self.book(lesson)

    def add_file(self, **kwargs):
        values = dict(topic_id=self.topic.id, kind='recap', visibility='students',
                      file_id='telegram-file', file_unique_id='unique-file', file_name='guide.pdf',
                      file_size=120, mime='application/pdf')
        values.update(kwargs)
        material = run(svc.add_material(self.a, MENTOR, **values))
        self.s.commit()
        return material

    def test_27_public_bank_document_no_duplicate_and_can_be_reused(self):
        material = self.add_file()
        again = self.add_file()
        self.assertEqual(material.id, again.id)
        self.assertEqual(run(svc.material_for(self.a, ALICE, material.id)).file_id, 'telegram-file')
        self.assertEqual(len(run(svc.material_list(self.a, ALICE, self.topic.id))), 1)

    def test_28_private_material_never_exposed_to_students_or_automatic_recap(self):
        public = self.add_file()
        private = self.add_file(visibility='staff', file_unique_id='answers')
        with self.assertRaises(svc.RuleError):
            run(svc.material_for(self.a, ALICE, private.id))
        self.assertEqual(len(run(svc.material_list(self.a, MENTOR, self.topic.id))), 2)
        self.assertEqual([x.id for x in run(svc.material_list(self.a, ALICE, self.topic.id))], [public.id])
        self.assertEqual([x.id for x in run(svc.material_list(self.a, ADMIN, self.topic.id, recap_only=True))], [public.id])

    def test_29_students_cannot_upload_and_other_mentors_cannot_archive(self):
        material = self.add_file()
        with self.assertRaises(svc.RuleError):
            run(svc.add_material(self.a, ALICE, topic_id=self.topic.id, kind='recap', visibility='students',
                file_id='x', file_unique_id='x', file_name='x.pdf'))
        with self.assertRaises(svc.RuleError):
            run(svc.archive_material(self.a, OTHER, material.id))
        run(svc.archive_material(self.a, MENTOR, material.id))
        self.s.commit()
        with self.assertRaises(svc.RuleError):
            run(svc.material_for(self.a, ALICE, material.id))
        self.assertIsNotNone(self.s.get(LearningMaterial, material.id))

    def test_30_session_material_autofills_correct_topic_and_owner_guard(self):
        lesson = self.create()
        material = self.add_file(topic_id=999, session_id=lesson.id, kind='material')
        self.assertEqual(material.topic_id, lesson.topic_id)
        self.assertEqual(material.session_id, lesson.id)
        with self.assertRaises(svc.RuleError):
            run(svc.add_material(self.a, OTHER, topic_id=lesson.topic_id, session_id=lesson.id,
                kind='material', visibility='students', file_id='x', file_unique_id='x', file_name='x.docx'))

    def test_31_file_type_size_and_mime_validation(self):
        self.assertEqual(svc.validate_file('DIR/Guide.PDF', 100, 'application/pdf'), 'Guide.PDF')
        svc.validate_file('test.docx', 100, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        for name, size, mime in [('x.png',100,'image/png'), ('x.doc',100,None), ('x.pdf', svc.MAX_FILE_BYTES+1,None),
                                 ('x.pdf',100,'image/png'), ('x.docm',100,None), ('x.pdf',0,None), (None,100,None)]:
            with self.assertRaises(svc.RuleError):
                svc.validate_file(name,size,mime)

    def test_32_items_limits_duplicates_and_unicode_budget(self):
        self.assertEqual(svc.parse_items('1. for\n2. while'), ['for', 'while'])
        for text in ('', 'for\nFor', 'x'*111, '\n'.join(str(i) for i in range(21)), '😀'*56):
            with self.assertRaises(svc.RuleError):
                svc.parse_items(text)

    def test_33_local_time_roundtrip_almaty(self):
        value = svc.parse_start('05.10.2026 18:30', 'Asia/Almaty', NOW)
        self.assertEqual(value, datetime(2026,10,5,13,30))
        self.assertEqual(svc.local_time(value, SETTINGS), '05.10.2026 18:30')
        for text in ('31.02.2026 10:00', 'tomorrow', '02.10.2026 13:00'):
            with self.assertRaises(svc.RuleError):
                svc.parse_start(text, 'Asia/Almaty', NOW)

    def test_34_stats_separate_zero_missing_absent_and_cancelled(self):
        one = self.create()
        first = self.book(one)
        self.answer(first,0,2)
        self.answer(first,1,1)
        run(svc.set_attendance(self.a, MENTOR, first.id, 'present', one.ends_at))
        run(svc.complete_session(self.a, MENTOR, one.id, one.ends_at))
        self.s.commit()
        two = self.create()
        self.book(two, BOB)
        run(svc.cancel_session(self.a, ADMIN, two.id, 'Отмена', NOW))
        self.s.commit()
        result = run(svc.stats(self.a))['rows']
        r = next(r for r in result if r['lesson'].id == one.id)
        self.assertEqual((r['registered'],r['present'],r['absent'],r['unknown'],r['checklists']), (1,1,0,0,1))
        self.assertFalse(r['has_materials'])
        self.add_file(session_id=one.id, kind='material')
        result = run(svc.stats(self.a))['rows']
        self.assertTrue(next(r for r in result if r['lesson'].id == one.id)['has_materials'])
        self.assertEqual(next(r for r in result if r['lesson'].id == two.id)['lesson'].status, 'cancelled')

    def test_35_restart_creates_new_tables_without_losing_existing_data(self):
        lesson = self.create()
        booking = self.book(lesson)
        self.answer(booking)
        run(init_db(EngineAdapter(self.engine)))
        self.s.expire_all()
        self.assertEqual(self.s.get(SessionBooking, booking.id).answers, {'0': 2})
        self.assertEqual(self.s.get(MentoringSession, lesson.id).mentor_id, 30)
        self.assertEqual(len(self.notes('checklist_24')), 1)

    def test_36_database_constraints_reject_duplicates_and_invalid_attendance(self):
        lesson = self.create()
        self.book(lesson)
        self.s.add(SessionBooking(session_id=lesson.id, student_id=ALICE.id, question='duplicate'))
        with self.assertRaises(IntegrityError):
            self.s.commit()
        self.s.rollback()
        booking = run(svc.own_booking(self.a, lesson.id, ALICE.id))
        booking.attendance = 'yes'
        with self.assertRaises(IntegrityError):
            self.s.commit()
        self.s.rollback()

    def test_37_postgres_ddl_compiles_without_changing_original_tables(self):
        for cls in (Subject, Topic, TopicChecklist, MentoringSession, SessionBooking, LearningMaterial, MentoringNotification):
            ddl = str(CreateTable(cls.__table__).compile(dialect=postgresql.dialect()))
            self.assertIn('CREATE TABLE', ddl)
        self.assertNotIn('session_id', User.__table__.columns)
        self.assertIn('mentors', Base.metadata.tables)

    def test_38_checklist_refresh_without_answers_and_access_control(self):
        lesson = self.create()
        run(svc.put_checklist(self.a, MENTOR, self.topic.id, ['Новый пункт']))
        self.s.commit()
        with self.assertRaises(svc.RuleError):
            run(svc.refresh_checklist(self.a, OTHER, lesson.id, NOW))
        run(svc.refresh_checklist(self.a, MENTOR, lesson.id, NOW))
        self.s.commit()
        self.assertEqual(lesson.checklist_items, ['Новый пункт'])

    def test_39_reminders_do_not_send_after_start_and_do_not_repeat_after_enqueue(self):
        lesson = self.create()
        self.book(lesson)
        n = self.notes('checklist_2')[0]
        self.assertIsNone(run(jobs.prepare(self.a, n, SETTINGS, lesson.starts_at)))
        for _ in range(3):
            run(svc.enqueue(self.a, lesson, MENTOR.id, 'assigned', NOW))
        self.s.commit()
        self.assertEqual(len(self.notes('assigned')), 1)

    def test_40_material_prompt_suppressed_when_file_already_uploaded(self):
        lesson = self.create()
        booking = self.book(lesson)
        run(svc.set_attendance(self.a, MENTOR, booking.id, 'present', lesson.ends_at))
        run(svc.complete_session(self.a, MENTOR, lesson.id, lesson.ends_at))
        self.s.commit()
        n = self.notes('materials')[0]
        self.assertIsNotNone(run(jobs.prepare(self.a, n, SETTINGS, lesson.ends_at)))
        self.add_file(session_id=lesson.id, kind='material')
        self.assertIsNone(run(jobs.prepare(self.a, n, SETTINGS, lesson.ends_at)))

    def test_41_rejoin_reactivates_a_skipped_reminder(self):
        lesson = self.create()
        booking = self.book(lesson)
        n = self.notes('checklist_2')[0]
        run(svc.withdraw(self.a, ALICE, lesson.id, NOW))
        n.status = 'skipped'
        self.s.commit()
        self.book(lesson)
        self.s.refresh(n)
        self.assertEqual(n.status, 'pending')

    def test_42_session_lists_enforce_scope(self):
        one = self.create()
        two = self.create(mentor_id=OTHER.id)
        self.book(one)
        self.assertEqual([x.id for x in run(svc.session_list(self.a, ALICE, 'bookings'))], [one.id])
        self.assertEqual([x.id for x in run(svc.session_list(self.a, MENTOR, 'teaching'))], [one.id])
        self.assertEqual(len(run(svc.session_list(self.a, ADMIN, 'all'))), 2)
        with self.assertRaises(svc.RuleError):
            run(svc.session_list(self.a, ALICE, 'all'))
        with self.assertRaises(svc.RuleError):
            run(svc.session_list(self.a, ALICE, 'teaching'))

    def test_43_syntax_and_new_callback_payload_lengths(self):
        root = Path(__file__).parents[1]
        for path in (root / 'bot').rglob('*.py'):
            ast.parse(path.read_text(), filename=str(path))
        examples = [f'ms:answer:{2**63-1}:19:2', 'ms:cancelconfirm:' + 'a'*32,
                    f'ms:topics:schedule:{2**63-1}:999999999', 'ms:editconfirm:' + 'a'*32]
        self.assertTrue(all(len(x.encode()) <= 64 for x in examples))


    def test_44_removed_assignee_generates_admin_warning_not_automatic_cancellation(self):
        lesson = self.create(starts_at=NOW + timedelta(hours=1))
        self.s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR.id))
        self.s.commit()
        run(jobs.generate_due(self.a, SETTINGS, NOW))
        self.s.commit()
        warnings = self.notes('mentor_missing')
        self.assertEqual({n.recipient_id for n in warnings}, {10, 20})
        self.assertIsNotNone(run(jobs.prepare(self.a, warnings[0], SETTINGS, NOW)))
        self.assertEqual(lesson.status, 'scheduled')
        self.s.add(Mentor(telegram_id=MENTOR.id))
        self.s.commit()
        self.assertIsNone(run(jobs.prepare(self.a, warnings[0], SETTINGS, NOW)))


if __name__ == '__main__':
    unittest.main()
