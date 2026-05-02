from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import TrainerProfileJob
from ..utils.logger import get_logger
from .profile_pdf import render_trainer_profile_pdf

logger = get_logger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]  # .../trainer-profile/backend


def job_pdf_filename(job_id: str) -> str:
    return f"{job_id}.pdf"


def job_pdf_abs_path(job_id: str) -> Path:
    settings = get_settings()
    root = Path(settings.pdf_storage_dir)
    if not root.is_absolute():
        # Anchor to backend/ so PDF paths don't depend on process cwd.
        root = _BACKEND_DIR / root
    root.mkdir(parents=True, exist_ok=True)
    return root / job_pdf_filename(job_id)


async def ensure_job_pdf_on_disk(
    *,
    db: Session,
    job: TrainerProfileJob,
    public_base_url: str,
) -> Path:
    """
    Ensure `storage/pdfs/{job_id}.pdf` exists for a completed job.

    Returns the absolute PDF path.
    """
    if job.status != "completed" or not job.generated_profile:
        raise ValueError("Job is not ready for PDF export")

    target = job_pdf_abs_path(job.id)
    if target.is_file() and target.stat().st_size > 0:
        return target

    logger.info("Generating PDF file job_id=%s path=%s", job.id, str(target))
    pdf_bytes = await render_trainer_profile_pdf(public_base_url=public_base_url, job_id=job.id)

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(pdf_bytes)
    os.replace(tmp, target)

    job.pdf_path = str(target)
    job.pdf_bytes = len(pdf_bytes)
    job.pdf_generated_at = datetime.utcnow()
    db.add(job)
    db.commit()
    db.refresh(job)

    logger.info(
        "Saved PDF job_id=%s bytes=%s path=%s",
        job.id,
        len(pdf_bytes),
        str(target),
    )
    return target
