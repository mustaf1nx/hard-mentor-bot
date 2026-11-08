"""Таблицы в `<pre>`: выравнивание, ширина, экранирование."""
from bot import tableview


def _plain(html: str) -> str:
    assert html.startswith('<pre>') and html.endswith('</pre>')
    return html[len('<pre>'):-len('</pre>')]


def test_columns_are_aligned():
    text = _plain(tableview.render(['Предмет', 'оценка'], [['Calculus 1', '4.5'], ['ICT', '—']]))
    lines = text.split('\n')
    assert lines[0].startswith('Предмет')
    # все строки одинаковой длины до правого края колонок
    assert len({len(line.rstrip()) >= len('Предмет') for line in lines}) == 1


def test_long_subject_is_truncated_not_wrapped():
    html = tableview.render(['Предмет', 'м с п о б'],
                            [['Очень длинное название дисциплины', '✓ ✓ ✓ · ·']], marks=True)
    text = _plain(html)
    longest = max(len(line) for line in text.split('\n'))
    assert longest <= tableview.MAX_WIDTH
    assert '…' in text


def test_html_inside_cells_is_escaped():
    html = tableview.render(['a'], [['<script>&']])
    assert '&lt;script&gt;&amp;' in html
    assert '<script>' not in html


def test_flags_render_one_glyph_per_cell():
    assert tableview.flags([True, False, True]) == '✓ · ✓'
