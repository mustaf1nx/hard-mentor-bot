from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./mentors.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
