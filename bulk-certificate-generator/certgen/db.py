"""SQLite access. Plain sqlite3 keeps the project dependency-free; the SQL is
standard, so moving to PostgreSQL later only means swapping this module."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    issuer      TEXT NOT NULL,
    issue_date  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- pending | processing | completed | completed_with_errors | failed
    total       INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,   -- index of the recipient in the original request
    recipient_name  TEXT,               -- NULL if the input had no usable name
    recipient_email TEXT,
    status          TEXT NOT NULL,      -- pending | generated | failed
    error           TEXT,
    file_path       TEXT,               -- relative to CERT_DIR
    created_at      TEXT NOT NULL,
    generated_at    TEXT,
    UNIQUE (job_id, position)
);

CREATE INDEX IF NOT EXISTS idx_certificates_job_status ON certificates (job_id, status);
"""


def connect(path):
    # One connection per unit of work (request / worker job). sqlite3
    # connections must not be shared between threads.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
