from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GenerateProfileRequest(BaseModel):
    zoho_record_id: str = Field(min_length=1, max_length=128)
    # Zoho CRM attachment / file id (webhook payload); downloaded server-side.
    cv: str | None = Field(default=None, max_length=256)
    # Local filesystem path (dev / non-Zoho callers).
    cv_path: str | None = Field(default=None)
    course_outline_paths: list[str] = Field(default_factory=list)
    provider: Literal["openai", "anthropic"] | None = None
    model_name: str | None = None
    prompt_version: str = "v1"

    @field_validator("course_outline_paths")
    @classmethod
    def validate_outline_paths(cls, value: list[str]) -> list[str]:
        return [v for v in value if v and v.strip()]

    @model_validator(mode="after")
    def require_cv_source(self) -> GenerateProfileRequest:
        has_zoho = bool(self.cv and self.cv.strip())
        has_local = bool(self.cv_path and self.cv_path.strip())
        if not has_zoho and not has_local:
            raise ValueError("Provide either 'cv' (Zoho CRM file id) or 'cv_path' (local file path).")
        if has_zoho and has_local:
            raise ValueError("Provide only one of 'cv' or 'cv_path', not both.")
        return self


class GeneratedProfilePayload(BaseModel):
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


class GenerateProfileResponse(BaseModel):
    """Minimal webhook-friendly payload from POST /api/v1/profiles/generate."""

    status: str
    zoho_record_id: str
    pdf_url: str
    generated_profile: GeneratedProfilePayload


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
    created_at: datetime
    updated_at: datetime
