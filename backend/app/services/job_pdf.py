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
    force: bool = False,
) -> Path:
    """
    Ensure `storage/pdfs/{job_id}.pdf` exists for a completed job.

    When ``force=True``, always re-render (e.g. after refine) instead of reusing an existing file.

    Returns the absolute PDF path.
    """
    if job.status != "completed" or job.generated_profile is None:
        raise ValueError("Job is not ready for PDF export")

    target = job_pdf_abs_path(job.id)
    if not force and target.is_file() and target.stat().st_size > 0:
        return target

    if force and target.is_file():
        try:
            target.unlink()
            logger.info("PDF_FORCE_REMOVE_OLD job_id=%s path=%s", job.id, str(target))
        except OSError as exc:
            logger.warning("PDF_FORCE_REMOVE_FAILED job_id=%s path=%s err=%s", job.id, str(target), exc)

    logger.info("Generating PDF file job_id=%s path=%s force=%s", job.id, str(target), force)
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
