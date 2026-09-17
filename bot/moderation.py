"""Фильтр спама/мата (раздел 12 спека): только помечает, ничего не удаляет.

Пометка видна команде в карточке тикета — решение остаётся за человеком.
"""
from __future__ import annotations

import re

# Основы слов, а не полные формы — так ловятся склонения. Список намеренно короткий:
# расширяйте по итогам пилота.
_PROFANITY = re.compile(
    r"(?<![а-яёa-z])("
    r"бля|бляд|сук[аи]|сучк|хуй|хуе|хуё|хуя|пизд|еба|ёба|ебл|ебу|заеб|уеб|мудак|мудил|гандон|пидор|пидар|долбо[её]б"
    r"|fuck|shit|bitch|asshole|cunt|dick(?:head)?|bastard"
    r")",
    re.IGNORECASE,
)
_LINK = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)
_REPEAT = re.compile(r"(.)\1{7,}")


def check(text: str) -> list[str]:
    """Вернуть список причин для пометки (пустой — всё чисто)."""
    reasons: list[str] = []
    if _PROFANITY.search(text):
        reasons.append("profanity")
    if len(_LINK.findall(text)) >= 2:
        reasons.append("links")
    if _REPEAT.search(text):
        reasons.append("repeated chars")
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 30 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        reasons.append("caps")
    return reasons
