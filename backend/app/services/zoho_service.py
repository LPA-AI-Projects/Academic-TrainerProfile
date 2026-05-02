"""
Download CRM attachment bytes from Zoho using OAuth2 (refresh token or static access token).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from ..config import get_settings, normalize_zoho_dc_value
from ..utils.logger import get_logger

logger = get_logger(__name__)

_TOKEN_CACHE: dict[str, object] = {"access_token": "", "expires_at": 0.0, "api_domain": ""}


def format_zoho_field_debug(value: object, max_len: int = 500) -> str:
    """Short, log-safe string for Zoho field payloads (truncated; never includes tokens)."""
    if value is None:
        return "(null)"
    if isinstance(value, (str, int, float, bool)):
        s = repr(value)
        return s if len(s) <= max_len else s[: max_len - 3] + "..."
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        s = repr(value)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


_TOKEN_LOCK = threading.Lock()

# Refresh the access token this many seconds before Zoho's expires_in (access tokens are ~1 hour).
_TOKEN_REFRESH_SKEW_SEC = 120


def _crm_api_host(dc: str) -> str:
    suf = normalize_zoho_dc_value(dc)
    if suf == "com":
        return "https://www.zohoapis.com"
    return f"https://www.zohoapis.{suf}"


def _accounts_host(dc: str) -> str:
    suf = normalize_zoho_dc_value(dc)
    if suf == "com":
        return "https://accounts.zoho.com"
    return f"https://accounts.zoho.{suf}"


def _invalidate_token_cache() -> None:
    """Clear cached access token (e.g. after 401 or before forced refresh)."""
    _TOKEN_CACHE["access_token"] = ""
    _TOKEN_CACHE["expires_at"] = 0.0
    # Keep api_domain: Zoho returns it with refresh and it stays valid for the org.


def _resolved_accounts_base() -> str:
    """OAuth token host: explicit ``zoho_accounts_base_url`` or derived from ``zoho_dc``."""
    s = get_settings()
    u = (s.zoho_accounts_base_url or "").strip().rstrip("/")
    if u:
        return u
    return _accounts_host(s.zoho_dc)


def _crm_api_base() -> str:
    """
    Base URL for CRM APIs.

    1. ``api_domain`` from the last refresh-token response (Zoho OAuth).
    2. Explicit ``zoho_crm_api_base`` in settings (same role as ZOHO_CRM_API_BASE elsewhere).
    3. Derived from ``zoho_dc`` (e.g. www.zohoapis.com for com).
    """
    domain = str(_TOKEN_CACHE.get("api_domain") or "").strip().rstrip("/")
    if domain:
        return domain
    s = get_settings()
    explicit = (s.zoho_crm_api_base or "").strip().rstrip("/")
    if explicit:
        return explicit
    return _crm_api_host(s.zoho_dc)


def _can_use_refresh_token() -> bool:
    s = get_settings()
    return bool(
        (s.zoho_refresh_token or "").strip()
        and (s.zoho_client_id or "").strip()
        and (s.zoho_client_secret or "").strip()
    )


def _refresh_access_token_with_lock(*, force: bool) -> str:
    """
    Obtain a new access token using the refresh_token grant (Zoho access tokens expire ~3600s).
    Uses a lock so concurrent requests do not stampede the token endpoint.
    """
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    settings = get_settings()
    cid = (settings.zoho_client_id or "").strip()
    csec = (settings.zoho_client_secret or "").strip()
    refresh = (settings.zoho_refresh_token or "").strip()

    with _TOKEN_LOCK:
        now = time.time()
        if not force:
            cached = str(_TOKEN_CACHE.get("access_token") or "")
            exp = float(_TOKEN_CACHE.get("expires_at") or 0.0)
            if cached and now < exp - _TOKEN_REFRESH_SKEW_SEC:
                return cached

        url = f"{_resolved_accounts_base()}/oauth/v2/token"
        resp = requests.post(
            url,
            data={
                "refresh_token": refresh,
                "client_id": cid,
                "client_secret": csec,
                "grant_type": "refresh_token",
            },
            timeout=60,
        )
        if not resp.ok:
            logger.error("Zoho token refresh failed status=%s body=%s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Zoho token response missing access_token")
        expires_in = int(data.get("expires_in") or 3600)
        _TOKEN_CACHE["access_token"] = token
        _TOKEN_CACHE["expires_at"] = now + max(60, expires_in)
        api_domain = str(data.get("api_domain") or "").strip().rstrip("/")
        if api_domain:
            _TOKEN_CACHE["api_domain"] = api_domain
        logger.info(
            "Zoho access token refreshed expires_in=%s api_domain=%s cache_until_epoch=%.0f",
            expires_in,
            api_domain or "(from ZOHO_DC)",
            float(_TOKEN_CACHE["expires_at"]),
        )
        return token


def _get_access_token(*, force_refresh: bool = False) -> str:
    """
    Return a valid Zoho-oauthtoken value.

    With refresh_token configured: uses in-memory cache until shortly before expiry
    (Zoho access tokens last about one hour), then refreshes. Call with force_refresh=True
    after a 401 to obtain a new access token immediately.
    """
    settings = get_settings()
    static = (settings.zoho_access_token or "").strip()
    if static and not (settings.zoho_refresh_token or "").strip():
        return static

    if _can_use_refresh_token():
        if not force_refresh:
            now = time.time()
            cached = str(_TOKEN_CACHE.get("access_token") or "")
            exp = float(_TOKEN_CACHE.get("expires_at") or 0.0)
            if cached and now < exp - _TOKEN_REFRESH_SKEW_SEC:
                return cached
        return _refresh_access_token_with_lock(force=force_refresh)

    if static:
        return static

    raise RuntimeError(
        "Zoho is not configured: set ZOHO_ACCESS_TOKEN, or set "
        "ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET + ZOHO_REFRESH_TOKEN "
        "(optional ZOHO_DC, default com)."
    )


def download_crm_file_to_path(file_id: str, dest_dir: Path) -> Path:
    """
    Download a CRM file by id (Zoho CRM /files?id=...) and write under dest_dir.

    Returns absolute path to the saved file (extension guessed from Content-Type when possible).
    """
    file_id = (file_id or "").strip()
    if not file_id:
        raise ValueError("Zoho file id is empty")
    logger.info("ZOHO_FILE_DOWNLOAD_START file_id=%s dest_dir=%s", file_id, dest_dir)
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    token = _get_access_token()
    base = _crm_api_base()
    url = f"{base}/crm/v2/files"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    resp = requests.get(url, headers=headers, params={"id": file_id}, timeout=120)
    if resp.status_code == 401 and _can_use_refresh_token():
        logger.warning("Zoho file download got 401; refreshing access token and retrying once id=%s", file_id)
        _invalidate_token_cache()
        token = _get_access_token(force_refresh=True)
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = requests.get(url, headers=headers, params={"id": file_id}, timeout=120)
    if not resp.ok:
        logger.error(
            "Zoho file download failed id=%s status=%s body=%s",
            file_id,
            resp.status_code,
            (resp.text or "")[:800],
        )
    resp.raise_for_status()

    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = ".bin"
    if "pdf" in ctype:
        ext = ".pdf"
    elif "msword" in ctype or "wordprocessingml" in ctype:
        ext = ".docx"
    elif "plain" in ctype:
        ext = ".txt"

    out = dest_dir / f"zoho_cv_{file_id}_{uuid.uuid4().hex[:10]}{ext}"
    out.write_bytes(resp.content)
    logger.info("Zoho CV downloaded file_id=%s bytes=%s path=%s", file_id, len(resp.content), out)
    return out


def _crm_v2_get(path: str) -> dict:
    """GET Zoho CRM v2 path (e.g. /crm/v2/Leads/123)."""
    return _crm_v2_get_with_params(path, None)


def _crm_v2_get_with_params(path: str, params: dict | None) -> dict:
    """GET with optional query string (e.g. Search Records ``?criteria=...``)."""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    token = _get_access_token()
    base = _crm_api_base()
    url = f"{base}{path}"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=120)
    if resp.status_code == 401 and _can_use_refresh_token():
        logger.warning("Zoho CRM GET got 401; refreshing access token and retrying once path=%s", path)
        _invalidate_token_cache()
        token = _get_access_token(force_refresh=True)
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = requests.get(url, headers=headers, params=params or {}, timeout=120)
    if not resp.ok:
        logger.error(
            "Zoho CRM GET failed path=%s status=%s body=%s",
            path,
            resp.status_code,
            (resp.text or "")[:800],
        )
    resp.raise_for_status()
    return resp.json()


def _looks_like_zoho_crm_record_id(s: str) -> bool:
    """Zoho record ids are long digit strings; reject display names like 'Sabith Test'."""
    t = (s or "").strip()
    if len(t) < 10 or not t.isdigit():
        return False
    return True


def search_crm_record_ids_by_field(
    module_api_name: str,
    field_api_name: str,
    value: str,
    *,
    operator: str = "equals",
) -> list[str]:
    """
    Zoho CRM `Search Records` API (v2): match one field.

    ``operator`` is the Zoho criteria operator, e.g. ``equals``, ``starts_with``.

    See: https://www.zoho.com/crm/developer/docs/api/v2/search-records.html
    """
    mod = (module_api_name or "").strip()
    field = (field_api_name or "").strip()
    v = (value or "").strip()
    op = (operator or "equals").strip() or "equals"
    if not mod or not field or not v:
        return []
    crit = f"({field}:{op}:{v})"
    path = f"/crm/v2/{mod}/search"
    try:
        data = _crm_v2_get_with_params(path, {"criteria": crit})
    except Exception:
        logger.exception(
            "ZOHO_SEARCH_FAILED module=%s field=%s op=%s value_len=%s",
            mod,
            field,
            op,
            len(v),
        )
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            rid = str(row.get("id") or "").strip()
            if rid:
                out.append(rid)
    logger.info(
        "ZOHO_SEARCH_OK module=%s field=%s op=%s value_preview=%r match_count=%s",
        mod,
        field,
        op,
        v[:120],
        len(out),
    )
    return out


def search_crm_record_ids_by_field_equals(
    module_api_name: str,
    field_api_name: str,
    value: str,
) -> list[str]:
    """Backward-compatible alias for ``search_crm_record_ids_by_field(..., operator=\"equals\")``."""
    return search_crm_record_ids_by_field(
        module_api_name, field_api_name, value, operator="equals"
    )


def extract_file_id_from_zoho_field(value: object) -> str | None:
    """
    Parse Zoho File Upload field value(s) and return a CRM file id for /crm/v2/files?id=...
    Handles dict, list of dicts, and plain id strings.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        for key in (
            "file_Id",
            "file_id",
            "File_Id",
            "File_ID",
            "id",
            "Id",
            "attachment_id",
            "Attachment_Id",
        ):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        # Nested single-file shape
        nested = value.get("value")
        if nested is not None:
            return extract_file_id_from_zoho_field(nested)
        return None
    if isinstance(value, list):
        for item in value:
            fid = extract_file_id_from_zoho_field(item)
            if fid:
                return fid
        return None
    return None


def fetch_crm_record(module_api_name: str, crm_record_id: str) -> dict:
    module_api_name = (module_api_name or "").strip()
    crm_record_id = (crm_record_id or "").strip()
    if not module_api_name or not crm_record_id:
        raise ValueError("module_api_name and crm_record_id are required")
    # Path segment: module API name + record id
    path = f"/crm/v2/{module_api_name}/{crm_record_id}"
    data = _crm_v2_get(path)
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Zoho CRM record not found or empty: module={module_api_name} id={crm_record_id}")
    row = rows[0]
    if not isinstance(row, dict):
        raise RuntimeError("Unexpected Zoho CRM record shape")
    keys = sorted(row.keys())
    logger.info(
        "ZOHO_CRM_RECORD_GET module=%s id=%s field_count=%s field_names=%s",
        module_api_name,
        crm_record_id,
        len(keys),
        keys[:80],
    )
    return row


def get_file_id_from_record_field(
    module_api_name: str,
    crm_record_id: str,
    field_api_name: str,
) -> str | None:
    field_api_name = (field_api_name or "").strip()
    if not field_api_name:
        return None
    record = fetch_crm_record(module_api_name, crm_record_id)
    raw = record.get(field_api_name)
    fid = extract_file_id_from_zoho_field(raw)
    logger.info(
        "ZOHO_CRM_FILE_FIELD module=%s record_id=%s field=%s resolved_file_id=%s raw_type=%s raw_preview=%s",
        module_api_name,
        crm_record_id,
        field_api_name,
        fid or "(none)",
        type(raw).__name__,
        format_zoho_field_debug(raw),
    )
    return fid


def extract_multiselect_lookup_ids(raw: object) -> list[str]:
    """
    Parse Zoho **multi-select lookup** / **lookup** values into CRM record id strings.

    Zoho Get Record typically returns:
    - Multi-select lookup: ``[{"id": "...", "name": "..."}, ...]``
    - Single lookup: ``{"id": "...", "name": "..."}``

    Plain strings are **only** treated as ids when they look like Zoho record ids (long digits).
    A human-readable string (e.g. ``'Sabith Test'``) returns ``[]`` — use a real lookup field or
    enable name search via ``ZOHO_TRAINER_LOOKUP_RESOLVE_BY_NAME`` + ``ZOHO_TRAINER_SEARCH_FIELD_API_NAME``.
    """
    out: list[str] = []
    if raw is None:
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=None raw_preview=(null) parsed_ids=[] count=0",
        )
        return out
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rid = str(item.get("id") or item.get("Id") or "").strip()
                if rid:
                    out.append(rid)
            elif isinstance(item, str) and item.strip() and _looks_like_zoho_crm_record_id(item):
                out.append(item.strip())
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=list raw_preview=%s parsed_ids=%s count=%s",
            format_zoho_field_debug(raw),
            out,
            len(out),
        )
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if s and _looks_like_zoho_crm_record_id(s):
            out = [s]
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=str value_preview=%r parsed_ids=%s count=%s",
            s[:200] if s else s,
            out,
            len(out),
        )
        return out
    if isinstance(raw, dict):
        rid = str(raw.get("id") or raw.get("Id") or "").strip()
        if rid:
            out.append(rid)
    logger.info(
        "ZOHO_MS_LOOKUP_PARSE raw_type=%s raw_preview=%s parsed_ids=%s count=%s",
        type(raw).__name__,
        format_zoho_field_debug(raw),
        out,
        len(out),
    )
    return out


def get_scalar_field_str(record: dict, field_api_name: str) -> str | None:
    """Read a text / number / auto-number / single-line field as string."""
    if not field_api_name:
        return None
    raw = record.get(field_api_name)
    # Zoho API names are case-sensitive; env often uses Trainer_Unique_code vs CRM Trainer_Unique_Code.
    if raw is None:
        fl = field_api_name.lower()
        if "trainer" in fl and "unique" in fl:
            for alt in ("Trainer_Unique_Code", "Trainer_Unique_code"):
                if alt == field_api_name:
                    continue
                t = record.get(alt)
                if t is not None:
                    raw = t
                    logger.info(
                        "ZOHO_SCALAR_FIELD_ALIAS resolved=%s requested=%s",
                        alt,
                        field_api_name,
                    )
                    break
    if raw is None:
        logger.info("ZOHO_SCALAR_FIELD field=%s raw_type=None resolved=(null)", field_api_name)
        return None
    result: str | None = None
    if isinstance(raw, dict):
        for key in ("name", "Name", "display_value", "Display_Value", "value"):
            v = raw.get(key)
            if v is not None and str(v).strip():
                result = str(v).strip()
                break
    else:
        s = str(raw).strip()
        result = s or None
    logger.info(
        "ZOHO_SCALAR_FIELD field=%s raw_type=%s raw_preview=%s resolved=%s",
        field_api_name,
        type(raw).__name__,
        format_zoho_field_debug(raw),
        result or "(empty)",
    )
    return result
