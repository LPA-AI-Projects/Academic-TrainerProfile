from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class GenerateProfileRequest(BaseModel):
    zoho_record_id: str = Field(min_length=1, max_length=128)
    # Optional: Google Drive folder segment + upload filename (see GOOGLE_DRIVE_* env). Webhook can pass this field.
    course_name: str | None = Field(default=None, max_length=200)
    # Zoho CRM file id for the trainer CV (downloaded server-side). Omit when ``ZOHO_*`` env loads ``Trainer_CV`` from the record (legacy) or when using the parent multi-trainer flow (CV on each trainer row).
    cv: str | None = Field(default=None, max_length=256)
    course_outline_paths: list[str] = Field(
        default_factory=list,
        description="Optional local paths to outline files; outline can also be loaded from Zoho when configured.",
    )
    # Optional client-supplied program/course titles; merged into programs_trained first, then CV/outline-backed items (no duplicates).
    programs_trained: list[str] = Field(default_factory=list)
    provider: Literal["openai", "anthropic"] | None = None
    model_name: str | None = None
    prompt_version: str = "v1"

    @field_validator("course_outline_paths")
    @classmethod
    def validate_outline_paths(cls, value: list[str]) -> list[str]:
        return [v for v in value if v and v.strip()]

    @field_validator("programs_trained")
    @classmethod
    def validate_programs_trained(cls, value: list[str]) -> list[str]:
        out = [str(v).replace("\n", " ").strip() for v in value if v and str(v).strip()]
        return out[:50]

    @field_validator("course_name", mode="before")
    @classmethod
    def empty_course_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            return s or None
        return str(value).strip() or None

class GeneratedProfilePayload(BaseModel):
    # When set, the HTML template uses this for the main heading instead of "This Trainer".
    trainer_display_name: str | None = None
    professional_titles: list[str] = Field(default_factory=list)
    csat_score: float | None = None
    batches_delivered: int | None = None
    profile: str = ""
    programs_trained: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    professional_experience: list[str] = Field(default_factory=list)
    core_competencies: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    awards_and_recognitions: list[str] = Field(default_factory=list)
    board_experience: list[str] = Field(default_factory=list)
    training_delivered: list[str] = Field(default_factory=list)
    key_skills: list[str] = Field(default_factory=list)


class ProfileExportLinks(BaseModel):
    """Convenience links to open the HTML template and print/export a PDF in the browser."""

    trainer_profile_ui: str
    trainer_profile_print: str
    trainer_profile_pdf: str
    pdf_url: str
    job_json: str
    note: str = (
        "`pdf_url` is the stable public URL to the saved PDF file under `/pdfs/`. "
        "`trainer_profile_pdf` is the API render endpoint (also works). "
        "`trainer_profile_print` supports browser print-to-PDF."
    )


class GenerateProfileJobItem(BaseModel):
    job_id: str
    zoho_record_id: str
    trainer_record_id: str | None = None
    pdf_url: str
    generated_profile: GeneratedProfilePayload
    google_drive_pdf_url: str | None = None


class GenerateProfileResponse(BaseModel):
    """Minimal webhook-friendly payload from POST /api/v1/profiles/generate."""

    status: str
    zoho_record_id: str
    pdf_url: str
    generated_profile: GeneratedProfilePayload
    # Set when GOOGLE_DRIVE_AUTO_UPLOAD=true and OAuth is configured (same as POST /upload-to-drive).
    google_drive_pdf_url: str | None = None
    google_drive_upload_error: str | None = None
    # When the parent-record + multi-trainer Zoho flow runs, one entry per trainer.
    jobs: list[GenerateProfileJobItem] | None = None


class RefineProfileRequest(BaseModel):
    """Body for ``POST /api/v1/profiles/refine``."""

    # Primary field: narrative change instructions (no artificial max length; HTTP limits still apply).
    refine: str | None = Field(default=None, description="How to change the profile narrative.")
    # Deprecated alias for Deluge/old clients — use ``refine``.
    feedback: str | None = Field(default=None, description="Same as ``refine``; ignored when ``refine`` is set.")
    # Parent course / campaign record id (same as webhook zoho_record_id when using parent flow).
    zoho_record_id: str | None = Field(default=None, max_length=128)
    # Trainer_Unique_code from Zoho — use with zoho_record_id when multiple trainers exist.
    # Optional ``_vN`` (e.g. ``TR2001_v2``) selects the Zoho PDF attachment slot; base code targets the latest slot.
    unique_code: str | None = Field(default=None, max_length=128)
    # Deluge-friendly alias for Trainer_Unique_code (same as unique_code).
    title: str | None = Field(default=None, max_length=128)
    profile_name: str | None = Field(default=None, max_length=200)

    @field_validator("zoho_record_id", "unique_code", "title", "refine", "feedback", mode="before")
    @classmethod
    def empty_optional_strings(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            return s or None
        return str(value).strip() or None

    @model_validator(mode="after")
    def merge_title_refine(self) -> Self:
        z = (self.zoho_record_id or "").strip()
        u = (self.unique_code or "").strip()
        t = (self.title or "").strip()
        if t and u and t != u:
            raise ValueError("title and unique_code must match when both are set.")
        merged_lookup = (u or t).strip() or None
        if not z and not merged_lookup:
            raise ValueError(
                "Provide at least one of zoho_record_id, unique_code, or title (as Trainer_Unique_Code)."
            )
        r = (self.refine or "").strip()
        f = (self.feedback or "").strip()
        merged_refine = r or f
        if not merged_refine:
            raise ValueError("Provide refine (or legacy feedback).")
        return self.model_copy(
            update={
                "zoho_record_id": z or None,
                "unique_code": merged_lookup,
                "title": None,
                "refine": merged_refine,
                "feedback": None,
            }
        )


class RefineProfilePathBody(BaseModel):
    """Body for ``POST /api/v1/profiles/refine/{zoho_record_id}`` — parent id is taken from the path."""

    feedback: str = Field(min_length=1, max_length=4000)
    unique_code: str | None = Field(default=None, max_length=128)
    title: str | None = Field(
        default=None,
        max_length=128,
        description="Trainer_Unique_Code (alias of unique_code). Optional _vN selects Zoho PDF attachment slot.",
    )
    profile_name: str | None = Field(default=None, max_length=200)

    @field_validator("unique_code", "title", mode="before")
    @classmethod
    def strip_optional(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            return s or None
        return str(value).strip() or None

    @model_validator(mode="after")
    def merge_title_into_unique(self) -> Self:
        u = (self.unique_code or "").strip()
        t = (self.title or "").strip()
        if t and u and t != u:
            raise ValueError("title and unique_code must match when both are set.")
        merged = (u or t).strip() or None
        return self.model_copy(update={"unique_code": merged, "title": None})


class JobStatusResponse(BaseModel):
    id: str
    status: str
    zoho_record_id: str
    provider: str
    model_name: str
    cv_path: str
    course_outline_paths: list[str]
    generated_profile: GeneratedProfilePayload | None = None
    pdf_url: str | None = None
    export: ProfileExportLinks | None = None
    error_message: str | None = None
    pdf_generation_error: str | None = None
    feedback_comment: str | None = None
    feedback_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DriveUploadRequest(BaseModel):
    zoho_record_id: str = Field(min_length=1, max_length=128)
    course_name: str = Field(min_length=1, max_length=200)
    # Trainer unique code (matches Trainer_Unique_code / parsed trainer_unique_code). Required when multiple trainers share the same zoho_record_id.
    unique_code: str | None = Field(default=None, max_length=128)


class DriveUploadResponse(BaseModel):
    status: str
    zoho_record_id: str
    course_name: str
    unique_code: str | None = None
    pdf_link: str
