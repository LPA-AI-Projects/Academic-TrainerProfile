import logging
import random
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import TrainerProfileJob
from ..schemas import GenerateProfileRequest
from .file_parser import read_text_from_path, truncate_inputs
from .job_pdf import ensure_job_pdf_on_disk
from .llm_client import generate_profile_json
from .prompt_builder import build_prompt
from .zoho_service import download_crm_file_to_path

logger = logging.getLogger("trainer_profile.generate")

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _temp_cv_dir() -> Path:
    d = _BACKEND_DIR / "storage" / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_profile_payload(raw: dict) -> dict:
    csat_raw = raw.get("csat_score")
    batches_raw = raw.get("batches_delivered")
    try:
        csat = round(float(csat_raw), 1)
    except Exception:
        csat = round(random.uniform(4.5, 4.9), 1)
    csat = min(4.9, max(4.5, csat))
    try:
        batches = int(batches_raw)
    except Exception:
        batches = random.randint(10, 20)
    batches = min(20, max(10, batches))

    normalized = {
        "professional_titles": _as_string_list(raw.get("professional_titles")),
        "csat_score": csat,
        "batches_delivered": batches,
        "profile": str(raw.get("profile", "")).strip(),
        "programs_trained": _as_string_list(raw.get("programs_trained")),
        "training_delivered": _as_string_list(raw.get("training_delivered")),
        "education": _as_string_list(raw.get("education")),
        "professional_experience": _as_string_list(raw.get("professional_experience")),
        "core_competencies": _as_string_list(raw.get("core_competencies")),
        "certificates": _as_string_list(raw.get("certificates")),
        "awards_and_recognitions": _as_string_list(raw.get("awards_and_recognitions")),
        "board_experience": _as_string_list(raw.get("board_experience")),
        "key_skills": _as_string_list(raw.get("key_skills")),
    }
    if not normalized["training_delivered"]:
        normalized["training_delivered"] = _as_string_list(raw.get("board_experience"))
    if len(normalized["key_skills"]) < 10:
        complement = [
            *normalized["core_competencies"],
            *normalized["programs_trained"],
            "Stakeholder communication",
            "Learning facilitation",
        ]
        for skill in complement:
            if skill not in normalized["key_skills"]:
                normalized["key_skills"].append(skill)
            if len(normalized["key_skills"]) >= 10:
                break
    return normalized


def generate_and_store_profile(
    payload: GenerateProfileRequest, db: Session, *, public_base_url: str | None = None
) -> TrainerProfileJob:
    settings = get_settings()
    t0 = time.perf_counter()

    temp_zoho_path: Path | None = None
    cv_source = "zoho" if (payload.cv and payload.cv.strip()) else "local"
    logger.info(
        "GEN_START zoho_record_id=%s cv_source=%s outlines=%s provider=%s model=%s",
        payload.zoho_record_id,
        cv_source,
        len(payload.course_outline_paths),
        payload.provider or settings.default_provider,
        payload.model_name or settings.default_model,
    )
    try:
        if payload.cv and payload.cv.strip():
            zoho_id = payload.cv.strip()
            temp_zoho_path = download_crm_file_to_path(zoho_id, _temp_cv_dir())
            local_cv = str(temp_zoho_path)
            cv_path_stored = f"zoho://{zoho_id}"
            logger.info("GEN_CV_DOWNLOADED zoho_record_id=%s file_id=%s local_cv=%s", payload.zoho_record_id, zoho_id, local_cv)
        else:
            p = (payload.cv_path or "").strip()
            if not p:
                raise ValueError("cv_path is required when cv (Zoho file id) is not provided")
            local_cv = p
            cv_path_stored = local_cv
            logger.info("GEN_CV_LOCAL zoho_record_id=%s local_cv=%s", payload.zoho_record_id, local_cv)

        t_read = time.perf_counter()
        cv_text = read_text_from_path(local_cv)
        outlines = [read_text_from_path(path) for path in payload.course_outline_paths]
        cv_trimmed, outline_trimmed = truncate_inputs(cv_text, outlines)
        logger.info(
            "GEN_CV_PARSED path_used=%s cv_chars=%s outline_files=%s outline_chars=%s read_ms=%.1f",
            local_cv,
            len(cv_trimmed),
            len(outline_trimmed),
            sum(len(x) for x in outline_trimmed),
            (time.perf_counter() - t_read) * 1000,
        )
    finally:
        if temp_zoho_path is not None and temp_zoho_path.is_file():
            try:
                temp_zoho_path.unlink()
                logger.info("GEN_CV_TEMP_REMOVED path=%s", temp_zoho_path)
            except OSError as exc:
                logger.warning("GEN_CV_TEMP_REMOVE_FAILED path=%s error=%s", temp_zoho_path, exc)

    prompt = build_prompt(cv_trimmed, outline_trimmed)

    job = TrainerProfileJob(
        zoho_record_id=payload.zoho_record_id,
        cv_path=cv_path_stored,
        course_outline_paths=payload.course_outline_paths,
        provider=payload.provider or settings.default_provider,
        model_name=payload.model_name or settings.default_model,
        status="processing",
        prompt_version=payload.prompt_version,
        parsed_inputs={
            "cv_excerpt": cv_trimmed[:4000],
            "outline_count": len(outline_trimmed),
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("GEN_JOB_CREATED job_id=%s status=%s cv_path=%s", job.id, job.status, job.cv_path)

    try:
        t_llm = time.perf_counter()
        generated_json, resolved_provider, raw_output = generate_profile_json(
            prompt=prompt,
            provider=payload.provider,
            model_name=payload.model_name,
        )
        job.generated_profile = normalize_profile_payload(generated_json)
        job.provider = resolved_provider
        job.raw_model_output = raw_output
        job.status = "completed"
        job.pdf_generation_error = None
        logger.info(
            "GEN_LLM_DONE job_id=%s provider=%s model=%s llm_ms=%.1f raw_chars=%s",
            job.id,
            resolved_provider,
            job.model_name,
            (time.perf_counter() - t_llm) * 1000,
            len(raw_output or ""),
        )
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)
        logger.exception("GEN_LLM_FAILED zoho_record_id=%s", payload.zoho_record_id)

    db.add(job)
    db.commit()
    db.refresh(job)

    if job.status == "completed":
        public_base = (public_base_url or settings.public_base_url or "http://127.0.0.1:8000").rstrip("/")
        try:
            t_pdf = time.perf_counter()
            pdf_path = ensure_job_pdf_on_disk(db=db, job=job, public_base_url=public_base)
            logger.info(
                "GEN_PDF_DONE job_id=%s pdf_ms=%.1f pdf_path=%s",
                job.id,
                (time.perf_counter() - t_pdf) * 1000,
                str(pdf_path),
            )
        except Exception as exc:
            job.pdf_generation_error = str(exc)
            db.add(job)
            db.commit()
            db.refresh(job)
            logger.exception("GEN_PDF_FAILED job_id=%s error=%s", job.id, exc)

    logger.info(
        "GEN_DONE job_id=%s status=%s total_ms=%.1f pdf_error=%s",
        job.id,
        job.status,
        (time.perf_counter() - t0) * 1000,
        bool(job.pdf_generation_error),
    )
    return job
