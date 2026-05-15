"""JSON-safe HTTP / validation error helpers."""

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


def validation_errors_for_response(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic error dicts with ``ctx`` values safe for ``JSONResponse``."""
    out: list[dict[str, Any]] = []
    for err in exc.errors():
        item = dict(err)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {str(k): _json_safe_value(v) for k, v in ctx.items()}
        out.append(item)
    return out


class PostOnlyAccessLogFilter(logging.Filter):
    """Uvicorn access log: keep POST lines only (reduces static asset noise)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return ' "POST ' in msg
