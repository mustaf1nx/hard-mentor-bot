"""Лимиты в оперативной памяти.

Почему не в БД: лимит «5 обращений в день» требует помнить, кто сколько отправил,
а анонимность требует не хранить, кто что отправил. Счётчик в памяти процесса решает
обе задачи: он никогда не попадает на диск и в бэкапы, не связан с тикетами и
обнуляется при перезапуске. Бот работает в одной реплике, так что этого достаточно.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date


class DailyCounter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._day: date | None = None
        self._counts: defaultdict[int, int] = defaultdict(int)

    def _roll(self, today: date) -> None:
        if self._day != today:
            self._day = today
            self._counts.clear()

    def allowed(self, user_id: int, today: date) -> bool:
        self._roll(today)
        return self._counts[user_id] < self.limit

    def hit(self, user_id: int, today: date) -> None:
        self._roll(today)
        self._counts[user_id] += 1
