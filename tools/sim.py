"""Прогон сценария на настоящем процессе бота через фейковый Bot API.

Поднимает `tools/fake_telegram.py` на свободных портах, запускает `python -m bot`
как отдельный процесс с BOT_API_BASE, шлёт апдейты от имени пользователя
и проверяет результат: текст, кнопки или состояние базы.

    python tools/sim.py --scenario tools/scenarios/dash.json
    python tools/sim.py --scenario tools/scenarios/dash.json --transcript shots/dash.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
DB_FILE = ROOT / "_sim.db"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HTML = re.compile(r"</?(?:b|i|u|s|code|pre|tg-spoiler)>")
_WHEN = re.compile(r"\{\{now([+-]\d+)([hdm])\}\}")
API = ""
CONTROL = ""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(url: str, payload: dict | None = None, timeout: float = 5.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ctl(action: str, payload: dict | None = None, timeout: float = 5.0):
    return call(f"{CONTROL}/ctl/{action}", payload, timeout)


def strip_html(text: str) -> str:
    return _HTML.sub("", text)


def expand(value):
    """Подставляет {{now+20h}} как локальное время в формате бота."""
    if not isinstance(value, str):
        return value
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    def repl(match: re.Match) -> str:
        amount = int(match.group(1))
        unit = {"h": "hours", "d": "days", "m": "minutes"}[match.group(2)]
        moment = datetime.now(ZoneInfo("Asia/Almaty")) + timedelta(**{unit: amount})
        return moment.strftime("%d.%m.%Y %H:%M")

    return _WHEN.sub(repl, value)


def _outbox(uid: int, since: int = 0) -> list[dict]:
    with urllib.request.urlopen(f"{CONTROL}/ctl/outbox?user_id={uid}", timeout=5) as resp:
        return json.loads(resp.read().decode())["items"][since:]


def buttons(uid: int, since: int = 0) -> list[dict]:
    items = _outbox(uid, since)
    for item in reversed(items):
        rows = (item.get("markup") or {}).get("inline_keyboard") or []
        if rows:
            return [button for row in rows for button in row]
    return []


def labels(uid: int, since: int = 0) -> list[str]:
    return [strip_html(b.get("text", "")) for b in buttons(uid, since)]


def all_labels(uid: int, since: int = 0) -> list[str]:
    """Кнопки из всех сообщений окна: уведомление не должно перебивать ответ на команду."""
    out: list[str] = []
    for item in _outbox(uid, since):
        for row in (item.get("markup") or {}).get("inline_keyboard") or []:
            out += [strip_html(b.get("text", "")) for b in row]
    return out


def pick_button(uid: int, label: str | None, index: int | None, since: int = 0) -> str | None:
    items = _outbox(uid, since)
    if index is not None:
        for item in reversed(items):
            rows = (item.get("markup") or {}).get("inline_keyboard") or []
            found = [b for row in rows for b in row]
            if 0 <= int(index) < len(found):
                return found[int(index)]["callback_data"]
        return None
    for item in reversed(items):
        rows = (item.get("markup") or {}).get("inline_keyboard") or []
        found = [b for row in rows for b in row]
        exact = [b for b in found if strip_html(b.get("text", "")).strip() == (label or "").strip()]
        if exact:
            return exact[0]["callback_data"]
        partial = [b for b in found if (label or "").casefold() in strip_html(b.get("text", "")).casefold()]
        if len(partial) == 1:
            return partial[0]["callback_data"]
        if len(partial) > 1:
            return None
    return None


def pick_prefix(uid: int, prefix: str, since: int = 0) -> str | None:
    for item in reversed(_outbox(uid, since)):
        for row in (item.get("markup") or {}).get("inline_keyboard") or []:
            for button in row:
                if (button.get("callback_data") or "").startswith(prefix):
                    return button["callback_data"]
    return None


def button_label(uid: int, callback_data: str, since: int = 0) -> str | None:
    """Название реально выбранной кнопки для понятного журнала сценария."""
    for item in reversed(_outbox(uid, since)):
        for row in (item.get('markup') or {}).get('inline_keyboard') or []:
            for button in row:
                if button.get('callback_data') == callback_data:
                    return button.get('text')
    return None


def db_query(sql: str) -> list[tuple]:
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def wait_for(check, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return True
        except (OSError, sqlite3.Error):
            pass
        time.sleep(0.2)
    return False


TAGS = {"b", "i", "u", "s", "code", "pre", "tg-spoiler", "blockquote"}


def validate_telegram(items: list[dict]) -> list[str]:
    """То, за что настоящий Telegram вернул бы 400: длина, теги, callback_data."""
    problems = []
    for item in items:
        text = item.get("text") or ""
        if len(text) > 4096:
            problems.append(f"сообщение {len(text)} символов > 4096")
        stack = []
        for closing, tag in re.findall(r"<(/?)(" + "|".join(TAGS) + r")>", text):
            if closing:
                if not stack or stack.pop() != tag:
                    problems.append(f"непарный </{tag}>")
                    stack = []
                    break
            else:
                stack.append(tag)
        if stack:
            problems.append(f"незакрытые теги {stack}")
        for row in (item.get("markup") or {}).get("inline_keyboard") or []:
            for button in row:
                if len(button.get("callback_data", "").encode()) > 64:
                    problems.append(f"callback_data длиннее 64 байт: {button['callback_data'][:40]}")
    return problems


def check_expectations(step: dict, uid: int, since: int, index: int) -> bool:
    """Проверки ответа с последнего ввода пользователя, включая фоновые сообщения."""
    timeout = float(step.get("timeout", 6))

    if "expect" in step:
        needle = step["expect"].casefold()
        ok = wait_for(lambda: any(needle in (m.get("text") or "").casefold()
                                  for m in _outbox(uid, since)), timeout)
        report(index, ok, f"текст «{step['expect']}»")
        if not ok:
            dump(uid)
            return False

    if "expect_button" in step:
        needle = step["expect_button"].casefold()
        ok = wait_for(lambda: any(needle in label.casefold() for label in all_labels(uid, since)), timeout)
        report(index, ok, f"кнопка «{step['expect_button']}»")
        if not ok:
            print("       доступные кнопки: " + ", ".join(sorted(set(all_labels(uid, since)))))
            return False

    if "expect_button_prefix" in step:
        prefix = step["expect_button_prefix"]
        ok = wait_for(lambda: pick_prefix(uid, prefix, since) is not None, timeout)
        report(index, ok, f"кнопка с префиксом {prefix}")
        if not ok:
            return False

    if "expect_kind" in step:
        expected_kind = step["expect_kind"]
        ok = wait_for(lambda: any(m.get("kind") == expected_kind for m in _outbox(uid, since)), timeout)
        report(index, ok, f"метод ответа {expected_kind}")
        if not ok:
            return False

    if "expect_no_button" in step:
        needle = step["expect_no_button"].casefold()
        time.sleep(float(step.get("wait", 0.6)))
        ok = not any(needle in label.casefold() for label in all_labels(uid, since))
        report(index, ok, f"нет кнопки «{step['expect_no_button']}»")
        if not ok:
            return False

    if "expect_db" in step:
        sql = step["expect_db"]
        expected = step.get("equals")
        check = (lambda: [tuple(r) for r in db_query(sql)] == [tuple(e) if isinstance(e, (list, tuple))
                                                               else (e,) for e in expected]) \
            if expected is not None else (lambda: db_query(sql) == [])
        ok = wait_for(check, timeout)
        report(index, ok, f"БД: {sql}" + (f" → {expected}" if expected is not None else " пусто"))
        if not ok:
            print(f"       в базе: {db_query(sql)}")
            return False

    if step.get("expect_silence"):
        marker = len(_outbox(uid))
        time.sleep(float(step.get("wait", 1.0)))
        quiet = len(_outbox(uid)) == marker
        report(index, quiet, "бот не ответил")
        if not quiet:
            dump(uid)
            return False

    return True


def report(index: int, ok: bool, what: str) -> None:
    print(f"  [{index:02d}] {'OK  ' if ok else 'FAIL'} {what}")


def dump(uid: int) -> None:
    items = _outbox(uid)
    print(f"       получено {len(items)} сообщений, последние:")
    for item in items[-4:]:
        print("       · " + (item.get("text") or "").replace("\n", " ")[:110])


def main() -> int:
    global API, CONTROL
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--transcript", default="")
    args = parser.parse_args()

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8-sig"))
    if DB_FILE.exists():
        DB_FILE.unlink()

    admin = int(scenario.get("admin", 100500))
    users = {name: int(uid) for name, uid in scenario.get("users", {}).items()}
    users.setdefault("admin", admin)

    api_port, control_port = free_port(), free_port()
    API, CONTROL = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{control_port}"

    env = dict(os.environ)
    env.update({
        "BOT_TOKEN": "123456:TESTTOKEN",
        "DATABASE_URL": "sqlite+aiosqlite:///./_sim.db",
        "COORDINATOR_IDS": str(admin),
        "LEAD_IDS": str(admin),
        "TIMEZONE": "Asia/Almaty",
        "BOT_API_BASE": API,
        "PYTHONIOENCODING": "utf-8",
    })
    env.update({str(k): str(v) for k, v in (scenario.get("env") or {}).items()})

    seed_script = scenario.get("seed_script") or ("tools/seed_demo.py" if scenario.get("seed") else None)
    if seed_script:
        seed_cmd = [str(PY), str(ROOT / seed_script), "--admin", str(admin),
                    "--db", "sqlite+aiosqlite:///./_sim.db"]
        seed_cmd += [str(x) for x in scenario.get("seed_args", [])]
        result = subprocess.run(seed_cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            print("  сид упал:", result.stderr[-500:])
            return 1

    log_path, server_log = ROOT / "_sim_bot.log", ROOT / "_sim_server.log"
    server = subprocess.Popen([str(PY), "-u", str(ROOT / "tools" / "fake_telegram.py"),
                               "--port", str(api_port), "--control", str(control_port)],
                              cwd=ROOT, env=env, stdout=open(server_log, "wb"), stderr=subprocess.STDOUT)
    bot = None
    transcript: dict[int, list[dict]] = {}
    inputs: dict[int, list[dict]] = {}
    try:
        wait_for(lambda: ctl("health", timeout=1.0)["ok"], 20)
        bot = subprocess.Popen([str(PY), "-u", "-m", "bot"], cwd=ROOT, env=env,
                               stdout=open(log_path, "wb"), stderr=subprocess.STDOUT)
        time.sleep(float(scenario.get("warmup", 4.0)))
        if bot.poll() is not None:
            print("  бот завершился при старте, лог:")
            print("   " + log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
            return 1

        mark: dict[int, int] = {}
        response_start: dict[int, int] = {}
        for index, step in enumerate(scenario["steps"], 1):
            who = users.get(step.get("as", "admin"), admin)
            uid = int(step.get("user_id") or who)
            before = len(_outbox(uid))
            since = mark.pop(uid, 0) if ("tap_label" in step or "tap_first" in step
                                         or "tap_prefix" in step) else 0
            shown = None
            if "command" in step:
                ctl("command", {"user_id": uid, "command": step["command"]})
                shown = step["command"]
            elif "say" in step:
                ctl("say", {"user_id": uid, "text": expand(step["say"])})
                shown = step["say"]
            elif "tap" in step:
                ctl("tap", {"user_id": uid, "data": step["tap"]})
                shown = "→ " + step["tap"]
            elif "tap_label" in step or "tap_first" in step:
                data = pick_button(uid, step.get("tap_label"), step.get("tap_first"), since)
                if data is None:
                    print(f"  [{index:02d}] FAIL кнопка {step.get('tap_label') or step.get('tap_first')} не найдена")
                    print("       доступные: " + ", ".join(labels(uid, since)))
                    return 1
                selected_label = button_label(uid, data, since)
                ctl("tap", {"user_id": uid, "data": data})
                shown = "→ " + (selected_label or step.get("tap_label") or f"кнопка #{step.get('tap_first')}")
            elif "tap_prefix" in step:
                data = pick_prefix(uid, step["tap_prefix"], since)
                if data is None:
                    print(f"  [{index:02d}] FAIL кнопка с префиксом {step['tap_prefix']} не найдена")
                    print("       доступные: " + ", ".join(labels(uid, since)))
                    return 1
                selected_label = button_label(uid, data, since)
                ctl("tap", {"user_id": uid, "data": data})
                shown = "→ " + (selected_label or step["tap_prefix"] + "…")
            elif "document" in step:
                ctl("document", {"user_id": uid, "file_name": step["document"]})
                shown = "📎 " + step["document"]
            elif "photo" in step:
                ctl("photo", {"user_id": uid})
                shown = "🖼 " + str(step.get("photo") or "photo.jpg")
            elif "fixture_end_cycle_lesson" in step:
                # Только локальный стенд: занятие завершается по времени без ожидания дней.
                # DATABASE_URL процесса бота жёстко указывает на _sim.db выше.
                with sqlite3.connect(DB_FILE) as con:
                    updated = con.execute(
                        "update mentoring_sessions set starts_at=datetime('now','-2 hours'), "
                        "ends_at=datetime('now','-1 hour') "
                        "where creation_key='cycle-demo' and status='scheduled'"
                    ).rowcount
                    if updated != 1:
                        raise RuntimeError('Тестовое занятие для перемотки не найдено')
                print('  Тестовый стенд: время занятия сдвинуто в прошлое.')
                before = None
            elif "sleep" in step:
                time.sleep(float(step["sleep"]))
                before = None
            else:
                before = None
            if before is not None:
                mark[uid] = before
                response_start[uid] = before
                inputs.setdefault(uid, []).append({"after": before, "text": shown,
                                                  "step": index, "role": step.get("as", "admin")})
            if not check_expectations(step, uid, response_start.get(uid, 0), index):
                return 1
            time.sleep(float(step.get("wait", 0.6)))
            if before is not None:
                # Фиксируем конец окна сразу после этого действия: следующие роли могут
                # отправлять сообщения этому же пользователю до его следующего шага.
                inputs[uid][-1]["until"] = len(_outbox(uid))

        for name, uid in users.items():
            transcript[uid] = _outbox(uid)
    finally:
        # даже упавший прогон должен оставить переписку для разбора
        try:
            for name, uid in users.items():
                transcript.setdefault(uid, _outbox(uid))
            if args.transcript:
                target = Path(args.transcript)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"messages": {str(k): v for k, v in transcript.items()},
                                             "inputs": {str(k): v for k, v in inputs.items()}},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        for proc in (bot, server):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    problems = validate_telegram([m for items in transcript.values() for m in items])
    if problems:
        print("  Telegram отклонил бы:")
        for problem in problems[:5]:
            print("   · " + problem)
        return 1

    print("\n" + "=" * 70)
    for uid, items in transcript.items():
        print(f"\n### ЧАТ {uid} — сообщений от бота: {len(items)}")
        for item in items:
            kind = {"sendMessage": "  ", "editMessageText": "✎ ", "deleteMessage": "🗑 "}.get(item["kind"], "  ")
            print(kind + strip_html(item.get("text") or "").replace("\n", "\n      "))
            for row in (item.get("markup") or {}).get("inline_keyboard", []) or []:
                print("      [ " + " ] [ ".join(b.get("text", "") for b in row) + " ]")
    if args.transcript:
        payload = {"messages": {str(k): v for k, v in transcript.items()},
                   "inputs": {str(k): v for k, v in inputs.items()}}
        target = Path(args.transcript)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nпереписка сохранена: {args.transcript}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
