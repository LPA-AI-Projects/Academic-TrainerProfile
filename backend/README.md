# Trainer Profile Backend (FastAPI + Postgres)

This service generates structured trainer profiles from:

- CV only
- CV + one or more course outlines

It stores request metadata and generated output with a `zoho_record_id` for Zoho mapping.

## 1) Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Update `.env` with your Postgres connection and API keys.
For Anthropic:

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`)
- `ANTHROPIC_MODEL` (for example `claude-sonnet-4-6`)

## 2) Postgres

Create database:

```sql
CREATE DATABASE trainer_profiles;
```

On startup, tables are created automatically by SQLAlchemy.

### Quick Docker Postgres (recommended for local setup)

From `../` (the `trainer-profile` folder):

```bash
docker compose up -d postgres
```

This exposes Postgres on `localhost:5433` with:

- user: `postgres`
- password: `postgres`
- database: `trainer_profiles`

## 3) Run API

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

Open docs:

- [http://localhost:8080/docs](http://localhost:8080/docs)

## 4) Main API

### POST `/api/v1/profiles/generate`

Body must be **`application/x-www-form-urlencoded`** (Zoho webhook style). Trainer CV comes from Zoho only: send **`cv`** (Zoho file id) and/or configure **`ZOHO_MODULE_API_NAME`** + **`ZOHO_CV_FIELD_API_NAME`** so the server reads the CV from the CRM record. **`course_name`** and **`course_outline_paths`** are optional.

Example form fields:

- `zoho_record_id` — required  
- `cv` — Zoho attachment id (omit when CRM field env supplies the CV)  
- `course_name` — optional (Drive / display naming)  
- `course_outline_paths` — comma- or newline-separated paths (optional)  
- `programs_trained` — optional comma/newline-separated or JSON-array program titles; merged first into the profile’s programs list, then CV/outline-backed items (duplicates removed)  
- `provider`, `model_name` — optional overrides  

### GET `/api/v1/profiles/{job_id}`

Returns stored status and generated profile payload.

### GET `/api/v1/profiles/{job_id}/pdf`

Returns a real `application/pdf` file (downloadable in Postman) by rendering the same static CV layout
(`trainer-profile/index.html`) in headless Chromium (Playwright).

The PDF is also saved to disk and served as a stable public URL:

- `GET /pdfs/{job_id}.pdf`

**One-time setup (after installing Python deps):**

```bash
python -m playwright install chromium
```

## 5) Frontend Integration

Open `../index.html` and fill:

- API Base URL
- Zoho Record ID
- CV path
- optional course outline paths (one path per line)
- provider/model

Then click **Generate and Fill CV**.

Once mapped into the HTML preview, click **Print / Export PDF** to download the final trainer profile PDF from the browser.

## 6) Prompt Logic

Prompt behavior is in `app/services/prompt_builder.py`.

- CV-only mode: strict extraction with no hallucination
- CV + outlines mode: subtle enrichment from course context
- enforced output schema for profile sections

Tune this file based on your exact tone and content rules.
