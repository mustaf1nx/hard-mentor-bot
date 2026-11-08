"""Рендер переписки в PNG: смотреть на экран так, как его видит первокурсник.

Нужен Pillow, поэтому запускать питоном, где он есть:

    "C:\\Users\\Yedig\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" \
        tools\\render_chat.py --input _sim_out.json --out-dir shots

Вход — JSON из `tools/sim.py --transcript`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WIDTH = 470
PAD = 12
BOT_BG = (24, 37, 51)
USER_BG = (43, 82, 120)
BTN_BG = (32, 43, 54)
BTN_LINE = (48, 63, 78)
TEXT = (245, 245, 245)
MUTED = (125, 143, 160)
LINK = (108, 175, 235)
CANVAS = (14, 22, 33)
FONT_DIR = Path("C:/Windows/Fonts")

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u20E3]")
TAG = re.compile(r"(</?(?:b|code|pre)>)")


def font(name: str, size: int):
    for candidate in (name, "segoeui.ttf", "DejaVuSans.ttf"):
        path = FONT_DIR / candidate
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def segments(text: str) -> list[tuple[str, str]]:
    """Разбирает <b>, <code>, <pre> на куски со стилем."""
    out, style = [], ""
    for part in TAG.split(text):
        if part == "<b>":
            style = "b"
        elif part == "</b>":
            style = ""
        elif part == "<code>":
            style = "code"
        elif part == "</code>":
            style = ""
        elif part in ("<pre>", "</pre>"):
            continue  # сам тег не рисуем, моноширинность включается на уровне блока
        elif part:
            out.append((part, style))
    return out or [("", "")]


class Renderer:
    def __init__(self, size: int = 15, mono: bool = False):
        self.size = size
        base = "consola.ttf" if mono else "segoeui.ttf"
        self.f = {"": font(base, size), "b": font(base, size), "code": font("consola.ttf", size - 1)}
        self.emoji = font("seguiemj.ttf", size + 1)

    def measure(self, text: str, style: str) -> float:
        total = 0.0
        for chunk in re.split(f"({EMOJI.pattern}+)", text):
            if not chunk:
                continue
            total += (self.emoji if EMOJI.fullmatch(chunk[0]) else self.f[style]).getlength(chunk)
        return total

    def wrap(self, text: str, limit: float) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        width = 0.0
        for chunk, style in segments(text):
            for token in re.split(r"(\s+)", chunk):
                if not token:
                    continue
                w = self.measure(token, style)
                if token.isspace():
                    if current:
                        current.append((" ", style))
                        width += w
                    continue
                if width + w > limit and current:
                    lines.append(current)
                    current, width = [], 0.0
                current.append((token, style))
                width += w
        if current:
            lines.append(current)
        return lines or [[("", "")]]

    def line_height(self) -> int:
        return self.size + 6

    def draw_line(self, draw, x: float, y: float, line: list[tuple[str, str]], fill) -> None:
        for token, style in line:
            for chunk in re.split(f"({EMOJI.pattern}+)", token):
                if not chunk:
                    continue
                if EMOJI.fullmatch(chunk[0]):
                    try:
                        draw.text((x, y), chunk, font=self.emoji, embedded_color=True)
                    except (ValueError, OSError):
                        draw.text((x, y), chunk, font=self.emoji, fill=fill)
                    x += self.emoji.getlength(chunk)
                else:
                    draw.text((x, y), chunk, font=self.f[style], fill=fill)
                    x += self.f[style].getlength(chunk)


def build(input_path: Path, out_dir: Path) -> list[Path]:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(data, list):  # старый формат: просто список сообщений
        data = {"messages": {"chat": data}, "inputs": {}}
    messages = data.get("messages", {})
    inputs = data.get("inputs", {})
    out_dir.mkdir(parents=True, exist_ok=True)

    r = Renderer()
    r_mono = Renderer(mono=True)  # внутри <pre> Telegram рисует моноширинным
    saved = []
    for uid, items in messages.items():
        ins = {i["after"]: i["text"] for i in inputs.get(uid, [])}
        blocks = []
        marker = 0
        while marker <= len(items):
            if marker in ins:
                text = str(ins[marker] or "")
                lines = r.wrap(text, WIDTH - 3 * PAD - 40)
                blocks.append({"kind": "in", "lines": lines,
                               "h": len(lines) * r.line_height() + 18, "buttons": [], "edited": False,
                               "mono": False})
            if marker == len(items):
                break
            item = items[marker]
            text = item.get("text") or ""
            mono = "<pre>" in text
            pen = r_mono if mono else r
            lines = []
            for paragraph in text.split("\n"):
                lines += pen.wrap(paragraph, WIDTH - 3 * PAD - 24) if paragraph else [[("", "")]]
            buttons = []
            for row in (item.get("markup") or {}).get("inline_keyboard", []) or []:
                for button in row:
                    buttons.append(button.get("text", ""))
            edited = item["kind"] == "editMessageText"
            blocks.append({"kind": "bot", "lines": lines,
                           "h": len(lines) * pen.line_height() + 18 + (14 if edited else 0),
                           "buttons": buttons, "edited": edited, "mono": mono})
            marker += 1

        height = PAD + sum(b["h"] for b in blocks) + sum(34 * len(b["buttons"]) for b in blocks) + PAD
        image = Image.new("RGB", (WIDTH, max(height + 40, 200)), CANVAS)
        draw = ImageDraw.Draw(image)
        y = PAD

        for block in blocks:
            pen = r_mono if block["mono"] else r
            lines, block_h = block["lines"], block["h"]
            buttons = block["buttons"]
            if block["kind"] == "in":
                bubble_w = max(120, min(WIDTH - PAD * 2 - 30, int(max(
                    sum(r.measure(t, s) for t, s in line) for line in lines)) + 22))
                x0 = WIDTH - PAD - bubble_w
                draw.rounded_rectangle([x0, y, x0 + bubble_w, y + block_h - 8], 12, fill=USER_BG)
                ly = y + 6
                for line in lines:
                    r.draw_line(draw, x0 + 11, ly, line, TEXT)
                    ly += r.line_height()
                y += block_h - 4
                continue

            edited = block["edited"]
            bubble_w = WIDTH - PAD * 2 - 16
            draw.rounded_rectangle([PAD, y, PAD + bubble_w, y + block_h], 12, fill=BOT_BG)
            ly = y + 8
            if edited:
                draw.text((PAD + 12, ly), "изменено на месте", font=r.f[""], fill=MUTED)
                ly += 14
            for line in lines:
                pen.draw_line(draw, PAD + 12, ly, line, TEXT)
                ly += pen.line_height()
            y += block_h

            for label in buttons:
                draw.rounded_rectangle([PAD, y + 3, PAD + bubble_w, y + 33], 8, fill=BTN_BG, outline=BTN_LINE)
                text_w = r.measure(label, "")
                r.draw_line(draw, PAD + (bubble_w - text_w) / 2, y + 10, [(label, "")], LINK)
                y += 34
            y += 4

        path = out_dir / f"chat_{uid}.png"
        image.crop((0, 0, WIDTH, y + PAD)).save(path) if y + PAD < image.height else image.save(path)
        saved.append(path)
    return saved


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", default="shots")
    args = parser.parse_args()
    paths = build(Path(args.input), Path(args.out_dir))
    for path in paths:
        print(f"готово: {path}")
