import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify

from . import db, service
from .api import bp

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("CERTGEN_DB", str(BASE_DIR / "data" / "certgen.db")),
        CERT_DIR=os.environ.get("CERTGEN_CERT_DIR", str(BASE_DIR / "data" / "certificates")),
        MAX_RECIPIENTS=int(os.environ.get("CERTGEN_MAX_RECIPIENTS", 1000)),
        DEFAULT_ISSUER=os.environ.get("CERTGEN_DEFAULT_ISSUER", "Certificate Authority"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,   # reject huge bodies early (413)
        # True  -> jobs run in a background thread pool; POST returns immediately.
        # False -> jobs run inside the request (used by most tests; deterministic).
        ASYNC_PROCESSING=True,
        WORKERS=int(os.environ.get("CERTGEN_WORKERS", 2)),
        REQUEUE_ON_START=True,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["CERT_DIR"]).mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DATABASE"])

    executor = None
    if app.config["ASYNC_PROCESSING"]:
        executor = ThreadPoolExecutor(max_workers=app.config["WORKERS"],
                                      thread_name_prefix="certgen")
    app.extensions["executor"] = executor

    # Crash recovery: finish jobs a previous process left pending/processing.
    # process_job only touches `pending` certificates, so this is idempotent.
    if executor and app.config["REQUEUE_ON_START"]:
        for job_id in service.unfinished_job_ids(app.config["DATABASE"]):
            executor.submit(service.process_job, app.config["DATABASE"],
                            app.config["CERT_DIR"], job_id)

    app.register_blueprint(bp)

    @app.errorhandler(404)
    def not_found(_):
        return jsonify(error="not_found", message="Resource not found"), 404

    @app.errorhandler(405)
    def method_not_allowed(_):
        return jsonify(error="method_not_allowed", message="Method not allowed"), 405

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="payload_too_large", message="Request body too large"), 413

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


logging.basicConfig(level=logging.INFO)
