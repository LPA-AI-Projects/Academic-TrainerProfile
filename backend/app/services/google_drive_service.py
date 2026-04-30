import os
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
