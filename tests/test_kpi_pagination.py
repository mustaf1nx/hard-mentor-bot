"""KPI постранично: на 30 менторах один экран был стеной текста."""
from sqlalchemy import select

from bot.db import Mentor, MentorRole, Subject, Term, User, utcnow
from bot.mentoring import service as svc
from tests.conftest import COORD, SETTINGS


async def _many_mentors(h, count: int = 12):
    today = utcnow().date()
    monday = today - __import__("datetime").timedelta(days=today.weekday())
    async with h.sm() as s:
        term = Term(name="kpi-pages", starts_on=(monday - __import__("datetime").timedelta(weeks=1)).isoformat(),
                    weeks=12, cohort=2500, active=True)
        s.add(term)
        topic = await svc.ensure_topic(s, "Calculus", "Integrals")
        await s.flush()  # id триместра в Postgres не начинается с единицы
        for index in range(count):
            uid = 500_100 + index
            s.add(User(telegram_id=uid, display_name=f"Ментор {index}", username=f"m{index}"))
        await s.flush()  # Postgres проверяет FK: пользователи раньше менторов
        for index in range(count):
            uid = 500_100 + index
            s.add(Mentor(telegram_id=uid))
            s.add(MentorRole(telegram_id=uid, subject_id=topic.subject_id,
                             term_id=term.id, role="respondent"))
        await s.commit()


async def test_first_page_and_navigation(h):
    await _many_mentors(h, 12)
    await h.say(COORD, "/kpi")
    text = h.texts_to(COORD)[-1]
    assert "Менторы 1–10 из 12" in text
    buttons = h.buttons(h.last_to(COORD))
    assert any(b == "kpi:1" for b in buttons), buttons
    assert not any(b == "kpi:-1" for b in buttons)
    await h.tap(COORD, "kpi:1")
    text = h.texts_to(COORD)[-1]
    assert "Менторы 11–12 из 12" in text
    assert any(b == "kpi:0" for b in h.buttons(h.last_to(COORD)))


async def test_small_registry_has_no_navigation(h):
    await _many_mentors(h, 3)
    await h.say(COORD, "/kpi")
    text = h.texts_to(COORD)[-1]
    assert "Менторы 1–3 из 3" in text
    assert not any(b.startswith("kpi:") for b in h.buttons(h.last_to(COORD)))


async def test_student_cannot_page(h):
    await _many_mentors(h, 12)
    await h.tap(1002, "kpi:1")
    assert any("координатору" in alert for alert in h.alerts())
