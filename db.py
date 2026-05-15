"""Database engine + session factory.

Resolution priority:
  1. DATABASE_URL  (full SQLAlchemy URL)
  2. DB_CONNECTION=mysql + DB_HOST/PORT/DATABASE/USERNAME/PASSWORD
  3. SQLite fallback (./scolapp_payments.db) for local dev
"""
import os
import re
import logging
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger("DmoneyAPI.db")


def _build_db_url() -> str:
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    conn = os.getenv("DB_CONNECTION", "").strip().lower()
    if conn == "mysql":
        user = os.getenv("DB_USERNAME", "root")
        pwd  = os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "127.0.0.1")
        port = os.getenv("DB_PORT", "3306")
        db   = os.getenv("DB_DATABASE", "school_payments")
        return f"mysql+pymysql://{user}:{quote_plus(pwd)}@{host}:{port}/{db}?charset=utf8mb4"

    return "sqlite:///./scolapp_payments.db"


DB_URL = _build_db_url()
_masked = re.sub(r"://([^:]+):([^@]*)@", r"://\1:***@", DB_URL)
logger.info(f"Database URL: {_masked}")

if DB_URL.startswith("sqlite"):
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DB_URL,
        pool_recycle=1800,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
