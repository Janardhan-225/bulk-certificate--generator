"""HTTP layer (Flask blueprint). Thin: validate, call the service, serialise."""
import io
import re
import unicodedata
import zipfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file, url_for

from . import db, service
from .validation import RequestValidationError, validate_job_payload

bp = Blueprint("api", __name__, url_prefix="/api")

MAX_PAGE_SIZE = 500
FAILURE_PREVIEW = 20


# ---------- helpers ----------------------------------------------------------

def _error(status, code, message, **extra):
    return jsonify({"error": code, "message": message, **extra}), status


def _conn():
    return db.connect(current_app.config["DATABASE"])


def _get_job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _certificate_json(row):
    data = {
        "id": row["id"],
        "index": row["position"],
        "recipient_name": row["recipient_name"],
        "recipient_email": row["recipient_email"],
        "status": row["status"],
        "error": row["error"],
    }
    if row["status"] == "generated":
        data["download_url"] = url_for("api.download_certificate",
                                       certificate_id=row["id"], _external=False)
    return data


def _job_json(conn, job, include_failures=True):
    counts = service.job_counts(conn, job["id"])
    done = counts["generated"] + counts["failed"]
    data = {
        "id": job["id"],
        "title": job["title"],
        "issuer": job["issuer"],
        "issue_date": job["issue_date"],
        "status": job["status"],
        "counts": counts,
        "progress_percent": round(100 * done / counts["total"], 1) if counts["total"] else 100.0,
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "links": {
            "self": url_for("api.get_job", job_id=job["id"]),
            "certificates": url_for("api.list_certificates", job_id=job["id"]),
            "download_zip": url_for("api.download_zip", job_id=job["id"]),
        },
    }
    if include_failures:
        rows = conn.execute(
            "SELECT * FROM certificates WHERE job_id = ? AND status = 'failed'"
            " ORDER BY position LIMIT ?", (job["id"], FAILURE_PREVIEW)).fetchall()
        data["failures"] = [_certificate_json(r) for r in rows]
        data["failures_truncated"] = counts["failed"] > len(rows)
    return data


def _slug(text):
    """Filename-safe ASCII slug: 'Maria José' -> 'maria-jose'."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()[:40] or "certificate"


def _dispatch(job_id):
    """Run the job now (tests / simple mode) or hand it to the worker pool."""
    cfg = current_app.config
    executor = current_app.extensions.get("executor")
    if executor is None:
        service.process_job(cfg["DATABASE"], cfg["CERT_DIR"], job_id)
    else:
        executor.submit(service.process_job, cfg["DATABASE"], cfg["CERT_DIR"], job_id)


# ---------- routes -----------------------------------------------------------

@bp.post("/jobs")
def create_job():
    payload = request.get_json(silent=True)
    if payload is None:
        return _error(400, "invalid_json", "Request body must be valid JSON "
                      "with Content-Type: application/json")
    try:
        job_fields, recipients = validate_job_payload(
            payload,
            max_recipients=current_app.config["MAX_RECIPIENTS"],
            default_issuer=current_app.config["DEFAULT_ISSUER"])
    except RequestValidationError as exc:
        return _error(400, "validation_error", "Invalid request", details=exc.details)

    job_id = service.create_job(current_app.config["DATABASE"], job_fields, recipients)
    _dispatch(job_id)

    with _conn() as conn:
        job = _get_job(conn, job_id)
        body = _job_json(conn, job)
    resp = jsonify(body)
    resp.status_code = 202  # accepted; generation may still be running
    resp.headers["Location"] = body["links"]["self"]
    return resp


@bp.get("/jobs/<job_id>")
def get_job(job_id):
    conn = _conn()
    try:
        job = _get_job(conn, job_id)
        if job is None:
            return _error(404, "not_found", "Job not found")
        return jsonify(_job_json(conn, job))
    finally:
        conn.close()


@bp.get("/jobs/<job_id>/certificates")
def list_certificates(job_id):
    status = request.args.get("status")
    if status not in (None, "pending", "generated", "failed"):
        return _error(400, "validation_error", "status must be pending, generated or failed")
    try:
        limit = min(int(request.args.get("limit", 100)), MAX_PAGE_SIZE)
        offset = int(request.args.get("offset", 0))
        if limit < 1 or offset < 0:
            raise ValueError
    except ValueError:
        return _error(400, "validation_error", "limit must be >= 1 and offset >= 0 (integers)")

    conn = _conn()
    try:
        if _get_job(conn, job_id) is None:
            return _error(404, "not_found", "Job not found")
        where, params = "job_id = ?", [job_id]
        if status:
            where += " AND status = ?"
            params.append(status)
        total = conn.execute(f"SELECT COUNT(*) FROM certificates WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM certificates WHERE {where} ORDER BY position LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return jsonify({"total": total, "limit": limit, "offset": offset,
                        "results": [_certificate_json(r) for r in rows]})
    finally:
        conn.close()


@bp.get("/certificates/<certificate_id>/download")
def download_certificate(certificate_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM certificates WHERE id = ?", (certificate_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error(404, "not_found", "Certificate not found")
    if row["status"] != "generated":
        return _error(409, f"certificate_{row['status']}",
                      "Certificate is not available: " + (row["error"] or "still being generated"))
    path = Path(current_app.config["CERT_DIR"]) / row["file_path"]
    if not path.is_file():
        return _error(410, "file_missing", "Certificate file is no longer on disk")
    return send_file(path, mimetype="application/pdf",
                     download_name=f"certificate-{_slug(row['recipient_name'])}.pdf")


@bp.get("/jobs/<job_id>/download")
def download_zip(job_id):
    conn = _conn()
    try:
        job = _get_job(conn, job_id)
        if job is None:
            return _error(404, "not_found", "Job not found")
        if job["status"] in service.ACTIVE_STATUSES:
            return _error(409, "job_not_finished", "Job is still running; poll the job status first")
        rows = conn.execute(
            "SELECT * FROM certificates WHERE job_id = ? AND status = 'generated' ORDER BY position",
            (job_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return _error(409, "nothing_to_download", "No certificates were generated for this job")

    # In-memory zip: fine for the capped job size (MAX_RECIPIENTS * ~2 KB).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for row in rows:
            path = Path(current_app.config["CERT_DIR"]) / row["file_path"]
            if path.is_file():
                zf.write(path, f"{row['position']:04d}-{_slug(row['recipient_name'])}.pdf")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", download_name=f"certificates-{job_id}.zip")
