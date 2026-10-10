# Bulk Certificate Generator

Backend API that accepts **one request containing many recipients**, generates a PDF certificate for each from a single predefined template, tracks progress, and lets the client download the results (individually or as a ZIP).

**Stack:** Python 3.10+ · Flask · SQLite (relational) · ReportLab (PDF) · `unittest`-style tests (run with `unittest` or `pytest`).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python run.py                        # http://127.0.0.1:5000
```

Data (SQLite DB + generated PDFs) lives in `./data/`. Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CERTGEN_DB` | `data/certgen.db` | SQLite file |
| `CERTGEN_CERT_DIR` | `data/certificates` | where PDFs are stored |
| `CERTGEN_MAX_RECIPIENTS` | `1000` | max recipients per request |
| `CERTGEN_WORKERS` | `2` | background worker threads |
| `CERTGEN_DEFAULT_ISSUER` | `Certificate Authority` | issuer if request omits it |

## Tests

```bash
python -m unittest discover -s tests -t .      # or: pytest
```

36 tests cover: job creation, request validation, per-recipient validation, PDF content, status/progress (including mid-job), a single certificate failing, background processing, resume/idempotency, single and ZIP downloads.

## API

### 1. Submit a request - `POST /api/jobs`

```bash
curl -X POST http://127.0.0.1:5000/api/jobs \
  -H "Content-Type: application/json" \
  -d @sample_request.json
```

```json
{
  "title": "Python Backend Bootcamp 2026",
  "issuer": "Acme Academy",
  "issue_date": "2026-10-10",
  "recipients": [
    {"name": "Asha Verma", "email": "asha@example.com"},
    {"name": "Rahul Nair"}
  ]
}
```

| Field | Required | Rules |
|---|---|---|
| `title` | yes | event/course name, <= 150 chars |
| `issuer` | no | <= 100 chars, default from config |
| `issue_date` | no | ISO `YYYY-MM-DD`, default today |
| `recipients[].name` | yes | 1-100 chars, printable, Latin-1/cp1252 characters only |
| `recipients[].email` | no | must look like an email if present |

Response `202 Accepted` (+ `Location` header) with the job: `id`, `status`, `counts`, `progress_percent`, `failures`, `links`.

### 2. Check progress - `GET /api/jobs/{id}`

```bash
curl http://127.0.0.1:5000/api/jobs/<job_id>
```

```json
{
  "id": "0f70...", "status": "completed_with_errors", "progress_percent": 100.0,
  "counts": {"total": 5, "generated": 3, "failed": 2, "pending": 0},
  "failures": [{"index": 3, "recipient_name": null, "status": "failed", "error": "name: must not be empty"}],
  "links": {"self": "...", "certificates": "...", "download_zip": "..."}
}
```

Job `status`: `pending` -> `processing` -> `completed` (all ok) | `completed_with_errors` (some failed) | `failed` (none generated).
`failures` shows the first 20 failed recipients; use the listing below for all.

### 3. List certificates - `GET /api/jobs/{id}/certificates?status=failed&limit=100&offset=0`

Paginated; `status` filter is `pending | generated | failed`. Each item has `index` (position in your request), `status`, `error`, and `download_url` when generated.

### 4. Retrieve certificates

```bash
curl -OJ http://127.0.0.1:5000/api/certificates/<certificate_id>/download   # one PDF
curl -OJ http://127.0.0.1:5000/api/jobs/<job_id>/download                   # ZIP of all successful PDFs
```

| Situation | Response |
|---|---|
| Unknown job/certificate | `404` |
| Certificate failed or still pending | `409` |
| ZIP requested while job is running, or nothing was generated | `409` |
| Malformed request | `400` with `details: [{field, message}]` |

## Design decisions

**Background processing, not synchronous.** `POST` stores the job and returns `202` immediately; a `ThreadPoolExecutor` generates certificates and the client polls `GET /api/jobs/{id}`. A request with 1,000 recipients would otherwise hold an HTTP connection open for a long time and risk gateway timeouts. Setting `ASYNC_PROCESSING=False` runs the job inside the request (used in tests for determinism).

**Two levels of validation.** A malformed *request* (no title, empty/oversized list, bad date) -> `400`, nothing stored. A bad *recipient* -> the job is still created and that recipient is stored as `failed` with a reason. One typo in a CSV of thousands shouldn't block everyone else, and the client sees exactly which rows to fix.

**Failure isolation.** Each certificate is generated in its own `try/except` and committed individually. A failure marks only that row `failed`; the loop continues. Committing per certificate also makes progress visible live.

**Progress is computed, not stored.** Counts come from `COUNT`/`SUM` over the certificate rows, so there are no counters to drift out of sync under concurrency. Only the job's coarse `status` is stored.

**Crash recovery / idempotency.** `process_job` only touches `pending` rows. On startup, jobs left `pending`/`processing` are re-queued, so a restart resumes instead of losing work or duplicating finished certificates. PDFs are written to a temp file then atomically renamed, so there are never half-written PDFs.

**Storage.** SQLite for simplicity (zero setup, WAL mode, one connection per request/worker). PDFs on local disk, DB stores a *relative* path. Two tables: `jobs`, `certificates` (FK, `UNIQUE(job_id, position)`, index on `(job_id, status)`).

**Text / fonts.** The template uses the built-in Helvetica font, which only covers Latin-1/cp1252. Names with other scripts (e.g. Devanagari) are rejected per-recipient with a clear error instead of rendering garbage. Supporting them means registering a TTF font (e.g. Noto Sans) in `generator.py` and relaxing the check in `validation.py`.

## Known limitations / what I'd do next

- **Single process.** The worker pool lives inside the web process; don't run several server processes against the same DB (startup re-queue would double-process). For production: Celery/RQ + Redis with a separate worker, PostgreSQL instead of SQLite, and S3 for files.
- No authentication or rate limiting.
- ZIP is built in memory (fine for <= 1,000 small PDFs; stream it for larger jobs).
- No retry endpoint for failed certificates (easy to add: reset failed rows to `pending` and re-dispatch).
- No cleanup/expiry of old jobs and files.

## Project layout

```
certgen/
  __init__.py     app factory, config, worker pool, startup re-queue
  api.py          routes (thin HTTP layer)
  service.py      create_job, process_job (core logic)
  validation.py   request- and recipient-level validation
  generator.py    PDF template (ReportLab)
  db.py           schema + connection helper
tests/            unittest tests
run.py            entry point
sample_request.json
```
