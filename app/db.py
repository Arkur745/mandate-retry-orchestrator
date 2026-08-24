"""SQLAlchemy engine/session setup, config sourced from .env via pydantic-settings."""
from collections.abc import Generator

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./orchestrator.db"
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    time_scale: float = 1.0  # 1.0 = real-time; >1.0 = fast-forward (see app/executor.py Clock)


settings = Settings()

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
