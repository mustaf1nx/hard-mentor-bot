"""Сквозные тесты: апдейты проходят через настоящий диспетчер и хэндлеры."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from bot import reports
from bot.db import Note, Ticket, utcnow
from tests.conftest import ALICE, BOB, COORD, LEAD, MENTOR, SETTINGS

pytestmark = pytest.mark.asyncio(loop_scope="session")

COMPLAINT = "Ментор не пришёл на сессию в четверг, никто не предупредил."


async def all_tickets(h) -> list[Ticket]:
    async with h.sm() as s:
        return list((await s.execute(select(Ticket).order_by(Ticket.id))).scalars())


# ---------- подача ----------

async def test_named_complaint_goes_to_coordinator_only(h):
    await h.submit(ALICE, "complaint", COMPLAINT, related="CS101, четверг")
    (ticket,) = await all_tickets(h)
    assert ticket.category == "complaint" and ticket.status == "new"
    assert ticket.submitter_id == ALICE and "User1001" in ticket.submitter_name
    assert ticket.related_course == "CS101, четверг"
    assert ticket.priority == "normal" and not ticket.lead_only
    assert any(ticket.code in x and "48" in x for x in h.texts_to(ALICE))
    assert any(ticket.code in x for x in h.texts_to(COORD))
    assert h.sent_to(LEAD) == []  # LEAD_GETS_ALL выключен


async def test_anonymous_ticket_stores_no_identity(h):
    """Главный тест спека (6.2, 12): анонимность настоящая, а не косметическая."""
    await h.submit(ALICE, "complaint", COMPLAINT, anonymous=True)
    (ticket,) = await all_tickets(h)
    assert ticket.is_anonymous
    assert ticket.submitter_id is None and ticket.submitter_name is None
    assert ticket.created_at.minute == 0 and ticket.created_at.second == 0  # время округлено до часа
    # Ни в одной колонке тикета нет ни ID, ни имени, ни юзернейма автора.
    dump = " ".join(str(v) for v in vars(ticket).values())
    assert str(ALICE) not in dump and "user1001" not in dump.lower()
    # И координатор в уведомлении автора не видит.
    coord_text = " ".join(h.texts_to(COORD))
    assert "анонимно" in coord_text and "User1001" not in coord_text and str(ALICE) not in coord_text


async def test_anonymous_access_code_roundtrip(h):
    await h.submit(ALICE, "question", "Как работает запись на пересдачу экзамена?", anonymous=True)
    (ticket,) = await all_tickets(h)
    confirmation = next(x for x in h.texts_to(ALICE) if "/status" in x and "<code>" in x)
    command = re.search(r"<code>(/status [^<]+)</code>", confirmation).group(1)

    # координатор отвечает — анониму уведомление не уходит
    await h.tap(COORD, f"rp:{ticket.id}")
    h.clear()
    await h.say(COORD, "Через личный кабинет, до 25 числа.")
    assert h.sent_to(ALICE) == []

    # чужой человек без кода ничего не видит; с кодом — видит ответ (даже с другого аккаунта)
    await h.say(BOB, f"/status {ticket.code} AAAA-BBBB")
    assert "не найдено" in h.texts_to(BOB)[-1]
    await h.say(BOB, command)
    assert any("до 25 числа" in x for x in h.texts_to(BOB))
    # /status без кода анонимные тикеты не показывает
    h.clear()
    await h.say(ALICE, "/status")
    assert "нет именных обращений" in h.texts_to(ALICE)[-1]


async def test_text_validation_and_cancel(h):
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:suggestion")
    await h.say(ALICE, "коротко")
    assert "Слишком коротко" in h.texts_to(ALICE)[-1]
    await h.say(ALICE, "/cancel")
    assert "Отменено" in h.texts_to(ALICE)[-1]
    assert await all_tickets(h) == []


async def test_menu_button_is_not_captured_as_text(h):
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:suggestion")
    await h.say(ALICE, "📋 Мои обращения")  # кнопка меню посреди анкеты
    assert await all_tickets(h) == []
    assert "именных обращений" in h.texts_to(ALICE)[-1]


async def test_attachment_kept_as_file_id(h):
    from aiogram.types import PhotoSize

    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:suggestion")
    await h.say(ALICE, "Давайте записывать сессии на видео")
    await h.say(ALICE, None, photo=[PhotoSize(file_id="FILE123", file_unique_id="u", width=10, height=10)])
    await h.tap(ALICE, "anon:0")
    (ticket,) = await all_tickets(h)
    assert ticket.attachment_file_id == "FILE123" and ticket.attachment_type == "photo"
    h.clear()
    await h.tap(COORD, f"at:{ticket.id}")
    assert h.last_to(COORD).photo == "FILE123"


async def test_rate_limit(h):
    for i in range(SETTINGS.daily_limit):
        await h.submit(ALICE, "suggestion", f"Предложение номер {i}, достаточно длинное")
    h.clear()
    await h.say(ALICE, "/feedback")
    assert "лимит" in h.texts_to(ALICE)[-1]
    assert len(await all_tickets(h)) == SETTINGS.daily_limit


async def test_english_user_gets_english(h):
    await h.submit(BOB, "question", "How does retake registration work?", lang="en")
    assert any("logged as ticket" in x for x in h.texts_to(BOB))


# ---------- маршрутизация ----------

async def test_serious_report_reaches_coordinator_and_lead_urgently(h):
    await h.submit(ALICE, "serious", "Ментор требует деньги за положительный отзыв.", anonymous=True)
    (ticket,) = await all_tickets(h)
    assert ticket.priority == "urgent" and not ticket.lead_only
    for uid in (COORD, LEAD):
        assert any("СРОЧНО" in x and ticket.code in x for x in h.texts_to(uid))


async def test_complaint_about_coordinator_goes_to_lead_only(h):
    await h.submit(ALICE, "complaint", "Координатор игнорирует мои сообщения уже две недели.", related="rel:coord")
    (ticket,) = await all_tickets(h)
    assert ticket.lead_only
    assert h.sent_to(COORD) == []
    assert any(ticket.code in x for x in h.texts_to(LEAD))
    assert any("только руководитель" in x for x in h.texts_to(ALICE))

    # координатор не видит тикет ни в очереди, ни по прямой ссылке, ни в отчёте
    h.clear()
    await h.say(COORD, "/queue")
    assert ticket.code not in h.texts_to(COORD)[-1]
    await h.say(COORD, f"/ticket {ticket.code}")
    assert "Нет доступа" in h.texts_to(COORD)[-1]
    await h.tap(COORD, f"tk:{ticket.id}")
    await h.tap(COORD, f"st:{ticket.id}:closed")
    assert (await all_tickets(h))[0].status == "new"
    report = await reports.build_weekly_report(h.sm, "ru", include_lead_only=False)
    assert "Всего: 0 / 0" in report
    report = await reports.build_weekly_report(h.sm, "ru", include_lead_only=True)
    assert "Всего: 1 / 0" in report
    # руководитель — видит
    await h.say(LEAD, "/queue")
    assert ticket.code in h.texts_to(LEAD)[-1]


async def test_serious_report_about_coordinator(h):
    await h.submit(ALICE, "serious", "Координатор давит на меня, чтобы я забрал жалобу.", related="rel:coord")
    (ticket,) = await all_tickets(h)
    assert ticket.lead_only and ticket.priority == "urgent"
    assert h.sent_to(COORD) == [] and h.sent_to(LEAD) != []


async def test_praise_is_delivered_to_mentor(h):
    await h.say(COORD, f"/addmentor {MENTOR}")
    h.clear()
    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:praise")
    assert f"rel:m:{MENTOR}" in h.buttons(h.last_to(ALICE))
    await h.tap(ALICE, f"rel:m:{MENTOR}")
    await h.say(ALICE, "Спасибо, наконец-то понял рекурсию!")
    await h.tap(ALICE, "att:skip")
    await h.tap(ALICE, "anon:0")
    (ticket,) = await all_tickets(h)
    assert ticket.status == "closed" and ticket.assigned_to == MENTOR
    assert any("благодарность" in x and "рекурсию" in x for x in h.texts_to(MENTOR))
    assert any(ticket.code in x for x in h.texts_to(COORD))  # сигнал для удержания менторов


# ---------- работа команды ----------

async def test_status_change_reply_and_rating(h):
    await h.submit(ALICE, "complaint", COMPLAINT)
    (ticket,) = await all_tickets(h)
    h.clear()

    await h.tap(COORD, f"st:{ticket.id}:in_progress")
    assert any("В работе" in x and ticket.code in x for x in h.texts_to(ALICE))

    await h.tap(COORD, f"nt:{ticket.id}")
    await h.say(COORD, "Проверил журнал посещений: ментора не было.")
    await h.tap(COORD, f"rp:{ticket.id}")
    await h.say(COORD, "Разобрались, сессию перенесли на субботу.")
    alice = " ".join(h.texts_to(ALICE))
    assert "перенесли на субботу" in alice
    assert "журнал посещений" not in alice  # внутренняя заметка заявителю не видна

    h.clear()
    await h.say(ALICE, "/status")
    await h.tap(ALICE, f"my:{ticket.id}")
    card = " ".join(h.texts_to(ALICE))
    assert "перенесли на субботу" in card and "журнал посещений" not in card

    await h.tap(COORD, f"st:{ticket.id}:resolved")
    rating_msg = h.last_to(ALICE)
    assert f"rate:{ticket.id}:5" in h.buttons(rating_msg)
    await h.tap(BOB, f"rate:{ticket.id}:1")  # чужой оценить не может
    await h.tap(ALICE, f"rate:{ticket.id}:5")
    (ticket,) = await all_tickets(h)
    assert ticket.satisfaction_rating == 5
    assert ticket.first_response_at is not None and ticket.resolved_at is not None


async def test_submitter_comment_reaches_staff(h):
    await h.submit(ALICE, "complaint", COMPLAINT)
    (ticket,) = await all_tickets(h)
    h.clear()
    await h.tap(BOB, f"cm:{ticket.id}")  # чужой дополнить не может
    await h.say(BOB, "взлом взлом взлом взлом")
    await h.tap(ALICE, f"cm:{ticket.id}")
    await h.say(ALICE, "Уточняю: это была сессия по CS101.")
    async with h.sm() as s:
        notes = list((await s.execute(select(Note))).scalars())
    assert [n.text for n in notes] == ["Уточняю: это была сессия по CS101."]
    assert notes[0].author_id is None and notes[0].author_kind == "submitter"
    assert any("CS101" in x for x in h.texts_to(COORD))


async def test_mentee_cannot_use_staff_tools(h):
    await h.submit(ALICE, "complaint", COMPLAINT)
    (ticket,) = await all_tickets(h)
    h.clear()
    await h.say(BOB, "/queue")
    assert "только команде" in h.texts_to(BOB)[-1]
    await h.tap(BOB, f"tk:{ticket.id}")
    await h.tap(BOB, f"st:{ticket.id}:closed")
    await h.tap(BOB, f"asg:{ticket.id}:{BOB}")
    await h.say(BOB, f"/addmentor {BOB}")
    (ticket,) = await all_tickets(h)
    assert ticket.status == "new" and ticket.assigned_to is None
    assert COMPLAINT not in " ".join(h.texts_to(BOB))


async def test_mentor_sees_only_assigned(h):
    await h.say(COORD, f"/addmentor @user{MENTOR}")
    await h.submit(ALICE, "question", "Как записаться на дополнительную сессию по матану?")
    await h.submit(ALICE, "complaint", COMPLAINT)
    question, complaint = await all_tickets(h)
    await h.tap(COORD, f"as:{question.id}")
    assert f"asg:{question.id}:{MENTOR}" in h.buttons(h.last_to(COORD))
    h.clear()
    await h.tap(COORD, f"asg:{question.id}:{MENTOR}")
    assert any(question.code in x for x in h.texts_to(MENTOR))
    h.clear()
    await h.say(MENTOR, "/queue")
    queue = h.texts_to(MENTOR)[-1]
    assert question.code in queue and complaint.code not in queue
    await h.tap(MENTOR, f"tk:{complaint.id}")
    assert COMPLAINT not in " ".join(h.texts_to(MENTOR))
    # координатор не может «назначить» тикет постороннему
    await h.tap(COORD, f"asg:{complaint.id}:{BOB}")
    assert (await all_tickets(h))[1].assigned_to is None


async def test_queue_filters(h):
    await h.submit(ALICE, "question", "Как записаться на дополнительную сессию по матану?")
    await h.submit(ALICE, "complaint", COMPLAINT)
    question, complaint = await all_tickets(h)
    h.clear()
    await h.tap(COORD, "q:complaint:open:0")
    text = h.texts_to(COORD)[-1]
    assert complaint.code in text and question.code not in text
    await h.tap(COORD, f"st:{complaint.id}:closed")
    await h.tap(COORD, "q:all:open:0")
    assert complaint.code not in h.texts_to(COORD)[-1]


async def test_stale_button_is_answered(h):
    await h.tap(ALICE, "cat:complaint")  # состояние потеряно (например, после редеплоя)
    assert any("устарела" in a for a in h.alerts())


# ---------- SLA и отчёт ----------

async def test_sla_alert_fires_once_and_escalates(h):
    await h.submit(ALICE, "serious", "Сообщение о серьёзной проблеме с безопасностью.")
    h.clear()
    assert await reports.check_sla_once(h.bot, h.sm, SETTINGS, now=utcnow() + timedelta(hours=3)) == 0
    assert await reports.check_sla_once(h.bot, h.sm, SETTINGS, now=utcnow() + timedelta(hours=5)) == 1
    assert any("SLA" in x for x in h.texts_to(COORD)) and any("SLA" in x for x in h.texts_to(LEAD))
    assert await reports.check_sla_once(h.bot, h.sm, SETTINGS, now=utcnow() + timedelta(hours=6)) == 0


async def test_weekly_report_numbers(h):
    await h.submit(ALICE, "complaint", COMPLAINT, anonymous=True)
    await h.submit(BOB, "question", "Как записаться на дополнительную сессию по матану?")
    _, question = await all_tickets(h)
    await h.tap(COORD, f"st:{question.id}:in_review")
    h.clear()
    await h.say(COORD, "/report")
    report = h.texts_to(COORD)[-1]
    assert "Всего: 2 / 0" in report
    assert "50% (1/2)" in report
    assert "100%" in report  # единственный ответ уложился в SLA


# ---------- устойчивость ----------

async def test_html_in_user_input_is_escaped(h):
    nasty = "<b>жирный</b> & <script>alert(1)</script> — а вот так?"
    await h.submit(ALICE, "complaint", nasty, related="<i>CS101</i> & Co")
    (ticket,) = await all_tickets(h)
    await h.tap(COORD, f"tk:{ticket.id}")
    await h.tap(COORD, f"rp:{ticket.id}")
    await h.say(COORD, "Ответ с <тегом> & амперсандом")
    await h.tap(ALICE, f"cm:{ticket.id}")
    await h.say(ALICE, "Дополнение с <тегом> & амперсандом")
    await h.tap(ALICE, f"my:{ticket.id}")
    await h.say(COORD, "/queue")
    assert "&lt;script&gt;" in " ".join(h.texts_to(COORD))  # FakeSession заодно проверил валидность HTML


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_every_screen_renders(h, lang):
    """Дымовой тест: все команды и экраны на обоих языках проходят проверку Telegram-HTML."""
    if lang == "en":
        for uid in (COORD, BOB):
            await h.say(uid, "/start", lang)
            await h.say(uid, "/lang", lang)
            if "English" not in h.texts_to(uid)[-1]:
                await h.say(uid, "/lang", lang)
    for cmd in ("/start", "/help", "/myid", "/status", "/status bad", "/cancel", "/queue", "/report", "что-то"):
        await h.say(BOB, cmd, lang)
    await h.say(COORD, f"/addmentor {MENTOR}", lang)
    for cmd in ("/help", "/queue", "/report", "/mentors", "/ticket", "/ticket HM-9999", "/addmentor", "/delmentor 5"):
        await h.say(COORD, cmd, lang)
    for category in ("question", "complaint", "suggestion", "praise", "serious"):
        h.dp["limiter"]._counts.clear()
        await h.submit(BOB, category, "Достаточно длинный текст обращения", lang=lang, anonymous=category == "serious")
    for ticket in await all_tickets(h):
        await h.tap(COORD, f"tk:{ticket.id}", lang)
        await h.tap(COORD, f"as:{ticket.id}", lang)
        await h.tap(COORD, f"st:{ticket.id}:resolved", lang)
    await h.say(BOB, "/status", lang)
    await h.say(COORD, "/queue", lang)
    await h.tap(COORD, "q:all:all:0", lang)
    await reports.send_weekly_report(h.bot, h.sm, SETTINGS)
    assert len(h.sent_to(COORD)) > 10


async def test_logs_never_contain_submitter_identity(h, caplog):
    """Логи Railway видны всем участникам проекта — личности заявителя там быть не должно."""
    import logging

    caplog.set_level(logging.INFO)
    await h.submit(ALICE, "serious", "Анонимное сообщение о серьёзной проблеме.", anonymous=True)
    await h.submit(ALICE, "complaint", COMPLAINT)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "HM-" in logged  # сам факт создания обращения в логах есть
    assert str(ALICE) not in logged and "user1001" not in logged.lower()


async def test_long_emoji_text_still_fits_telegram_limits(h):
    """Эмодзи занимают два UTF-16 юнита: карточка с максимальным текстом обязана влезть в 4096."""
    from bot.views import u16len

    await h.say(ALICE, "/feedback")
    await h.tap(ALICE, "cat:suggestion")
    await h.say(ALICE, "🔥" * 1600)  # 3200 юнитов — больше лимита
    assert "Слишком длинно" in h.texts_to(ALICE)[-1]
    await h.say(ALICE, "🔥" * 1500)  # ровно 3000
    await h.tap(ALICE, "att:skip")
    await h.tap(ALICE, "anon:0")
    (ticket,) = await all_tickets(h)
    for i in range(12):
        await h.tap(COORD, f"nt:{ticket.id}")
        await h.say(COORD, f"заметка {i} " + "я" * 600)
    await h.tap(COORD, f"tk:{ticket.id}")
    card = h.texts_to(COORD)[-1]
    assert u16len(card) <= 4096 and "заметка 11" in card and "заметка 0 " not in card


async def test_anonymous_submitter_can_read_long_replies_in_full(h):
    await h.submit(ALICE, "question", "Как работает запись на пересдачу экзамена?", anonymous=True)
    (ticket,) = await all_tickets(h)
    command = re.search(r"<code>(/status [^<]+)</code>", " ".join(h.texts_to(ALICE))).group(1)
    for marker in ("ПЕРВЫЙ", "ВТОРОЙ"):
        await h.tap(COORD, f"rp:{ticket.id}")
        await h.say(COORD, marker + " " + "очень подробный ответ " * 120 + marker + "-КОНЕЦ")
    h.clear()
    await h.say(ALICE, command)
    seen = " ".join(h.texts_to(ALICE))
    assert "ПЕРВЫЙ-КОНЕЦ" in seen and "ВТОРОЙ-КОНЕЦ" in seen
    assert any(d.startswith(f"cm:{ticket.id}:") for c in h.sent_to(ALICE) for d in h.buttons(c))
