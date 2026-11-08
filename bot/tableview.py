"""Таблицы для Telegram.

В Bot API нет сущности «таблица» (в aiogram 3.31 / Bot API 10.3 список типов:
bold, italic, underline, strikethrough, spoiler, blockquote, expandable_blockquote,
code, pre, text_link, text_mention, custom_emoji, date_time). Единственный блок,
где колонки выравниваются, — `pre`: Telegram рисует его моноширинным.

Поэтому таблицы отдаются как `<pre>` с выровненными колонками. Важное:
внутри pre ширина ограничена экраном телефона, поэтому таблица должна быть узкой
(до ~34 символов), иначе Telegram переносит строки и выравнивание ломается.
"""
from __future__ import annotations

from html import escape

# Больше этого числа символов строка не влезет на телефоне без переноса.
MAX_WIDTH = 34
# Однобуквенные заголовки не объясняют смысл, поэтому подпись идёт отдельной строкой.
DASH = '-'
ON, OFF = '✓', '·'


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + '…'


def render(headers: list[str], rows: list[list[str]], *, max_width: int = MAX_WIDTH,
           marks: bool = False) -> str:
    """Собирает `<pre>`-таблицу с выровненными колонками.

    `marks=True` — для колонок-галочек: ширина 1 и никакой обрезки.
    """
    width = len(headers) if headers else len(rows[0])
    widths = []
    for index in range(width):
        cells = [headers[index]] if headers else []
        cells += [str(row[index]) for row in rows]
        widths.append(max(len(cell) for cell in cells))

    total = sum(widths) + (width - 1)
    if total > max_width:
        # Сжимаем только текстовые колонки, не трогая колонки-галочки.
        over = total - max_width
        for index in sorted(range(width), key=lambda i: widths[i], reverse=True):
            if over <= 0:
                break
            if marks and index > 0:
                continue
            cut = min(over, max(0, widths[index] - 3))
            widths[index] -= cut
            over -= cut

    def line(cells: list[str]) -> str:
        parts = []
        for index, cell in enumerate(cells):
            cell = str(cell)
            if len(cell) > widths[index]:
                cell = cell[:max(0, widths[index] - 1)] + '…'
            parts.append(cell.ljust(widths[index]))
        return ' '.join(parts).rstrip()

    out = []
    if headers:
        out.append(line(headers))
        out.append(' '.join(DASH * w for w in widths))
    out += [line(row) for row in rows]
    return '<pre>' + escape('\n'.join(out), quote=False) + '</pre>'


def flags(row: list[bool], labels: list[str] | None = None) -> str:
    """Одна строка из галочек — для разреза «материал · слот · менторка · ОС · Банк»."""
    return ' '.join(ON if value else OFF for value in row)
