"""
Bitrix24 **outbound** webhook handling (Bitrix pushes events to our API).

Configure in Bitrix24 → Developer resources → Outbound webhook (**one** webhook):

  Handler URL: ``.../api/v1/bitrix/trainer-profile/generate``
  Event: ONTASKCOMMENTADD (Task comment added)
  Application token → BITRIX_APPLICATION_TOKEN

The backend routes generate vs refine from the comment text.

Uses BITRIX_REST_WEBHOOK_URL (inbound) only to read task chat comments via REST.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from ..config import get_settings
from ..utils.logger import get_logger
from .bitrix_service import bitrix_call

logger = get_logger(__name__)


@dataclass(frozen=True)
class ParsedTrainerProfileRefine:
    unique_code: str | None
    refine_instruction: str


@dataclass(frozen=True)
class TaskCommentRoute:
    """How a single ONTASKCOMMENTADD comment should be handled."""

    action: str  # "refine" | "generate" | "ignore"
    refine: ParsedTrainerProfileRefine | None = None


@dataclass(frozen=True)
class BitrixOutboundEvent:
    event: str
    task_id: str
    message_id: str
    raw: dict[str, str]


@dataclass(frozen=True)
class OutboundTaskComment:
    """Resolved task chat comment from an ONTASKCOMMENTADD outbound webhook."""

    event: str
    task_id: str
    message_id: str
    comment: str


def flatten_bitrix_form(form: dict[str, Any]) -> dict[str, str]:
    return {str(k): str(v).strip() for k, v in form.items() if v is not None and str(v).strip()}


def verify_bitrix_application_token(flat: dict[str, str]) -> None:
    """Validate ``auth[application_token]`` from Bitrix outbound webhook."""
    expected = (get_settings().bitrix_application_token or "").strip()
    if not expected:
        logger.warning("BITRIX_OUTBOUND token check skipped: BITRIX_APPLICATION_TOKEN not set")
        return
    token = (
        flat.get("auth[application_token]")
        or flat.get("auth[application_token]".lower())
        or flat.get("application_token")
        or ""
    ).strip()
    if token != expected:
        logger.warning("BITRIX_OUTBOUND token rejected received=%s", token[:8] + "..." if token else "(empty)")
        raise HTTPException(status_code=403, detail="Invalid Bitrix application token.")


def _positive_id(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in ("undefined", "null", "0"):
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return str(n) if n > 0 else None


def extract_task_id(flat: dict[str, str]) -> str | None:
    for key in (
        "data[FIELDS_AFTER][TASK_ID]",
        "data[FIELDS_BEFORE][TASK_ID]",
        "data[FIELDS][TASK_ID]",
        "TASK_ID",
        "task_id",
        "taskId",
    ):
        tid = _positive_id(flat.get(key))
        if tid:
            return tid
    for key in ("data[FIELDS_AFTER][ID]", "data[FIELDS][ID]", "ID", "id"):
        tid = _positive_id(flat.get(key))
        if tid:
            return tid
    return None


def extract_message_id(flat: dict[str, str]) -> str | None:
    for key in (
        "data[FIELDS_AFTER][MESSAGE_ID]",
        "data[FIELDS_BEFORE][MESSAGE_ID]",
        "MESSAGE_ID",
        "message_id",
    ):
        mid = _positive_id(flat.get(key))
        if mid:
            return mid
    return None


def parse_outbound_event(flat: dict[str, str]) -> BitrixOutboundEvent | None:
    event = (flat.get("event") or flat.get("EVENT") or "").strip().upper()
    if not event:
        return None
    task_id = extract_task_id(flat) or ""
    message_id = extract_message_id(flat) or ""
    return BitrixOutboundEvent(event=event, task_id=task_id, message_id=message_id, raw=flat)


def is_task_comment_event(event: str) -> bool:
    e = (event or "").upper().replace("_", "")
    return e in ("ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE")


def _has_outline_block(text: str) -> bool:
    return bool(re.search(r"(?im)^\s*outline\s*:", text))


def _has_trainers_block(text: str) -> bool:
    return bool(re.search(r"(?im)^\s*trainers?\s*:", text))


def _has_trainer_profile_header(text: str) -> bool:
    if re.search(r"(?im)^\s*trainer_profile\s*$", text):
        return True
    if re.search(r"(?im)^\s*/trainer-profile\s*$", text):
        return True
    low = text.lower()
    return "trainerprofile" in low.replace(" ", "") and "trainer_profile" not in low


TRAINER_PROFILE_BOT_REPLY_PREFIX = "Trainer profile "


def is_trainer_profile_bot_reply(comment: str | None) -> bool:
    """True for our own task-chat success replies (ignore to avoid webhook loops)."""
    text = (comment or "").strip()
    if not text.startswith(TRAINER_PROFILE_BOT_REPLY_PREFIX):
        return False
    first_line = text.split("\n", 1)[0].strip()
    if not first_line.endswith(" successfully."):
        return False
    return bool(re.search(r"(?im)^\s*trainer_id\s*:", text))


def is_trainer_profile_generate_command(comment: str | None) -> bool:
    """
    Generate when comment includes ``outline:`` + ``trainers:`` (optionally prefixed with ``trainer_profile``).
    """
    text = (comment or "").strip()
    if not text:
        return False
    low = text.lower()
    has_outline = _has_outline_block(text)
    has_trainers = _has_trainers_block(text)
    has_zoho = "zoho.com/crm" in low
    has_drive = "drive.google.com" in low or "docs.google.com" in low

    if has_outline and (has_trainers or has_zoho):
        return True
    if _has_trainer_profile_header(text) and has_outline and (has_trainers or has_zoho or has_drive):
        return True
    if "/trainer-profile" in low and has_drive and has_zoho:
        return True
    return False


def is_trainer_profile_refine_command(comment: str | None) -> bool:
    """
    Refine when comment has ``unique_code:`` + ``refine:`` and is **not** a generate command.

    Plain ``Refine:`` without ``unique_code:`` is ignored (avoids course-outline collisions).
    """
    text = (comment or "").strip()
    if not text:
        return False
    if is_trainer_profile_generate_command(text):
        return False
    if not re.search(r"(?im)^\s*refine\s*:", text):
        return False
    if not re.search(r"(?im)^\s*unique_code\s*:", text):
        return False
    return True


def parse_trainer_profile_refine_command(comment: str | None) -> ParsedTrainerProfileRefine | None:
    """
    Parse refine comments, e.g.::

        unique_code: TR2001
        refine:
        Make the executive summary shorter and emphasize leadership experience.
    """
    if not is_trainer_profile_refine_command(comment):
        return None
    text = (comment or "").strip()

    unique_code: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\s*unique_code\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if m:
            code = m.group(1).strip().strip('"').strip("'")
            unique_code = code or None

    refine_m = re.search(r"(?is)(?:^|\n)\s*refine\s*:\s*\n?(.*)$", text)
    if not refine_m:
        return None
    instruction = refine_m.group(1).strip()
    if len(instruction) < 10:
        logger.warning(
            "BITRIX_REFINE_TOO_SHORT chars=%s need>=10 preview=%.80s",
            len(instruction),
            instruction,
        )
        return None
    return ParsedTrainerProfileRefine(unique_code=unique_code, refine_instruction=instruction)


def route_task_comment(comment: str | None) -> TaskCommentRoute:
    """Classify one task chat comment — generate vs refine vs ignore."""
    if is_trainer_profile_bot_reply(comment):
        return TaskCommentRoute(action="ignore")
    if is_trainer_profile_generate_command(comment):
        return TaskCommentRoute(action="generate")
    refined = parse_trainer_profile_refine_command(comment)
    if refined:
        return TaskCommentRoute(action="refine", refine=refined)
    return TaskCommentRoute(action="ignore")


def resolve_outbound_task_comment(flat: dict[str, str]) -> OutboundTaskComment | None:
    """
    Parse ONTASKCOMMENTADD form payload and load comment text from Bitrix REST.

    Returns None when the event should be ignored (wrong event type).
    Raises HTTPException on missing ids or Bitrix REST failures (caller may catch).
    """
    verify_bitrix_application_token(flat)
    outbound = parse_outbound_event(flat)
    if not outbound or not is_task_comment_event(outbound.event):
        return None

    task_id = outbound.task_id
    message_id = outbound.message_id
    if not task_id or not message_id:
        raise HTTPException(
            status_code=422,
            detail="ONTASKCOMMENTADD payload missing task id or message id.",
        )

    comment = fetch_task_comment_text(task_id, message_id)
    return OutboundTaskComment(
        event=outbound.event,
        task_id=task_id,
        message_id=message_id,
        comment=comment,
    )


# Deprecated aliases — kept for imports/tests
def parse_refine_instruction(comment: str | None) -> str | None:
    parsed = parse_trainer_profile_refine_command(comment)
    return parsed.refine_instruction if parsed else None


def parse_refine_unique_code(comment: str | None) -> str | None:
    parsed = parse_trainer_profile_refine_command(comment)
    return parsed.unique_code if parsed else None


def is_trainer_profile_command(comment: str | None) -> bool:
    return is_trainer_profile_generate_command(comment)


def _message_text_from_im_row(row: dict[str, Any]) -> str:
    for key in ("text", "message", "MESSAGE", "TEXT"):
        v = row.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _text_from_comment_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("POST_MESSAGE", "postMessage", "MESSAGE", "message", "TEXT", "text"):
        v = result.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _normalize_im_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
    return []


def _find_im_message_text(messages: list[dict[str, Any]], message_id: int) -> str:
    for row in messages:
        row_id = row.get("id") or row.get("ID")
        try:
            if row_id is not None and int(row_id) == message_id:
                text = _message_text_from_im_row(row)
                if text:
                    return text
        except (TypeError, ValueError):
            continue
    return ""


def _fetch_task_chat_id(task_id: int) -> int | None:
    try:
        data = bitrix_call(
            "tasks.task.get",
            {"taskId": task_id, "select": ["ID", "CHAT_ID", "chatId"]},
        )
    except Exception as exc:
        logger.warning("BITRIX tasks.task.get failed task_id=%s err=%s", task_id, exc)
        return None
    result = data.get("result") if isinstance(data, dict) else data
    if not isinstance(result, dict):
        return None
    item = result.get("task") if isinstance(result.get("task"), dict) else result
    if not isinstance(item, dict):
        return None
    chat = item.get("chat")
    if isinstance(chat, dict) and chat.get("id") is not None:
        return int(chat["id"])
    for key in ("chatId", "CHAT_ID", "chat_id"):
        v = item.get(key)
        if v is not None:
            return int(v)
    return None


def _im_dialog_messages(dialog_id: str, *, message_id: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()
    for params in (
        {"DIALOG_ID": dialog_id, "LAST_ID": message_id + 1, "LIMIT": 50},
        {"DIALOG_ID": dialog_id, "FIRST_ID": max(message_id - 1, 0), "LIMIT": 50},
        {"DIALOG_ID": dialog_id, "LIMIT": 50},
    ):
        try:
            data = bitrix_call("im.dialog.messages.get", params)
            payload = data.get("result") if isinstance(data, dict) else data
        except Exception as exc:
            logger.warning("BITRIX im.dialog.messages.get failed dialog=%s err=%s", dialog_id, exc)
            continue
        for row in _normalize_im_messages(payload):
            rid = row.get("id") or row.get("ID")
            try:
                i = int(rid) if rid is not None else None
            except (TypeError, ValueError):
                i = None
            if i is not None and i in seen:
                continue
            if i is not None:
                seen.add(i)
            merged.append(row)
    return merged


def fetch_task_comment_text(task_id: str | int, message_id: str | int) -> str:
    """
    Load task chat comment for ONTASKCOMMENTADD.

    Tries IM dialog messages first (new task card), then legacy task.commentitem.get.
    """
    tid = int(task_id)
    mid = int(message_id)
    logger.info("BITRIX_FETCH_COMMENT task_id=%s message_id=%s", tid, mid)

    chat_id = _fetch_task_chat_id(tid)
    dialogs: list[str] = []
    if chat_id is not None:
        dialogs.append(f"chat{chat_id}")
    dialogs.extend((f"TASKS_TASK_{tid}", f"TASK_{tid}"))

    for dialog_id in dialogs:
        messages = _im_dialog_messages(dialog_id, message_id=mid)
        text = _find_im_message_text(messages, mid)
        if text:
            logger.info("BITRIX_COMMENT source=im dialog=%s chars=%s", dialog_id, len(text))
            return text

    if mid > 0:
        try:
            data = bitrix_call("task.commentitem.get", {"TASKID": tid, "ITEMID": mid})
            result = data.get("result") if isinstance(data, dict) else data
            text = _text_from_comment_result(result)
            if text:
                logger.info("BITRIX_COMMENT source=task.commentitem.get chars=%s", len(text))
                return text
        except Exception as exc:
            logger.warning("BITRIX task.commentitem.get failed task_id=%s item_id=%s err=%s", tid, mid, exc)

    logger.warning("BITRIX_COMMENT empty task_id=%s message_id=%s", tid, mid)
    return ""


def build_trainer_profile_reply_message(
    *,
    action: str,
    entries: list[dict[str, str | None]],
) -> str:
    """
    Build a task-chat reply: header plus per-trainer name, trainer_id, and Google Drive link.

    ``entries`` items: ``name``, ``trainer_id``, ``drive_url``.
    """
    header = f"Trainer profile {action} successfully."
    blocks: list[str] = []
    for row in entries:
        name = (row.get("name") or "").strip()
        trainer_id = (row.get("trainer_id") or "").strip()
        drive_url = (row.get("drive_url") or "").strip()
        if not drive_url:
            continue
        lines: list[str] = []
        if name:
            lines.append(f"name: {name}")
        if trainer_id:
            lines.append(f"trainer_id: {trainer_id}")
        lines.append(f"Google Drive: {drive_url}")
        blocks.append("\n".join(lines))
    if not blocks:
        return header
    return header + "\n\n" + "\n\n".join(blocks)


def post_task_chat_reply(task_id: str | int, message: str) -> bool:
    """Post a reply into the Bitrix task chat / comment feed (inbound webhook REST)."""
    settings = get_settings()
    if not settings.bitrix_reply_to_chat:
        logger.info("BITRIX_REPLY_SKIP disabled bitrix_reply_to_chat=false task_id=%s", task_id)
        return False
    if not (settings.bitrix_rest_webhook_url or "").strip():
        logger.info("BITRIX_REPLY_SKIP no BITRIX_REST_WEBHOOK_URL task_id=%s", task_id)
        return False

    msg = (message or "").strip()
    if not msg:
        return False

    tid = int(task_id)
    try:
        bitrix_call(
            "task.commentitem.add",
            {"TASKID": tid, "FIELDS": {"POST_MESSAGE": msg}},
        )
        logger.info("BITRIX_REPLY_OK method=task.commentitem.add task_id=%s chars=%s", tid, len(msg))
        return True
    except Exception as exc:
        logger.warning("BITRIX_REPLY_TASK_COMMENT_FAILED task_id=%s err=%s", tid, exc)

    chat_id = _fetch_task_chat_id(tid)
    if chat_id is None:
        logger.warning("BITRIX_REPLY_SKIP no chat_id task_id=%s", tid)
        return False
    for dialog_id in (f"chat{chat_id}", f"TASKS_TASK_{tid}"):
        try:
            bitrix_call(
                "im.message.add",
                {"DIALOG_ID": dialog_id, "MESSAGE": msg},
            )
            logger.info(
                "BITRIX_REPLY_OK method=im.message.add dialog=%s task_id=%s chars=%s",
                dialog_id,
                tid,
                len(msg),
            )
            return True
        except Exception as exc:
            logger.warning("BITRIX_REPLY_IM_FAILED dialog=%s task_id=%s err=%s", dialog_id, tid, exc)
    return False
