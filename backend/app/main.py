import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .config import get_settings
from .database import Base, engine, get_db
from .db_migrations import apply_light_migrations
from .models import TrainerProfileJob
from .schemas import GenerateProfileRequest, GenerateProfileResponse, JobStatusResponse, ProfileExportLinks
from .services.job_pdf import ensure_job_pdf_on_disk
from .services.profile_service import generate_and_store_profile

settings = get_settings()

app = FastAPI(title=settings.app_name)

logger = logging.getLogger("trainer_profile.api")

_BACKEND_ROOT = Path(__file__).resolve().parents[1]  # .../trainer-profile/backend

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


def _export_links_for_job(request: Request, job_id: str) -> ProfileExportLinks:
    base = _public_base_url(request)
    api_base = quote(base, safe=":/?&=")
    ui = f"{base}/trainer-profile/index.html?job={job_id}&api_base={api_base}"
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


def _public_base_url(request: Request) -> str:
    if settings.public_base_url:
        return str(settings.public_base_url).rstrip("/")
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
    configured = str(getattr(settings, "log_level", "INFO")).upper()
    level = getattr(logging, configured, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)

    Base.metadata.create_all(bind=engine)
    apply_light_migrations(engine)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.post("/api/v1/profiles/generate", response_model=GenerateProfileResponse)
def generate_profile(payload: GenerateProfileRequest, request: Request, db: Session = Depends(get_db)):
    logger.info(
        "API_GENERATE_REQUEST zoho_record_id=%s cv_present=%s cv_path_present=%s outlines=%s provider=%s model=%s",
        payload.zoho_record_id,
        bool(payload.cv),
        bool(payload.cv_path),
        len(payload.course_outline_paths),
        payload.provider or settings.default_provider,
        payload.model_name or settings.default_model,
    )
    job = generate_and_store_profile(
        payload,
        db,
        public_base_url=_public_base_url(request),
    )
    if job.status == "failed":
        logger.error("API_GENERATE_FAILED zoho_record_id=%s error=%s", payload.zoho_record_id, job.error_message)
        raise HTTPException(status_code=400, detail=job.error_message or "Generation failed")
    export = _export_links_for_job(request, job.id)
    logger.info(
        "API_GENERATE_RESPONSE job_id=%s status=%s pdf_url=%s pdf_generation_error=%s",
        job.id,
        job.status,
        export.pdf_url,
        job.pdf_generation_error or "",
    )
    return GenerateProfileResponse(
        id=job.id,
        status=job.status,
        zoho_record_id=job.zoho_record_id,
        provider=job.provider,
        model_name=job.model_name,
        generated_profile=job.generated_profile,
        pdf_url=export.pdf_url,
        export=export,
        pdf_generation_error=job.pdf_generation_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/api/v1/profiles/{job_id}", response_model=JobStatusResponse)
def get_profile_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    logger.info("API_JOB_GET_REQUEST job_id=%s", job_id)
    job = db.get(TrainerProfileJob, job_id)
    if not job:
        logger.warning("API_JOB_GET_NOT_FOUND job_id=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "completed" and job.generated_profile:
        try:
            ensure_job_pdf_on_disk(db=db, job=job, public_base_url=_public_base_url(request))
            db.refresh(job)
        except Exception as exc:
            logger.exception("Failed ensuring PDF exists for job_id=%s", job_id)
            job.pdf_generation_error = str(exc)
            db.add(job)
            db.commit()
            db.refresh(job)

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
        generated_profile=job.generated_profile if job.generated_profile else None,
        pdf_url=pdf_url,
        export=export,
        error_message=job.error_message,
        pdf_generation_error=job.pdf_generation_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/api/v1/profiles/{job_id}/pdf")
def download_profile_pdf(job_id: str, request: Request, db: Session = Depends(get_db)):
    logger.info("API_PDF_REQUEST job_id=%s", job_id)
    job = db.get(TrainerProfileJob, job_id)
    if not job:
        logger.warning("API_PDF_NOT_FOUND job_id=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or not job.generated_profile:
        logger.warning("API_PDF_NOT_READY job_id=%s status=%s", job_id, job.status)
        raise HTTPException(status_code=400, detail="Job is not ready for PDF export")

    path = ensure_job_pdf_on_disk(db=db, job=job, public_base_url=_public_base_url(request))
    logger.info("API_PDF_READY job_id=%s path=%s", job_id, str(path))
    filename = f"trainer_profile_{job_id}.pdf"
    return FileResponse(
        path=str(path),
        media_type="application/pdf",
        filename=filename,
    )
