import pytest

from scripts import example_rest


def test_missing_job_explains_process_local_status(monkeypatch):
    class MissingJobResponse:
        status_code = 404
        ok = False

        def json(self):
            return {"ok": False, "error_code": "JOB_NOT_FOUND", "message": "job not found"}

    def request(method, url, **kwargs):
        assert method == "GET"
        assert url == "http://127.0.0.1:8767/jobs/missing"
        return MissingJobResponse()

    monkeypatch.setattr(example_rest, "BASE_URL", "http://127.0.0.1:8767")
    monkeypatch.setattr(example_rest.requests, "request", request)
    with pytest.raises(RuntimeError, match="submitted instance: unknown"):
        example_rest.call("GET", "/jobs/missing", {})


def test_missing_job_reports_instance_switch(monkeypatch):
    class MissingJobResponse:
        status_code = 404
        ok = False

        def json(self):
            return {"ok": False, "error_code": "JOB_NOT_FOUND", "message": "job not found",
                    "detail": {"instance_id": "new"}}

    monkeypatch.setattr(example_rest.requests, "request", lambda *args, **kwargs: MissingJobResponse())
    with pytest.raises(RuntimeError, match="submitted to instance old.*instance new"):
        example_rest.call("GET", "/jobs/missing", {}, expected_instance="old")