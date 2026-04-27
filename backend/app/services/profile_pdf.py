from __future__ import annotations

import time
from urllib.parse import quote

import logging

logger = logging.getLogger("trainer_profile.pdf")


def render_trainer_profile_pdf(*, public_base_url: str, job_id: str) -> bytes:
    """
    Render the static CV builder page to a PDF using headless Chromium.

    This relies on the front-end `trainer-profile/js/app.js` loading the job and filling the layout.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright is not installed. Install dependencies and run: "
            "`python -m playwright install chromium`"
        ) from exc

    base = public_base_url.rstrip("/")
    api_base = quote(base, safe=":/?&=")
    url = f"{base}/trainer-profile/index.html?job={job_id}&api_base={api_base}&render_mode=pdf"
    logger.info("PDF_RENDER_START job_id=%s url=%s", job_id, url)
    t0 = time.perf_counter()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_viewport_size({"width": 595, "height": 842})
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
            page.emulate_media(media="print")
            # `networkidle` can hang on local dev servers; `load` is more reliable here.
            page.goto(url, wait_until="load", timeout=120_000)

            # Wait for rendered content. Support both:
            # 1) dynamic builder template selectors, and
            # 2) fixed HTML templates without those specific ids/classes.
            page.wait_for_function(
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
            page.wait_for_timeout(800)

            pdf = page.pdf(
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
            browser.close()
