from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_plain_zoho_dc_suffix(s: str) -> bool:
    """Allow com, in, eu, com.au — no ``re`` module needed."""
    if not s or len(s) > 12 or ".." in s or s in ("https", "http"):
        return False
    if not s[0].isalpha():
        return False
    for c in s:
        if c.islower() or c.isdigit() or c == ".":
            continue
        return False
    return True


def normalize_zoho_dc_value(v: object) -> str:
    """
    Zoho multi-DC uses a short suffix (com, in, eu, com.au), not a full URL.

    OAuth: https://accounts.zoho.{suffix}/oauth/v2/token
    CRM API: https://www.zohoapis.{suffix}/crm/...

    If someone pastes ``https://www.zohoapis.com`` (US/global API base per Zoho docs), we map it to ``com``.
    Zoho uses separate CRM API and Accounts hosts per region; see Zoho CRM multi-DC docs.
    """
    default = "com"
    if not isinstance(v, str):
        return default
    raw = v.strip()
    if not raw:
        return default
    s = raw.lower()

    if s in ("https", "http"):
        return default

    if "://" in s or "zohoapis" in s or "accounts.zoho" in s or s.startswith("www."):
        url = s if "://" in s else f"https://{s}"
        try:
            p = urlparse(url)
            host = (p.hostname or "").lower().strip()
        except Exception:
            host = ""
        if not host:
            return default

        if host in ("www.zohoapis.com", "zohoapis.com"):
            return default
        if host.endswith("zohoapis.com"):
            return default
        if "zohoapis." in host:
            return host.rsplit("zohoapis.", 1)[-1].split("/")[0]

        if host == "accounts.zoho.com":
            return default
        if host.startswith("accounts.zoho."):
            return host.split("accounts.zoho.", 1)[1].split("/")[0]

    suffix = s.lstrip(".")
    if _is_plain_zoho_dc_suffix(suffix):
        return suffix
    return default


def _zoho_str_or_default(v: object, default: str) -> str:
    """Empty or missing env values fall back to in-code defaults (Zoho API names are case-sensitive)."""
    if v is None:
        return default
    if isinstance(v, str):
        t = v.strip()
        return t if t else default
    return default


class Settings(BaseSettings):
    app_name: str = "Trainer Profile API"
    app_env: str = "dev"
    log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    database_url: str = Field(
        default="postgresql+psycopg2://postgres:postgres@localhost:5432/trainer_profiles"
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, v: object) -> object:
        """
        Accept URLs from Railway / Supabase / .env that omit the SQLAlchemy driver.

        - postgres:// → postgresql+psycopg2://
        - postgresql:// → postgresql+psycopg2://
        - postgresql+asyncpg:// → postgresql+psycopg2:// (this app uses sync psycopg2)
        Supabase hosts get sslmode=require when not already set.
        """
        if not isinstance(v, str):
            return v
        s = v.strip()
        if not s:
            return s
        if s.startswith("postgresql+psycopg2://"):
            out = s
        elif s.startswith("postgres://"):
            out = "postgresql+psycopg2://" + s[len("postgres://") :]
        elif s.startswith("postgresql+asyncpg://"):
            out = "postgresql+psycopg2://" + s[len("postgresql+asyncpg://") :]
        elif s.startswith("postgresql://"):
            out = "postgresql+psycopg2://" + s[len("postgresql://") :]
        else:
            out = s
        if "supabase.co" in out.lower():
            parsed = urlparse(out)
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if "sslmode" not in {k.lower() for k in q}:
                q["sslmode"] = "require"
            new_query = urlencode(q)
            out = urlunparse(parsed._replace(query=new_query))
        return out

    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    openai_api_key: str | None = None
    # Used when provider is openai and request does not specify model_name.
    openai_model: str = "gpt-4.1-mini"

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

    # Zoho CRM — OAuth + CRM API (full base URLs in env).
    # Defaults: US/global — https://accounts.zoho.com and https://www.zohoapis.com
    # Other regions: set both to your DC hosts and matching ZOHO_DC (e.g. .in + ZOHO_DC=in).
    zoho_accounts_base_url: str = Field(default="https://accounts.zoho.com")
    zoho_crm_api_base: str = Field(default="https://www.zohoapis.com")
    zoho_dc: str = "com"
    zoho_client_id: str | None = None
    zoho_client_secret: str | None = None
    zoho_refresh_token: str | None = None
    zoho_access_token: str | None = None

    @field_validator("zoho_dc", mode="before")
    @classmethod
    def normalize_zoho_dc(cls, v: object) -> str:
        return normalize_zoho_dc_value(v)

    @field_validator("zoho_accounts_base_url", mode="before")
    @classmethod
    def normalize_zoho_accounts_base_url(cls, v: object) -> str:
        default = "https://accounts.zoho.com"
        if v is None:
            return default
        if isinstance(v, str):
            t = v.strip().rstrip("/")
            return t if t else default
        return default

    @field_validator("zoho_crm_api_base", mode="before")
    @classmethod
    def normalize_zoho_crm_api_base(cls, v: object) -> str:
        default = "https://www.zohoapis.com"
        if v is None:
            return default
        if isinstance(v, str):
            t = v.strip().rstrip("/")
            return t if t else default
        return default

    # When request has only `zoho_record_id`, fetch file-upload field(s) from this module.
    # Default matches the Trainers module; override if your single-record layout differs.
    zoho_module_api_name: str = Field(default="Trainers")
    # CRM field API names for File Upload fields (defaults match Trainers layout).
    zoho_cv_field_api_name: str = Field(default="Trainer_CV")
    zoho_outline_field_api_name: str | None = None

    # Optional: course / parent record flow — outline on parent, multi-select lookup to Trainers, CV on each trainer.
    # Set ZOHO_PARENT_MODULE_API_NAME to your parent module API name (e.g. Closure_Activities); other names default below.
    zoho_parent_module_api_name: str | None = None
    zoho_parent_outline_field_api_name: str = Field(default="Final_Course_Outline")
    zoho_parent_trainers_lookup_field_api_name: str = Field(default="Trainers")
    # Target module for each linked trainer id (Search API + Get Record).
    zoho_trainer_module_api_name: str = Field(default="Trainers")
    zoho_trainer_cv_field_api_name: str = Field(default="Trainer_CV")
    zoho_trainer_unique_code_field_api_name: str = Field(default="Trainer_Unique_Code")
    # When parent field has display text only (e.g. "Sabith Test"), search Trainers by this field then fetch CV + code.
    zoho_trainer_lookup_resolve_by_name: bool = True
    # Trainers module field API name to match parent text (Name, Full_Name, custom text field, …).
    zoho_trainer_search_field_api_name: str = Field(default="Name")
    # Parent module field used as Drive folder name (ai_automation/trainer_profile/{course}/).
    zoho_parent_course_name_field_api_name: str = Field(default="Product_Course_Name1")

    # Google Drive OAuth (for uploading generated trainer profile PDFs).
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    # Optional parent folder in Drive; if empty, My Drive root is used.
    google_drive_folder_id: str | None = None
    # After PDF is saved, upload to Drive when OAuth env vars are set. Set to false to disable uploads.
    google_drive_auto_upload: bool = True
    google_drive_fallback_course_name: str = "Course"

    @field_validator(
        "zoho_module_api_name",
        "zoho_trainer_module_api_name",
        "zoho_parent_trainers_lookup_field_api_name",
        mode="before",
    )
    @classmethod
    def default_zoho_trainers_module_name(cls, v: object) -> str:
        return _zoho_str_or_default(v, "Trainers")

    @field_validator("zoho_cv_field_api_name", "zoho_trainer_cv_field_api_name", mode="before")
    @classmethod
    def default_zoho_trainer_cv_field(cls, v: object) -> str:
        return _zoho_str_or_default(v, "Trainer_CV")

    @field_validator("zoho_trainer_unique_code_field_api_name", mode="before")
    @classmethod
    def default_zoho_trainer_unique_code_field(cls, v: object) -> str:
        return _zoho_str_or_default(v, "Trainer_Unique_Code")

    @field_validator("zoho_parent_outline_field_api_name", mode="before")
    @classmethod
    def default_zoho_parent_outline_field(cls, v: object) -> str:
        return _zoho_str_or_default(v, "Final_Course_Outline")

    @field_validator("zoho_trainer_lookup_resolve_by_name", mode="before")
    @classmethod
    def coerce_zoho_trainer_resolve_bool(cls, v: object) -> bool:
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("0", "false", "no", "off", ""):
                return False
            return s in ("1", "true", "yes", "on")
        return bool(v)

    @field_validator("zoho_trainer_search_field_api_name", mode="before")
    @classmethod
    def strip_trainer_search_field(cls, v: object) -> str:
        if v is None or not isinstance(v, str):
            return "Name"
        t = v.strip()
        return t if t else "Name"

    @field_validator("google_drive_auto_upload", mode="before")
    @classmethod
    def coerce_google_drive_auto_upload(cls, v: object) -> bool:
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("0", "false", "no", "off", ""):
                return False
            return s in ("1", "true", "yes", "on")
        return bool(v)

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
