"""
Bitrix24 inbound webhook client, chat-message parser, and CRM record helpers.

Webhook base example:
  https://learnerspoint.bitrix24.com/rest/161836/m5u7wmgnjide8ss0/
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import get_settings
from ..utils.logger import get_logger

logger = get_logger(__name__)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BITRIX_URL_TAG = re.compile(r"\[url(?:=([^\]]+))?\](.*?)\[/url\]", re.IGNORECASE | re.DOTALL)


def normalize_bitrix_chat_text(text: str) -> str:
    """Unpack Bitrix BBCode ``[URL]...[/URL]`` links so URL parsers see plain https URLs."""
    if not text:
        return ""

    def _repl(match: re.Match[str]) -> str:
        href = (match.group(1) or match.group(2) or "").strip()
        return f"\n{href}\n" if href else " "

    out = _BITRIX_URL_TAG.sub(_repl, text)
    out = re.sub(r"\[/?[^\]]+\]", " ", out)
    return out


def _sanitize_extracted_url(url: str) -> str:
    """Strip BBCode residue and glued duplicate URLs from a single extracted link."""
    u = (url or "").strip().rstrip('",\'')
    for sep in ("]", "[/url", "%5d", "%5b", " "):
        low = u.lower()
        idx = low.find(sep)
        if idx > 0:
            u = u[:idx]
    m = re.match(r"(https?://[^\s\"'<>\[\]]+)", u, re.IGNORECASE)
    if m:
        u = m.group(1)
    return u.rstrip("/.,;)]")


_ZOHO_CRM_TAB_RECORD_PATH = re.compile(r"(?i)/crm/(org\d+)/tab/([^/]+)/(\d+)")


def is_zoho_trainer_crm_url(url: str) -> bool:
    """True for standard crm.zoho.com tab links and Zoho One cxapp CRM tab links."""
    raw = _sanitize_extracted_url((url or "").strip())
    if not raw:
        return False
    low = raw.lower()
    if "crm.zoho.com/crm" in low:
        return True
    if "one.zoho.com" in low:
        path = urlparse(raw).path or raw
        return bool(_ZOHO_CRM_TAB_RECORD_PATH.search(path))
    return False


def normalize_zoho_trainer_profile_url(url: str) -> str:
    """
    Normalize Zoho One cxapp CRM tab links to standard ``crm.zoho.com`` URLs (Bitrix layer only).

    Example::

      https://one.zoho.com/.../cxapp/crm/org901534269/tab/CustomModule1/7026232000023625041
    → https://crm.zoho.com/crm/org901534269/tab/CustomModule1/7026232000023625041
    """
    raw = _sanitize_extracted_url((url or "").strip())
    if not raw:
        return raw
    if "crm.zoho.com/crm" in raw.lower():
        return raw.rstrip("/")
    path = urlparse(raw).path or raw
    m = _ZOHO_CRM_TAB_RECORD_PATH.search(path)
    if not m:
        return raw
    org_id, tab_module, record_id = m.group(1), m.group(2), m.group(3)
    normalized = f"https://crm.zoho.com/crm/{org_id}/tab/{tab_module}/{record_id}"
    if normalized != raw.rstrip("/"):
        logger.info("ZOHO_URL_NORMALIZED from=%s to=%s", raw[:120], normalized)
    return normalized


def normalize_zoho_trainer_profile_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    for u in urls:
        if not is_zoho_trainer_crm_url(u):
            continue
        norm = normalize_zoho_trainer_profile_url(u)
        if norm and norm not in out:
            out.append(norm)
    return out


def message_contains_zoho_trainer_crm_link(text: str) -> bool:
    return any(is_zoho_trainer_crm_url(u) for u in _clean_quoted_urls(text or ""))


def _clean_quoted_urls(blob: str) -> list[str]:
    blob = normalize_bitrix_chat_text(blob or "")
    urls = _URL_RE.findall(blob)
    cleaned: list[str] = []
    for u in urls:
        u = _sanitize_extracted_url(u)
        if not u or "your_file" in u.lower():
            continue
        if u not in cleaned:
            cleaned.append(u)
    return cleaned


@dataclass(frozen=True)
class ParsedTrainerProfileChat:
    """Structured fields extracted from a Bitrix chat / automation message."""

    outline_url: str | None
    outline_urls: list[str]
    zoho_trainer_urls: list[str]
    bitrix_record_id: str | None = None
    entity_type_id: int | None = None
    course_name: str | None = None


@dataclass(frozen=True)
class ZohoCrmLinkRef:
    record_id: str
    module_api_name: str
    source_url: str


def _bitrix_webhook_base() -> str:
    base = (get_settings().bitrix_rest_webhook_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "BITRIX_REST_WEBHOOK_URL is not configured (e.g. "
            "https://your-domain.bitrix24.com/rest/USER_ID/WEBHOOK_TOKEN)"
        )
    return base


def bitrix_call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a Bitrix24 REST method via the inbound webhook."""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'requests'. Install backend requirements first.") from exc

    method = (method or "").strip().lstrip("/")
    if not method:
        raise ValueError("Bitrix REST method name is required")

    url = f"{_bitrix_webhook_base()}/{method}"
    payload = params or {}
    resp = requests.post(url, json=payload, timeout=120)
    if not resp.ok:
        logger.error(
            "BITRIX_REST_FAIL method=%s status=%s body=%s",
            method,
            resp.status_code,
            (resp.text or "")[:800],
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected Bitrix REST response for {method!r}")
    if "error" in data:
        raise RuntimeError(
            f"Bitrix REST error method={method!r} code={data.get('error')} "
            f"description={data.get('error_description')}"
        )
    return data


def fetch_crm_item(entity_type_id: int, record_id: int | str) -> dict[str, Any]:
    """Return the ``item`` dict from ``crm.item.get``."""
    eid = int(entity_type_id)
    rid = int(str(record_id).strip())
    data = bitrix_call(
        "crm.item.get",
        {"entityTypeId": eid, "id": rid, "useOriginalUfNames": "Y"},
    )
    item = data.get("result", {}).get("item")
    if not isinstance(item, dict):
        raise RuntimeError(
            f"Bitrix CRM item not found entityTypeId={eid} id={rid}"
        )
    logger.info(
        "BITRIX_CRM_ITEM_GET entityTypeId=%s id=%s field_count=%s",
        eid,
        rid,
        len(item),
    )
    return item


def _scalar_from_bitrix_field(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("value", "VALUE", "url", "URL", "text", "TEXT"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if isinstance(value, list) and value:
        return _scalar_from_bitrix_field(value[0])
    return None


def _urls_from_bitrix_field(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _URL_RE.findall(value)
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                for key in ("url", "urlMachine", "downloadUrl", "DOWNLOAD_URL"):
                    u = item.get(key)
                    if isinstance(u, str) and u.strip():
                        out.append(u.strip())
                        break
                else:
                    out.extend(_urls_from_bitrix_field(item.get("value")))
            elif isinstance(item, str):
                out.extend(_URL_RE.findall(item))
        return out
    if isinstance(value, dict):
        for key in ("url", "urlMachine", "downloadUrl"):
            u = value.get(key)
            if isinstance(u, str) and u.strip():
                return [u.strip()]
        scalar = _scalar_from_bitrix_field(value)
        if scalar:
            return _URL_RE.findall(scalar)
    return []


def extract_outline_url_from_bitrix_item(item: dict[str, Any], field_name: str) -> str | None:
    """Read outline Google Drive / file URL from a configured Bitrix CRM field."""
    field_name = (field_name or "").strip()
    if not field_name:
        return None
    raw = item.get(field_name)
    if raw is None:
        # camelCase fallback when useOriginalUfNames=Y still misses alias keys
        alt = field_name[0].lower() + field_name[1:] if field_name else field_name
        raw = item.get(alt)
    urls = _urls_from_bitrix_field(raw)
    if urls:
        return urls[0]
    scalar = _scalar_from_bitrix_field(raw)
    if scalar and scalar.startswith("http"):
        return scalar
    return None


def extract_zoho_links_from_bitrix_item(item: dict[str, Any], field_name: str) -> list[str]:
    field_name = (field_name or "").strip()
    if not field_name:
        return []
    raw = item.get(field_name)
    if raw is None:
        alt = field_name[0].lower() + field_name[1:] if field_name else field_name
        raw = item.get(alt)
    scalar = _scalar_from_bitrix_field(raw)
    urls: list[str] = []
    if scalar:
        urls.extend(_URL_RE.findall(scalar))
    urls.extend(_urls_from_bitrix_field(raw))
    zoho_only: list[str] = []
    for u in urls:
        if not is_zoho_trainer_crm_url(u):
            continue
        norm = normalize_zoho_trainer_profile_url(u)
        if norm not in zoho_only:
            zoho_only.append(norm)
    return zoho_only


def download_bitrix_file_to_path(download_url: str, dest_dir: Path) -> Path:
    """Download a Bitrix CRM / disk file (``url`` or ``urlMachine``) to a temp path."""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency 'requests'.") from exc

    url = (download_url or "").strip()
    if not url:
        raise ValueError("Bitrix file download URL is empty")
    dest_dir.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=180, allow_redirects=True)
    resp.raise_for_status()
    suffix = ".bin"
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        if "." in name:
            suffix = "." + name.rsplit(".", 1)[-1].lower()[:8]
    elif "pdf" in (resp.headers.get("content-type") or "").lower():
        suffix = ".pdf"
    elif "word" in (resp.headers.get("content-type") or "").lower():
        suffix = ".docx"
    out = dest_dir / f"bitrix_{next(tempfile._get_candidate_names())}{suffix}"
    out.write_bytes(resp.content)
    logger.info("BITRIX_FILE_DOWNLOADED url=%s bytes=%s path=%s", url[:120], len(resp.content), out)
    return out


def _zoho_url_segment_is_api_module(segment: str) -> bool:
    """
    Zoho CRM browser URLs use ``/tab/CustomModule1/{id}`` — ``CustomModule1`` is not the REST API name.
    """
    s = (segment or "").strip()
    if not s:
        return False
    if re.match(r"^CustomModule\d+$", s, re.IGNORECASE):
        return False
    if re.match(r"^org\d+$", s, re.IGNORECASE):
        return False
    if s.lower() in ("crm", "tab", "entity", "custom", "module", "org"):
        return False
    return True


def parse_zoho_crm_url(url: str, *, default_module: str | None = None) -> ZohoCrmLinkRef | None:
    """
    Parse Zoho CRM record URLs such as:
      https://crm.zoho.com/crm/org901534269/tab/CustomModule1/7026232000010532226

    Extracts the numeric **record id** from the URL. The module API name comes from
    ``ZOHO_TRAINER_MODULE_API_NAME`` (default ``Trainers``) when the URL only contains
    a UI tab name like ``CustomModule1``.
    """
    raw = normalize_zoho_trainer_profile_url(_sanitize_extracted_url((url or "").strip()))
    if not raw or "zoho.com/crm" not in raw.lower():
        return None
    parsed = urlparse(raw)
    parts = [p for p in parsed.path.split("/") if p]
    record_id: str | None = None
    url_module: str | None = None
    for i, part in enumerate(parts):
        if part.lower() in ("tab", "entity", "custom", "module") and i + 2 < len(parts):
            url_module = parts[i + 1]
            record_id = parts[i + 2]
            break
    if not record_id:
        tail = parts[-1] if parts else ""
        id_m = re.match(r"^(\d+)", tail)
        if id_m:
            record_id = id_m.group(1)
            if len(parts) >= 2:
                url_module = parts[-2]
    elif record_id:
        id_m = re.match(r"^(\d+)", record_id)
        record_id = id_m.group(1) if id_m else record_id.strip("[]")
    if not record_id:
        return None

    settings = get_settings()
    fallback_mod = (
        (default_module or "").strip()
        or (settings.zoho_trainer_module_api_name or "").strip()
        or "Trainers"
    )
    if url_module and _zoho_url_segment_is_api_module(url_module):
        mod = url_module
    else:
        mod = fallback_mod
        if url_module and not _zoho_url_segment_is_api_module(url_module):
            logger.info(
                "ZOHO_URL_MODULE_IGNORED url_segment=%s using_api_module=%s record_id=%s",
                url_module,
                mod,
                record_id,
            )

    return ZohoCrmLinkRef(record_id=record_id, module_api_name=mod, source_url=raw)


def parse_zoho_crm_urls(urls: list[str], *, default_module: str | None = None) -> list[ZohoCrmLinkRef]:
    out: list[ZohoCrmLinkRef] = []
    seen: set[str] = set()
    for u in urls:
        ref = parse_zoho_crm_url(u, default_module=default_module)
        if not ref:
            continue
        key = f"{ref.module_api_name}:{ref.record_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def parse_trainerprofile_chat_message(text: str) -> ParsedTrainerProfileChat:
    """
    Parse chat / automation text such as::

        trainer_profile

        outline:
        https://drive.google.com/file/d/abc/view

        trainers:
        https://crm.zoho.com/.../CustomModule1/123
        https://crm.zoho.com/.../456

    Legacy format also supported::

        Trainerprofile :
        Outline : "https://drive.google.com/..."
        TrainerZohoLink : "https://crm.zoho.com/.../123","https://crm.zoho.com/.../456"
    """
    raw = (text or "").strip()
    raw = normalize_bitrix_chat_text(raw)
    if not raw:
        return ParsedTrainerProfileChat(outline_url=None,outline_urls=[], zoho_trainer_urls=[])

    outline_url: str | None = None
    zoho_urls: list[str] = []

    # Slash command blocks: outline: / trainers:
    outline_block = re.search(
        r"(?is)(?:^|\n)\s*outline\s*:\s*\n?(.*?)(?=\n\s*trainers?\s*:|$)",
        raw,
    )
    if outline_block:
        outline_urls = _clean_quoted_urls(outline_block.group(1))
        outline_url = outline_urls[0] if outline_urls else None

    trainers_block = re.search(r"(?is)(?:^|\n)\s*trainers?\s*:\s*\n?(.*)$", raw)
    if trainers_block:
        zoho_urls = normalize_zoho_trainer_profile_urls(_clean_quoted_urls(trainers_block.group(1)))

    outline_m = re.search(
        r"(?im)^\s*Outline(?:Link)?\s*:\s*(.+)$",
        raw,
    )
    if outline_m and not outline_url:
        outline_urls = _clean_quoted_urls(outline_m.group(1))
        outline_url = outline_urls[0] if outline_urls else None

    zoho_m = re.search(
        r"(?im)^\s*TrainerZohoLink(?:s)?\s*:\s*(.+)$",
        raw,
    )
    if zoho_m and not zoho_urls:
        zoho_urls = normalize_zoho_trainer_profile_urls(_clean_quoted_urls(zoho_m.group(1)))

    # Fallback: any Zoho CRM links anywhere in the message
    if not zoho_urls:
        zoho_urls = normalize_zoho_trainer_profile_urls(_clean_quoted_urls(raw))

    # Fallback outline: first Google Drive / Docs link not already used as Zoho
    if not outline_url:
        for u in _clean_quoted_urls(raw):
            low = u.lower()
            if "drive.google.com" in low or "docs.google.com" in low:
                outline_url = u
                break

    bitrix_id: str | None = None
    entity_type_id: int | None = None
    id_m = re.search(r"(?im)^\s*(?:Bitrix(?:Record)?Id|RecordId|Id)\s*:\s*(\d+)\s*$", raw)
    if id_m:
        bitrix_id = id_m.group(1)
    et_m = re.search(r"(?im)^\s*(?:EntityTypeId|entity_type_id)\s*:\s*(\d+)\s*$", raw)
    if et_m:
        entity_type_id = int(et_m.group(1))

    course_name: str | None = None
    cn_m = re.search(r"(?im)^\s*(?:CourseName|course_name|Product)\s*:\s*(.+)$", raw)
    if cn_m:
        course_name = cn_m.group(1).strip().strip('"').strip("'") or None

    return ParsedTrainerProfileChat(
        outline_url=outline_url,
        outline_urls=outline_urls,
        zoho_trainer_urls=zoho_urls,
        bitrix_record_id=bitrix_id,
        entity_type_id=entity_type_id,
        course_name=course_name,
    )


def merge_webhook_payload(
    *,
    message: str | None,
    outline: str | None,
    trainer_zoho_links: str | None,
    bitrix_record_id: str | None,
    entity_type_id: int | str | None,
    course_name: str | None,
) -> ParsedTrainerProfileChat:
    """Merge explicit webhook form/JSON fields with optional chat message body."""
    parsed = parse_trainerprofile_chat_message(message or "")
    # outline_url = (outline or "").strip() or parsed.outline_url
    outline_urls = []
    if outline:
        outline_urls.extend(_clean_quoted_urls(outline))
    outline_urls.extend(parsed.outline_urls)
    outline_urls = list(dict.fromkeys(outline_urls))
    outline_url = outline_urls[0] if outline_urls else None

    zoho_blob = (trainer_zoho_links or "").strip()
    if zoho_blob:
        zoho_urls = normalize_zoho_trainer_profile_urls(_clean_quoted_urls(zoho_blob))
    else:
        zoho_urls = normalize_zoho_trainer_profile_urls(list(parsed.zoho_trainer_urls))
    rid = (bitrix_record_id or "").strip() or parsed.bitrix_record_id
    et: int | None = parsed.entity_type_id
    if entity_type_id is not None and str(entity_type_id).strip().isdigit():
        et = int(str(entity_type_id).strip())
    cn = (course_name or "").strip() or parsed.course_name
    return ParsedTrainerProfileChat(
        outline_url=outline_url,
        outline_urls=outline_urls,
        zoho_trainer_urls=zoho_urls,
        bitrix_record_id=rid,
        entity_type_id=et,
        course_name=cn,
    )


def enrich_from_bitrix_record(parsed: ParsedTrainerProfileChat) -> ParsedTrainerProfileChat:
    """
    When ``bitrix_record_id`` and ``entity_type_id`` are known, load outline / Zoho links
    from configured CRM fields when not already present in the message.
    """
    settings = get_settings()
    rid = (parsed.bitrix_record_id or "").strip()
    et = parsed.entity_type_id
    if not rid:
        default_et = settings.bitrix_entity_type_id
        if default_et:
            et = et or int(default_et)
        return parsed
    if et is None:
        default_et = settings.bitrix_entity_type_id
        if not default_et:
            return parsed
        et = int(default_et)

    item = fetch_crm_item(et, rid)
    outline_url = parsed.outline_url
    outline_field = (settings.bitrix_outline_field_api_name or "").strip()
    if not outline_url and outline_field:
        outline_url = extract_outline_url_from_bitrix_item(item, outline_field)

    zoho_urls = list(parsed.zoho_trainer_urls)
    zoho_field = (settings.bitrix_zoho_links_field_api_name or "").strip()
    if zoho_field:
        for u in extract_zoho_links_from_bitrix_item(item, zoho_field):
            if u not in zoho_urls:
                zoho_urls.append(u)

    course_name = parsed.course_name
    cn_field = (settings.bitrix_course_name_field_api_name or "").strip()
    if not course_name and cn_field:
        course_name = _scalar_from_bitrix_field(item.get(cn_field))

    return ParsedTrainerProfileChat(
        outline_url=outline_url,
        zoho_trainer_urls=zoho_urls,
        bitrix_record_id=rid,
        entity_type_id=et,
        course_name=course_name,
    )


def format_bitrix_field_debug(value: object, max_len: int = 400) -> str:
    if value is None:
        return "(null)"
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except TypeError:
        s = repr(value)
    return s if len(s) <= max_len else s[: max_len - 3] + "..."
