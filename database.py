from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

# Бот и админка работают в одном процессе, но база — в Postgres, а не в файле
# на диске контейнера: так заявки переживают любой передеплой без волюмов.
# Для локальной разработки без Postgres под рукой падаем в SQLite-файл.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mentors.db")

# Railway (как и Heroku) отдаёт URL со схемой postgres://. SQLAlchemy ждёт
# диалект+драйвер, поэтому переписываем на postgresql+psycopg:// — это
# psycopg3 (пакет psycopg[binary] в requirements.txt), у него есть готовые
# wheel'ы под свежий Python, в отличие от psycopg2-binary.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Application(Base):
    """Заявка студента на хард-ментора по предмету."""

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)

    telegram_id = Column(Integer, nullable=False)
    telegram_username = Column(String, nullable=True)

    full_name = Column(String, nullable=False)
    course = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    comment = Column(Text, nullable=True)

    # new -> in_progress -> closed
    status = Column(String, default="new")
    assigned_mentor = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)
