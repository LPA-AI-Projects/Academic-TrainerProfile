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
    url = (
        f"{base}/trainer-profile/index.html?job={job_id}&api_base={api_base}"
        f"&render_mode=pdf&pdf_server_apply=1"
    )
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

            # Fetch job JSON in the Playwright context (same headers as API) so the page does not depend on
            # in-browser fetch, query-string api_key, or timing — fixes empty PDFs when the client fetch fails.
            api_headers = {}
            if secret:
                api_headers["X-API-Key"] = secret
            api_resp = await page.request.get(
                f"{base}/api/v1/profiles/{job_id}",
                headers=api_headers,
            )
            if not api_resp.ok:
                err_body = (await api_resp.text())[:800]
                raise RuntimeError(
                    f"PDF prefetch failed: HTTP {api_resp.status} for job_id={job_id}: {err_body}"
                )
            job_payload = await api_resp.json()

            await page.wait_for_function(
                "() => typeof window.__applyTrainerProfile === 'function'",
                timeout=120_000,
            )
            applied = await page.evaluate(
                """(payload) => {
                  if (!payload || payload.status !== 'completed') return false;
                  const gp = payload.generated_profile;
                  if (gp == null) return false;
                  window.__applyTrainerProfile(gp);
                  const m = document.getElementById('cv-profile-loaded');
                  if (m) m.setAttribute('data-ready', '1');
                  return true;
                }""",
                job_payload,
            )
            if not applied:
                raise RuntimeError(
                    f"PDF apply failed for job_id={job_id}: "
                    f"status={job_payload.get('status')!r} "
                    f"generated_profile_present={job_payload.get('generated_profile') is not None}"
                )

            await page.wait_for_function(
                """
                () => {
                  const marker = document.getElementById('cv-profile-loaded');
                  return marker && marker.getAttribute('data-ready') === '1';
                }
                """,
                timeout=30_000,
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
