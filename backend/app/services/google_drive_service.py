import os
import re
from pathlib import Path
from typing import Any

import requests

from ..config import get_settings
from ..utils.logger import get_logger

logger = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_RESUMABLE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
GOOGLE_DRIVE_PERMISSIONS_URL_TMPL = "https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
GOOGLE_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_DRIVE_MY_DRIVE_ROOT_ID = "root"
_FOLDER_AI_AUTOMATION = "ai_automation"
_FOLDER_TRAINER_PROFILE = "trainer_profile"


class GoogleDriveUploadError(RuntimeError):
    """Raised when Google Drive upload/auth fails."""


class GoogleDriveDownloadError(RuntimeError):
    """Raised when Google Drive outline download fails."""


_DRIVE_FILE_ID_PATTERNS = (
    re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"drive\.google\.com/uc\?(?:[^#]*&)?id=([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)", re.I),
    re.compile(r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)", re.I),
)


def extract_google_drive_file_id(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    for pat in _DRIVE_FILE_ID_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1)
    return None


def _guess_suffix_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "word" in ct or "officedocument.wordprocessingml" in ct:
        return ".docx"
    if "plain" in ct:
        return ".txt"
    return ".bin"


def _download_public_drive_file(file_id: str, dest_dir: Path) -> Path:
    """Best-effort download for publicly shared Drive files (no OAuth)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        f"https://drive.google.com/uc?export=download&id={file_id}",
        f"https://docs.google.com/document/d/{file_id}/export?format=pdf",
        f"https://docs.google.com/document/d/{file_id}/export?format=txt",
    ]
    last_err: Exception | None = None
    for url in candidates:
        try:
            resp = requests.get(url, timeout=180, allow_redirects=True)
            if resp.status_code >= 400 or not resp.content:
                continue
            suffix = _guess_suffix_from_content_type(resp.headers.get("content-type") or "")
            out = dest_dir / f"gdrive_{file_id[:24]}{suffix}"
            out.write_bytes(resp.content)
            logger.info(
                "DRIVE_PUBLIC_DOWNLOAD file_id=%s bytes=%s path=%s url=%s",
                file_id,
                len(resp.content),
                out,
                url[:100],
            )
            return out
        except Exception as exc:
            last_err = exc
            continue
    raise GoogleDriveDownloadError(
        f"Could not download public Google Drive file id={file_id!r}: {last_err}"
    )


def _download_oauth_drive_file(file_id: str, dest_dir: Path, access_token: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta_resp = requests.get(
        f"{GOOGLE_DRIVE_FILES_URL}/{file_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": "id,name,mimeType"},
        timeout=60,
    )
    if meta_resp.status_code >= 400:
        raise GoogleDriveDownloadError(
            f"Google Drive metadata failed: HTTP {meta_resp.status_code} body={(meta_resp.text or '')[:800]}"
        )
    meta = meta_resp.json() if meta_resp.content else {}
    mime = str(meta.get("mimeType") or "")
    name = str(meta.get("name") or file_id)
    suffix = Path(name).suffix.lower() if "." in name else ""
    if mime == "application/vnd.google-apps.document":
        export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
        export_params = {"mimeType": "application/pdf"}
        suffix = suffix or ".pdf"
    elif mime == "application/vnd.google-apps.spreadsheet":
        export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
        export_params = {"mimeType": "application/pdf"}
        suffix = suffix or ".pdf"
    else:
        export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        export_params = {"alt": "media"}
        if not suffix:
            suffix = _guess_suffix_from_content_type(mime)

    resp = requests.get(
        export_url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=export_params,
        timeout=300,
    )
    if resp.status_code >= 400:
        raise GoogleDriveDownloadError(
            f"Google Drive download failed: HTTP {resp.status_code} body={(resp.text or '')[:800]}"
        )
    out = dest_dir / f"gdrive_{file_id[:24]}{suffix or '.bin'}"
    out.write_bytes(resp.content)
    logger.info("DRIVE_OAUTH_DOWNLOAD file_id=%s bytes=%s path=%s", file_id, len(resp.content), out)
    return out


def download_drive_file_to_path(url: str, dest_dir: Path) -> Path:
    """
    Download an outline file from a Google Drive / Docs share link to ``dest_dir``.
    Uses OAuth when configured; otherwise tries public export URLs.
    """
    file_id = extract_google_drive_file_id(url)
    if not file_id:
        raise GoogleDriveDownloadError(f"Could not parse Google Drive file id from URL: {url!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        access_token = _get_access_token()
        return _download_oauth_drive_file(file_id, dest_dir, access_token)
    except GoogleDriveUploadError:
        logger.info("DRIVE_DOWNLOAD_FALLBACK_PUBLIC file_id=%s (OAuth unavailable)", file_id)
        return _download_public_drive_file(file_id, dest_dir)


def _credential(name_upper: str, setting_attr: str) -> str:
    settings = get_settings()
    value = (os.getenv(name_upper) or "").strip()
    if not value:
        value = str(getattr(settings, setting_attr, "") or "").strip()
    return value


def _get_required_credential(name_upper: str, setting_attr: str) -> str:
    value = _credential(name_upper, setting_attr)
    if not value:
        raise GoogleDriveUploadError(f"Missing required environment variable: {name_upper}")
    return value


def _sanitize_drive_name(name: str) -> str:
    cleaned = (name or "").strip()
    forbidden = '\\/:*?"<>|'
    for ch in forbidden:
        cleaned = cleaned.replace(ch, "_")
    return cleaned[:120] or "course"


def _get_access_token() -> str:
    client_id = _get_required_credential("GOOGLE_CLIENT_ID", "google_client_id")
    client_secret = _get_required_credential("GOOGLE_CLIENT_SECRET", "google_client_secret")
    refresh_token = _get_required_credential("GOOGLE_REFRESH_TOKEN", "google_refresh_token")
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    response = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=30)
    if response.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google OAuth token exchange failed: HTTP {response.status_code} body={(response.text or '')[:1000]}"
        )
    payload = response.json() if response.content else {}
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleDriveUploadError("Google OAuth token response missing access_token.")
    return access_token


def _resolve_parent_folder_id() -> str:
    parent = _credential("GOOGLE_DRIVE_FOLDER_ID", "google_drive_folder_id")
    return parent or _DRIVE_MY_DRIVE_ROOT_ID


def _find_folder_by_name(*, name: str, parent_folder_id: str, access_token: str) -> dict[str, str] | None:
    safe_name_q = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name_q}' and mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}' and trashed = false "
        f"and '{parent_folder_id}' in parents"
    )
    response = requests.get(
        GOOGLE_DRIVE_FILES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query, "fields": "files(id,name)", "pageSize": 1},
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google Drive folder search failed: HTTP {response.status_code} body={(response.text or '')[:2000]}"
        )
    files = (response.json() or {}).get("files") or []
    if files:
        folder_id = str(files[0].get("id") or "").strip()
        if folder_id:
            return {"folder_id": folder_id, "folder_link": f"https://drive.google.com/drive/folders/{folder_id}"}
    return None


def _ensure_folder(*, name: str, parent_folder_id: str, access_token: str) -> dict[str, str]:
    existing = _find_folder_by_name(name=name, parent_folder_id=parent_folder_id, access_token=access_token)
    if existing:
        return existing

    metadata: dict[str, Any] = {"name": name, "mimeType": GOOGLE_DRIVE_FOLDER_MIME, "parents": [parent_folder_id]}
    response = requests.post(
        GOOGLE_DRIVE_FILES_URL,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=metadata,
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google Drive folder create failed: HTTP {response.status_code} body={(response.text or '')[:2000]}"
        )
    payload = response.json() if response.content else {}
    folder_id = str(payload.get("id") or "").strip()
    if not folder_id:
        raise GoogleDriveUploadError("Google Drive folder creation succeeded but folder id was missing.")
    return {"folder_id": folder_id, "folder_link": f"https://drive.google.com/drive/folders/{folder_id}"}


def _set_public_read_permission(file_id: str, access_token: str) -> None:
    permission_payload = {"type": "anyone", "role": "reader"}
    permission_url = GOOGLE_DRIVE_PERMISSIONS_URL_TMPL.format(file_id=file_id)
    response = requests.post(
        permission_url,
        headers={"Authorization": f"Bearer {access_token}"},
        json=permission_payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google Drive permission update failed: HTTP {response.status_code} body={(response.text or '')[:2000]}"
        )


def upload_trainer_profile_pdf(*, pdf_bytes: bytes, unique_code: str, course_name: str) -> dict[str, str]:
    """
    Ensure Drive hierarchy:
    {parent}/ai_automation/trainer_profile/{course_name}/
    Upload file as: {unique_code}_{course_name}.pdf
    """
    if not pdf_bytes:
        raise GoogleDriveUploadError("Cannot upload empty PDF bytes to Google Drive.")

    safe_course = _sanitize_drive_name(course_name)
    safe_unique = _sanitize_drive_name(unique_code) or "trainer"
    filename = f"{safe_unique}_{safe_course}.pdf"
    logger.info(
        "DRIVE_UPLOAD_START filename=%s pdf_bytes=%s course_folder=%s",
        filename,
        len(pdf_bytes),
        safe_course,
    )
    access_token = _get_access_token()
    parent = _resolve_parent_folder_id()

    ai_folder = _ensure_folder(name=_FOLDER_AI_AUTOMATION, parent_folder_id=parent, access_token=access_token)
    trainer_folder = _ensure_folder(
        name=_FOLDER_TRAINER_PROFILE, parent_folder_id=ai_folder["folder_id"], access_token=access_token
    )
    course_folder = _ensure_folder(
        name=safe_course, parent_folder_id=trainer_folder["folder_id"], access_token=access_token
    )

    start_resp = requests.post(
        GOOGLE_DRIVE_RESUMABLE_UPLOAD_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/pdf",
            "X-Upload-Content-Length": str(len(pdf_bytes)),
        },
        json={"name": filename, "mimeType": "application/pdf", "parents": [course_folder["folder_id"]]},
        timeout=120,
    )
    if start_resp.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google Drive PDF resumable init failed: HTTP {start_resp.status_code} body={(start_resp.text or '')[:2000]}"
        )
    resumable_url = (start_resp.headers.get("Location") or "").strip()
    if not resumable_url:
        raise GoogleDriveUploadError("Google Drive resumable init succeeded but Location header was missing.")

    upload_resp = requests.put(
        resumable_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/pdf",
        },
        data=pdf_bytes,
        timeout=300,
    )
    if upload_resp.status_code >= 400:
        raise GoogleDriveUploadError(
            f"Google Drive PDF upload failed: HTTP {upload_resp.status_code} body={(upload_resp.text or '')[:2000]}"
        )
    uploaded = upload_resp.json() if upload_resp.content else {}
    file_id = str(uploaded.get("id") or "").strip()
    if not file_id:
        raise GoogleDriveUploadError("Google Drive PDF upload succeeded but file id was missing.")

    _set_public_read_permission(file_id, access_token)
    logger.info(
        "DRIVE_UPLOAD_DONE file_id=%s filename=%s folder_id=%s view_link=%s",
        file_id,
        filename,
        course_folder["folder_id"],
        f"https://drive.google.com/file/d/{file_id}/view",
    )
    return {
        "file_id": file_id,
        "view_link": f"https://drive.google.com/file/d/{file_id}/view",
        "folder_id": course_folder["folder_id"],
        "folder_link": course_folder["folder_link"],
    }
