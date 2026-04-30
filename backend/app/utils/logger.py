"""
Slides-style module loggers: use ``get_logger(__name__)`` so every line carries a stable
``trainer_profile.*`` logger name for grep in Railway / Datadog.

Preserves existing logger names (api, generate, zoho, …) for dashboards that already filter on them.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

# Map import path -> historical logging.getLogger(...) name
_LOGGER_NAMES: Final[dict[str, str]] = {
    "backend.app.main": "trainer_profile.api",
    "backend.app.db_migrations": "trainer_profile.db",
    "backend.app.services.profile_service": "trainer_profile.generate",
    "backend.app.services.zoho_service": "trainer_profile.zoho",
    "backend.app.services.google_drive_service": "trainer_profile.google_drive",
    "backend.app.services.profile_pdf": "trainer_profile.pdf",
    "backend.app.services.job_pdf": "trainer_profile.job_pdf",
    "backend.app.services.llm_client": "trainer_profile.llm",
    # If the app is started as ``uvicorn app.main:app`` from ``backend/``:
    "app.main": "trainer_profile.api",
    "app.db_migrations": "trainer_profile.db",
    "app.services.profile_service": "trainer_profile.generate",
    "app.services.zoho_service": "trainer_profile.zoho",
    "app.services.google_drive_service": "trainer_profile.google_drive",
    "app.services.profile_pdf": "trainer_profile.pdf",
    "app.services.job_pdf": "trainer_profile.job_pdf",
    "app.services.llm_client": "trainer_profile.llm",
}


def get_logger(module_name: str | None = None) -> logging.Logger:
    """
    Return the module logger used across trainer-profile (same pattern as slides ``get_logger``).

    Pass ``__name__`` from each module so stack traces and log filters stay consistent.

    Example::

        from backend.app.utils.logger import get_logger

        logger = get_logger(__name__)
    """
    mod = module_name
    if mod is None:
        frame = sys._getframe(1)
        mod = str(frame.f_globals.get("__name__", "trainer_profile.unknown"))

    resolved = _LOGGER_NAMES.get(mod)
    if resolved:
        return logging.getLogger(resolved)
    if mod.startswith("backend.app."):
        suffix = mod.removeprefix("backend.app.").replace(".", "_")
        return logging.getLogger(f"trainer_profile.{suffix}")
    if mod.startswith("app."):
        suffix = mod.removeprefix("app.").replace(".", "_")
        return logging.getLogger(f"trainer_profile.{suffix}")
    return logging.getLogger(mod)
