from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("trainer_profile.db")


def apply_light_migrations(engine: Engine) -> None:
    """
    Lightweight, dev-friendly migrations for small additive schema changes.

    For production, prefer Alembic migrations. This exists so local Postgres/SQLite
    environments don't break when new columns are added.
    """
    inspector = inspect(engine)
    if not inspector.has_table("trainer_profile_jobs"):
        return

    columns = {col["name"] for col in inspector.get_columns("trainer_profile_jobs")}
    dialect = engine.dialect.name

    statements: list[str] = []
    if "pdf_path" not in columns:
        statements.append("ALTER TABLE trainer_profile_jobs ADD COLUMN pdf_path TEXT")
    if "pdf_bytes" not in columns:
        statements.append("ALTER TABLE trainer_profile_jobs ADD COLUMN pdf_bytes INTEGER")
    if "pdf_generated_at" not in columns:
        if dialect == "sqlite":
            statements.append("ALTER TABLE trainer_profile_jobs ADD COLUMN pdf_generated_at DATETIME")
        else:
            statements.append("ALTER TABLE trainer_profile_jobs ADD COLUMN pdf_generated_at TIMESTAMP")
    if "pdf_generation_error" not in columns:
        statements.append("ALTER TABLE trainer_profile_jobs ADD COLUMN pdf_generation_error TEXT")

    if not statements:
        return

    logger.info("Applying lightweight DB migrations: %s", "; ".join(statements))
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
