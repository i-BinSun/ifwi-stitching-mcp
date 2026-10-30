import sys

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

    monkeypatch.setattr(example_rest.requests, "request", request)
    with pytest.raises(RuntimeError, match="submitted instance: unknown"):
        example_rest.call("GET", "/jobs/missing", {}, base_url="http://127.0.0.1:8767")


def test_missing_job_reports_instance_switch(monkeypatch):
    class MissingJobResponse:
        status_code = 404
        ok = False

        def json(self):
            return {"ok": False, "error_code": "JOB_NOT_FOUND", "message": "job not found",
                    "detail": {"instance_id": "new"}}

    monkeypatch.setattr(example_rest.requests, "request", lambda *args, **kwargs: MissingJobResponse())
    with pytest.raises(RuntimeError, match="submitted to instance old.*instance new"):
        example_rest.call("GET", "/jobs/missing", {}, base_url="http://127.0.0.1:8767",
                          expected_instance="old")


def test_client_requires_explicit_base_url(monkeypatch, capsys):
    monkeypatch.delenv("IFWI_MCP_BASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["example_rest.py", "query"])
    with pytest.raises(SystemExit) as exc:
        example_rest.main()
    assert exc.value.code == 2
    assert "set IFWI_MCP_BASE_URL" in capsys.readouterr().err


def test_client_requires_token_from_environment(monkeypatch, capsys):
    monkeypatch.setenv("IFWI_MCP_BASE_URL", "http://example.com")
    monkeypatch.delenv("IFWI_MCP_SERVER_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["example_rest.py", "query"])
    with pytest.raises(SystemExit) as exc:
        example_rest.main()
    assert exc.value.code == 2
    assert "client process environment" in capsys.readouterr().err


def test_stitching_submits_real_job_and_polls(monkeypatch, capsys):
    monkeypatch.setenv("IFWI_MCP_BASE_URL", "http://example.com/")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["example_rest.py", "stitching"])
    observed = {}

    def submit(method, path, headers, *, base_url, payload):
        observed.update(method=method, path=path, headers=headers,
                        base_url=base_url, payload=payload)
        return {"status_url": "/jobs/123", "instance_id": "current"}

    def poll(status_url, headers, *, base_url, expected_instance):
        assert (status_url, base_url, expected_instance) == (
            "/jobs/123", "http://example.com", "current")
        assert headers == observed["headers"]
        return {"stitched_bin": {"url": "https://example.com/result.bin"}}

    monkeypatch.setattr(example_rest, "call", submit)
    monkeypatch.setattr(example_rest, "wait_for_result", poll)
    assert example_rest.main() == 0
    assert observed["method"] == "POST"
    assert observed["path"] == "/stitching"
    assert observed["headers"] == {"Authorization": "Bearer test-token"}
    assert observed["payload"]["async"] is True
    assert observed["payload"].get("dry_run") is not True
    assert len(observed["payload"]["client_request_id"]) == 32
    assert "https://example.com/result.bin" in capsys.readouterr().out

def test_wait_for_result_stops_on_terminal_and_honours_poll_after(monkeypatch):
    answers = [{"state": "running", "terminal": False, "poll_after_seconds": 30},
               {"state": "cancelled", "terminal": True, "result": {"ok": False}}]
    sleeps = []
    monkeypatch.setattr(example_rest, "call", lambda *a, **kw: answers.pop(0))
    monkeypatch.setattr(example_rest.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="Job cancelled"):
        example_rest.wait_for_result("/jobs/1", {}, base_url="http://x")
    assert sleeps == [30]