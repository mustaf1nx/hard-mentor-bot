"""Настройки бота. Всё берётся из переменных окружения (Railway → Variables)."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Целевое время первого ответа по категориям, часы (раздел 6.3 спека).
SLA_HOURS: dict[str, int | None] = {
    "serious": 4,
    "complaint": 48,
    "question": 24,
    "suggestion": 48,
    "praise": None,
}


def _ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    parts = raw.replace(";", ",").replace(" ", ",").split(",")
    return frozenset(int(p) for p in parts if p.strip())


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def normalize_db_url(url: str) -> tuple[str, dict]:
    """Railway отдаёт postgresql://…, а SQLAlchemy+asyncpg нужен postgresql+asyncpg://…

    asyncpg не понимает параметр sslmode в строке — переносим его в connect_args.
    """
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        sslmode = query.pop("sslmode", None)
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = "require"
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, connect_args


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    db_connect_args: dict = field(default_factory=dict)
    coordinator_ids: frozenset[int] = frozenset()
    lead_ids: frozenset[int] = frozenset()
    lead_gets_all: bool = False
    daily_limit: int = 5
    timezone: str = "Asia/Almaty"
    report_weekday: int = 0  # 0 = понедельник
    report_hour: int = 9
    secret: bytes = b"dev-secret"

    @property
    def staff_ids(self) -> frozenset[int]:
        return self.coordinator_ids | self.lead_ids

    def role_for(self, telegram_id: int, db_role: str = "mentee") -> str:
        """Роли координатора и руководителя задаются только через env —
        их нельзя получить или потерять командой в чате."""
        if telegram_id in self.lead_ids:
            return "lead"
        if telegram_id in self.coordinator_ids:
            return "coordinator"
        return "mentor" if db_role == "mentor" else "mentee"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN не задан. Добавьте его в Railway → Variables.")
        raw_url = os.environ.get("DATABASE_URL", "").strip()
        if not raw_url:
            raise SystemExit(
                "DATABASE_URL не задан. В Railway добавьте Postgres и переменную "
                "DATABASE_URL = ${{Postgres.DATABASE_URL}}."
            )
        url, connect_args = normalize_db_url(raw_url)
        secret = os.environ.get("SECRET_KEY", "").encode() or hashlib.sha256(
            ("hm-feedback:" + token).encode()
        ).digest()
        return cls(
            bot_token=token,
            database_url=url,
            db_connect_args=connect_args,
            coordinator_ids=_ids(os.environ.get("COORDINATOR_IDS")),
            lead_ids=_ids(os.environ.get("LEAD_IDS")),
            lead_gets_all=_bool(os.environ.get("LEAD_GETS_ALL")),
            daily_limit=int(os.environ.get("DAILY_LIMIT", "5")),
            timezone=os.environ.get("TIMEZONE", "Asia/Almaty"),
            report_weekday=int(os.environ.get("REPORT_WEEKDAY", "0")),
            report_hour=int(os.environ.get("REPORT_HOUR", "9")),
            secret=secret,
        )
