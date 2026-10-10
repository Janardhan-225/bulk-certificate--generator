"""Creating a job, input validation, status/progress."""
from certgen import generator
from tests.base import ApiTestCase


class CreateJobTests(ApiTestCase):
    def test_create_job_returns_202_and_job_details(self):
        resp = self.client.post("/api/jobs", json=self.payload(issuer="Acme Academy"))
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertEqual(body["counts"]["total"], 2)
        self.assertEqual(body["issuer"], "Acme Academy")
        self.assertTrue(resp.headers["Location"].endswith(f"/api/jobs/{body['id']}"))

    def test_defaults_are_applied(self):
        body = self.create_job()
        self.assertEqual(body["issuer"], "Certificate Authority")
        self.assertRegex(body["issue_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_one_request_creates_one_certificate_per_recipient(self):
        job = self.create_job(self.payload(names=[f"Person {i}" for i in range(30)]))
        self.assertEqual(job["counts"]["generated"], 30)
        listing = self.client.get(job["links"]["certificates"]).get_json()
        self.assertEqual(listing["total"], 30)


class RequestValidationTests(ApiTestCase):
    def assert_400_on(self, body, field):
        resp = self.client.post("/api/jobs", json=body)
        self.assertEqual(resp.status_code, 400)
        err = resp.get_json()
        self.assertEqual(err["error"], "validation_error")
        self.assertIn(field, [d["field"] for d in err["details"]])

    def test_missing_title(self):
        self.assert_400_on({"recipients": [{"name": "A"}]}, "title")

    def test_blank_title(self):
        self.assert_400_on(self.payload(title="   "), "title")

    def test_missing_recipients(self):
        self.assert_400_on({"title": "T"}, "recipients")

    def test_empty_recipients(self):
        self.assert_400_on({"title": "T", "recipients": []}, "recipients")

    def test_recipients_not_a_list(self):
        self.assert_400_on({"title": "T", "recipients": "Asha"}, "recipients")

    def test_too_many_recipients(self):
        self.assert_400_on(self.payload(names=[f"P{i}" for i in range(51)]), "recipients")

    def test_bad_issue_date(self):
        self.assert_400_on(self.payload(issue_date="10/10/2026"), "issue_date")

    def test_body_not_json(self):
        resp = self.client.post("/api/jobs", data="hello", content_type="text/plain")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_json")

    def test_body_not_an_object(self):
        resp = self.client.post("/api/jobs", json=["a", "b"])
        self.assertEqual(resp.status_code, 400)

    def test_nothing_is_created_when_request_is_invalid(self):
        self.client.post("/api/jobs", json={"title": "T", "recipients": []})
        from certgen import service
        self.assertEqual(service.unfinished_job_ids(self.app.config["DATABASE"]), [])


class RecipientValidationTests(ApiTestCase):
    """Bad recipients fail individually; valid ones in the same job still succeed."""

    def test_invalid_recipients_are_reported_not_fatal(self):
        job = self.create_job({
            "title": "T",
            "recipients": [
                {"name": "Good One", "email": "good@example.com"},
                {"name": "", "email": "x@example.com"},          # empty name
                {"email": "noname@example.com"},                  # missing name
                {"name": "Bad Email", "email": "nope"},           # bad email
                {"name": "x" * 101},                              # too long
                {"name": "Hindi \u0905\u0936\u094b\u0915"},       # glyphs font can't render
                "just a string",                                  # wrong type
                {"name": "Also Good"},
            ]})
        self.assertEqual(job["status"], "completed_with_errors")
        self.assertEqual(job["counts"], {"total": 8, "generated": 2, "failed": 6, "pending": 0})

        failed = self.client.get(job["links"]["certificates"] + "?status=failed").get_json()
        by_index = {c["index"]: c["error"] for c in failed["results"]}
        self.assertEqual(sorted(by_index), [1, 2, 3, 4, 5, 6])
        self.assertIn("name", by_index[1])
        self.assertIn("email", by_index[3])
        self.assertIn("at most 100", by_index[4])
        self.assertIn("cannot render", by_index[5])

    def test_all_invalid_gives_failed_job(self):
        job = self.create_job({"title": "T", "recipients": [{"name": ""}, {}]})
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["counts"]["generated"], 0)

    def test_name_whitespace_is_normalised(self):
        job = self.create_job(self.payload(names=["  Asha    Verma  "]))
        cert = self.client.get(job["links"]["certificates"]).get_json()["results"][0]
        self.assertEqual(cert["recipient_name"], "Asha Verma")


class StatusAndProgressTests(ApiTestCase):
    def test_finished_job_status(self):
        job = self.create_job()
        fetched = self.client.get(f"/api/jobs/{job['id']}").get_json()
        self.assertEqual(fetched["status"], "completed")
        self.assertEqual(fetched["progress_percent"], 100.0)
        self.assertEqual(fetched["counts"], {"total": 2, "generated": 2, "failed": 0, "pending": 0})
        self.assertIsNotNone(fetched["finished_at"])

    def test_unknown_job_is_404(self):
        self.assertEqual(self.client.get("/api/jobs/doesnotexist").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/doesnotexist/certificates").status_code, 404)

    def test_progress_is_visible_while_job_is_running(self):
        """Poll the status endpoint from INSIDE the generator, i.e. mid-job."""
        snapshots = []
        real = generator.generate_certificate
        status_client = self.app.test_client()

        def spying_generate(path, **kwargs):
            job_id = path.parent.name
            snapshots.append(status_client.get(f"/api/jobs/{job_id}").get_json())
            return real(path, **kwargs)

        generator.generate_certificate = spying_generate
        try:
            self.create_job(self.payload(names=["A", "B", "C", "D"]))
        finally:
            generator.generate_certificate = real

        self.assertEqual([s["status"] for s in snapshots], ["processing"] * 4)
        self.assertEqual([s["counts"]["generated"] for s in snapshots], [0, 1, 2, 3])
        self.assertEqual([s["progress_percent"] for s in snapshots], [0.0, 25.0, 50.0, 75.0])

    def test_pagination_and_filters(self):
        job = self.create_job(self.payload(names=[f"P{i}" for i in range(5)]))
        page = self.client.get(job["links"]["certificates"] + "?limit=2&offset=2").get_json()
        self.assertEqual(page["total"], 5)
        self.assertEqual([c["index"] for c in page["results"]], [2, 3])
        self.assertEqual(self.client.get(job["links"]["certificates"] + "?status=bogus").status_code, 400)
        self.assertEqual(self.client.get(job["links"]["certificates"] + "?limit=abc").status_code, 400)


class AsyncProcessingTests(ApiTestCase):
    app_config = {"ASYNC_PROCESSING": True, "REQUEUE_ON_START": False}

    def test_background_processing_reaches_a_final_state(self):
        import time
        job = self.create_job(self.payload(names=[f"P{i}" for i in range(10)]))
        deadline = time.time() + 15
        status = job
        while time.time() < deadline:
            status = self.client.get(f"/api/jobs/{job['id']}").get_json()
            if status["status"] not in ("pending", "processing"):
                break
            time.sleep(0.05)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["counts"]["generated"], 10)


class ResumeTests(ApiTestCase):
    def test_reprocessing_a_finished_job_does_not_regenerate(self):
        from certgen import service
        job = self.create_job()
        calls = []
        real = generator.generate_certificate
        generator.generate_certificate = lambda *a, **k: calls.append(1)
        try:
            service.process_job(self.app.config["DATABASE"], self.app.config["CERT_DIR"], job["id"])
        finally:
            generator.generate_certificate = real
        self.assertEqual(calls, [])
