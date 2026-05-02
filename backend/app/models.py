from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class TrainerProfileJob(Base):
    __tablename__ = "trainer_profile_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    zoho_record_id: Mapped[str] = mapped_column(String(128), index=True)
    cv_path: Mapped[str] = mapped_column(Text())
    course_outline_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="anthropic")
    model_name: Mapped[str] = mapped_column(String(128), default="gpt-4.1-mini")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1")
    parsed_inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    generated_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_model_output: Mapped[str] = mapped_column(Text(), default="")
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    pdf_generation_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    pdf_bytes: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feedback_rating: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    feedback_comment: Mapped[str | None] = mapped_column(Text(), nullable=True)
    feedback_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
