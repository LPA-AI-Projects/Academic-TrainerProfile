from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Trainer Profile API"
    app_env: str = "dev"
    log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = Field(
        default="postgresql+psycopg2://postgres:postgres@localhost:5432/trainer_profiles"
    )

    default_provider: str = "openai"
    default_model: str = "gpt-4.1-mini"
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str | None = None

    max_cv_chars: int = 30000
    max_outline_chars: int = 15000
    max_total_input_chars: int = 80000

    # If true, returns stub output when API keys are missing.
    allow_mock_generation: bool = True

    # Optional override for publicly reachable base URL (used in export links and PDF rendering).
    # If unset, the server will infer from the incoming request, normalizing 0.0.0.0 to 127.0.0.1.
    public_base_url: str | None = None

    # Where generated PDFs are stored on disk and served from `/pdfs/...`.
    pdf_storage_dir: str = "storage/pdfs"

    # Optional: require `X-API-Key` on API routes when set (matches common webhook / internal service pattern).
    api_secret_key: str | None = None

    # Zoho CRM file download (webhook sends `cv` = attachment / file id, not a local path).
    zoho_dc: str = "com"
    zoho_client_id: str | None = None
    zoho_client_secret: str | None = None
    zoho_refresh_token: str | None = None
    zoho_access_token: str | None = None
    # When request has only `zoho_record_id`, fetch file-upload field(s) from this module (e.g. Trainers, Contacts).
    zoho_module_api_name: str | None = None
    # CRM field API names for File Upload fields (configure to match your Zoho layout).
    zoho_cv_field_api_name: str | None = None
    zoho_outline_field_api_name: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]
