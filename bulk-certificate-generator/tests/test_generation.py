"""PDF generation, per-certificate failure isolation, retrieval."""
import io
import zipfile

from certgen import generator
from tests.base import ApiTestCase


class GeneratorUnitTests(ApiTestCase):
    def test_pdf_contains_recipient_data(self):
        out = self.app.config["CERT_DIR"] + "/unit/one.pdf"
        generator.generate_certificate(
            out, recipient_name="Asha Verma", title="Python Bootcamp",
            issuer="Acme Academy", issue_date="2026-10-10", certificate_id="abc123")
        with open(out, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"%PDF"))
        text = self.pdf_text(data)
        for expected in ("Asha Verma", "Python Bootcamp", "Acme Academy",
                         "10 October 2026", "abc123"):
            self.assertIn(expected, text)

    def test_very_long_name_still_renders(self):
        out = self.app.config["CERT_DIR"] + "/unit/long.pdf"
        name = "Wolfeschlegelsteinhausenbergerdorff " * 2
        generator.generate_certificate(
            out, recipient_name=name.strip(), title="T", issuer="I",
            issue_date="2026-01-01", certificate_id="x")
        with open(out, "rb") as fh:
            self.assertIn("Wolfeschlegel", self.pdf_text(fh.read()))

    def test_no_temp_files_left_behind_on_failure(self):
        import os
        out = self.app.config["CERT_DIR"] + "/unit2/bad.pdf"
        with self.assertRaises(Exception):
            generator.generate_certificate(  # invalid date -> raises while drawing
                out, recipient_name="A", title="T", issuer="I",
                issue_date="not-a-date", certificate_id="x")
        self.assertEqual(os.listdir(os.path.dirname(out)), [])
        self.assertFalse(os.path.exists(out))


class GenerationThroughApiTests(ApiTestCase):
    def test_each_recipient_gets_their_own_pdf(self):
        job = self.create_job(self.payload(names=["Asha Verma", "Rahul Nair"], title="Data Science 101"))
        certs = self.client.get(job["links"]["certificates"]).get_json()["results"]
        self.assertEqual(len(certs), 2)
        for cert, name in zip(certs, ["Asha Verma", "Rahul Nair"]):
            pdf = self.client.get(cert["download_url"])
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf.mimetype, "application/pdf")
            text = self.pdf_text(pdf.data)
            pdf.close()
            self.assertIn(name, text)
            self.assertIn("Data Science 101", text)
            other = "Rahul Nair" if name == "Asha Verma" else "Asha Verma"
            self.assertNotIn(other, text)


class IndividualFailureTests(ApiTestCase):
    def _generate_with_boom(self, names):
        real = generator.generate_certificate

        def flaky(path, **kwargs):
            if kwargs["recipient_name"] == "Boom":
                raise RuntimeError("renderer exploded")
            return real(path, **kwargs)

        generator.generate_certificate = flaky
        try:
            return self.create_job(self.payload(names=names))
        finally:
            generator.generate_certificate = real

    def test_one_generation_failure_does_not_stop_the_others(self):
        job = self._generate_with_boom(["Before", "Boom", "After"])
        self.assertEqual(job["status"], "completed_with_errors")
        self.assertEqual(job["counts"], {"total": 3, "generated": 2, "failed": 1, "pending": 0})

        # failure details are right in the job status
        self.assertEqual(len(job["failures"]), 1)
        self.assertEqual(job["failures"][0]["recipient_name"], "Boom")
        self.assertIn("renderer exploded", job["failures"][0]["error"])

        certs = {c["recipient_name"]: c for c in
                 self.client.get(job["links"]["certificates"]).get_json()["results"]}
        self.assertEqual(certs["Before"]["status"], "generated")
        self.assertEqual(certs["After"]["status"], "generated")   # processed AFTER the failure
        self.assertEqual(certs["Boom"]["status"], "failed")
        self.assertNotIn("download_url", certs["Boom"])

    def test_every_certificate_failing_marks_job_failed(self):
        real = generator.generate_certificate
        generator.generate_certificate = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            job = self.create_job()
        finally:
            generator.generate_certificate = real
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["counts"]["failed"], 2)


class RetrievalTests(ApiTestCase):
    def test_download_single_certificate(self):
        job = self.create_job(self.payload(names=["Asha Verma"]))
        cert = self.client.get(job["links"]["certificates"]).get_json()["results"][0]
        resp = self.client.get(cert["download_url"])
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b"%PDF"))
        self.assertIn("certificate-asha-verma.pdf", resp.headers["Content-Disposition"])
        resp.close()

    def test_download_unknown_certificate_404(self):
        self.assertEqual(self.client.get("/api/certificates/nope/download").status_code, 404)

    def test_download_failed_certificate_409(self):
        job = self.create_job({"title": "T", "recipients": [{"name": "Good"}, {"name": ""}]})
        failed = self.client.get(job["links"]["certificates"] + "?status=failed").get_json()["results"][0]
        resp = self.client.get(f"/api/certificates/{failed['id']}/download")
        self.assertEqual(resp.status_code, 409)

    def test_zip_contains_only_successful_certificates(self):
        job = self.create_job({"title": "T", "recipients": [
            {"name": "Asha Verma"}, {"name": ""}, {"name": "Rahul Nair"}]})
        resp = self.client.get(job["links"]["download_zip"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        self.assertEqual(zf.namelist(), ["0000-asha-verma.pdf", "0002-rahul-nair.pdf"])
        self.assertIn("Rahul Nair", self.pdf_text(zf.read("0002-rahul-nair.pdf")))

    def test_accented_names_make_clean_filenames(self):
        job = self.create_job(self.payload(names=["Maria Jos\u00e9 \u00c1lvarez"]))
        zf = zipfile.ZipFile(io.BytesIO(self.client.get(job["links"]["download_zip"]).data))
        self.assertEqual(zf.namelist(), ["0000-maria-jose-alvarez.pdf"])

    def test_zip_unavailable_when_nothing_generated(self):
        job = self.create_job({"title": "T", "recipients": [{"name": ""}]})
        self.assertEqual(self.client.get(job["links"]["download_zip"]).status_code, 409)

    def test_zip_unknown_job_404(self):
        self.assertEqual(self.client.get("/api/jobs/nope/download").status_code, 404)

    def test_zip_409_while_job_still_running(self):
        from certgen import db
        job = self.create_job()
        conn = db.connect(self.app.config["DATABASE"])
        conn.execute("UPDATE jobs SET status='processing' WHERE id=?", (job["id"],))
        conn.commit(); conn.close()
        resp = self.client.get(job["links"]["download_zip"])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "job_not_finished")
