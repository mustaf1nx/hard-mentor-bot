"""Тестовый стенд: настоящий диспетчер и хэндлеры, SQLite в памяти и фейковый Telegram.

Фейковая сессия записывает всё, что бот «отправил», — по этим записям тесты проверяют,
кто какое сообщение получил. В сеть ничего не уходит.
"""
from __future__ import annotations

import itertools
import os
import re
from datetime import datetime, timezone

import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SetMyCommands
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete

from bot.app import build_dispatcher
from bot.config import Settings, normalize_db_url
from bot.db import Base, init_db, make_engine, make_sessionmaker

COORD, LEAD, MENTOR, ALICE, BOB = 100, 200, 300, 1001, 1002
# По умолчанию — SQLite в памяти. Чтобы прогнать то же самое на Postgres:
#   TEST_DATABASE_URL=postgresql://user@localhost/db pytest
_DB_URL, _DB_ARGS = normalize_db_url(os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite://"))
SETTINGS = Settings(
    bot_token="42:TEST",
    database_url=_DB_URL,
    db_connect_args=_DB_ARGS,
    coordinator_ids=frozenset({COORD}),
    lead_ids=frozenset({LEAD}),
    daily_limit=5,
    secret=b"test-secret",
)
_ids = itertools.count(1)


_TAG = re.compile(r"<(/?)([a-z]+)>")


def assert_valid_for_telegram(method) -> None:
    """То, за что настоящий Telegram вернул бы 400: битый HTML, длинный текст, длинный callback_data."""
    text = getattr(method, "text", None) or getattr(method, "caption", None)
    if isinstance(text, str) and not isinstance(method, AnswerCallbackQuery):
        assert 0 < len(text) <= 4096, f"длина текста {len(text)}"
        stack = []
        for closing, tag in _TAG.findall(text):
            assert tag in {"b", "i", "code", "u", "s", "pre"}, f"неизвестный тег <{tag}>"
            if closing:
                assert stack and stack.pop() == tag, f"непарный </{tag}> в: {text[:80]}"
            else:
                stack.append(tag)
        assert not stack, f"незакрытые теги {stack} в: {text[:80]}"
        leftover = _TAG.sub("", text)
        assert "<" not in leftover and ">" not in leftover, f"неэкранированные < > в: {text[:80]}"
        assert not re.search(r"&(?!(amp|lt|gt|quot|#x27|#\d+);)", leftover), f"голый & в: {text[:80]}"
    if isinstance(method, AnswerCallbackQuery) and method.text:
        assert len(method.text) <= 200
    markup = getattr(method, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or []:
        for button in row:
            assert button.text and len(button.callback_data.encode()) <= 64, button


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        assert_valid_for_telegram(method)
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, SetMyCommands)):
            return True
        chat_id = getattr(method, "chat_id", None) or 0
        return Message(
            message_id=next(_ids), date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"), text=getattr(method, "text", None) or "",
        )


class Harness:
    def __init__(self, dp, bot, session, sm) -> None:
        self.dp, self.bot, self.session, self.sm = dp, bot, session, sm

    # --- входящие ---
    def _user(self, uid: int, lang: str = "ru") -> User:
        return User(id=uid, is_bot=False, first_name=f"User{uid}", username=f"user{uid}", language_code=lang)

    async def say(self, uid: int, text: str | None = None, lang: str = "ru", **extra) -> None:
        msg = Message(
            message_id=next(_ids), date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type="private"), from_user=self._user(uid, lang), text=text, **extra,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=msg))

    async def tap(self, uid: int, data: str, lang: str = "ru") -> None:
        origin = Message(
            message_id=next(_ids), date=datetime.now(timezone.utc),
            chat=Chat(id=uid, type="private"), text="…",
        )
        cb = CallbackQuery(
            id=str(next(_ids)), from_user=self._user(uid, lang), chat_instance="x", data=data, message=origin,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), callback_query=cb))

    # --- исходящие ---
    def sent_to(self, uid: int) -> list:
        return [c for c in self.session.calls if getattr(c, "chat_id", None) == uid]

    def texts_to(self, uid: int) -> list[str]:
        return [getattr(c, "text", None) or getattr(c, "caption", None) or "" for c in self.sent_to(uid)]

    def last_to(self, uid: int):
        return self.sent_to(uid)[-1]

    def alerts(self) -> list[str]:
        return [c.text for c in self.session.calls if isinstance(c, AnswerCallbackQuery) and c.text]

    def buttons(self, call) -> list[str]:
        kb = getattr(call, "reply_markup", None)
        rows = getattr(kb, "inline_keyboard", None) or []
        return [b.callback_data for row in rows for b in row]

    def clear(self) -> None:
        self.session.calls.clear()

    # --- типовой сценарий ---
    async def submit(self, uid: int, category: str, text: str, *, anonymous: bool = False,
                     related: str | None = "rel:skip", lang: str = "ru") -> None:
        await self.say(uid, "/feedback", lang)
        await self.tap(uid, f"cat:{category}", lang)
        if category != "suggestion" and related:
            if related.startswith("rel:"):
                await self.tap(uid, related, lang)
            else:
                await self.say(uid, related, lang)
        await self.say(uid, text, lang)
        await self.tap(uid, "att:skip", lang)
        await self.tap(uid, "anon:1" if anonymous else "anon:0", lang)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _stand():
    engine = make_engine(SETTINGS.database_url, SETTINGS.db_connect_args)
    await init_db(engine)
    sm = make_sessionmaker(engine)
    session = FakeSession()
    bot = Bot(SETTINGS.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = build_dispatcher(SETTINGS, sm)
    yield Harness(dp, bot, session, sm)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def h(_stand: Harness):
    async with _stand.sm() as s:
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(delete(table))
        await s.commit()
    _stand.clear()
    _stand.dp["limiter"]._counts.clear()
    _stand.dp["code_limiter"]._counts.clear()
    _stand.dp.storage.storage.clear()
    # сотрудники «нажали /start» — иначе бот не знает их имён и языка
    for uid in (COORD, LEAD, MENTOR):
        await _stand.say(uid, "/start")
    _stand.clear()
    return _stand
