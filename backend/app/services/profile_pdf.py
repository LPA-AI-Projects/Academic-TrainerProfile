from __future__ import annotations

import time
from urllib.parse import quote

from ..config import get_settings
from ..utils.logger import get_logger

logger = get_logger(__name__)


def _append_api_key_query(url: str, secret: str) -> str:
    """So headless Chromium can call protected GET /api/v1/profiles/{id} (see index.html ``api_key`` param)."""
    if not secret:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={quote(secret, safe='')}"


def _redact_api_key_from_url_for_log(url: str) -> str:
    marker = "api_key="
    i = url.find(marker)
    if i == -1:
        return url
    value_start = i + len(marker)
    j = url.find("&", value_start)
    if j == -1:
        return url[:i] + "api_key=(redacted)"
    return url[:i] + "api_key=(redacted)" + url[j:]


async def render_trainer_profile_pdf(*, public_base_url: str, job_id: str) -> bytes:
    """
    Render the static CV builder page to a PDF using headless Chromium.

    This relies on the front-end `trainer-profile/js/app.js` loading the job and filling the layout.

    Must use the **async** Playwright API when called from FastAPI (async event loop).
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright is not installed. Install dependencies and run: "
            "`python -m playwright install chromium`"
        ) from exc

    base = public_base_url.rstrip("/")
    api_base = quote(base, safe=":/?&=")
    settings = get_settings()
    secret = (settings.api_secret_key or "").strip()
    url = f"{base}/trainer-profile/index.html?job={job_id}&api_base={api_base}&render_mode=pdf"
    url = _append_api_key_query(url, secret)
    logger.info(
        "PDF_RENDER_START job_id=%s url=%s",
        job_id,
        _redact_api_key_from_url_for_log(url),
    )
    t0 = time.perf_counter()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_viewport_size({"width": 595, "height": 842})
            page.on(
                "console",
                lambda msg: logger.info(
                    "PDF_PAGE_CONSOLE job_id=%s level=%s text=%s",
                    job_id,
                    msg.type,
                    msg.text[:500],
                ),
            )
            page.on(
                "pageerror",
                lambda err: logger.error("PDF_PAGE_ERROR job_id=%s error=%s", job_id, str(err)),
            )
            page.on(
                "requestfailed",
                lambda req: logger.warning(
                    "PDF_REQUEST_FAILED job_id=%s method=%s url=%s failure=%s",
                    job_id,
                    req.method,
                    req.url,
                    req.failure,
                ),
            )
            await page.emulate_media(media="print")
            # `networkidle` can hang on local dev servers; `load` is more reliable here.
            await page.goto(url, wait_until="load", timeout=120_000)

            # Wait for rendered content. Support both:
            # 1) dynamic builder template selectors, and
            # 2) fixed HTML templates without those specific ids/classes.
            await page.wait_for_function(
                """
                () => {
                  const hasPages = document.querySelectorAll('.cv-page, .page').length >= 1;
                  if (!hasPages) return false;

                  const dynName = document.querySelector('.cv-p1-name')?.textContent?.trim() || '';
                  const staticName = document.querySelector('h1')?.textContent?.trim() || '';
                  const name = dynName || staticName;
                  if (name.length < 2) return false;

                  const dynPrograms = document.querySelectorAll('#cv-p1-programs-ul li, #cv-p2-programs-ul li').length;
                  const dynTraining = document.querySelectorAll('#cv-p2-training-ul li').length;
                  const genericItems = document.querySelectorAll('section.page ul li, .cv-page ul li').length;

                  return dynPrograms > 0 || dynTraining > 0 || genericItems > 0 || name.length >= 3;
                }
                """,
                timeout=120_000,
            )
            await page.wait_for_timeout(800)

            pdf = await page.pdf(
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
            logger.info(
                "PDF_RENDER_DONE job_id=%s bytes=%s elapsed_ms=%.1f",
                job_id,
                len(pdf),
                (time.perf_counter() - t0) * 1000,
            )
            return pdf
        finally:
            await browser.close()
