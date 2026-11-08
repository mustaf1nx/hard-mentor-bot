"""Золотые отпечатки экранов: ловим непреднамеренные изменения UI.

Прогоняет сценарий через `tools/sim.py`, нормализует переписку (убирает даты, время, id)
и сравнивает с эталоном в `shots/golden/<сценарий>.md`.

    python tools/golden.py --all --update      # записать эталоны
    python tools/golden.py --all               # проверить
    python tools/golden.py --scenario tools/scenarios/ticket.json --update

Нормализация нужна потому, что сид и время зависят от момента запуска: без неё
эталон ломается каждую минуту.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
GOLDEN = ROOT / "shots" / "golden"
TMP = ROOT / "_golden_tmp.json"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TAGS = re.compile(r"</?(?:b|i|u|s|code|pre|tg-spoiler|blockquote)>")
DATE = re.compile(r"\d{1,2}\.\d{2}\.\d{4}")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
TICKET = re.compile(r"HM-\d+")
HASH_ID = re.compile(r"#\d+")
STUDENT = re.compile(r"\b6\d{5}\b")
MENTOR = re.compile(r"\b5\d{5}\b")

# Фоновые push-уведомления приходят в случайный момент (цикл раз в минуту).
# Для эталона экранов они шум: в сценариях их проверяют отдельными `expect`.
NOISE = re.compile(
    r"^(?:📝 Как прошла менторка|⏰ SLA просрочен|⚠️ На менторку|✅ Время менторки|"
    r"📎 Занятие #\d+ завершено|🎓 Вам назначена менторка|Пробный эндтерм)")


def normalize(text: str) -> str:
    text = TAGS.sub("", text or "")
    text = DATE.sub("<дата>", text)
    text = TIME.sub("<время>", text)
    text = TICKET.sub("HM-<id>", text)
    text = HASH_ID.sub("#<id>", text)
    text = STUDENT.sub("<студент>", text)
    text = MENTOR.sub("<ментор>", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def digest(transcript: dict) -> str:
    lines = []
    for uid in sorted(transcript.get("messages", {}), key=str):
        lines.append(f"### чат {uid}")
        for item in transcript["messages"][uid]:
            body = normalize(item.get("text") or "")
            if NOISE.match(body):
                continue
            kind = "✎" if item.get("kind") == "editMessageText" else " "
            lines.append(kind + " " + body)
            for row in (item.get("markup") or {}).get("inline_keyboard") or []:
                lines.append("   [ " + " ] [ ".join(normalize(b.get("text", "")) for b in row) + " ]")
    return "\n".join(lines) + "\n"


def run_scenario(path: Path) -> bool:
    if TMP.exists():
        TMP.unlink()
    result = subprocess.run([str(PY), str(ROOT / "tools" / "sim.py"),
                             "--scenario", str(path), "--transcript", str(TMP)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    ok = result.returncode == 0 and TMP.exists()
    if not ok:
        tail = (result.stdout or result.stderr or "").strip().splitlines()[-3:]
        print("   прогон не удался: " + " / ".join(tail))
    return ok


def compare(name: str, current: str, update: bool) -> bool:
    target = GOLDEN / f"{name}.md"
    if update or not target.exists():
        GOLDEN.mkdir(parents=True, exist_ok=True)
        target.write_text(current, encoding="utf-8")
        state = "записан" if not target.exists() else "обновлён"
        print(f"  {name:12} эталон {state}")
        return True
    expected = target.read_text(encoding="utf-8")
    if expected == current:
        print(f"  {name:12} OK  {hashlib.sha256(current.encode()).hexdigest()[:12]}")
        return True
    print(f"  {name:12} ИЗМЕНИЛСЯ")
    diff = list(difflib.unified_diff(expected.splitlines(), current.splitlines(),
                                     "эталон", "сейчас", lineterm="", n=1))
    for line in diff[:24]:
        print("   " + line)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--slow", action="store_true", help="включить autocancel (≈90 с)")
    args = parser.parse_args()

    if args.all:
        # В той же папке лежат транскрипты прогонов (`_run_*.json`, `_audit_*.json`):
        # это не сценарии, у них нет `steps`. Иначе --all падает на первом же.
        paths = []
        for candidate in sorted((ROOT / "tools" / "scenarios").glob("*.json")):
            try:
                if "steps" not in json.loads(candidate.read_text(encoding="utf-8-sig")):
                    continue
            except (OSError, json.JSONDecodeError):
                continue
            paths.append(candidate)
        if not args.slow:
            paths = [p for p in paths if p.stem != "autocancel"]
    elif args.scenario:
        paths = [Path(args.scenario)]
    else:
        parser.error("нужен --all или --scenario")

    failures = 0
    for path in paths:
        print(f"\n{path.stem}")
        if not run_scenario(path):
            failures += 1
            continue
        data = json.loads(TMP.read_text(encoding="utf-8"))
        if not compare(path.stem, digest(data), args.update):
            failures += 1
    if TMP.exists():
        TMP.unlink()
    print(f"\nИТОГ: {'эталоны совпадают' if not failures else str(failures) + ' расхождений'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
