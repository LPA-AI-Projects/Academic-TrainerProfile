FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

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

# Copy full repository because backend serves /app/index.html and related static assets.
COPY . /app

# Railway (and others) set PORT; default 8080 for local compose / Railway-style setups.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD /bin/sh -c 'curl -fsS "http://127.0.0.1:${PORT:-8080}/health" || exit 1'

# If you see Caddy `fileserver notFound` on /api/*, your edge is serving static files only — proxy /api to this process or run this container as the public HTTP service (no static-only Caddy in front).
CMD ["/bin/sh", "-c", "exec uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
