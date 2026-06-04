"""
Download CRM attachment bytes from Zoho using OAuth2 (refresh token or static access token).
"""

from __future__ import annotations

import json
import re
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


def _extension_from_content_type(ctype: str) -> str | None:
    c = (ctype or "").split(";")[0].strip().lower()
    if not c:
        return None
    if "pdf" in c:
        return ".pdf"
    if "msword" in c or "wordprocessingml" in c:
        return ".docx"
    if "plain" in c:
        return ".txt"
    return None


def _extension_from_zoho_attachment(value: object) -> str | None:
    """Guess extension from Zoho file-upload metadata (``extn``, ``name`` in field JSON)."""
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            ext = _extension_from_zoho_attachment(item)
            if ext:
                return ext
        return None
    if not isinstance(value, dict):
        return None
    extn = str(value.get("extn") or value.get("Extn") or "").strip().lower()
    if extn == "pdf":
        return ".pdf"
    if extn in ("word", "doc", "docx", "document"):
        return ".docx"
    if extn in ("text", "txt"):
        return ".txt"
    name = str(
        value.get("name")
        or value.get("Name")
        or value.get("File_Name__s")
        or value.get("file_name__s")
        or ""
    ).strip().lower()
    for suffix in (".docx", ".pdf", ".txt", ".md", ".rtf"):
        if name.endswith(suffix):
            return suffix
    nested = value.get("value")
    if nested is not None:
        return _extension_from_zoho_attachment(nested)
    return None


def _extension_from_file_bytes(data: bytes) -> str | None:
    if len(data) >= 4 and data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 2 and data[:2] == b"PK":
        return ".docx"
    return None


def download_crm_file_to_path(
    file_id: str,
    dest_dir: Path,
    *,
    attachment_meta: object | None = None,
) -> Path:
    """
    Download a CRM file by id (Zoho CRM /files?id=...) and write under dest_dir.

    Returns absolute path to the saved file (extension from Content-Type, Zoho metadata, or file signature).
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
    ext = (
        _extension_from_content_type(ctype)
        or _extension_from_zoho_attachment(attachment_meta)
        or _extension_from_file_bytes(resp.content)
        or ".bin"
    )
    if ext == ".bin":
        logger.warning(
            "ZOHO_FILE_EXTENSION_GUESS_FAILED file_id=%s content_type=%r attachment_meta_type=%s "
            "bytes=%s — parser may reject .bin",
            file_id,
            ctype or "(none)",
            type(attachment_meta).__name__ if attachment_meta is not None else "None",
            len(resp.content),
        )

    out = dest_dir / f"zoho_cv_{file_id}_{uuid.uuid4().hex[:10]}{ext}"
    if not resp.content:
        raise RuntimeError(
            f"Zoho file download returned 0 bytes for id={file_id!r}. "
            "For v8 attachments use File_Id__s (file hash), not the attachment row id."
        )

    out.write_bytes(resp.content)
    logger.info(
        "Zoho file downloaded file_id=%s bytes=%s ext=%s content_type=%s path=%s",
        file_id,
        len(resp.content),
        ext,
        ctype or "(none)",
        out,
    )
    return out


def _crm_get(path: str, params: dict | None = None) -> dict:
    """GET Zoho CRM path (v2 or v8) with optional query string."""
    return _crm_get_with_params(path, params)


def _crm_v2_get(path: str) -> dict:
    """GET Zoho CRM v2 path (e.g. /crm/v2/Leads/123)."""
    return _crm_get_with_params(path, None)


def _crm_v2_get_with_params(path: str, params: dict | None) -> dict:
    """Backward-compatible alias for :func:`_crm_get_with_params`."""
    return _crm_get_with_params(path, params)


def _crm_get_with_params(path: str, params: dict | None) -> dict:
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
    text = (resp.text or "").strip()
    if resp.status_code == 204 or not text:
        return {}
    try:
        return resp.json()
    except ValueError:
        logger.error(
            "Zoho CRM GET non-JSON path=%s status=%s body_preview=%s",
            path,
            resp.status_code,
            text[:400],
        )
        return {}


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
        data = _crm_get_with_params(path, {"criteria": crit})
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


# Keys for /crm/v2/files?id=... — v8 attachments use File_Id__s; must be before generic ``id``.
_FILE_DOWNLOAD_ID_KEYS: tuple[str, ...] = (
    "File_Id__s",
    "file_id__s",
    "file_Id",
    "file_id",
    "File_Id",
    "File_ID",
    "attachment_Id",
    "attachment_id",
    "Attachment_Id",
    "id",
    "Id",
)


def _is_zoho_file_download_id(value: str) -> bool:
    """
    True when ``value`` is a Zoho **file hash** for GET /crm/v2/files, not a numeric CRM/attachment row id.
    """
    s = (value or "").strip()
    if not s:
        return False
    if _looks_like_zoho_crm_record_id(s):
        return False
    return True


def _file_id_from_attachment_dict(value: dict) -> str | None:
    """Pick the best file download id from one Zoho file-upload / attachment metadata dict."""
    record_fallback: str | None = None
    for key in _FILE_DOWNLOAD_ID_KEYS:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        s = raw.strip()
        if _is_zoho_file_download_id(s):
            return s
        if _looks_like_zoho_crm_record_id(s) and record_fallback is None:
            record_fallback = s
    for key, raw in value.items():
        if not isinstance(key, str) or not isinstance(raw, str) or not raw.strip():
            continue
        kl = key.lower()
        if "file" in kl and "id" in kl:
            s = raw.strip()
            if _is_zoho_file_download_id(s):
                return s
    if record_fallback:
        logger.warning(
            "ZOHO_FILE_ID_NUMERIC_FALLBACK id=%s (prefer File_Id__s / file_Id when present) keys=%s",
            record_fallback,
            list(value.keys())[:12],
        )
        return record_fallback
    nested = value.get("value")
    if nested is not None:
        return extract_file_id_from_zoho_field(nested)
    return None


def extract_file_id_from_zoho_field(value: object) -> str | None:
    """
    Parse Zoho File Upload field value(s) and return a CRM file id for /crm/v2/files?id=...

    v8 attachment rows expose ``File_Id__s`` (file hash) and ``id`` (attachment row id) — only the
    former works with the Files API.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        return _file_id_from_attachment_dict(value)
    if isinstance(value, list):
        for item in value:
            fid = extract_file_id_from_zoho_field(item)
            if fid:
                return fid
        return None
    return None


def extract_file_ids_from_zoho_field(value: object) -> list[str]:
    """
    Parse Zoho File Upload field value(s) and return all CRM file ids in source order.
    Supports scalar strings, dict wrappers, and list payloads; removes duplicates.
    """
    out: list[str] = []

    def _push(fid: str | None) -> None:
        if not fid:
            return
        clean = str(fid).strip()
        if clean and clean not in out:
            out.append(clean)

    if value is None:
        return out
    if isinstance(value, str):
        _push(value)
        return out
    if isinstance(value, dict):
        _push(extract_file_id_from_zoho_field(value))
        nested = value.get("value")
        if nested is not None:
            for fid in extract_file_ids_from_zoho_field(nested):
                _push(fid)
        return out
    if isinstance(value, list):
        for item in value:
            for fid in extract_file_ids_from_zoho_field(item):
                _push(fid)
        return out
    return out


def fetch_crm_record(
    module_api_name: str,
    crm_record_id: str,
    *,
    api_version: str = "v8",
) -> dict:
    """
    Fetch one CRM record by id.

    Defaults to **v8** (same as Zoho UI / Postman) so multi-module lookup subforms return full
    nested ``{{Trainers: {{id, name}}, id: junction_row_id}}`` lists instead of collapsed
    summary strings from v2 (e.g. ``'PRIYA MENON, Dalia E.. & More'``).
    """
    module_api_name = (module_api_name or "").strip()
    crm_record_id = (crm_record_id or "").strip()
    if not module_api_name or not crm_record_id:
        raise ValueError("module_api_name and crm_record_id are required")
    ver = (api_version or "v8").strip().lower()
    if ver not in ("v2", "v8"):
        ver = "v8"

    last_err: Exception | None = None
    attempts = (ver, "v2") if ver == "v8" else (ver,)
    for attempt_ver in attempts:
        path = f"/crm/{attempt_ver}/{module_api_name}/{crm_record_id}"
        try:
            data = _crm_get(path)
            rows = data.get("data")
            if not isinstance(rows, list) or not rows:
                msg = (
                    f"Zoho CRM record not found or empty: api={attempt_ver} "
                    f"module={module_api_name} id={crm_record_id}"
                )
                if attempt_ver == "v8" and "v2" in attempts:
                    logger.warning("%s; retrying v2", msg)
                    last_err = RuntimeError(msg)
                    continue
                raise RuntimeError(msg)
            row = rows[0]
            if not isinstance(row, dict):
                raise RuntimeError("Unexpected Zoho CRM record shape")
            keys = sorted(row.keys())
            logger.info(
                "ZOHO_CRM_RECORD_GET api=%s module=%s id=%s field_count=%s field_names=%s",
                attempt_ver,
                module_api_name,
                crm_record_id,
                len(keys),
                keys[:80],
            )
            return row
        except Exception as exc:
            last_err = exc
            if attempt_ver == "v2" or "v2" not in attempts:
                raise
            logger.warning(
                "ZOHO_CRM_RECORD_GET_V8_FAILED module=%s id=%s err=%s; retrying v2",
                module_api_name,
                crm_record_id,
                exc,
            )
    if last_err:
        raise last_err
    raise RuntimeError(f"Zoho CRM record fetch failed: module={module_api_name} id={crm_record_id}")


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


def _normalize_collapsed_lookup_display(s: str) -> str:
    """Strip Zoho UI suffixes like ``'..., Dalia E.. & More'`` before name search."""
    t = (s or "").strip()
    t = re.sub(r"\s*&\s*More\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def _lookup_linked_record_id(item: dict, *, lookup_field_name: str = "") -> str | None:
    """
    Linked module record id from a lookup row.

    Closure_Activities subform rows look like::

        {"Trainers": {"id": "<trainer_id>", "name": "PRIYA MENON"}, "id": "<junction_row_id>"}

    The top-level ``id`` is the subform/junction row — **not** the Trainers module id.
    """
    if not isinstance(item, dict):
        return None
    lf = (lookup_field_name or "").strip()
    prefer_keys = tuple(k for k in (lf, "Trainers") if k)

    for key in prefer_keys:
        nested = item.get(key)
        if isinstance(nested, dict):
            rid = str(nested.get("id") or nested.get("Id") or "").strip()
            if _looks_like_zoho_crm_record_id(rid):
                return rid

    linked: list[str] = []
    for k, v in item.items():
        if k in ("id", "Id") or str(k).startswith("$"):
            continue
        if isinstance(v, dict):
            rid = str(v.get("id") or v.get("Id") or "").strip()
            if _looks_like_zoho_crm_record_id(rid):
                linked.append(rid)

    if len(linked) == 1:
        return linked[0]

    top = str(item.get("id") or item.get("Id") or "").strip()
    if _looks_like_zoho_crm_record_id(top) and not linked:
        return top
    return None


def extract_multiselect_lookup_ids(
    raw: object,
    *,
    lookup_field_name: str = "",
) -> list[str]:
    """
    Parse Zoho **multi-select lookup** / **lookup** / **subform** values into linked record ids.

    Supports:
    - Multi-select lookup: ``[{"id": "...", "name": "..."}, ...]``
    - Single lookup: ``{"id": "...", "name": "..."}``
    - Multi-module subform (v8): ``[{"Trainers": {"id": "...", "name": "..."}, "id": "..."}, ...]``

    Plain strings are **only** treated as ids when they look like Zoho record ids (long digits).
    Collapsed UI strings (e.g. ``'PRIYA MENON, Dalia E.. & More'``) return ``[]``.
    """
    out: list[str] = []
    seen: set[str] = set()
    lf = (lookup_field_name or "").strip()

    def push(rid: str) -> None:
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)

    if raw is None:
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=None raw_preview=(null) parsed_ids=[] count=0",
        )
        return out
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                rid = _lookup_linked_record_id(item, lookup_field_name=lf)
                if rid:
                    push(rid)
            elif isinstance(item, str) and item.strip() and _looks_like_zoho_crm_record_id(item):
                push(item.strip())
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=list lookup_field=%s raw_preview=%s parsed_ids=%s count=%s",
            lf or "(none)",
            format_zoho_field_debug(raw),
            out,
            len(out),
        )
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if s and _looks_like_zoho_crm_record_id(s):
            push(s)
        logger.info(
            "ZOHO_MS_LOOKUP_PARSE raw_type=str value_preview=%r parsed_ids=%s count=%s collapsed=%s",
            s[:200] if s else s,
            out,
            len(out),
            bool(s and "&" in s.lower() and "more" in s.lower()),
        )
        return out
    if isinstance(raw, dict):
        rid = _lookup_linked_record_id(raw, lookup_field_name=lf)
        if rid:
            push(rid)
    logger.info(
        "ZOHO_MS_LOOKUP_PARSE raw_type=%s lookup_field=%s raw_preview=%s parsed_ids=%s count=%s",
        type(raw).__name__,
        lf or "(none)",
        format_zoho_field_debug(raw),
        out,
        len(out),
    )
    return out


def extract_lookup_search_terms(
    raw: object,
    *,
    lookup_field_name: str = "",
) -> list[str]:
    """
    Human-readable labels from a Zoho lookup / multi-select value for Search API resolution.

    Used when ``extract_multiselect_lookup_ids`` returns no ids (text field, or list items with
    ``name`` but missing ``id``). Skips values that look like CRM record ids.
    """
    out: list[str] = []
    seen: set[str] = set()
    lf = (lookup_field_name or "").strip()

    def add(value: object) -> None:
        t = _normalize_collapsed_lookup_display(str(value or ""))
        t = re.sub(r"\.{2,}$", "", t).strip()
        if not t or _looks_like_zoho_crm_record_id(t):
            return
        if re.search(r"&\s*more\s*$", t, flags=re.IGNORECASE):
            return
        key = t.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(t)

    if raw is None:
        return out
    if isinstance(raw, str):
        normalized = _normalize_collapsed_lookup_display(raw)
        for part in re.split(r"[,;]", normalized):
            add(part)
        return out
    if isinstance(raw, dict):
        nested = raw.get(lf) if lf else None
        if isinstance(nested, dict):
            for key in ("name", "Name", "display_value", "Display_Value"):
                v = nested.get(key)
                if v is not None:
                    add(v)
        for key in ("name", "Name", "display_value", "Display_Value"):
            v = raw.get(key)
            if v is not None:
                add(v)
        return out
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                if _lookup_linked_record_id(item, lookup_field_name=lf):
                    continue
                if lf:
                    nested = item.get(lf)
                    if isinstance(nested, dict):
                        for key in ("name", "Name", "display_value", "Display_Value"):
                            v = nested.get(key)
                            if v is not None:
                                add(v)
                for key in ("name", "Name", "display_value", "Display_Value"):
                    v = item.get(key)
                    if v is not None:
                        add(v)
            elif isinstance(item, str) and not _looks_like_zoho_crm_record_id(item):
                add(item)
        return out
    return out


def resolve_trainer_record_ids_from_parent_lookup(
    lookup_raw: object,
    *,
    trainer_module: str,
    lookup_field_name: str = "",
    resolve_by_name: bool,
    search_field_api_name: str,
) -> list[str]:
    """
    Resolve Trainers module record ids from a parent lookup field.

    Main setup: parse CRM lookup ids from v8 nested subform rows (``Trainers.id``).
    When that yields no ids and ``resolve_by_name`` is enabled, search the Trainers module using
    display labels from the field (plain text, or ``name`` on lookup objects without an id).
    """
    trainer_mod = (trainer_module or "").strip()
    lf = (lookup_field_name or "").strip()
    trainer_ids = extract_multiselect_lookup_ids(lookup_raw, lookup_field_name=lf)
    if trainer_ids or not resolve_by_name or not trainer_mod:
        return trainer_ids

    match_field = (search_field_api_name or "").strip()
    if not match_field:
        return trainer_ids

    for part in extract_lookup_search_terms(lookup_raw, lookup_field_name=lf):
        found: list[str] = []
        for op in ("equals", "starts_with"):
            found = search_crm_record_ids_by_field(
                trainer_mod, match_field, part, operator=op
            )
            if found:
                logger.info(
                    "GEN_PARENT_NAME_SEARCH_HIT part=%r operator=%s field=%s ids=%s",
                    part,
                    op,
                    match_field,
                    found,
                )
                break
        for rid in found:
            if rid not in trainer_ids:
                trainer_ids.append(rid)

    return trainer_ids


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
        # Multiline / textarea occasionally returned as a single-key wrapper dict.
        if result is None and raw:
            if len(raw) == 1:
                sole = next(iter(raw.values()))
                if isinstance(sole, str) and sole.strip():
                    result = sole.strip()
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        result = str(raw).strip() or None
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


def list_crm_record_attachments(*, module_api_name: str, crm_record_id: str) -> list[dict]:
    """
    List file attachments on a CRM record (names used for trainer PDF ``{code}_vN`` sequencing).

    Uses CRM v2 ``GET /crm/v2/{module}/{record_id}/Attachments``.
    """
    mod = (module_api_name or "").strip().strip("/")
    rid = (crm_record_id or "").strip()
    if not mod or not rid:
        return []
    path = f"/crm/v2/{mod}/{rid}/Attachments"
    try:
        data = _crm_v2_get(path)
    except Exception:
        logger.exception(
            "ZOHO_LIST_ATTACHMENTS_FAILED module=%s record_id=%s",
            mod,
            rid,
        )
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    logger.info(
        "ZOHO_LIST_ATTACHMENTS_OK module=%s record_id=%s count=%s",
        mod,
        rid,
        len(rows),
    )
    return rows


def attach_crm_v8_attachment_link(
    *,
    module_api_name: str,
    crm_record_id: str,
    public_url: str,
    title: str,
) -> dict:
    """
    POST ``/crm/v8/{module}/{record_id}/Attachments`` with multipart ``attachmentUrl`` + ``title``.

    Zoho fetches the URL server-side and stores a linked attachment on the record.
    See: https://www.zoho.com/crm/developer/docs/api/v8/upload-attachment.html
    """
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    from urllib.parse import quote

    mod = (module_api_name or "").strip().strip("/")
    rid = (crm_record_id or "").strip()
    url_in = (public_url or "").strip()
    if not mod or not rid or not url_in:
        raise ValueError("module_api_name, crm_record_id, and public_url are required")

    token = _get_access_token()
    base = _crm_api_base()
    path = f"{base}/crm/v8/{quote(mod, safe='')}/{quote(rid, safe='')}/Attachments"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    files = {
        "attachmentUrl": (None, url_in),
        "title": (None, (title or "Trainer profile")[:255]),
    }
    logger.info(
        "ZOHO_CRM_ATTACH_LINK_START module=%s record_id=%s title_len=%s url_len=%s",
        mod,
        rid,
        len(title or ""),
        len(url_in),
    )
    resp = requests.post(path, headers=headers, files=files, timeout=120)
    if resp.status_code == 401 and _can_use_refresh_token():
        logger.warning("Zoho CRM attach got 401; refreshing token once record_id=%s", rid)
        _invalidate_token_cache()
        token = _get_access_token(force_refresh=True)
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = requests.post(path, headers=headers, files=files, timeout=120)
    if not resp.ok:
        logger.error(
            "ZOHO_CRM_ATTACH_LINK_FAILED module=%s record_id=%s status=%s body=%s",
            mod,
            rid,
            resp.status_code,
            (resp.text or "")[:2000],
        )
    resp.raise_for_status()
    try:
        out = resp.json() if resp.content else {}
    except Exception:
        out = {"raw": (resp.text or "")[:500]}
    logger.info("ZOHO_CRM_ATTACH_LINK_OK module=%s record_id=%s", mod, rid)
    return out if isinstance(out, dict) else {"data": out}


def list_crm_record_attachments(*, module_api_name: str, crm_record_id: str) -> list[dict]:
    """
    GET ``/crm/v8/{module}/{record_id}/Attachments`` — used to pick the next ``{TrainerCode}_vN`` attachment title.

    See: https://www.zoho.com/crm/developer/docs/api/v8/get-attachments.html
    """
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    from urllib.parse import quote

    mod = (module_api_name or "").strip().strip("/")
    rid = (crm_record_id or "").strip()
    if not mod or not rid:
        raise ValueError("module_api_name and crm_record_id are required")

    token = _get_access_token()
    base = _crm_api_base()
    path = f"{base}/crm/v8/{quote(mod, safe='')}/{quote(rid, safe='')}/Attachments"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    params = {"fields": "id,File_Name,Created_Time"}
    logger.info("ZOHO_CRM_ATTACH_LIST_START module=%s record_id=%s", mod, rid)
    resp = requests.get(path, headers=headers, params=params, timeout=60)
    if resp.status_code == 401 and _can_use_refresh_token():
        logger.warning("Zoho CRM list attachments got 401; refreshing token once record_id=%s", rid)
        _invalidate_token_cache()
        token = _get_access_token(force_refresh=True)
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = requests.get(path, headers=headers, params=params, timeout=60)
    if not resp.ok:
        logger.error(
            "ZOHO_CRM_ATTACH_LIST_FAILED module=%s record_id=%s status=%s body=%s",
            mod,
            rid,
            resp.status_code,
            (resp.text or "")[:2000],
        )
    resp.raise_for_status()
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def delete_crm_record_attachment(
    *,
    module_api_name: str,
    crm_record_id: str,
    attachment_id: str,
) -> dict:
    """
    DELETE ``/crm/v8/{module}/{record_id}/Attachments/{attachment_id}``.

    See: https://www.zoho.com/crm/developer/docs/api/v8/delete-attachments.html
    """
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    from urllib.parse import quote

    mod = (module_api_name or "").strip().strip("/")
    rid = (crm_record_id or "").strip()
    aid = (attachment_id or "").strip()
    if not mod or not rid or not aid:
        raise ValueError("module_api_name, crm_record_id, and attachment_id are required")

    token = _get_access_token()
    base = _crm_api_base()
    path = (
        f"{base}/crm/v8/{quote(mod, safe='')}/{quote(rid, safe='')}/Attachments/{quote(aid, safe='')}"
    )
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    logger.info("ZOHO_CRM_ATTACH_DELETE_START module=%s record_id=%s attachment_id=%s", mod, rid, aid)
    resp = requests.delete(path, headers=headers, timeout=60)
    if resp.status_code == 401 and _can_use_refresh_token():
        logger.warning("Zoho CRM delete attachment got 401; refreshing token once record_id=%s", rid)
        _invalidate_token_cache()
        token = _get_access_token(force_refresh=True)
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        resp = requests.delete(path, headers=headers, timeout=60)
    if not resp.ok:
        logger.error(
            "ZOHO_CRM_ATTACH_DELETE_FAILED module=%s record_id=%s attachment_id=%s status=%s body=%s",
            mod,
            rid,
            aid,
            resp.status_code,
            (resp.text or "")[:2000],
        )
    resp.raise_for_status()
    try:
        out = resp.json() if resp.content else {}
    except Exception:
        out = {"raw": (resp.text or "")[:500]}
    logger.info("ZOHO_CRM_ATTACH_DELETE_OK module=%s record_id=%s attachment_id=%s", mod, rid, aid)
    return out if isinstance(out, dict) else {"data": out}
