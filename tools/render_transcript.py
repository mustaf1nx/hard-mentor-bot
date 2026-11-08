"""Рендер визуализации диалога симулятора в PNG для отчёта владельцу.

Это не скриншот Telegram: HTML-карточки строятся из transcript JSON,
полученного реальным процессом бота против фейкового Bot API.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(
    r"C:\Users\Yedig\AppData\Local\ms-playwright"
    r"\chromium_headless_shell-1248\chrome-headless-shell-win64\chrome-headless-shell.exe"
)
TAG = re.compile(r"</?(?:b|i|u|s|code|pre|tg-spoiler|blockquote)>", re.IGNORECASE)


def clean(text: str) -> str:
    return html.escape(TAG.sub("", text or "")).replace("\n", "<br>")


def screen(message: dict, caption: str) -> str:
    rows = []
    for row in (message.get("markup") or {}).get("inline_keyboard") or []:
        rows.append("<div class='row'>" + "".join(
            f"<span class='btn'>{html.escape(button.get('text', ''))}</span>" for button in row
        ) + "</div>")
    buttons = "".join(rows)
    return f"""
    <article class="card">
      <div class="caption">{html.escape(caption)}</div>
      <div class="phone">
        <div class="bar"><span>Telegram-симулятор</span><span>тестовый чат</span></div>
        <div class="body"><div class="bubble">{clean(message.get('text', ''))}</div></div>
        <div class="keys">{buttons}</div>
      </div>
    </article>"""


def step_count(transcript: dict) -> int:
    total = 0
    messages = transcript.get("messages") or {}
    inputs = transcript.get("inputs") or {}
    for uid, items in messages.items():
        chosen = {int(a.get("after") or 0) - 1 for a in (inputs.get(uid) or [])
                  if 0 < int(a.get("after") or 0) <= len(items)}
        total += len(chosen) if chosen else (1 if items else 0)
    return max(1, total)


def build_html(transcript: dict, title: str) -> str:
    messages = transcript.get("messages") or {}
    inputs = transcript.get("inputs") or {}
    sections = []
    for uid in messages:
        items = messages[uid]
        acts = inputs.get(uid) or []
        selected = []
        seen = set()
        for act in acts:
            after = int(act.get("after") or 0)
            if after <= 0 or after > len(items):
                continue
            index = after - 1
            if index in seen:
                continue
            seen.add(index)
            selected.append((items[index], act.get("text") or "действие"))
        if not selected:
            selected = [(items[-1], "итоговый экран")] if items else []
        cards = "".join(screen(message, f"Шаг: {caption}") for message, caption in selected)
        sections.append(f"<h2>Чат {html.escape(str(uid))}</h2><div class='grid'>{cards}</div>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
    *{{box-sizing:border-box}} body{{margin:0;background:#eef4f7;color:#14273a;font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
    header{{padding:28px 40px;background:#103754;color:#fff}} h1{{margin:0 0 8px;font-size:26px}}
    header p{{margin:0;color:#dbeaf3;max-width:1100px}} .note{{margin:18px 40px;padding:14px 18px;background:#fff5df;border-left:4px solid #bb7e15}}
    h2{{margin:28px 40px 12px}} .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;padding:0 40px 30px}}
    .card{{background:#fff;border:1px solid #cddae3;border-radius:12px;padding:12px;break-inside:avoid}}
    .caption{{font-size:13px;font-weight:700;color:#4b6172;margin:0 0 8px;min-height:36px}}
    .phone{{border:1px solid #cbd8e1;border-radius:18px;overflow:hidden;background:#e8f0f4}}
    .bar{{display:flex;justify-content:space-between;background:#2b5270;color:#fff;font-size:12px;padding:8px 12px}}
    .body{{padding:12px;min-height:120px}} .bubble{{background:#fff;border-radius:12px;padding:12px;font-size:14px;line-height:1.45;word-break:break-word}}
    .keys{{padding:0 10px 12px}} .row{{display:flex;gap:6px;margin-top:6px}} .btn{{display:block;background:#dcebf3;border-radius:8px;padding:8px 10px;font-size:12px;flex:1}}
    footer{{padding:20px 40px 40px;color:#4b6172;font-size:13px}}
    </style></head><body><header><h1>{html.escape(title)}</h1><p>Визуализация диалога из проверенного сценария симулятора. Это не скриншот Telegram и не макет продукта: тексты и кнопки взяты из transcript JSON.</p></header>
    <div class="note">Тестовые аккаунты и демонстрационные данные. Реальные имена и сообщения не показываются.</div>
    {''.join(sections)}
    <footer>Сформировано из transcript JSON. Оформление — визуализация отчёта, не интерфейс Telegram.</footer>
    </body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    html_path = output.with_suffix(".html")
    html_path.write_text(build_html(transcript, args.title), encoding="utf-8")
    if not CHROME.exists():
        print("Chrome not found:", CHROME)
        return 1
    rows = (step_count(transcript) + 2) // 3
    height = min(16000, max(1200, 260 + rows * 520))
    result = subprocess.run([
        str(CHROME), "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size=1500,{height}",
        f"--screenshot={output}", html_path.as_uri(),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not output.exists():
        print(result.stderr[-1000:])
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
