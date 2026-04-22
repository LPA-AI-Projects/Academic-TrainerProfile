"""
Download CRM attachment bytes from Zoho using OAuth2 (refresh token or static access token).
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from ..config import get_settings

logger = logging.getLogger("trainer_profile.zoho")

_TOKEN_CACHE: dict[str, object] = {"access_token": "", "expires_at": 0.0}


def _crm_api_host(dc: str) -> str:
    dc = (dc or "com").strip().lower().lstrip(".")
    if dc == "com":
        return "https://www.zohoapis.com"
    return f"https://www.zohoapis.{dc}"


def _accounts_host(dc: str) -> str:
    dc = (dc or "com").strip().lower().lstrip(".")
    if dc == "com":
        return "https://accounts.zoho.com"
    return f"https://accounts.zoho.{dc}"


def _get_access_token() -> str:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    settings = get_settings()
    static = (settings.zoho_access_token or "").strip()
    if static and not settings.zoho_refresh_token:
        return static

    cid = (settings.zoho_client_id or "").strip()
    csec = (settings.zoho_client_secret or "").strip()
    refresh = (settings.zoho_refresh_token or "").strip()

    if refresh and cid and csec:
        now = time.time()
        cached = str(_TOKEN_CACHE.get("access_token") or "")
        exp = float(_TOKEN_CACHE.get("expires_at") or 0.0)
        if cached and now < exp - 120:
            return cached

        url = f"{_accounts_host(settings.zoho_dc)}/oauth/v2/token"
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
        logger.info("Zoho access token refreshed expires_in=%s", expires_in)
        return token

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
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'requests'. Install backend requirements first."
        ) from exc

    settings = get_settings()
    token = _get_access_token()
    base = _crm_api_host(settings.zoho_dc)
    url = f"{base}/crm/v2/files"
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
