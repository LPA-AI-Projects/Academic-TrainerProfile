FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Runtime packages:
# - curl for healthcheck
# - Playwright + Chromium dependencies for PDF rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency manifest first for better layer caching.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/backend/requirements.txt

# Install Chromium used by Playwright PDF generation.
RUN playwright install --with-deps chromium

# Copy app + static UI (index.html, js/) served by FastAPI.
COPY . /app

# Railway sets PORT at runtime; 8080 matches common platform defaults and local compose.
ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD /bin/sh -c 'curl -fsS "http://127.0.0.1:${PORT:-8080}/health" || exit 1'

# Single HTTP process: FastAPI on Uvicorn. Public URL should point here (no extra static-only reverse proxy on /api).
CMD ["/bin/sh", "-c", "exec python -m uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
