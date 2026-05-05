import asyncio
import random
import re
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..utils.logger import get_logger
from ..models import TrainerProfileJob
from ..schemas import GenerateProfileRequest
from .file_parser import read_text_from_path, truncate_inputs
from .google_drive_service import GoogleDriveUploadError, upload_trainer_profile_pdf
from .job_pdf import ensure_job_pdf_on_disk, job_pdf_abs_path
from .llm_client import generate_profile_json
from .prompt_builder import build_prompt
from .zoho_service import (
    attach_crm_v8_attachment_link,
    delete_crm_record_attachment,
    list_crm_record_attachments,
    download_crm_file_to_path,
    extract_file_id_from_zoho_field,
    extract_multiselect_lookup_ids,
    fetch_crm_record,
    format_zoho_field_debug,
    get_file_id_from_record_field,
    get_scalar_field_str,
    search_crm_record_ids_by_field,
)

logger = get_logger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _google_drive_oauth_ready(settings: object) -> bool:
    return bool(
        (getattr(settings, "google_client_id", None) or "").strip()
        and (getattr(settings, "google_client_secret", None) or "").strip()
        and (getattr(settings, "google_refresh_token", None) or "").strip()
    )


def _google_drive_oauth_missing_env_names(settings: object) -> list[str]:
    out: list[str] = []
    if not (getattr(settings, "google_client_id", None) or "").strip():
        out.append("GOOGLE_CLIENT_ID")
    if not (getattr(settings, "google_client_secret", None) or "").strip():
        out.append("GOOGLE_CLIENT_SECRET")
    if not (getattr(settings, "google_refresh_token", None) or "").strip():
        out.append("GOOGLE_REFRESH_TOKEN")
    return out


def _job_trainer_unique_for_drive(job: TrainerProfileJob) -> str:
    parsed = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    return (str(parsed.get("trainer_unique_code") or "").strip() or "trainer")[:120]


def trainer_unique_lookup_base(value: str) -> str:
    """Strip trailing ``_vN`` so ``TR2002_v2`` matches stored ``TR2002`` for job lookup."""
    return re.sub(r"_v\d+$", "", (value or "").strip(), flags=re.IGNORECASE)


def _next_trainer_pdf_attachment_title(
    *, unique_from_job: str, module_api_name: str, crm_record_id: str
) -> str:
    """
    Zoho attachment display name: ``{Trainer_Unique_Code}_vN``.

    ``N`` is ``max(existing N for this base on the CRM row) + 1``, or **2** when none exist.
    Each refine/generate adds a new higher version (e.g. ``TR2002_v2`` then ``TR2002_v3``; refining with
    ``title`` ``TR2002_v2`` still picks the same job but the new file becomes the next ``_vN``).
    """
    raw = re.sub(r"[^A-Za-z0-9_-]+", "", (unique_from_job or "").strip())
    base = re.sub(r"_v\d+$", "", raw, flags=re.IGNORECASE)
    if not base:
        base = "trainer"
    base = base[:120]

    rows: list[dict] = []
    try:
        rows = list_crm_record_attachments(
            module_api_name=module_api_name, crm_record_id=crm_record_id
        )
    except Exception as exc:
        logger.warning(
            "ZOHO_ATTACH_TITLE_LIST_FAIL module=%s record_id=%s err=%s fallback_v2",
            module_api_name,
            crm_record_id,
            exc,
        )
        return f"{base}_v2"[:255]

    def _stem(fn: object) -> str:
        s = str(fn or "").strip()
        if s.lower().endswith(".pdf"):
            s = s[:-4]
        return s.strip()

    pat = re.compile(rf"^{re.escape(base)}_v(\d+)$", re.IGNORECASE)
    versions: list[int] = []
    for row in rows:
        name = row.get("File_Name") or row.get("file_name") or row.get("$file_name")
        stem = _stem(name)
        m = pat.match(stem)
        if not m:
            continue
        try:
            versions.append(int(m.group(1)))
        except ValueError:
            continue

    next_v = max(versions) + 1 if versions else 2
    return f"{base}_v{next_v}"[:255]


def _job_drive_course_name(job: TrainerProfileJob) -> str:
    settings = get_settings()
    parsed = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    v = parsed.get("drive_course_name")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return (settings.google_drive_fallback_course_name or "Course").strip()


def trainer_unique_lookup_base(value: str) -> str:
    """Strip optional ``_vN`` suffix for comparing Trainer_Unique_Code (e.g. ``TR2001_v2`` → ``TR2001``)."""
    return re.sub(r"_v\d+$", "", (value or "").strip(), flags=re.IGNORECASE)


def parse_trainer_field_explicit_version(value: str | None) -> int | None:
    """If ``value`` ends with ``_vN``, return ``N``; else ``None`` (caller treats as *latest slot*)."""
    s = (value or "").strip()
    m = re.search(r"_v(\d+)$", s, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        n = int(m.group(1))
        return n if n >= 1 else None
    except ValueError:
        return None


def _zoho_trainer_pdf_attachment_title(
    *, unique: str, module_api_name: str, crm_record_id: str, job: TrainerProfileJob
) -> str:
    """
    Zoho CRM attachment ``title``: ``{Trainer_Unique_Code}_vN``.

    - **Latest** (no ``zoho_pdf_attachment_explicit_v`` on the job): reuse the highest existing ``N`` for this
      code on the record, or **v2** when none exist; any previous file with that stem is deleted then re-uploaded
      (replace-in-place naming).
    - **Explicit** (set on refine when ``unique_code`` / ``title`` includes ``_vN``): use that **N**; delete
      matching attachment(s) with the same stem, then upload again (e.g. re-publish ``TR2001_v2``).
    """
    pi = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    raw_ev = pi.get("zoho_pdf_attachment_explicit_v")
    explicit_v: int | None = None
    if raw_ev is not None:
        try:
            explicit_v = int(raw_ev)
        except (TypeError, ValueError):
            explicit_v = None
        if explicit_v is not None and explicit_v < 1:
            explicit_v = None

    raw = re.sub(r"[^A-Za-z0-9_-]+", "", (unique or "").strip())
    base = re.sub(r"_v\d+$", "", raw, flags=re.IGNORECASE)
    if not base:
        base = "trainer"
    base = base[:120]

    rows: list[dict] = []
    try:
        rows = list_crm_record_attachments(
            module_api_name=module_api_name, crm_record_id=crm_record_id
        )
    except Exception as exc:
        logger.warning(
            "ZOHO_ATTACH_TITLE_LIST_FAIL module=%s record_id=%s err=%s fallback_v2",
            module_api_name,
            crm_record_id,
            exc,
        )
        return f"{base}_v2"[:255]

    def _stem(fn: object) -> str:
        s = str(fn or "").strip()
        if s.lower().endswith(".pdf"):
            s = s[:-4]
        return s.strip()

    pat = re.compile(rf"^{re.escape(base)}_v(\d+)$", re.IGNORECASE)
    versions: list[int] = []
    for row in rows:
        stem = _stem(row.get("File_Name"))
        m = pat.match(stem)
        if m:
            try:
                versions.append(int(m.group(1)))
            except ValueError:
                continue

    if explicit_v is not None:
        target_v = explicit_v
    else:
        target_v = max(versions) if versions else 2

    slot = f"{base}_v{target_v}"
    for row in rows:
        stem = _stem(row.get("File_Name"))
        if stem.lower() != slot.lower():
            continue
        aid = row.get("id") or row.get("Id")
        if not aid:
            continue
        try:
            delete_crm_record_attachment(
                module_api_name=module_api_name,
                crm_record_id=crm_record_id,
                attachment_id=str(aid).strip(),
            )
        except Exception as exc:
            logger.warning(
                "ZOHO_ATTACH_DELETE_OLD_FAIL module=%s record_id=%s attachment_id=%s stem=%s err=%s",
                module_api_name,
                crm_record_id,
                aid,
                stem,
                exc,
            )
    return slot[:255]


async def _maybe_google_drive_upload_after_pdf(job: TrainerProfileJob, db: Session) -> None:
    """
    When `google_drive_auto_upload` is true (default) and Google OAuth env vars are set,
    upload the saved PDF and store `google_drive_pdf_url` / `google_drive_upload_error` on `parsed_inputs`.
    Set GOOGLE_DRIVE_AUTO_UPLOAD=false to disable.
    """
    settings = get_settings()
    if not settings.google_drive_auto_upload:
        logger.info("GEN_DRIVE_SKIP disabled=1 reason=GOOGLE_DRIVE_AUTO_UPLOAD=false")
        return
    if not _google_drive_oauth_ready(settings):
        missing = _google_drive_oauth_missing_env_names(settings)
        logger.info(
            "GEN_DRIVE_SKIP oauth_incomplete missing_env=%s (set all three on the server for uploads)",
            ",".join(missing),
        )
        return
    path = job_pdf_abs_path(job.id)
    if not path.is_file() or path.stat().st_size <= 0:
        logger.warning("GEN_DRIVE_SKIP missing_pdf job_id=%s path=%s", job.id, path)
        return
    drive_link: str | None = None
    drive_err: str | None = None
    logger.info(
        "GEN_DRIVE_START job_id=%s unique=%s course_folder=%s",
        job.id,
        _job_trainer_unique_for_drive(job),
        _job_drive_course_name(job),
    )
    try:
        result = await asyncio.to_thread(
            upload_trainer_profile_pdf,
            pdf_bytes=path.read_bytes(),
            unique_code=_job_trainer_unique_for_drive(job),
            course_name=_job_drive_course_name(job),
        )
        drive_link = str(result.get("view_link") or "").strip() or None
        logger.info("GEN_DRIVE_OK job_id=%s view_link=%s", job.id, drive_link or "")
    except GoogleDriveUploadError as exc:
        drive_err = str(exc)
        logger.exception("GEN_DRIVE_FAILED job_id=%s", job.id)

    pi = dict(job.parsed_inputs) if isinstance(job.parsed_inputs, dict) else {}
    if drive_link:
        pi["google_drive_pdf_url"] = drive_link
        pi.pop("google_drive_upload_error", None)
    if drive_err:
        pi["google_drive_upload_error"] = drive_err
    job.parsed_inputs = pi
    db.add(job)
    db.commit()
    db.refresh(job)


async def maybe_google_drive_upload_after_pdf(job: TrainerProfileJob, db: Session) -> None:
    """Public hook for routes that save a PDF outside `_complete_job_after_prompt` (e.g. refine)."""
    await _maybe_google_drive_upload_after_pdf(job, db)


def _resolve_zoho_pdf_attachment_module_and_record(job: TrainerProfileJob) -> tuple[str, str] | None:
    """
    Choose CRM module + record id for the Attachments API.

    Parent flow: by default attach to the parent row (webhook record) using ``parent_module`` / env.
    Set ``ZOHO_ATTACH_PDF_TO_PARENT_RECORD=false`` to attach on the Trainers row instead.
    """
    settings = get_settings()
    pi = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}

    parent_rid = str(pi.get("parent_record_id") or "").strip()
    parent_mod_stored = str(pi.get("parent_module") or "").strip()
    parent_mod_cfg = (settings.zoho_parent_module_api_name or "").strip()

    if settings.zoho_attach_pdf_to_parent_record and parent_rid:
        mod = (parent_mod_stored or parent_mod_cfg).strip()
        if mod:
            return mod, parent_rid
        logger.warning(
            "ZOHO_ATTACH_PDF parent_record_id=%s but parent module name missing; "
            "falling back to trainer row if available (set ZOHO_PARENT_MODULE_API_NAME)",
            parent_rid,
        )

    tid = pi.get("trainer_record_id")
    if tid is not None and str(tid).strip():
        mod = (settings.zoho_trainer_pdf_attach_module_api_name or "").strip() or (
            settings.zoho_trainer_module_api_name or "Trainers"
        ).strip()
        return mod, str(tid).strip()

    rid = (job.zoho_record_id or "").strip()
    if not rid:
        return None
    mod = (settings.zoho_trainer_pdf_attach_module_api_name or "").strip() or (
        settings.zoho_module_api_name or settings.zoho_trainer_module_api_name or "Trainers"
    ).strip()
    return mod, rid


async def _maybe_zoho_attach_trainer_pdf_link(
    job: TrainerProfileJob, db: Session, *, public_base_url: str
) -> None:
    """
    If ``ZOHO_ATTACH_TRAINER_PDF_LINK=true`` and OAuth is configured, POST an ``attachmentUrl`` to Zoho CRM v8
    Attachments. Prefers ``google_drive_pdf_url`` when ``ZOHO_ATTACH_TRAINER_PDF_PREFER_GOOGLE_DRIVE_URL=true``
    (after Drive upload). Otherwise uses public ``/pdfs/{job_id}.pdf``.

    Target record: parent (webhook) vs trainer — see ``ZOHO_ATTACH_PDF_TO_PARENT_RECORD`` and
    ``_resolve_zoho_pdf_attachment_module_and_record``. Set ``ZOHO_PARENT_MODULE_API_NAME`` to your parent API
    name (e.g. ``Closure_Activities`` or ``Closure_Activity``).
    """
    settings = get_settings()
    if not settings.zoho_attach_trainer_pdf_link:
        logger.info(
            "ZOHO_ATTACH_PDF_SKIP feature_off=1 job_id=%s (set ZOHO_ATTACH_TRAINER_PDF_LINK=true to POST PDF to CRM)",
            job.id,
        )
        return
    db.refresh(job)
    oauth_ok = bool(
        (settings.zoho_refresh_token or "").strip()
        and (settings.zoho_client_id or "").strip()
        and (settings.zoho_client_secret or "").strip()
    ) or bool((settings.zoho_access_token or "").strip() and not (settings.zoho_refresh_token or "").strip())
    if not oauth_ok:
        logger.info(
            "ZOHO_ATTACH_PDF_SKIP oauth_incomplete (set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN "
            "or a static ZOHO_ACCESS_TOKEN)"
        )
        return

    pi_live = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    drive_url = str(pi_live.get("google_drive_pdf_url") or "").strip()

    base = (public_base_url or settings.public_base_url or "http://127.0.0.1:8080").rstrip("/")
    pdfs_url = f"{base}/pdfs/{job.id}.pdf"

    use_drive = bool(settings.zoho_attach_trainer_pdf_prefer_google_drive_url and drive_url)
    if use_drive:
        attachment_url = drive_url
        logger.info("ZOHO_ATTACH_PDF using google_drive_pdf_url job_id=%s", job.id)
    else:
        path = job_pdf_abs_path(job.id)
        if not path.is_file() or path.stat().st_size <= 0:
            logger.warning("ZOHO_ATTACH_PDF_SKIP missing_pdf job_id=%s", job.id)
            return
        attachment_url = pdfs_url
    resolved = _resolve_zoho_pdf_attachment_module_and_record(job)
    if not resolved:
        logger.warning("ZOHO_ATTACH_PDF_SKIP could_not_resolve_module_record job_id=%s", job.id)
        return
    mod, crm_id = resolved
    unique = _job_trainer_unique_for_drive(job)
    title = await asyncio.to_thread(
        _zoho_trainer_pdf_attachment_title,
        unique=unique,
        module_api_name=mod,
        crm_record_id=crm_id,
        job=job,
    )

    err: str | None = None
    try:
        await asyncio.to_thread(
            attach_crm_v8_attachment_link,
            module_api_name=mod,
            crm_record_id=crm_id,
            public_url=attachment_url,
            title=title,
        )
        logger.info(
            "ZOHO_ATTACH_PDF_OK job_id=%s module=%s crm_record_id=%s parent_flow=%s source=%s",
            job.id,
            mod,
            crm_id,
            bool(
                isinstance(job.parsed_inputs, dict)
                and str((job.parsed_inputs or {}).get("parent_record_id") or "").strip()
            ),
            "google_drive" if use_drive else "pdfs",
        )
    except Exception as exc:
        err = str(exc)
        logger.exception("ZOHO_ATTACH_PDF_FAILED job_id=%s", job.id)

    pi = dict(job.parsed_inputs) if isinstance(job.parsed_inputs, dict) else {}
    pi["zoho_trainer_pdf_attachment_url"] = attachment_url
    pi["zoho_trainer_pdf_attachment_title"] = title
    pi["zoho_trainer_pdf_attachment_source"] = "google_drive" if use_drive else "pdfs"
    if err:
        pi["zoho_trainer_pdf_attachment_error"] = err
    else:
        pi.pop("zoho_trainer_pdf_attachment_error", None)
        pi["zoho_trainer_pdf_attachment_at"] = datetime.utcnow().isoformat() + "Z"
        pi.pop("zoho_pdf_attachment_explicit_v", None)
    job.parsed_inputs = pi
    db.add(job)
    db.commit()
    db.refresh(job)


async def maybe_zoho_attach_trainer_pdf_link(
    job: TrainerProfileJob, db: Session, *, public_base_url: str
) -> None:
    """Public hook for routes that save a PDF outside `_complete_job_after_prompt` (e.g. refine)."""
    await _maybe_zoho_attach_trainer_pdf_link(job, db, public_base_url=public_base_url)


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


def _program_merge_key(s: str) -> str:
    """Case- and whitespace-insensitive key for de-duplicating program lines."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _merge_programs_trained_priority(hints: list[str], model_programs: list[str]) -> list[str]:
    """Client hints first, then model list, dropping case/whitespace duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for h in _dedupe_list(_as_string_list(hints)):
        k = _program_merge_key(h)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(h)
    for p in model_programs:
        compact = str(p).replace("\n", " ").strip()
        k = _program_merge_key(compact)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(compact)
    return out


def _parse_multiline_programs_from_zoho(raw: str | None) -> list[str]:
    """Split Zoho multiline text into one program/title per non-empty line."""
    if not raw or not str(raw).strip():
        return []
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _merge_request_and_crm_program_hints(
    payload: GenerateProfileRequest,
    crm_lines: list[str] | None,
) -> list[str] | None:
    """Request ``programs_trained`` first, then CRM lines, deduped (case-insensitive key), max 40."""
    req = [str(x).replace("\n", " ").strip() for x in (payload.programs_trained or []) if str(x).strip()]
    crm = _dedupe_list([str(x).strip() for x in (crm_lines or []) if str(x).strip()])
    merged = _merge_programs_trained_priority(req, crm)
    if not merged:
        return None
    return _dedupe_list(merged)[:40]


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


def _truncate_list_line(s: str, max_len: int) -> str:
    t = str(s or "").strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    cut = t[: max_len - 1].rstrip(" ,;–-|")
    return cut + "…"


def _truncate_list_strings(items: list[str], max_len: int) -> list[str]:
    return [x for x in (_truncate_list_line(x, max_len) for x in items) if x]


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


def _ensure_programs_count(raw: dict, programs: list[str], min_items: int = 18, max_items: int = 24) -> list[str]:
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


def normalize_profile_payload(
    raw: dict, *, programs_trained_hints: list[str] | None = None
) -> dict:
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
    model_programs = _dedupe_list(_as_string_list(raw.get("programs_trained")))
    if programs_trained_hints:
        merged_programs = _merge_programs_trained_priority(programs_trained_hints, model_programs)
    else:
        merged_programs = model_programs
    programs_trained = _truncate_list_strings(
        _ensure_programs_count(
            raw,
            merged_programs,
            min_items=18,
            max_items=24,
        ),
        72,
    )
    training_delivered = _truncate_list_strings(
        _compact_list(_as_string_list(raw.get("training_delivered")), max_items=14),
        58,
    )
    professional_experience = _truncate_list_strings(
        _dedupe_list(_as_string_list(raw.get("professional_experience"))),
        96,
    )
    key_skills = _truncate_list_strings(_ensure_strengths_count(raw, min_items=10, max_items=11), 50)
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
        normalized["training_delivered"] = _truncate_list_strings(
            _compact_list(_as_string_list(raw.get("board_experience")), max_items=14),
            58,
        )
    return normalized


async def _complete_job_after_prompt(
    job: TrainerProfileJob,
    db: Session,
    payload: GenerateProfileRequest,
    public_base_url: str | None,
    prompt: str,
    t0: float,
    *,
    trainer_display_name: str | None = None,
    programs_trained_hints: list[str] | None = None,
) -> TrainerProfileJob:
    settings = get_settings()
    try:
        t_llm = time.perf_counter()
        generated_json, resolved_provider, raw_output = generate_profile_json(
            prompt=prompt,
            provider=payload.provider,
            model_name=payload.model_name,
        )
        hints = programs_trained_hints
        if hints is None:
            hints = list(payload.programs_trained) if payload.programs_trained else None
        gen = normalize_profile_payload(generated_json, programs_trained_hints=hints)
        # Heading on the CV: Zoho Trainer_Unique_code when provided (parent multi-trainer flow).
        if trainer_display_name and str(trainer_display_name).strip():
            gen["trainer_display_name"] = str(trainer_display_name).strip()[:40]
        job.generated_profile = gen
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
        public_base = (public_base_url or settings.public_base_url or "http://127.0.0.1:8080").rstrip("/")
        try:
            t_pdf = time.perf_counter()
            pdf_path = await ensure_job_pdf_on_disk(db=db, job=job, public_base_url=public_base)
            logger.info(
                "GEN_PDF_DONE job_id=%s pdf_ms=%.1f pdf_path=%s",
                job.id,
                (time.perf_counter() - t_pdf) * 1000,
                str(pdf_path),
            )
            await _maybe_google_drive_upload_after_pdf(job, db)
            await _maybe_zoho_attach_trainer_pdf_link(job, db, public_base_url=public_base)
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


def _parent_multi_trainer_enabled(settings: object) -> bool:
    # Trainer module, CV/code fields, outline + lookup field API names default in config; only parent module must be set.
    return bool((getattr(settings, "zoho_parent_module_api_name", None) or "").strip())


async def generate_from_parent_with_trainers(
    payload: GenerateProfileRequest, db: Session, *, public_base_url: str | None = None
) -> list[TrainerProfileJob]:
    """
    Parent record provides outline file + multi-select lookup to Trainers.
    For each linked trainer: Trainer_CV + Trainer_Unique_code with the same outline text.
    """
    settings = get_settings()
    parent_mod = (settings.zoho_parent_module_api_name or "").strip()
    outline_f = (settings.zoho_parent_outline_field_api_name or "").strip()
    lookup_f = (settings.zoho_parent_trainers_lookup_field_api_name or "").strip()
    trainer_mod = (settings.zoho_trainer_module_api_name or "").strip()
    cv_f = (settings.zoho_trainer_cv_field_api_name or "").strip()
    code_f = (settings.zoho_trainer_unique_code_field_api_name or "").strip()
    parent_id = (payload.zoho_record_id or "").strip()

    logger.info(
        "GEN_PARENT_START parent_module=%s parent_id=%s outline_field=%s lookup_field=%s "
        "trainer_module=%s trainer_cv_field=%s trainer_code_field=%s",
        parent_mod,
        parent_id,
        outline_f,
        lookup_f,
        trainer_mod,
        cv_f,
        code_f,
    )

    parent_record = fetch_crm_record(parent_mod, parent_id)
    parent_keys = sorted(parent_record.keys()) if isinstance(parent_record, dict) else []
    logger.info(
        "GEN_PARENT_RECORD_SUMMARY parent_id=%s field_name_count=%s field_names_head=%s",
        parent_id,
        len(parent_keys),
        parent_keys[:60],
    )

    outline_raw = parent_record.get(outline_f)
    outline_fid = extract_file_id_from_zoho_field(outline_raw)
    logger.info(
        "GEN_PARENT_OUTLINE_FIELD field=%s resolved_file_id=%s raw_type=%s raw_preview=%s",
        outline_f,
        outline_fid or "(none)",
        type(outline_raw).__name__,
        format_zoho_field_debug(outline_raw),
    )
    if not outline_fid:
        raise ValueError(
            f"No outline file on parent record module={parent_mod!r} id={parent_id!r} field={outline_f!r}"
        )

    lookup_raw = parent_record.get(lookup_f)
    logger.info(
        "TRAINERS_RAW_FULL field=%s value=%s type=%s",
        lookup_f,
        repr(lookup_raw),
        type(lookup_raw).__name__,
    )
    logger.info(
        "GEN_PARENT_TRAINERS_LOOKUP_FIELD field=%s raw_type=%s raw_preview=%s",
        lookup_f,
        type(lookup_raw).__name__,
        format_zoho_field_debug(lookup_raw),
    )
    trainer_ids = extract_multiselect_lookup_ids(lookup_raw)
    if (
        not trainer_ids
        and settings.zoho_trainer_lookup_resolve_by_name
        and isinstance(lookup_raw, str)
        and lookup_raw.strip()
    ):
        match_field = settings.zoho_trainer_search_field_api_name.strip()
        for part in [p.strip() for p in re.split(r"[,;]", lookup_raw) if p.strip()]:
            found: list[str] = []
            for op in ("equals", "starts_with"):
                found = search_crm_record_ids_by_field(
                    trainer_mod, match_field, part, operator=op
                )
                if found:
                    logger.info(
                        "GEN_PARENT_NAME_SEARCH_HIT part=%r operator=%s ids=%s",
                        part,
                        op,
                        found,
                    )
                    break
            for rid in found:
                if rid not in trainer_ids:
                    trainer_ids.append(rid)
        logger.info(
            "GEN_PARENT_TRAINER_IDS_FROM_NAME_SEARCH parent_id=%s match_field=%s resolved_count=%s ids=%s",
            parent_id,
            match_field,
            len(trainer_ids),
            trainer_ids,
        )
    if not trainer_ids:
        raise ValueError(
            f"No trainer record ids from parent field={lookup_f!r} on record={parent_id!r}. "
            "Zoho multi-select lookup fields return a list of {{id, name}} per CRM API Get Record. "
            "Your API returned "
            f"{type(lookup_raw).__name__} {lookup_raw!r}. "
            "Fix: use a multi-select lookup field to Trainers on the parent layout, "
            "or set ZOHO_TRAINER_LOOKUP_RESOLVE_BY_NAME=true and "
            "ZOHO_TRAINER_SEARCH_FIELD_API_NAME to a Trainers-module field to match this text "
            "(see Zoho Search Records API)."
        )
    logger.info(
        "GEN_PARENT_TRAINER_IDS parent_id=%s trainer_count=%s trainer_ids=%s",
        parent_id,
        len(trainer_ids),
        trainer_ids,
    )

    jobs_out: list[TrainerProfileJob] = []
    outline_path: Path | None = None

    try:
        outline_path = download_crm_file_to_path(outline_fid, _temp_cv_dir())
        outline_text = read_text_from_path(str(outline_path))
        outline_blob = [outline_text]
        logger.info(
            "GEN_PARENT_OUTLINE_TEXT parent_id=%s outline_file_id=%s char_count=%s",
            parent_id,
            outline_fid,
            len(outline_text),
        )

        cn_field = (settings.zoho_parent_course_name_field_api_name or "").strip()
        if (payload.course_name or "").strip():
            parent_drive_course = (payload.course_name or "").strip()
        elif cn_field:
            raw_cn = get_scalar_field_str(parent_record, cn_field)
            parent_drive_course = (raw_cn or "").strip() or settings.google_drive_fallback_course_name
        else:
            parent_drive_course = settings.google_drive_fallback_course_name

        for trainer_id in trainer_ids:
            t0 = time.perf_counter()
            trainer_row = fetch_crm_record(trainer_mod, trainer_id)
            tr_keys = sorted(trainer_row.keys()) if isinstance(trainer_row, dict) else []
            logger.info(
                "GEN_PARENT_TRAINER_RECORD trainer_id=%s field_name_count=%s field_names_head=%s",
                trainer_id,
                len(tr_keys),
                tr_keys[:50],
            )

            cv_raw = trainer_row.get(cv_f)
            cv_file_id = extract_file_id_from_zoho_field(cv_raw)
            logger.info(
                "GEN_PARENT_TRAINER_CV_FIELD trainer_id=%s field=%s resolved_file_id=%s raw_type=%s raw_preview=%s",
                trainer_id,
                cv_f,
                cv_file_id or "(none)",
                type(cv_raw).__name__,
                format_zoho_field_debug(cv_raw),
            )
            if not cv_file_id:
                logger.warning(
                    "GEN_PARENT_SKIP_TRAINER no CV file id module=%s trainer_id=%s field=%s",
                    trainer_mod,
                    trainer_id,
                    cv_f,
                )
                continue

            unique_code = get_scalar_field_str(trainer_row, code_f) or "Trainer"
            heading_label = unique_code.strip()[:40]

            temp_cv: Path | None = None
            try:
                temp_cv = download_crm_file_to_path(cv_file_id, _temp_cv_dir())
                cv_text = read_text_from_path(str(temp_cv))
                cv_trimmed, outline_trimmed = truncate_inputs(cv_text, outline_blob)
                logger.info(
                    "GEN_PARENT_INPUT_SIZES trainer_id=%s cv_chars_after_truncate=%s outline_blocks=%s outline_chars_total=%s",
                    trainer_id,
                    len(cv_trimmed),
                    len(outline_trimmed),
                    sum(len(x) for x in outline_trimmed),
                )
                prog_field = (settings.zoho_trainer_programs_field_api_name or "").strip()
                crm_program_lines: list[str] = []
                if prog_field:
                    raw_prog = get_scalar_field_str(trainer_row, prog_field)
                    crm_program_lines = _parse_multiline_programs_from_zoho(raw_prog)
                    logger.info(
                        "GEN_PARENT_PROGRAMS_CRM trainer_id=%s field=%s line_count=%s",
                        trainer_id,
                        prog_field,
                        len(crm_program_lines),
                    )
                merged_program_hints = _merge_request_and_crm_program_hints(payload, crm_program_lines)

                prompt = build_prompt(
                    cv_trimmed,
                    outline_trimmed,
                    trainer_heading_name=heading_label,
                    programs_trained_hints=merged_program_hints,
                )

                cv_stored = f"zoho://record/{trainer_mod}/{cv_f}/{cv_file_id}"
                outline_refs = [f"zoho://record/{parent_mod}/{outline_f}/{outline_fid}"]

                job = TrainerProfileJob(
                    zoho_record_id=payload.zoho_record_id,
                    cv_path=cv_stored,
                    course_outline_paths=outline_refs,
                    provider=payload.provider or settings.default_provider,
                    model_name=payload.model_name or settings.default_model,
                    status="processing",
                    prompt_version=payload.prompt_version,
                    parsed_inputs={
                        "cv_excerpt": cv_trimmed[:4000],
                        "outline_count": len(outline_trimmed),
                        "parent_record_id": parent_id,
                        "parent_module": parent_mod,
                        "trainer_record_id": trainer_id,
                        "trainer_unique_code": heading_label,
                        "drive_course_name": parent_drive_course,
                        "programs_trained_hints": merged_program_hints or [],
                        "programs_trained_crm_lines": len(crm_program_lines),
                    },
                )
                db.add(job)
                db.commit()
                db.refresh(job)
                logger.info(
                    "GEN_PARENT_JOB_CREATED job_id=%s trainer_id=%s unique_code=%s",
                    job.id,
                    trainer_id,
                    heading_label,
                )

                await _complete_job_after_prompt(
                    job,
                    db,
                    payload,
                    public_base_url,
                    prompt,
                    t0,
                    trainer_display_name=heading_label,
                    programs_trained_hints=merged_program_hints,
                )
                db.refresh(job)
                jobs_out.append(job)
            finally:
                if temp_cv and temp_cv.is_file():
                    try:
                        temp_cv.unlink()
                    except OSError as exc:
                        logger.warning("GEN_TEMP_REMOVE_FAILED path=%s error=%s", temp_cv, exc)

        if not jobs_out:
            raise ValueError(
                "No trainer profiles were generated: check Trainer_CV file upload on each linked trainer record."
            )
        return jobs_out
    finally:
        if outline_path and outline_path.is_file():
            try:
                outline_path.unlink()
                logger.info("GEN_TEMP_REMOVED path=%s", outline_path)
            except OSError as exc:
                logger.warning("GEN_TEMP_REMOVE_FAILED path=%s error=%s", outline_path, exc)


async def generate_and_store_profile(
    payload: GenerateProfileRequest, db: Session, *, public_base_url: str | None = None
) -> list[TrainerProfileJob]:
    settings = get_settings()
    t0 = time.perf_counter()

    if (
        _parent_multi_trainer_enabled(settings)
        and not (payload.cv or "").strip()
        and not list(payload.course_outline_paths)
    ):
        logger.info(
            "GEN_ROUTE parent_multi_trainer_flow=1 zoho_record_id=%s (parent record id)",
            payload.zoho_record_id,
        )
        return await generate_from_parent_with_trainers(payload, db, public_base_url=public_base_url)

    logger.info(
        "GEN_ROUTE legacy_single_record_flow=1 zoho_record_id=%s zoho_module=%s",
        payload.zoho_record_id,
        settings.zoho_module_api_name,
    )
    temp_zoho_paths: list[Path] = []
    stored_outline_refs: list[str] = list(payload.course_outline_paths)
    crm_program_lines: list[str] = []
    logger.info(
        "GEN_START zoho_record_id=%s cv_present=%s outline_paths=%s provider=%s model=%s",
        payload.zoho_record_id,
        bool(payload.cv and payload.cv.strip()),
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
                "Trainer CV is required from Zoho CRM: pass `cv` (Zoho file id for the CV attachment) or set "
                "ZOHO_MODULE_API_NAME and ZOHO_CV_FIELD_API_NAME so the CV is read from the record."
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

        prog_field = (settings.zoho_trainer_programs_field_api_name or "").strip()
        if mod and prog_field:
            rid_prog = (payload.zoho_record_id or "").strip()
            try:
                tr_rec = fetch_crm_record(mod, rid_prog)
                raw_prog = get_scalar_field_str(tr_rec, prog_field)
                crm_program_lines = _parse_multiline_programs_from_zoho(raw_prog)
                logger.info(
                    "GEN_PROGRAMS_FROM_CRM zoho_record_id=%s field=%s line_count=%s",
                    rid_prog,
                    prog_field,
                    len(crm_program_lines),
                )
            except Exception as exc:
                logger.warning(
                    "GEN_PROGRAMS_CRM_FETCH_FAILED zoho_record_id=%s field=%s err=%s",
                    rid_prog,
                    prog_field,
                    exc,
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

    merged_program_hints = _merge_request_and_crm_program_hints(payload, crm_program_lines)

    prompt = build_prompt(
        cv_trimmed,
        outline_trimmed,
        programs_trained_hints=merged_program_hints,
    )

    drive_cn = (payload.course_name or "").strip() or settings.google_drive_fallback_course_name

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
            "drive_course_name": drive_cn,
            "programs_trained_hints": merged_program_hints or [],
            "programs_trained_crm_lines": len(crm_program_lines),
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("GEN_JOB_CREATED job_id=%s status=%s cv_path=%s", job.id, job.status, job.cv_path)

    job = await _complete_job_after_prompt(
        job,
        db,
        payload,
        public_base_url,
        prompt,
        t0,
        trainer_display_name=None,
        programs_trained_hints=merged_program_hints,
    )
    return [job]
