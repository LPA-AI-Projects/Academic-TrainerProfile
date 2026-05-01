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

    # When request has only `zoho_record_id`, fetch file-upload field(s) from this module (e.g. Trainers, Contacts).
    zoho_module_api_name: str | None = None
    # CRM field API names for File Upload fields (configure to match your Zoho layout).
    zoho_cv_field_api_name: str | None = None
    zoho_outline_field_api_name: str | None = None

    # Optional: course / parent record flow — outline on parent, multi-select lookup to Trainers, CV on each trainer.
    # When all of these are set, `zoho_record_id` in the request is the *parent* record id (e.g. course).
    zoho_parent_module_api_name: str | None = None
    # File upload on parent (e.g. Final_Course_Outline).
    zoho_parent_outline_field_api_name: str | None = None
    # Multi-select lookup on parent → Trainers (field API name on the parent module).
    zoho_parent_trainers_lookup_field_api_name: str | None = None
    # Target module for each linked id (API name, e.g. Trainers).
    zoho_trainer_module_api_name: str | None = None
    zoho_trainer_cv_field_api_name: str | None = None
    # Auto number or text — shown as main heading (e.g. Trainer_Unique_code).
    zoho_trainer_unique_code_field_api_name: str | None = None
    # When parent lookup returns plain text (not {id,name}), resolve Trainers via Search Records API.
    zoho_trainer_lookup_resolve_by_name: bool = False
    # Field on the **Trainers** module to match (API name), e.g. Name or Last_Name — required for name resolve.
    zoho_trainer_search_field_api_name: str | None = None

    # Google Drive OAuth (for uploading generated trainer profile PDFs).
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    # Optional parent folder in Drive; if empty, My Drive root is used.
    google_drive_folder_id: str | None = None

    @field_validator("zoho_trainer_lookup_resolve_by_name", mode="before")
    @classmethod
    def coerce_zoho_trainer_resolve_bool(cls, v: object) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
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
