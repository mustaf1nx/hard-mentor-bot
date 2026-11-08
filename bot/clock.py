"""Единственный источник «сейчас» для доменной логики.

Зачем: тесты не должны зависеть от реального времени. Раньше проверки «за 24 часа
до слота» и «дедлайн за 3 дня» ломались в полночь или на границе недели, потому что
считались от фактических часов машины.

    clock.freeze(datetime(2026, 11, 7, 10, 0))
    ...
    clock.reset()

В проде провайдер не задан — работает обычное время.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_provider = None


def _utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now() -> datetime:
    """Текущее время в UTC без таймзоны — как колонки в базе."""
    return (_provider or _utc)()


def freeze(value: datetime) -> datetime:
    """Замораживает время. Значение с таймзоной приводится к UTC."""
    global _provider
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    _provider = lambda: value  # noqa: E731
    return value


def travel(delta: timedelta) -> datetime:
    """Сдвигает время вперёд от текущего замороженного."""
    return freeze(now() + delta)


def reset() -> None:
    global _provider
    _provider = None


def frozen() -> bool:
    return _provider is not None
