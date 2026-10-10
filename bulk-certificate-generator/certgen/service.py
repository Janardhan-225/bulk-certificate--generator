"""Business logic: creating jobs and processing them.

Everything here takes plain paths/ids (no Flask objects) so it can run inside
a worker thread without needing an application context.
"""
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import db, generator
from .validation import validate_recipient

log = logging.getLogger(__name__)

PENDING, PROCESSING = "pending", "processing"
COMPLETED, COMPLETED_WITH_ERRORS, FAILED = "completed", "completed_with_errors", "failed"
ACTIVE_STATUSES = (PENDING, PROCESSING)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_job(db_path, job_fields, raw_recipients):
    """Persist the job and one certificate row per recipient, in ONE transaction.

    Invalid recipients are stored as already-`failed` rows with the reason, so
    the client sees them in the job result. Returns the job id.
    """
    job_id = uuid.uuid4().hex
    now = _now()
    conn = db.connect(db_path)
    try:
        with conn:  # commit on success, rollback on error
            conn.execute(
                "INSERT INTO jobs (id, title, issuer, issue_date, status, total, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, job_fields["title"], job_fields["issuer"],
                 job_fields["issue_date"], PENDING, len(raw_recipients), now))

            rows = []
            for position, raw in enumerate(raw_recipients):
                clean, error = validate_recipient(raw)
                name = clean["name"] if clean else (
                    raw.get("name") if isinstance(raw, dict) and isinstance(raw.get("name"), str) else None)
                email = clean["email"] if clean else None
                rows.append((uuid.uuid4().hex, job_id, position,
                             name[:100] if name else None, email,
                             "failed" if error else "pending", error, now))
            conn.executemany(
                "INSERT INTO certificates (id, job_id, position, recipient_name,"
                " recipient_email, status, error, created_at) VALUES (?,?,?,?,?,?,?,?)",
                rows)
    finally:
        conn.close()
    return job_id


def job_counts(conn, job_id):
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(status='generated'), 0) AS generated,"
        " COALESCE(SUM(status='failed'), 0)    AS failed,"
        " COALESCE(SUM(status='pending'), 0)   AS pending"
        " FROM certificates WHERE job_id = ?", (job_id,)).fetchone()
    return {k: row[k] for k in ("total", "generated", "failed", "pending")}


def _finalize_if_done(conn, job_id):
    counts = job_counts(conn, job_id)
    if counts["pending"]:
        return
    if counts["failed"] == 0:
        status = COMPLETED
    elif counts["generated"] == 0:
        status = FAILED
    else:
        status = COMPLETED_WITH_ERRORS
    conn.execute(
        "UPDATE jobs SET status = ?, finished_at = ?,"
        " started_at = COALESCE(started_at, ?) WHERE id = ?",
        (status, _now(), _now(), job_id))
    conn.commit()


def process_job(db_path, cert_dir, job_id):
    """Generate every still-pending certificate of a job.

    Safe to call again for the same job (e.g. after a crash/restart): it only
    touches rows that are still `pending`, so finished work is never redone.
    One certificate failing is caught and recorded; it never aborts the loop.
    """
    conn = db.connect(db_path)
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return
        conn.execute(
            "UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?)"
            " WHERE id = ? AND status IN (?, ?)",
            (PROCESSING, _now(), job_id, PENDING, PROCESSING))
        conn.commit()

        pending = conn.execute(
            "SELECT id, recipient_name FROM certificates"
            " WHERE job_id = ? AND status = 'pending' ORDER BY position",
            (job_id,)).fetchall()

        for cert in pending:
            _process_certificate(conn, cert_dir, job, cert)

        _finalize_if_done(conn, job_id)
    except Exception:
        # Unexpected infrastructure error (DB down, disk full on status write).
        # Job stays `processing`; it is re-queued on next startup.
        log.exception("job %s aborted unexpectedly", job_id)
    finally:
        conn.close()


def _process_certificate(conn, cert_dir, job, cert):
    relative = f"{job['id']}/{cert['id']}.pdf"
    try:
        generator.generate_certificate(
            Path(cert_dir) / relative,
            recipient_name=cert["recipient_name"],
            title=job["title"],
            issuer=job["issuer"],
            issue_date=job["issue_date"],
            certificate_id=cert["id"])
    except Exception as exc:  # isolate: one bad certificate must not stop the job
        log.warning("certificate %s failed: %s", cert["id"], exc)
        conn.execute(
            "UPDATE certificates SET status = 'failed', error = ? WHERE id = ?",
            (f"generation failed: {exc}"[:500], cert["id"]))
    else:
        conn.execute(
            "UPDATE certificates SET status = 'generated', file_path = ?,"
            " generated_at = ?, error = NULL WHERE id = ?",
            (relative, _now(), cert["id"]))
    conn.commit()  # commit per certificate => progress is visible live


def unfinished_job_ids(db_path):
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN (?, ?) ORDER BY created_at",
            ACTIVE_STATUSES).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()
