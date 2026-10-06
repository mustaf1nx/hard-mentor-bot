"""Integration tests: actual aiogram dispatcher, fake Telegram, SQLite/PostgreSQL.

Run with the project's requirements-dev.txt. Never point TEST_DATABASE_URL at
production: tests/conftest.py clears the database between scenarios.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendDocument
from aiogram.types import Document
from sqlalchemy import delete, select

from bot import repo
from bot.db import Mentor, User
from bot.mentor_refs import MAX_IMPORT_BYTES, MAX_MENTOR_REFS
from .conftest import ALICE, BOB, COORD, LEAD, MENTOR, SETTINGS


async def mentor_ids(h) -> set[int]:
    async with h.sm() as s:
        return set((await s.scalars(select(Mentor.telegram_id))).all())


def reports(h):
    return [c.document.data.decode("utf-8") for c in h.sent_to(COORD)
            if isinstance(c, SendDocument) and hasattr(c.document, "data")]


async def test_bulk_add_mixed_refs_and_alias_duplicates(h):
    await h.say(ALICE, "/start")
    await h.say(BOB, "/start")
    h.clear()
    await h.say(COORD, f"/addmentor\n@USER{MENTOR}, {ALICE}; https://t.me/user{BOB}\n{MENTOR} @user{MENTOR}")
    assert await mentor_ids(h) == {MENTOR, ALICE, BOB}
    summary = " ".join(h.texts_to(COORD))
    assert "Добавлено менторов: 3" in summary and "Повторы пропущены: 2" in summary
    assert reports(h)
    assert not h.sent_to(MENTOR) and not h.sent_to(ALICE) and not h.sent_to(BOB)
    async with h.sm() as s:
        assert (await s.get(User, MENTOR)).role == "mentee"  # not the permission source
        assert await repo.effective_role(s, SETTINGS, MENTOR) == "mentor"


async def test_repeat_add_does_not_notify_or_duplicate(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    h.clear()
    await h.say(COORD, f"/addmentor @user{MENTOR}")
    assert await mentor_ids(h) == {MENTOR}
    assert "Уже были менторами: 1" in h.texts_to(COORD)[-1]
    assert not h.sent_to(MENTOR)


async def test_unknown_invalid_and_ambiguous_are_reported(h):
    async with h.sm() as s:
        s.add_all([User(telegram_id=8001, username="reused"), User(telegram_id=8002, username="Reused")])
        await s.commit()
    await h.say(COORD, f"/addmentor {MENTOR} @missing @reused -42 <bad>")
    assert await mentor_ids(h) == {MENTOR}
    report = reports(h)[-1]
    assert "Не найдены: 1" in report and "Неоднозначные usernames: 1" in report
    assert "Неверный формат: 2" in report and "@missing" in report


async def test_direct_sql_membership_controls_runtime_access(h):
    async with h.sm() as s:
        s.add(Mentor(telegram_id=MENTOR))
        await s.commit()
    await h.say(MENTOR, "/queue")
    assert "только команде" not in h.texts_to(MENTOR)[-1]
    async with h.sm() as s:
        await s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR))
        (await s.get(User, MENTOR)).role = "mentor"  # stale legacy data cannot regrant access
        await s.commit()
    await h.say(MENTOR, "/queue")
    assert "только команде" in h.texts_to(MENTOR)[-1]


async def test_mentors_cannot_bulk_edit_or_page_admin_list(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    h.clear()
    await h.say(MENTOR, f"/addmentor {COORD} {LEAD}")
    await h.say(MENTOR, f"/delmentor {MENTOR}")
    await h.tap(MENTOR, "ml:1")
    assert await mentor_ids(h) == {MENTOR}
    assert h.alerts()


async def test_lead_and_coordinator_keep_rights_when_membership_removed(h):
    await h.say(COORD, f"/addmentor {COORD} {LEAD} {MENTOR}")
    await h.say(COORD, f"/delmentor {COORD} {LEAD} {MENTOR}")
    assert not await mentor_ids(h)
    async with h.sm() as s:
        assert await repo.effective_role(s, SETTINGS, COORD) == "coordinator"
        assert await repo.effective_role(s, SETTINGS, LEAD) == "lead"
        assert await repo.effective_role(s, SETTINGS, MENTOR) == "mentee"


async def test_bulk_delete_unassigns_only_active_work(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    await h.submit(ALICE, "question", "Нужна помощь с расписанием дополнительных занятий.")
    async with h.sm() as s:
        ticket = (await s.scalars(select(repo.Ticket))).one()
        ticket.assigned_to = MENTOR
        await s.commit()
        ticket_id = ticket.id
    await h.say(COORD, f"/delmentor {MENTOR}")
    async with h.sm() as s:
        assert (await repo.get_ticket(s, ticket_id)).assigned_to is None
        assert await s.get(User, MENTOR) is not None
    await h.tap(MENTOR, f"tk:{ticket_id}")
    assert h.alerts()


async def test_sql_removed_mentor_does_not_receive_followup(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    await h.submit(ALICE, "question", "Нужна помощь с расписанием дополнительных занятий.")
    async with h.sm() as s:
        ticket = (await s.scalars(select(repo.Ticket))).one()
        ticket.assigned_to = MENTOR
        await s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR))
        await s.commit()
        tid = ticket.id
    h.clear()
    await h.tap(ALICE, f"cm:{tid}")
    await h.say(ALICE, "Уточнение для координатора: это касается пятницы.")
    assert not h.sent_to(MENTOR)
    assert any("пятницы" in x for x in h.texts_to(COORD))


async def test_removed_mentor_cannot_be_picked_by_stale_or_forged_button(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:praise")
    await h.say(COORD, f"/delmentor {MENTOR}")
    h.clear()
    await h.tap(ALICE, f"rel:m:{MENTOR}")
    assert any("больше не в списке" in x for x in h.alerts())
    assert not h.sent_to(MENTOR)


async def test_removal_during_form_routes_praise_to_admins(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:praise")
    await h.tap(ALICE, f"rel:m:{MENTOR}")
    await h.say(COORD, f"/delmentor {MENTOR}")
    h.clear()
    await h.say(ALICE, "Спасибо за помощь с подготовкой к экзамену.")
    await h.tap(ALICE, "att:skip")
    await h.tap(ALICE, "anon:0")
    async with h.sm() as s:
        ticket = (await s.scalars(select(repo.Ticket))).one()
        assert ticket.status == "new" and ticket.mentor_id is None and ticket.assigned_to is None
    assert not h.sent_to(MENTOR)
    assert h.sent_to(COORD)


async def test_all_mentors_reachable_on_all_three_pickers(h):
    async with h.sm() as s:
        s.add_all(User(telegram_id=5000+i, display_name=f"Mentor {i:03}", username=f"mentor_{i}") for i in range(65))
        await s.commit()
    await h.say(COORD, "/addmentor " + " ".join(str(5000+i) for i in range(65)))
    await h.say(COORD, "/mentors")
    assert "ml:1" in h.buttons(h.last_to(COORD))
    await h.tap(COORD, "ml:999")
    assert "5064" in h.texts_to(COORD)[-1]
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:praise")
    assert "relpage:1" in h.buttons(h.last_to(ALICE))
    await h.tap(ALICE, "relpage:3")
    assert "rel:m:5064" in h.buttons(h.last_to(ALICE))
    await h.say(ALICE, "/cancel")
    await h.submit(ALICE, "question", "Вопрос по дополнительным занятиям на этой неделе.")
    async with h.sm() as s:
        ticket = (await s.scalars(select(repo.Ticket))).one()
    await h.tap(COORD, f"as:{ticket.id}:3")
    assert f"asg:{ticket.id}:5064" in h.buttons(h.last_to(COORD))


async def test_txt_caption_import(h, monkeypatch):
    async def download(self, file, destination=None, **kwargs):
        destination.write(f"\ufeff@user{MENTOR}\n{LEAD}\n".encode("utf-8"))
    monkeypatch.setattr(type(h.bot), "download", download)
    doc = Document(file_id="list", file_unique_id="list", file_name="mentors.TXT", file_size=100)
    await h.say(COORD, document=doc, caption="/addmentor")
    assert await mentor_ids(h) == {MENTOR, LEAD}
    assert reports(h)


async def test_txt_reply_import(h, monkeypatch):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message
    async def download(self, file, destination=None, **kwargs):
        destination.write(f"@user{MENTOR}".encode())
    monkeypatch.setattr(type(h.bot), "download", download)
    doc = Document(file_id="list", file_unique_id="list", file_name="mentors.txt")
    parent = Message(message_id=991, date=datetime.now(timezone.utc), chat=Chat(id=COORD, type="private"), document=doc)
    await h.say(COORD, "/addmentor", reply_to_message=parent)
    assert await mentor_ids(h) == {MENTOR}


@pytest.mark.parametrize("file_name,payload,size", [
    ("mentors.xlsx", b"", 0),
    ("mentors.txt", b"\xff\xfe", 2),
    ("mentors.txt", b"", MAX_IMPORT_BYTES + 1),
    ("mentors.txt", b"x" * (MAX_IMPORT_BYTES + 1), None),
])
async def test_bad_files_do_not_change_memberships(h, monkeypatch, file_name, payload, size):
    async def download(self, file, destination=None, **kwargs):
        destination.write(payload)
    monkeypatch.setattr(type(h.bot), "download", download)
    doc = Document(file_id="list", file_unique_id="list", file_name=file_name, file_size=size)
    await h.say(COORD, document=doc, caption=f"/addmentor {MENTOR}")
    assert not await mentor_ids(h)


async def test_nonadmin_cannot_trigger_file_download(h, monkeypatch):
    download = AsyncMock()
    monkeypatch.setattr(type(h.bot), "download", download)
    doc = Document(file_id="list", file_unique_id="list", file_name="mentors.txt", file_size=100)
    await h.say(BOB, document=doc, caption="/addmentor")
    download.assert_not_called()
    assert not await mentor_ids(h)


async def test_too_many_entries_does_not_partially_import(h):
    await h.say(COORD, "/addmentor " + " ".join([str(MENTOR)] * (MAX_MENTOR_REFS + 1)))
    assert not await mentor_ids(h)
    assert "Изменения не внесены" in h.texts_to(COORD)[-1]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_bulk_html_report_and_list_with_long_names(h, lang):
    async with h.sm() as s:
        (await s.get(User, COORD)).lang = lang
        for i in range(30):
            s.add(User(telegram_id=9000+i, display_name='"<&' * 42, username=f"long_user_{i}"))
        await s.commit()
    await h.say(COORD, "/addmentor " + " ".join(str(9000+i) for i in range(30)) + " <script>&")
    await h.say(COORD, "/mentors")
    await h.tap(COORD, "ml:1")
    assert reports(h)  # FakeSession validates HTML and output lengths.


async def test_one_time_migration_with_actual_async_driver(h):
    from bot.db import MENTORS_MIGRATION_KEY, Meta, init_db
    async with h.sm() as s:
        (await s.get(User, MENTOR)).role = "mentor"
        await s.execute(delete(Meta).where(Meta.key == MENTORS_MIGRATION_KEY))
        await s.commit()
    engine = h.sm.kw["bind"]
    await init_db(engine)
    assert MENTOR in await mentor_ids(h)
    async with h.sm() as s:
        await s.execute(delete(Mentor).where(Mentor.telegram_id == MENTOR))
        await s.commit()
    await init_db(engine)
    assert MENTOR not in await mentor_ids(h)
