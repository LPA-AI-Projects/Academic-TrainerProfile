from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _uses_external_pooler(url: str) -> bool:
    """
    Supabase pooler / pgBouncer in transaction mode: use NullPool so each checkout gets a
    fresh connection (mirrors the asyncpg pattern; sync driver uses psycopg2, not asyncpg).
    """
    u = (url or "").lower()
    return "pooler.supabase.com" in u or "pgbouncer=true" in u


def _create_engine():
    settings = get_settings()
    url = settings.database_url
    if _uses_external_pooler(url):
        return create_engine(
            url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=900,
    )


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
