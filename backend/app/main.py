import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal
from shutil import copyfileobj
from urllib.parse import quote

from starlette.requests import ClientDisconnect

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import Base, engine, get_db
from .utils.http_errors import PostOnlyAccessLogFilter, validation_errors_for_response
from .utils.logger import get_logger
from .db_migrations import apply_light_migrations
from .models import TrainerProfileJob
from .schemas import (
    DriveUploadRequest,
    DriveUploadResponse,
    GeneratedProfilePayload,
    GenerateProfileJobItem,
    GenerateProfileRequest,
    GenerateProfileResponse,
    JobStatusResponse,
    ProfileExportLinks,
    RefineProfilePathBody,
    RefineProfileRequest,
)
from .services.google_drive_service import GoogleDriveUploadError, upload_trainer_profile_pdf
from .services.job_pdf import ensure_job_pdf_on_disk
from .services.profile_service import (
    generate_and_store_profile,
    maybe_google_drive_upload_after_pdf,
    maybe_zoho_attach_trainer_pdf_link,
    normalize_profile_payload,
    parse_trainer_field_explicit_version,
    trainer_unique_lookup_base,
)
from .services.llm_client import refine_generated_profile_json

settings = get_settings()

app = FastAPI(title=settings.app_name)

logger = get_logger(__name__)

# Set True in startup after create_all + migrations; Railway /health stays up if DB is misconfigured.
_db_initialized: bool = False

_BACKEND_ROOT = Path(__file__).resolve().parents[1]  # .../trainer-profile/backend


def verify_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """When API_SECRET_KEY is set in the environment, require matching X-API-Key header."""
    secret = (get_settings().api_secret_key or "").strip()
    if not secret:
        return
    if not x_api_key or x_api_key.strip() != secret:
        logger.warning(
            "API_KEY_REJECTED X-API-Key header present=%s",
            bool(x_api_key and str(x_api_key).strip()),
        )
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


optional_api_key = Depends(verify_api_key)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the static CV builder under /trainer-profile/ so the API can return a real URL for mapping + print/PDF.
_TRAINER_PROFILE_ROOT = _BACKEND_ROOT.parent  # .../trainer-profile (sibling of /backend)
if (_TRAINER_PROFILE_ROOT / "index.html").is_file():
    app.mount(
        "/trainer-profile",
        StaticFiles(directory=str(_TRAINER_PROFILE_ROOT), html=True),
        name="trainer_profile",
    )
else:
    logger.warning(
        "Trainer profile UI not mounted: missing index.html at %s",
        str(_TRAINER_PROFILE_ROOT / "index.html"),
    )

_PDF_ROOT = Path(settings.pdf_storage_dir)
if not _PDF_ROOT.is_absolute():
    _PDF_ROOT = _BACKEND_ROOT / _PDF_ROOT
_PDF_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/pdfs", StaticFiles(directory=str(_PDF_ROOT)), name="pdfs")


def _trainer_unique_from_job(job: TrainerProfileJob) -> str:
    parsed = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    code = str(parsed.get("trainer_unique_code") or "").strip()
    if code:
        return code
    gp = job.generated_profile if isinstance(job.generated_profile, dict) else {}
    return str(gp.get("trainer_display_name") or "").strip()


def _resolve_completed_trainer_job(
    db: Session,
    *,
    zoho_record_id: str | None,
    unique_code: str | None,
) -> TrainerProfileJob:
    """Pick one completed job: disambiguate with unique_code when several profiles share the same parent zoho_record_id."""
    z = (zoho_record_id or "").strip()
    u = (unique_code or "").strip()

    q = db.query(TrainerProfileJob).filter(TrainerProfileJob.status == "completed")
    if z:
        q = q.filter(TrainerProfileJob.zoho_record_id == z)
    rows = q.order_by(TrainerProfileJob.created_at.desc()).all()

    if not rows:
        raise HTTPException(status_code=404, detail="No completed profile job found for the given criteria.")

    if u:
        u_base = trainer_unique_lookup_base(u)
        for j in rows:
            if _trainer_unique_from_job(j) == u:
                logger.info(
                    "RESOLVE_TRAINER_JOB matched job_id=%s zoho_record_id=%s unique_code=%s",
                    j.id,
                    j.zoho_record_id,
                    u,
                )
                return j
        for j in rows:
            jc = _trainer_unique_from_job(j)
            if jc and trainer_unique_lookup_base(jc) == u_base:
                logger.info(
                    "RESOLVE_TRAINER_JOB matched_base job_id=%s zoho_record_id=%s unique_code=%s job_code=%s",
                    j.id,
                    j.zoho_record_id,
                    u,
                    jc,
                )
                return j
        logger.warning(
            "RESOLVE_TRAINER_JOB no_match zoho_record_id=%s unique_code=%s candidates=%s codes_seen=%s",
            z or "(any)",
            u,
            len(rows),
            [_trainer_unique_from_job(j) for j in rows[:20]],
        )
        raise HTTPException(
            status_code=404,
            detail="No job matches the provided unique_code for this lookup.",
        )

    if len(rows) > 1:
        logger.warning(
            "RESOLVE_TRAINER_JOB ambiguous zoho_record_id=%s candidate_count=%s job_ids=%s",
            z,
            len(rows),
            [j.id for j in rows[:15]],
        )
        raise HTTPException(
            status_code=400,
            detail="Multiple trainer profiles match; provide unique_code (and zoho_record_id when possible).",
        )
    logger.info(
        "RESOLVE_TRAINER_JOB single_match job_id=%s zoho_record_id=%s unique_code=%s",
        rows[0].id,
        rows[0].zoho_record_id,
        _trainer_unique_from_job(rows[0]) or "(none)",
    )
    return rows[0]


def _parsed_drive_url(job: TrainerProfileJob) -> str | None:
    pi = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    u = pi.get("google_drive_pdf_url")
    return str(u).strip() if u else None


def _parsed_drive_err(job: TrainerProfileJob) -> str | None:
    pi = job.parsed_inputs if isinstance(job.parsed_inputs, dict) else {}
    e = pi.get("google_drive_upload_error")
    return str(e).strip() if e else None


def _export_links_for_job(request: Request, job_id: str) -> ProfileExportLinks:
    base = _public_base_url(request)
    api_base = quote(base, safe=":/?&=")
    secret = (settings.api_secret_key or "").strip()
    key_q = f"&api_key={quote(secret, safe='')}" if secret else ""
    ui = f"{base}/trainer-profile/index.html?job={job_id}&api_base={api_base}{key_q}"
    ui_print = f"{ui}&autoprint=1"
    pdf = f"{base}/api/v1/profiles/{job_id}/pdf"
    pdf_file = f"{base}/pdfs/{job_id}.pdf"
    job_json = f"{base}/api/v1/profiles/{job_id}"
    return ProfileExportLinks(
        trainer_profile_ui=ui,
        trainer_profile_print=ui_print,
        trainer_profile_pdf=pdf,
        pdf_url=pdf_file,
        job_json=job_json,
    )


def _build_generate_profile_response(
    request: Request,
    jobs: list[TrainerProfileJob],
    *,
    zoho_record_id: str | None = None,
    empty_message: str | None = None,
) -> GenerateProfileResponse:
    if not jobs:
        z = (zoho_record_id or "").strip()
        if not z:
            raise HTTPException(status_code=500, detail="No generation jobs returned")
        logger.warning(
            "API_GENERATE_EMPTY_RESPONSE zoho_record_id=%s message=%s",
            z,
            empty_message or "no jobs",
        )
        return GenerateProfileResponse(
            status="skipped",
            zoho_record_id=z,
            pdf_url="",
            generated_profile=GeneratedProfilePayload(),
            google_drive_pdf_url=None,
            google_drive_upload_error=None,
            jobs=None,
            message=empty_message
            or "No trainer profiles were generated (e.g. no linked trainer had a CV file on record).",
        )
    failed = [j for j in jobs if j.status == "failed"]
    if failed:
        raise HTTPException(status_code=400, detail=failed[0].error_message or "Generation failed")
    first = jobs[0]
    export = _export_links_for_job(request, first.id)
    items: list[GenerateProfileJobItem] | None = None
    if len(jobs) > 1:
        items = []
        for j in jobs:
            exp = _export_links_for_job(request, j.id)
            parsed = j.parsed_inputs if isinstance(j.parsed_inputs, dict) else {}
            tid = parsed.get("trainer_record_id")
            items.append(
                GenerateProfileJobItem(
                    job_id=j.id,
                    zoho_record_id=j.zoho_record_id,
                    trainer_record_id=str(tid) if tid is not None else None,
                    pdf_url=exp.pdf_url,
                    generated_profile=j.generated_profile,
                    google_drive_pdf_url=_parsed_drive_url(j),
                )
            )
    return GenerateProfileResponse(
        status=first.status,
        zoho_record_id=first.zoho_record_id,
        pdf_url=export.pdf_url,
        generated_profile=first.generated_profile,
        google_drive_pdf_url=_parsed_drive_url(first),
        google_drive_upload_error=_parsed_drive_err(first),
        jobs=items,
    )


def _public_base_url(request: Request) -> str:
    request_base = str(request.base_url).rstrip("/")
    request_host = request.url.hostname or ""
    request_port = request.url.port

    if settings.public_base_url:
        configured = str(settings.public_base_url).rstrip("/")
        # In local dev, stale PUBLIC_BASE_URL ports (e.g. 8080 vs 8010) break
        # Playwright PDF rendering because the preview page fetches the wrong API.
        if request_host in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::]"}:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(configured)
                configured_host = (parsed.hostname or "").lower()
                configured_port = parsed.port
                if configured_host in {"127.0.0.1", "localhost"} and configured_port != request_port:
                    logger.warning(
                        "PUBLIC_BASE_URL port mismatch configured=%s request_base=%s; using request base for this call",
                        configured,
                        request_base,
                    )
                    return request_base
            except Exception:
                logger.exception("Failed parsing PUBLIC_BASE_URL=%s; falling back to configured value", configured)
        return configured
    # Starlette's request.base_url is fine for most cases, but browsers cannot load http://0.0.0.0:port.
    if request.url.hostname in {"0.0.0.0", None}:
        port = f":{request.url.port}" if request.url.port else ""
        return f"http://127.0.0.1{port}"
    if request.url.hostname in {"[::]", "::1"}:
        port = f":{request.url.port}" if request.url.port else ""
        return f"http://127.0.0.1{port}"
    return str(request.base_url).rstrip("/")


@app.on_event("startup")
def startup() -> None:
    global _db_initialized
    configured = str(getattr(settings, "log_level", "INFO")).upper()
    level = getattr(logging, configured, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(level)
    access_logger.addFilter(PostOnlyAccessLogFilter())

    logger.info(
        "API_STARTUP log_level=%s app_env=%s api_key_required=%s",
        configured,
        settings.app_env,
        bool((settings.api_secret_key or "").strip()),
    )
    logger.info(
        "API_STARTUP zoho_attach_trainer_pdf_link=%s",
        bool(getattr(settings, "zoho_attach_trainer_pdf_link", False)),
    )

    _db_initialized = False
    try:
        Base.metadata.create_all(bind=engine)
        apply_light_migrations(engine)
        _db_initialized = True
        logger.info("API_STARTUP_DB_OK database_url_host=%s", _database_url_host_for_log())
    except Exception:
        logger.exception(
            "API_STARTUP_DB_FAILED — process still listens for /health; set Railway DATABASE_URL "
            "to your Postgres service (default localhost only works in local compose). "
            "host=%s",
            _database_url_host_for_log(),
        )


def _database_url_host_for_log() -> str:
    """Safe fragment for logs (no password)."""
    try:
        from urllib.parse import urlparse

        u = urlparse(settings.database_url.replace("postgresql+psycopg2", "postgresql", 1))
        return f"{u.hostname or '?'}:{u.port or '?'}"
    except Exception:
        return "?"


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "env": settings.app_env,
        "database_ready": _db_initialized,
    }


@app.get("/health/db")
def health_db() -> dict[str, str]:
    """
    DB readiness check.
    Returns 200 when a simple query succeeds, otherwise 503 with details.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database connection failed: {exc}")


@app.post(
    "/api/v1/profiles/generate",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
)
@app.post(
    "/api/v2/profiles/generate",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
    summary="Generate profile (alias of v1; same urlencoded webhook body)",
)
async def generate_profile(request: Request, db: Session = Depends(get_db)):
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" not in ctype:
        logger.warning(
            "API_GENERATE_REJECT_UNSUPPORTED_MEDIA path=%s content_type=%r user_agent=%r x_forwarded_for=%r",
            request.url.path,
            request.headers.get("content-type"),
            request.headers.get("user-agent"),
            request.headers.get("x-forwarded-for"),
        )
        raise HTTPException(
            status_code=415,
            detail="Unsupported Media Type. Use application/x-www-form-urlencoded.",
        )

    form = await request.form()
    form_data = {str(k): str(v).strip() for k, v in form.items()}
    zid = (form_data.get("zoho_record_id") or form_data.get("record_id") or form_data.get("id") or "").strip()
    if not zid:
        logger.warning(
            "API_GENERATE_REJECT_MISSING_RECORD_ID path=%s form_keys=%s x_zoho_crm_feature=%r",
            request.url.path,
            sorted(form_data.keys()),
            request.headers.get("x-zoho-crm-feature"),
        )
        raise HTTPException(
            status_code=422,
            detail="Missing zoho_record_id in form body (accepted aliases: zoho_record_id, record_id, id).",
        )

    client_ip = (
        request.headers.get("x-real-ip")
        or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
    )
    logger.info(
        "API_GENERATE_WEBHOOK_ACCEPTED zoho_record_id=%s client_ip=%s zoho_feature=%r ua=%.200s",
        zid,
        client_ip,
        request.headers.get("x-zoho-crm-feature"),
        (request.headers.get("user-agent") or ""),
    )

    if (form_data.get("cv_path") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Trainer CV must come from Zoho CRM: use the `cv` field (Zoho file id) or configure "
                "ZOHO_MODULE_API_NAME + ZOHO_CV_FIELD_API_NAME so the CV is read from the record. "
                "`cv_path` is not supported."
            ),
        )

    outline_paths_raw = form_data.get("course_outline_paths") or ""
    outline_list: list[str] = []
    if outline_paths_raw:
        outline_list = [p.strip() for p in outline_paths_raw.replace("\n", ",").split(",") if p.strip()]

    programs_list = _parse_outline_paths_form(form_data.get("programs_trained"))

    prov_in = (form_data.get("provider") or "").strip()
    prov: Literal["openai", "anthropic"] | None = None
    if prov_in == "openai":
        prov = "openai"
    elif prov_in == "anthropic":
        prov = "anthropic"

    try:
        payload = GenerateProfileRequest(
            zoho_record_id=zid,
            course_name=(form_data.get("course_name") or "").strip() or None,
            cv=(form_data.get("cv") or "").strip() or None,
            course_outline_paths=outline_list,
            programs_trained=programs_list,
            provider=prov,
            model_name=(form_data.get("model_name") or "").strip() or None,
            prompt_version=(form_data.get("prompt_version") or "v1").strip() or "v1",
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=validation_errors_for_response(exc)) from exc

    logger.info(
        "API_GENERATE_FORM_URLENC_REQUEST zoho_record_id=%s cv_present=%s outlines=%s provider=%s model=%s keys=%s",
        payload.zoho_record_id,
        bool(payload.cv),
        len(payload.course_outline_paths),
        payload.provider or settings.default_provider,
        payload.model_name or settings.default_model,
        sorted(form_data.keys()),
    )

    jobs = await generate_and_store_profile(
        payload,
        db,
        public_base_url=_public_base_url(request),
    )
    logger.info(
        "API_GENERATE_RESPONSE zoho_record_id=%s job_count=%s",
        zid,
        len(jobs),
    )
    return _build_generate_profile_response(request, jobs, zoho_record_id=zid)


async def _parse_refine_payload_from_request(request: Request) -> RefineProfileRequest:
    ctype = (request.headers.get("content-type") or "").lower()
    raw: dict[str, object]
    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            form = await request.form()
        except ClientDisconnect:
            raise HTTPException(
                status_code=400,
                detail="Client disconnected before request body was received.",
            ) from None
        raw = dict(form)
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = body if isinstance(body, dict) else {}

    try:
        return RefineProfileRequest(**raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=validation_errors_for_response(exc)) from exc


async def _refine_profile_impl(
    payload: RefineProfileRequest, request: Request, db: Session
) -> GenerateProfileResponse:
    """
    Refine INDUSTRY EXPOSURE and SOLUTIONS DELIVERED on the stored profile, then re-run PDF/Drive/Zoho.

    Requires ``zoho_record_id`` and a refine instruction (min 10 characters).
    ``unique_code`` / ``title`` are required only when multiple completed jobs share the same parent id.

    Zoho PDF attachment name is ``{Trainer_Unique_Code}_vN``: append ``_vN`` to target a specific slot.
    """
    logger.info(
        "API_REFINE_REQUEST zoho_record_id=%s unique_code=%s refine_chars=%s",
        payload.zoho_record_id or "(none)",
        payload.unique_code or "(none)",
        len(payload.refine or ""),
    )
    job = _resolve_completed_trainer_job(
        db,
        zoho_record_id=payload.zoho_record_id,
        unique_code=payload.unique_code,
    )
    exp_v = parse_trainer_field_explicit_version(payload.unique_code or "")
    pi_slot = dict(job.parsed_inputs) if isinstance(job.parsed_inputs, dict) else {}
    # Refine flow increments version slot by default unless an explicit _vN is supplied.
    pi_slot["zoho_pdf_attachment_increment_version"] = True
    if exp_v is not None:
        pi_slot["zoho_pdf_attachment_explicit_v"] = exp_v
    else:
        pi_slot.pop("zoho_pdf_attachment_explicit_v", None)
    job.parsed_inputs = pi_slot
    db.add(job)
    db.commit()
    db.refresh(job)
    gp = job.generated_profile if isinstance(job.generated_profile, dict) else {}
    if not gp:
        raise HTTPException(status_code=400, detail="Job has no generated profile to refine.")

    refine_label = (
        (payload.profile_name or "").strip()
        or _trainer_unique_from_job(job)
        or "Trainer"
    )

    try:
        merged_full, resolved_provider, raw_out = refine_generated_profile_json(
            existing_profile=dict(gp),
            refine_instruction=payload.refine or "",
            trainer_label=refine_label,
            provider=job.provider or settings.default_provider,
            model_name=job.model_name or settings.default_model,
        )
    except Exception as exc:
        logger.exception(
            "API_REFINE_FAILED job_id=%s zoho_record_id=%s unique_code=%s",
            job.id,
            job.zoho_record_id,
            payload.unique_code,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        gen = normalize_profile_payload(
            merged_full,
            programs_trained_hints=None,
            training_delivered_hints=None,
        )
    except Exception as exc:
        logger.warning("API_REFINE_NORMALIZE_FALLBACK job_id=%s err=%s", job.id, exc)
        gen = merged_full

    disp = merged_full.get("trainer_display_name") or gp.get("trainer_display_name")
    if disp:
        gen["trainer_display_name"] = str(disp).strip()[:40]

    old_snap = json.dumps(gp, sort_keys=True, default=str)
    new_snap = json.dumps(gen, sort_keys=True, default=str)
    logger.info(
        "API_REFINE_JSON_DIFF job_id=%s changed=%s raw_chars=%s",
        job.id,
        old_snap != new_snap,
        len(raw_out or ""),
    )

    job.generated_profile = gen
    job.provider = resolved_provider
    job.raw_model_output = raw_out or ""
    job.feedback_comment = (payload.refine or "").strip()
    job.feedback_updated_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    job.pdf_generation_error = None
    db.add(job)
    db.commit()
    db.refresh(job)

    # Rebuild PDF so exported file reflects refined profile text.
    try:
        pub = _public_base_url(request)
        await ensure_job_pdf_on_disk(db=db, job=job, public_base_url=pub, force=True)
        await maybe_google_drive_upload_after_pdf(job, db)
        await maybe_zoho_attach_trainer_pdf_link(job, db, public_base_url=pub)
    except Exception as exc:
        job.pdf_generation_error = str(exc)
        db.add(job)
        db.commit()
        db.refresh(job)
        logger.exception("API_V2_REFINE_PDF_FAILED job_id=%s", job.id)

    db.refresh(job)
    export = _export_links_for_job(request, job.id)
    logger.info("API_REFINE_DONE job_id=%s zoho_record_id=%s pdf_url=%s", job.id, job.zoho_record_id, export.pdf_url)
    return GenerateProfileResponse(
        status=job.status,
        zoho_record_id=job.zoho_record_id,
        pdf_url=export.pdf_url,
        generated_profile=job.generated_profile,
        google_drive_pdf_url=_parsed_drive_url(job),
        google_drive_upload_error=_parsed_drive_err(job),
    )


@app.post(
    "/api/v1/profiles/refine",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
    summary="Refine industry_exposure and solutions_delivered (zoho_record_id; optional unique_code)",
)
async def refine_profile(request: Request, db: Session = Depends(get_db)):
    payload = await _parse_refine_payload_from_request(request)
    return await _refine_profile_impl(payload, request, db)


@app.post(
    "/api/v1/profiles/refine/{zoho_record_id}",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
    summary="Refine profile narrative; parent CRM id in URL (body: feedback, title or unique_code)",
)
async def refine_profile_for_parent_zoho(
    body: RefineProfilePathBody,
    request: Request,
    zoho_record_id: str = PathParam(
        ...,
        min_length=1,
        max_length=128,
        description="Parent / webhook Zoho CRM record id (same as generate webhook).",
    ),
    db: Session = Depends(get_db),
) -> GenerateProfileResponse:
    """
    Same behavior as ``POST /api/v1/profiles/refine`` with ``zoho_record_id`` taken from the path.
    Use ``title`` or ``unique_code`` in the JSON body for Trainer_Unique_Code when multiple trainers share the parent.
    Optional ``_vN`` suffix selects which PDF version slot to replace on the CRM row; omit it to replace the latest slot.
    Zoho PDF attachment still targets the CRM row from the job (parent vs trainer per env), not the URL job id.
    """
    payload = RefineProfileRequest(
        feedback=body.feedback,
        zoho_record_id=zoho_record_id.strip(),
        unique_code=body.unique_code,
        title=body.title,
        profile_name=body.profile_name,
    )
    return await _refine_profile_impl(payload, request, db)


def _form_upload_temp_dir() -> Path:
    d = _BACKEND_ROOT / "storage" / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_upload_to_temp(upload: UploadFile | None) -> Path | None:
    """Persist an upload under backend storage/temp for the duration of generate (same root as Zoho temp files)."""
    if upload is None:
        return None
    name = (upload.filename or "").strip()
    if not name:
        return None
    suffix = Path(name).suffix
    d = _form_upload_temp_dir()
    with tempfile.NamedTemporaryFile(delete=False, prefix="form_upload_", suffix=suffix, dir=str(d)) as tmp:
        copyfileobj(upload.file, tmp)
    return Path(tmp.name)


def _parse_outline_paths_form(text: str | None) -> list[str]:
    if not text or not text.strip():
        return []
    raw = text.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(p).strip() for p in parsed if str(p).strip()]
        return []
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


@app.post(
    "/api/v1/profiles/generate/form",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
    summary="Generate profile (multipart — Postman-friendly uploads)",
)
@app.post(
    "/api/v2/profiles/generate/form",
    response_model=GenerateProfileResponse,
    dependencies=[optional_api_key],
    summary="Generate profile (multipart — Postman-friendly uploads)",
)
async def generate_profile_form(
    request: Request,
    db: Session = Depends(get_db),
    zoho_record_id: str = Form(...),
    course_name: str | None = Form(None, description="Optional: Drive folder segment + filename (see GOOGLE_DRIVE_AUTO_UPLOAD)."),
    cv: str | None = Form(
        None,
        description="Zoho CRM file id for CV (optional if CRM field env loads CV from the record; uploads are not supported).",
    ),
    cv_path: str | None = Form(None, description="Deprecated — rejected; use `cv` (Zoho file id) or CRM field env."),
    cv_file: UploadFile | None = File(None, description="Deprecated — rejected; CV must come from Zoho CRM."),
    course_outline_paths: str | None = Form(
        None,
        description="Comma/newline-separated server paths, or a JSON array string, e.g. [\"C:/outlines/a.txt\"]",
    ),
    course_outline_file: UploadFile | None = File(None, description="Uploaded course outline (optional)"),
    programs_trained: str | None = Form(
        None,
        description="Optional comma/newline-separated or JSON-array program titles; merged first into programs_trained output.",
    ),
    provider: str | None = Form(None),
    model_name: str | None = Form(None),
):
    """
    Same pipeline as `POST /api/v1/profiles/generate`, but accepts **multipart/form-data** (e.g. optional
    **course_outline_file** upload). Trainer CV must come from Zoho CRM: **cv** (Zoho file id) and/or CRM field
    env vars — **cv_file** and **cv_path** are rejected with 400.

    Outlines: paths from **course_outline_paths** plus any saved **course_outline_file** upload (optional).
    """
    zid = zoho_record_id.strip()
    if not zid or len(zid) > 128:
        raise HTTPException(status_code=400, detail="zoho_record_id must be 1–128 characters after trimming.")

    if cv_path and cv_path.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Trainer CV must come from Zoho CRM: use the `cv` field (Zoho file id) or configure "
                "ZOHO_MODULE_API_NAME + ZOHO_CV_FIELD_API_NAME. `cv_path` is not supported."
            ),
        )
    if cv_file is not None and (cv_file.filename or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Trainer CV must come from Zoho CRM: use the `cv` field (Zoho file id) or configure "
                "ZOHO_MODULE_API_NAME + ZOHO_CV_FIELD_API_NAME. CV file upload is not supported."
            ),
        )

    temp_uploads: list[Path] = []
    try:
        saved_outline = _save_upload_to_temp(course_outline_file)
        if saved_outline is not None:
            temp_uploads.append(saved_outline)

        outline_list = _parse_outline_paths_form(course_outline_paths)
        if saved_outline is not None:
            outline_list.append(str(saved_outline))

        programs_list = _parse_outline_paths_form(programs_trained)

        cv_id_effective: str | None = cv.strip() if cv and cv.strip() else None

        prov: Literal["openai", "anthropic"] | None = None
        if provider and provider.strip() == "openai":
            prov = "openai"
        elif provider and provider.strip() == "anthropic":
            prov = "anthropic"

        payload = GenerateProfileRequest(
            zoho_record_id=zid,
            course_name=course_name.strip() if course_name and course_name.strip() else None,
            cv=cv_id_effective,
            course_outline_paths=outline_list,
            programs_trained=programs_list,
            provider=prov,
            model_name=model_name.strip() if model_name and model_name.strip() else None,
        )
    except ValidationError as exc:
        for p in temp_uploads:
            p.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=validation_errors_for_response(exc)) from exc
    except HTTPException:
        for p in temp_uploads:
            p.unlink(missing_ok=True)
        raise
    except Exception:
        for p in temp_uploads:
            p.unlink(missing_ok=True)
        raise

    logger.info(
        "API_GENERATE_FORM zoho_record_id=%s cv_id=%s outline_paths_count=%s outline_file=%s",
        zoho_record_id,
        bool(cv_id_effective),
        len(outline_list),
        saved_outline is not None,
    )

    try:
        jobs = await generate_and_store_profile(
            payload,
            db,
            public_base_url=_public_base_url(request),
        )
    finally:
        for p in temp_uploads:
            p.unlink(missing_ok=True)

    logger.info(
        "API_GENERATE_FORM_RESPONSE zoho_record_id=%s job_count=%s",
        zoho_record_id,
        len(jobs),
    )
    return _build_generate_profile_response(request, jobs, zoho_record_id=zoho_record_id)


@app.get(
    "/api/v1/profiles/{profile_ref}",
    response_model=JobStatusResponse,
    dependencies=[optional_api_key],
)
@app.get(
    "/api/v2/profiles/{profile_ref}",
    response_model=JobStatusResponse,
    dependencies=[optional_api_key],
)
def get_profile_job(profile_ref: str, request: Request, db: Session = Depends(get_db)):
    logger.info("API_JOB_GET_REQUEST profile_ref=%s", profile_ref)
    job = db.get(TrainerProfileJob, profile_ref)
    if not job:
        # Backward compatible: if it's not a direct job id, treat it as Zoho record id.
        # Return latest job for that record.
        job = (
            db.query(TrainerProfileJob)
            .filter(TrainerProfileJob.zoho_record_id == profile_ref)
            .order_by(TrainerProfileJob.created_at.desc())
            .first()
        )
    if not job:
        logger.warning("API_JOB_GET_NOT_FOUND profile_ref=%s", profile_ref)
        raise HTTPException(status_code=404, detail="Job not found for provided reference")
    # IMPORTANT: Do not trigger PDF generation from status endpoint.
    # The trainer-profile HTML used by Playwright fetches this endpoint; auto-generating
    # PDF here can recursively invoke PDF rendering and lead to repeated timeouts.

    export = _export_links_for_job(request, job.id) if job.status == "completed" else None
    pdf_url = export.pdf_url if export else None
    logger.info(
        "API_JOB_GET_RESPONSE job_id=%s status=%s pdf_url=%s pdf_generation_error=%s",
        job.id,
        job.status,
        pdf_url or "",
        job.pdf_generation_error or "",
    )
    return JobStatusResponse(
        id=job.id,
        status=job.status,
        zoho_record_id=job.zoho_record_id,
        provider=job.provider,
        model_name=job.model_name,
        cv_path=job.cv_path,
        course_outline_paths=job.course_outline_paths,
        generated_profile=job.generated_profile if job.generated_profile is not None else None,
        pdf_url=pdf_url,
        export=export,
        error_message=job.error_message,
        pdf_generation_error=job.pdf_generation_error,
        feedback_comment=job.feedback_comment,
        feedback_updated_at=job.feedback_updated_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/api/v1/profiles/{job_id}/pdf", dependencies=[optional_api_key])
@app.get("/api/v2/profiles/{job_id}/pdf", dependencies=[optional_api_key])
async def download_profile_pdf(job_id: str, request: Request, db: Session = Depends(get_db)):
    logger.info("API_PDF_REQUEST job_id=%s", job_id)
    job = db.get(TrainerProfileJob, job_id)
    if not job:
        logger.warning("API_PDF_NOT_FOUND job_id=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or job.generated_profile is None:
        logger.warning("API_PDF_NOT_READY job_id=%s status=%s", job_id, job.status)
        raise HTTPException(status_code=400, detail="Job is not ready for PDF export")

    path = await ensure_job_pdf_on_disk(db=db, job=job, public_base_url=_public_base_url(request))
    logger.info("API_PDF_READY job_id=%s path=%s", job_id, str(path))
    filename = f"trainer_profile_{job_id}.pdf"
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
    )


@app.post(
    "/api/v1/profiles/upload-to-drive",
    response_model=DriveUploadResponse,
    dependencies=[optional_api_key],
)
async def upload_profile_pdf_to_drive(payload: DriveUploadRequest, request: Request, db: Session = Depends(get_db)):
    """
    Upload completed profile PDF into Drive:
    ai_automation/trainer_profile/{course_name}/{unique_code}_{course_name}.pdf
    Use unique_code + zoho_record_id when multiple trainers share the same parent record.
    """
    job = _resolve_completed_trainer_job(
        db,
        zoho_record_id=payload.zoho_record_id,
        unique_code=payload.unique_code,
    )
    if job.generated_profile is None:
        raise HTTPException(status_code=400, detail="Job is not ready for Drive upload")

    resolved_unique = (payload.unique_code or "").strip() or _trainer_unique_from_job(job) or "trainer"
    logger.info(
        "API_DRIVE_UPLOAD job_id=%s zoho_record_id=%s course_name=%s unique_code=%s",
        job.id,
        job.zoho_record_id,
        payload.course_name,
        resolved_unique,
    )

    pdf_path = await ensure_job_pdf_on_disk(db=db, job=job, public_base_url=_public_base_url(request))
    pdf_bytes = Path(pdf_path).read_bytes()
    try:
        result = upload_trainer_profile_pdf(
            pdf_bytes=pdf_bytes,
            unique_code=resolved_unique,
            course_name=payload.course_name,
        )
    except GoogleDriveUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DriveUploadResponse(
        status="completed",
        zoho_record_id=job.zoho_record_id,
        course_name=payload.course_name,
        unique_code=resolved_unique,
        pdf_link=result["view_link"],
    )
