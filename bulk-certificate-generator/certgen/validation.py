"""Input validation.

Two levels, deliberately different:
  * Request level  - the request is malformed (no title, no recipients, too
    many recipients...). Nothing is created: HTTP 400.
  * Recipient level - one recipient is bad. The job is still created and that
    recipient is recorded as `failed` with a reason, so one typo in 5,000 rows
    does not block the other 4,999 certificates.
"""
import re
from datetime import date

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

NAME_MAX = 100
TITLE_MAX = 150
ISSUER_MAX = 100
EMAIL_MAX = 254


class RequestValidationError(Exception):
    def __init__(self, details):
        super().__init__("invalid request")
        self.details = details  # list of {"field": ..., "message": ...}


def _clean_text(value, max_len):
    """Return (clean_value, error_message)."""
    if not isinstance(value, str):
        return None, "must be a string"
    value = " ".join(value.split())  # trim + collapse whitespace
    if not value:
        return None, "must not be empty"
    if len(value) > max_len:
        return None, f"must be at most {max_len} characters"
    if CONTROL_CHARS_RE.search(value):
        return None, "contains control characters"
    try:
        # The template uses a built-in PDF font (WinAnsi / cp1252). Anything
        # outside that would render as garbage, so reject it explicitly.
        value.encode("cp1252")
    except UnicodeEncodeError:
        return None, "contains characters the certificate font cannot render"
    return value, None


def validate_job_payload(payload, *, max_recipients, default_issuer):
    """Validate the top-level request. Returns (job_fields, raw_recipients)."""
    if not isinstance(payload, dict):
        raise RequestValidationError(
            [{"field": "body", "message": "must be a JSON object"}])

    details = []

    title, err = _clean_text(payload.get("title"), TITLE_MAX)
    if err:
        details.append({"field": "title", "message": err})

    issuer = default_issuer
    if payload.get("issuer") is not None:
        issuer, err = _clean_text(payload["issuer"], ISSUER_MAX)
        if err:
            details.append({"field": "issuer", "message": err})

    issue_date = date.today()
    if payload.get("issue_date") is not None:
        try:
            if not isinstance(payload["issue_date"], str):
                raise ValueError
            issue_date = date.fromisoformat(payload["issue_date"])
        except ValueError:
            details.append({"field": "issue_date",
                            "message": "must be an ISO date, e.g. 2026-10-10"})

    recipients = payload.get("recipients")
    if not isinstance(recipients, list):
        details.append({"field": "recipients", "message": "must be a list"})
    elif not recipients:
        details.append({"field": "recipients", "message": "must not be empty"})
    elif len(recipients) > max_recipients:
        details.append({"field": "recipients",
                        "message": f"at most {max_recipients} recipients per request"})

    if details:
        raise RequestValidationError(details)

    job = {"title": title, "issuer": issuer, "issue_date": issue_date.isoformat()}
    return job, recipients


def validate_recipient(raw):
    """Return (clean, error). Exactly one of the two is None.

    `clean` is {"name": str, "email": str | None}.
    """
    if not isinstance(raw, dict):
        return None, "recipient must be an object"

    errors = []
    name, err = _clean_text(raw.get("name"), NAME_MAX)
    if err:
        errors.append(f"name: {err}")

    email = raw.get("email")
    if email is not None:
        if not isinstance(email, str):
            errors.append("email: must be a string")
            email = None
        else:
            email = email.strip()
            if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
                errors.append("email: not a valid email address")
                email = None

    if errors:
        return None, "; ".join(errors)
    return {"name": name, "email": email}, None
