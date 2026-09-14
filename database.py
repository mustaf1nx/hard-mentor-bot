import os
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

# На Railway бот и админка — это два разных сервиса (два разных контейнера),
# поэтому локальный файл SQLite между ними не расшарить. Подключаем Postgres
# через DATABASE_URL, который Railway сам подставит, если в проект добавлен
# плагин Postgres и переменная указана на обоих сервисах.
# Для локальной разработки без Railway используется файл mentors.db.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mentors.db")

# Railway (как и Heroku) отдаёт URL со схемой postgres://, а SQLAlchemy 2.x
# требует postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

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
