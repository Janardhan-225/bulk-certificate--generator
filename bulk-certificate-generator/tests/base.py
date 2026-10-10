import io
import shutil
import tempfile
import unittest

from pypdf import PdfReader

from certgen import create_app


class ApiTestCase(unittest.TestCase):
    """Fresh temp DB + certificate dir for every test; synchronous processing
    by default so results are deterministic."""
    app_config = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config = {
            "TESTING": True,
            "DATABASE": f"{self.tmp}/test.db",
            "CERT_DIR": f"{self.tmp}/certs",
            "ASYNC_PROCESSING": False,
            "MAX_RECIPIENTS": 50,
        }
        config.update(self.app_config)
        self.app = create_app(config)
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def payload(self, names=("Asha Verma", "Rahul Nair"), **extra):
        body = {"title": "Python Bootcamp", "recipients": [{"name": n} for n in names]}
        body.update(extra)
        return body

    def create_job(self, body=None, expected=202):
        resp = self.client.post("/api/jobs", json=body or self.payload())
        self.assertEqual(resp.status_code, expected, resp.get_data(as_text=True))
        return resp.get_json()

    @staticmethod
    def pdf_text(data):
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
