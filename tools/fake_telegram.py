"""Фейковый Telegram Bot API: настоящий процесс бота думает, что работает с Telegram.

Поднимает HTTP-сервер на localhost, который отвечает на методы Bot API, и рядом
управляющий API, через который сценарий пишет «как пользователь» и читает ответы.

Запуск:
    python tools/fake_telegram.py --port 8081 --control 8082

Бот к нему подключается через BOT_API_BASE=http://127.0.0.1:8081 — см. bot/__main__.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BOT_USER = {"id": 900, "is_bot": True, "first_name": "Hard Mentor Bot", "username": "aitu_mentor_test_bot"}

# Методы, которые бот вызывает, но которые ничего не меняют в тесте.
IGNORED_METHODS = {
    "answerCallbackQuery", "setMyCommands", "deleteWebhook", "setWebhook", "getWebhookInfo",
    "getChat", "getChatMember", "close", "logOut", "getMe2",
}


def now_ts() -> int:
    return int(time.time())


class Store:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.outbox: list[dict] = []          # всё, что бот отправил/отредактировал
        self.cond = asyncio.Condition()
        self.next_update_id = 1
        self.next_message_id = 1
        self.error: str | None = None

    async def add_update(self, update: dict) -> None:
        async with self.cond:
            update["update_id"] = self.next_update_id
            self.next_update_id += 1
            self.updates.append(update)
            self.cond.notify_all()

    def message(self, chat_id: int, text: str, **extra) -> dict:
        self.next_message_id += 1
        msg = {
            "message_id": self.next_message_id,
            "date": now_ts(),
            "chat": {"id": chat_id, "type": "private"},
            "from": BOT_USER,
            "text": text,
        }
        msg.update(extra)
        return msg


STORE = Store()


def user_obj(uid: int, name: str | None = None) -> dict:
    return {"id": uid, "is_bot": False, "first_name": name or f"User{uid}", "username": f"user{uid}"}


# --------------------------------------------------------------- Bot API

def parse_payload(raw: bytes, content_type: str | None) -> dict:
    """aiogram шлёт form-urlencoded, а не JSON — поддерживаем оба."""
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    if "json" in (content_type or "").lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    data = dict(parse_qsl(text, keep_blank_values=True))
    for key, value in list(data.items()):
        # reply_markup, allowed_updates, entities приходят JSON-строками внутри формы
        if isinstance(value, str) and value[:1] in "[{":
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return data


SAMPLE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)
FILE_PATH = "documents/sample.pdf"


async def serve_file(request: web.Request) -> web.Response:
    """Отдача файла: без неё загрузка материалов в боте не проверяется."""
    return web.Response(body=SAMPLE_PDF, content_type="application/pdf")


async def api(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    trace = os.environ.get("FAKE_TRACE")
    raw = await request.read()
    if trace:
        with open("_fake_api_calls.log", "a", encoding="utf-8") as fh:
            fh.write(f"{method} ct={request.content_type} len={len(raw)} body={raw[:160]!r}\n")
    payload = parse_payload(raw, request.content_type)

    if method == "getMe":
        return web.json_response({"ok": True, "result": BOT_USER})

    if method == "getUpdates":
        offset = int(payload.get("offset") or 0)
        timeout = min(float(payload.get("timeout") or 0), 1.0)
        deadline = time.monotonic() + max(timeout, 0.0)
        async with STORE.cond:
            while True:
                # клиент подтверждает получение смещением
                STORE.updates = [u for u in STORE.updates if u["update_id"] >= offset]
                if STORE.updates:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    await asyncio.wait_for(STORE.cond.wait(), left)
                except asyncio.TimeoutError:
                    break
            batch = [u for u in STORE.updates if u["update_id"] >= offset]
        return web.json_response({"ok": True, "result": batch})

    if method in ("sendMessage", "editMessageText", "sendDocument", "sendPhoto", "editMessageReplyMarkup"):
        chat_id = (payload.get("chat_id") or 0)
        text = payload.get("text") or payload.get("caption") or ""
        STORE.outbox.append({
            "kind": method,
            "chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
            "text": text,
            "markup": payload.get("reply_markup"),
            "at": now_ts(),
        })
        return web.json_response({"ok": True, "result": STORE.message(int(chat_id), text,
                                                                      reply_markup=payload.get("reply_markup"))})

    if method == "deleteMessage":
        STORE.outbox.append({"kind": "deleteMessage", "chat_id": payload.get("chat_id"), "text": "", "at": now_ts()})
        return web.json_response({"ok": True, "result": True})

    if method == "getFile":
        return web.json_response({"ok": True, "result": {
            "file_id": payload.get("file_id", "f1"), "file_unique_id": "uniq1",
            "file_size": len(SAMPLE_PDF), "file_path": FILE_PATH}})

    if method in IGNORED_METHODS:
        return web.json_response({"ok": True, "result": True})

    # Незнакомый метод — это дыра в фейке, а не повод делать вид, что всё хорошо.
    return web.json_response(
        {"ok": False, "error_code": 404,
         "description": f"fake Telegram API: метод {method} не реализован"},
        status=404)


# ------------------------------------------------------------ Control API

async def control(request: web.Request) -> web.Response:
    action = request.match_info["action"]

    if action == "health":
        return web.json_response({"ok": True, "queued": len(STORE.updates), "sent": len(STORE.outbox)})

    if action == "say":
        payload = await request.json()
        uid = int(payload["user_id"])
        STORE.next_message_id += 1
        message = {
            "message_id": STORE.next_message_id,
            "date": now_ts(),
            "chat": {"id": uid, "type": "private"},
            "from": user_obj(uid, payload.get("name")),
            "text": payload["text"],
        }
        await STORE.add_update({"message": message})
        return web.json_response({"ok": True})

    if action == "command":
        payload = await request.json()
        uid = int(payload["user_id"])
        STORE.next_message_id += 1
        text = payload["command"]
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        message = {
            "message_id": STORE.next_message_id,
            "date": now_ts(),
            "chat": {"id": uid, "type": "private"},
            "from": user_obj(uid, payload.get("name")),
            "text": text,
            "entities": entities,
        }
        await STORE.add_update({"message": message})
        return web.json_response({"ok": True})

    if action == "tap":
        payload = await request.json()
        uid = int(payload["user_id"])
        STORE.next_message_id += 1
        origin = {
            "message_id": STORE.next_message_id,
            "date": now_ts(),
            "chat": {"id": uid, "type": "private"},
            "from": BOT_USER,
            "text": payload.get("origin_text", "…"),
        }
        await STORE.add_update({"callback_query": {
            "id": str(STORE.next_update_id),
            "from": user_obj(uid, payload.get("name")),
            "chat_instance": "ci",
            "data": payload["data"],
            "message": origin,
        }})
        return web.json_response({"ok": True})

    if action == "document":
        payload = await request.json()
        uid = int(payload["user_id"])
        STORE.next_message_id += 1
        message = {
            "message_id": STORE.next_message_id,
            "date": now_ts(),
            "chat": {"id": uid, "type": "private"},
            "from": user_obj(uid, payload.get("name")),
            "document": {
                "file_id": "f1",
                "file_unique_id": "uniq1",
                "file_name": payload.get("file_name", "guide.pdf"),
                "file_size": len(SAMPLE_PDF),
                "mime_type": "application/pdf",
            },
        }
        await STORE.add_update({"message": message})
        return web.json_response({"ok": True})

    if action == "photo":
        payload = await request.json()
        uid = int(payload["user_id"])
        STORE.next_message_id += 1
        message = {
            "message_id": STORE.next_message_id,
            "date": now_ts(),
            "chat": {"id": uid, "type": "private"},
            "from": user_obj(uid, payload.get("name")),
            "photo": [{
                "file_id": "photo1",
                "file_unique_id": "photo-uniq1",
                "file_size": 48_000,
                "width": 1280,
                "height": 960,
            }],
        }
        await STORE.add_update({"message": message})
        return web.json_response({"ok": True})

    if action == "outbox":
        uid = request.query.get("user_id")
        since = int(request.query.get("since") or 0)
        items = [m for m in STORE.outbox if since < m["at"] or True]
        if uid:
            items = [m for m in items if str(m["chat_id"]) == str(uid)]
        return web.json_response({"ok": True, "items": items[since:]})

    if action == "reset":
        STORE.outbox.clear()
        return web.json_response({"ok": True})

    return web.json_response({"ok": False, "error": "unknown action"}, status=404)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", api)
    app.router.add_get("/file/bot{token}/{path:.*}", serve_file)
    app.router.add_post("/ctl/{action}", control)
    app.router.add_get("/ctl/{action}", control)
    return app


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--control", type=int, default=8082)
    parser.add_argument("--transcript", default="", help="куда сохранить переписку при Ctrl+C")
    args = parser.parse_args()

    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    control_app = web.Application()
    control_app.router.add_post("/ctl/{action}", control)
    control_app.router.add_get("/ctl/{action}", control)
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    await web.TCPSite(control_runner, "127.0.0.1", args.control).start()
    print(f"Bot API:  http://127.0.0.1:{args.port}")
    print(f"Control:  http://127.0.0.1:{args.control}")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if args.transcript:
            Path(args.transcript).write_text(
                json.dumps(STORE.outbox, ensure_ascii=False, indent=1), encoding="utf-8")
        await runner.cleanup()
        await control_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
