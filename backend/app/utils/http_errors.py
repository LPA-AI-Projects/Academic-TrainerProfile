"""JSON-safe HTTP / validation error helpers and structured API error logging."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

logger = logging.getLogger("trainer_profile.api")


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    return str(value)


def errors_list_for_response(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize Pydantic/FastAPI validation error dicts for ``JSONResponse``."""
    out: list[dict[str, Any]] = []
    for err in errors:
        item = dict(err)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {str(k): _json_safe_value(v) for k, v in ctx.items()}
        out.append(item)
    return out


def validation_errors_for_response(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic error dicts with ``ctx`` values safe for ``JSONResponse``."""
    return errors_list_for_response(list(exc.errors()))


def is_dev_env(app_env: str) -> bool:
    return app_env.strip().lower() in ("dev", "development", "local", "test")


def exception_public_detail(exc: BaseException, *, app_env: str) -> str:
    """Client-safe error text; include type/message in dev for faster debugging."""
    if is_dev_env(app_env):
        msg = str(exc).strip() or "(no message)"
        return f"{type(exc).__name__}: {msg}"
    return "An internal error occurred. Check server logs (search by request_id if provided)."


def format_log_context(**context: object) -> str:
    parts: list[str] = []
    for key in sorted(context):
        val = context[key]
        if val is None:
            continue
        parts.append(f"{key}={val!r}")
    return " ".join(parts)


def log_operation_error(
    log: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
    **context: object,
) -> None:
    """
    One grep-friendly line plus stack trace for ERROR+.

    Example log line::

        GEN_LLM_FAILED exc_type=ValueError exc_msg=OPENAI_API_KEY is missing. job_id='...' zoho_record_id='...'
    """
    ctx = format_log_context(**context)
    msg = str(exc).strip() or "(no message)"
    if len(msg) > 500:
        msg = msg[:497] + "..."
    log.log(
        level,
        "%s exc_type=%s exc_msg=%s %s",
        event,
        type(exc).__name__,
        msg,
        ctx,
        exc_info=exc if level >= logging.ERROR else None,
    )


class PostOnlyAccessLogFilter(logging.Filter):
    """Uvicorn access log: keep POST lines only (reduces static asset noise)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return ' "POST ' in msg
