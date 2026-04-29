import logging
import random
import re
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
from .zoho_service import download_crm_file_to_path, get_file_id_from_record_field

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


def _compact_list(items: list[str], *, max_items: int) -> list[str]:
    out: list[str] = []
    for item in items:
        compact = item.replace("\n", " ").strip()
        if compact and compact not in out:
            out.append(compact)
        if len(out) >= max_items:
            break
    return out


def _dedupe_list(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        compact = item.replace("\n", " ").strip()
        if compact and compact not in out:
            out.append(compact)
    return out


def _normalize_profile_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Keep model-provided paragraphing when present.
    if "\n\n" in text:
        return text
    # If model returns one long paragraph, split into two readable blocks
    # to match fixed-layout page flow in the HTML template.
    sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    if len(sentences) < 4:
        return text
    split_at = max(2, len(sentences) // 2)
    first = ". ".join(sentences[:split_at]).strip()
    second = ". ".join(sentences[split_at:]).strip()
    if first and not first.endswith("."):
        first += "."
    if second and not second.endswith("."):
        second += "."
    return f"{first}\n\n{second}".strip()


def _title_case(text: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split() if w)


def _derive_program_suggestions(raw: dict) -> list[str]:
    seeds = _dedupe_list(
        _as_string_list(raw.get("professional_titles"))
        + _as_string_list(raw.get("key_skills"))
        + _as_string_list(raw.get("core_competencies"))
        + _as_string_list(raw.get("training_delivered"))
        + _as_string_list(raw.get("professional_experience"))
    )
    suggestions: list[str] = []
    for seed in seeds:
        clean_seed = re.sub(r"[^A-Za-z0-9&/ +.-]", " ", seed)
        clean_seed = re.sub(r"\s+", " ", clean_seed).strip(" -|")
        if len(clean_seed) < 3:
            continue
        token_count = len(clean_seed.split())
        if 1 <= token_count <= 5:
            suggestions.append(_title_case(clean_seed))
        elif token_count > 5:
            suggestions.append(_title_case(" ".join(clean_seed.split()[:5])))
    return _dedupe_list(suggestions)


def _ensure_programs_count(raw: dict, programs: list[str], min_items: int = 20, max_items: int = 26) -> list[str]:
    out = _compact_list(programs, max_items=max_items)
    if len(out) >= min_items:
        return out

    inferred = _derive_program_suggestions(raw)
    for item in inferred:
        if item not in out:
            out.append(item)
        if len(out) >= min_items:
            return out

    # Last-resort neutral CV-aligned expansions when raw model output is sparse.
    cv_aligned_fallback = _dedupe_list(
        _as_string_list(raw.get("core_competencies"))
        + _as_string_list(raw.get("key_skills"))
        + _as_string_list(raw.get("professional_titles"))
    )
    for item in cv_aligned_fallback:
        candidate = _title_case(re.sub(r"\s+", " ", item).strip())
        if candidate and candidate not in out:
            out.append(candidate)
        if len(out) >= min_items:
            break
    return _compact_list(out, max_items=max_items)


def _ensure_strengths_count(raw: dict, min_items: int = 10, max_items: int = 11) -> list[str]:
    primary = _dedupe_list(_as_string_list(raw.get("key_skills")))
    secondary = _dedupe_list(_as_string_list(raw.get("core_competencies")))
    tertiary = _dedupe_list(_as_string_list(raw.get("professional_titles")))
    out = _compact_list(primary + [x for x in secondary + tertiary if x not in primary], max_items=max_items)
    if len(out) >= min_items:
        return out

    # If still short, reuse CV-derived signals without truncating sentence content.
    cv_signals = _dedupe_list(
        _as_string_list(raw.get("training_delivered"))
        + _as_string_list(raw.get("professional_experience"))
    )
    for item in cv_signals:
        label = re.sub(r"\s+", " ", str(item or "")).strip(" -|,;")
        if label and label not in out:
            out.append(label)
        if len(out) >= min_items:
            break
    return _compact_list(out, max_items=max_items)


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

    professional_titles = _dedupe_list(_as_string_list(raw.get("professional_titles")))
    programs_trained = _ensure_programs_count(
        raw,
        _dedupe_list(_as_string_list(raw.get("programs_trained"))),
        min_items=20,
        max_items=26,
    )
    training_delivered = _compact_list(_as_string_list(raw.get("training_delivered")), max_items=16)
    professional_experience = _dedupe_list(_as_string_list(raw.get("professional_experience")))
    key_skills = _ensure_strengths_count(raw, min_items=10, max_items=11)
    awards_and_recognitions = _compact_list(_as_string_list(raw.get("awards_and_recognitions")), max_items=6)
    certificates = _compact_list(_as_string_list(raw.get("certificates")), max_items=6)

    bio_para1 = str(raw.get("bio_para1") or "").strip()
    bio_para2 = str(raw.get("bio_para2") or "").strip()
    combined_profile = _normalize_profile_text(raw.get("profile", ""))
    if bio_para1 or bio_para2:
        profile_text = f"{bio_para1}\n\n{bio_para2}".strip()
    else:
        profile_text = combined_profile

    normalized = {
        "professional_titles": professional_titles,
        "csat_score": csat,
        "batches_delivered": batches,
        "profile": profile_text,
        "programs_trained": programs_trained,
        "training_delivered": training_delivered,
        "education": _as_string_list(raw.get("education")),
        "professional_experience": professional_experience,
        "core_competencies": _as_string_list(raw.get("core_competencies")),
        "certificates": certificates,
        "awards_and_recognitions": awards_and_recognitions,
        "board_experience": _as_string_list(raw.get("board_experience")),
        "key_skills": key_skills,
    }
    if not normalized["training_delivered"]:
        normalized["training_delivered"] = _compact_list(_as_string_list(raw.get("board_experience")), max_items=16)
    return normalized


def generate_and_store_profile(
    payload: GenerateProfileRequest, db: Session, *, public_base_url: str | None = None
) -> TrainerProfileJob:
    settings = get_settings()
    t0 = time.perf_counter()

    temp_zoho_paths: list[Path] = []
    stored_outline_refs: list[str] = list(payload.course_outline_paths)
    logger.info(
        "GEN_START zoho_record_id=%s cv_present=%s cv_path_present=%s outline_paths=%s provider=%s model=%s",
        payload.zoho_record_id,
        bool(payload.cv and payload.cv.strip()),
        bool(payload.cv_path and payload.cv_path.strip()),
        len(payload.course_outline_paths),
        payload.provider or settings.default_provider,
        payload.model_name or settings.default_model,
    )
    try:
        mod = (settings.zoho_module_api_name or "").strip()
        cv_field = (settings.zoho_cv_field_api_name or "").strip()
        outline_field = (settings.zoho_outline_field_api_name or "").strip()

        if payload.cv and payload.cv.strip():
            zoho_id = payload.cv.strip()
            p = download_crm_file_to_path(zoho_id, _temp_cv_dir())
            temp_zoho_paths.append(p)
            local_cv = str(p)
            cv_path_stored = f"zoho://{zoho_id}"
            logger.info(
                "GEN_CV_DOWNLOADED zoho_record_id=%s file_id=%s local_cv=%s",
                payload.zoho_record_id,
                zoho_id,
                local_cv,
            )
        elif (payload.cv_path or "").strip():
            local_cv = (payload.cv_path or "").strip()
            cv_path_stored = local_cv
            logger.info("GEN_CV_LOCAL zoho_record_id=%s local_cv=%s", payload.zoho_record_id, local_cv)
        elif mod and cv_field:
            rid = (payload.zoho_record_id or "").strip()
            file_id = get_file_id_from_record_field(mod, rid, cv_field)
            if not file_id:
                raise ValueError(
                    f"No CV file id found in Zoho CRM module={mod!r} record={rid!r} field={cv_field!r}"
                )
            p = download_crm_file_to_path(file_id, _temp_cv_dir())
            temp_zoho_paths.append(p)
            local_cv = str(p)
            cv_path_stored = f"zoho://record/{mod}/{cv_field}/{file_id}"
            logger.info(
                "GEN_CV_FROM_RECORD zoho_record_id=%s module=%s field=%s file_id=%s path=%s",
                rid,
                mod,
                cv_field,
                file_id,
                local_cv,
            )
        else:
            raise ValueError(
                "Provide 'cv' (Zoho file id), 'cv_path' (local path), or set "
                "ZOHO_MODULE_API_NAME and ZOHO_CV_FIELD_API_NAME to load the CV from CRM using zoho_record_id."
            )

        outline_read_paths: list[str] = list(payload.course_outline_paths)
        if not outline_read_paths and mod and outline_field:
            rid = (payload.zoho_record_id or "").strip()
            oid = get_file_id_from_record_field(mod, rid, outline_field)
            if oid:
                op = download_crm_file_to_path(oid, _temp_cv_dir())
                temp_zoho_paths.append(op)
                outline_read_paths.append(str(op))
                stored_outline_refs = [f"zoho://record/{mod}/{outline_field}/{oid}"]
                logger.info(
                    "GEN_OUTLINE_FROM_RECORD zoho_record_id=%s module=%s field=%s file_id=%s",
                    rid,
                    mod,
                    outline_field,
                    oid,
                )
            else:
                logger.info(
                    "GEN_OUTLINE_SKIPPED zoho_record_id=%s field=%s (empty or no file id)",
                    rid,
                    outline_field,
                )

        t_read = time.perf_counter()
        cv_text = read_text_from_path(local_cv)
        outlines = [read_text_from_path(path) for path in outline_read_paths]
        cv_trimmed, outline_trimmed = truncate_inputs(cv_text, outlines)
        logger.info(
            "GEN_CV_PARSED path_used=%s cv_chars=%s outline_files=%s outline_chars=%s read_ms=%.1f",
            local_cv,
            len(cv_trimmed),
            len(outline_read_paths),
            sum(len(x) for x in outline_trimmed),
            (time.perf_counter() - t_read) * 1000,
        )
    finally:
        for temp_zoho_path in temp_zoho_paths:
            if temp_zoho_path.is_file():
                try:
                    temp_zoho_path.unlink()
                    logger.info("GEN_TEMP_REMOVED path=%s", temp_zoho_path)
                except OSError as exc:
                    logger.warning("GEN_TEMP_REMOVE_FAILED path=%s error=%s", temp_zoho_path, exc)

    prompt = build_prompt(cv_trimmed, outline_trimmed)

    job = TrainerProfileJob(
        zoho_record_id=payload.zoho_record_id,
        cv_path=cv_path_stored,
        course_outline_paths=stored_outline_refs,
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
