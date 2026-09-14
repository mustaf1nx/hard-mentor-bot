import asyncio
import os

import uvicorn

from database import init_db
from bot import run_bot
from admin_app import app as admin_app


async def run_admin():
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(admin_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    init_db()
    # Бот (long polling) и веб-админка работают параллельно в одном процессе.
    await asyncio.gather(run_bot(), run_admin())


if __name__ == "__main__":
    asyncio.run(main())
