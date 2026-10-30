import json
import time

import pytest
from starlette.testclient import TestClient

from ifwi_mcp import executor, http_api, interfaces, jobs
from ifwi_mcp.result import ErrorCode, err, ok

HEADERS = {"Authorization": "Bearer secret"}


@pytest.fixture
def client(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    from ifwi_mcp.server import mcp
    with TestClient(http_api.create_app(mcp)) as test_client:
        yield test_client


def _poll(client, url, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(url, headers=HEADERS)
        status = response.json()["data"]
        if status["terminal"] or time.monotonic() > deadline:
            return response
        time.sleep(0.02)


def test_authenticated_query_and_async_stitching(client, monkeypatch):
    monkeypatch.setattr(interfaces, "query", lambda payload: ok({"versions": payload["versions"]}))
    monkeypatch.setattr(interfaces, "resolve_stitching", lambda payload: ok(
        {"plan": {"plan_id": "p"}, "plan_id": "p", "warnings": ["resolve warn"]}))
    monkeypatch.setattr(executor, "execute_local", lambda plan, cfg: ok({"plan_id": plan["plan_id"],
                                                                          "warnings": []}))
    assert client.post("/query", json={"versions": ["v1"]}).status_code == 401
    assert client.post("/query", json={"versions": ["v1"]}, headers=HEADERS).json()["data"] == {"versions": ["v1"]}

    response = client.post("/stitching", json={"async": True}, headers=HEADERS)
    assert response.status_code == 202
    submitted = response.json()["data"]
    assert submitted["state"] == "running" and submitted["terminal"] is False
    assert submitted["instance_id"] and submitted["kind"] == "stitch"
    assert response.headers["Location"] == submitted["status_url"]
    assert response.headers["Retry-After"] == str(submitted["poll_after_seconds"])
    assert client.get(submitted["status_url"]).status_code == 401

    final = _poll(client, submitted["status_url"])
    status = final.json()["data"]
    assert status["state"] == "succeeded" and status["result_source"] == "server"
    assert status["result"]["data"]["plan_id"] == "p"
    assert status["result"]["data"]["warnings"] == ["resolve warn"]
    assert "Retry-After" not in final.headers

    missing = client.get("/jobs/unknown", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error_code"] == ErrorCode.JOB_NOT_FOUND
    assert missing.json()["detail"]["instance_id"] == submitted["instance_id"]


def test_query_async_and_stitching_sync_errors(client, monkeypatch):
    monkeypatch.setattr(interfaces, "query", lambda payload: ok({"count": 1}))
    monkeypatch.setattr(interfaces, "resolve_stitching", lambda payload: err(
        ErrorCode.IFWI_BINARY_AMBIGUOUS, "select binary", {"candidates": ["a", "b"]}))
    queued = client.post("/query", json={"async": True}, headers=HEADERS)
    assert queued.status_code == 202
    assert _poll(client, queued.json()["data"]["status_url"]).json()["data"]["result"] == ok({"count": 1})
    sync = client.post("/stitching", json={}, headers=HEADERS)
    assert sync.status_code == 409
    assert sync.json()["detail"]["job_id"] == sync.headers["X-Job-Id"]
    assert client.post("/query", json={"async": "yes"}, headers=HEADERS).status_code == 400
    assert client.post("/query", data="not-json", headers=HEADERS).status_code == 400
    bad_id = client.post("/query", json={"async": True, "client_request_id": "x y"}, headers=HEADERS)
    assert bad_id.status_code == 400


def test_sync_stitching_success_carries_the_task_id(client, monkeypatch):
    monkeypatch.setattr(interfaces, "resolve_stitching", lambda payload: ok(
        {"plan": {"plan_id": "p"}, "plan_id": "p", "warnings": []}))
    monkeypatch.setattr(executor, "execute_local", lambda plan, cfg: ok({"plan_id": "p"}))
    response = client.post("/stitching", json={}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["task_job_id"] == response.headers["X-Job-Id"]


def test_stitching_dry_run_http_contract(client, monkeypatch):
    monkeypatch.setattr(interfaces, "stitching", lambda payload: ok({
        "dry_run": True, "command_template": "{python} {entry_script}"}))
    response = client.post("/stitching", json={"dry_run": True}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["dry_run"] is True
    queued = client.post("/stitching", json={"dry_run": True, "async": True}, headers=HEADERS)
    assert queued.status_code == 200
    assert queued.json()["data"]["command_template"] == "{python} {entry_script}"
    assert "job_id" not in queued.json()["data"]


def test_idempotent_submission_list_and_cancel(client, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(interfaces, "query", lambda payload: gate.wait(5) and ok({"n": 1}))
    first = client.post("/query", json={"async": True, "client_request_id": "req-1"}, headers=HEADERS)
    again = client.post("/query", json={"async": True, "client_request_id": "req-1"}, headers=HEADERS)
    assert (first.status_code, again.status_code) == (202, 200)
    job_id = first.json()["data"]["job_id"]
    assert again.json()["data"]["job_id"] == job_id
    conflict = client.post("/query", json={"async": True, "client_request_id": "req-1", "x": 1},
                           headers=HEADERS)
    assert conflict.status_code == 409 and conflict.json()["error_code"] == ErrorCode.IDEMPOTENCY_CONFLICT

    listed = client.get("/jobs?state=running&kind=query", headers=HEADERS).json()["data"]
    assert [j["job_id"] for j in listed["jobs"]] == [job_id] and "result" not in listed["jobs"][0]
    assert client.get("/jobs?limit=0", headers=HEADERS).status_code == 400

    cancelled = client.post(f"/jobs/{job_id}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json()["data"]["state"] == "cancelled"
    assert cancelled.json()["data"]["result"]["error_code"] == ErrorCode.JOB_CANCELLED
    gate.set()
    time.sleep(0.1)
    assert client.get(f"/jobs/{job_id}", headers=HEADERS).json()["data"]["state"] == "cancelled"
    assert client.post("/jobs/nope/cancel", headers=HEADERS).status_code == 404


def test_job_survives_a_manager_restart(client, monkeypatch):
    monkeypatch.setattr(interfaces, "query", lambda payload: ok({"n": 1}))
    job_id = client.post("/query", json={"async": True}, headers=HEADERS).json()["data"]["job_id"]
    _poll(client, f"/jobs/{job_id}")
    jobs.reset_manager()                         # a new process reads the same jobs.db
    status = client.get(f"/jobs/{job_id}", headers=HEADERS)
    assert status.status_code == 200 and status.json()["data"]["state"] == "succeeded"
    assert "events" not in status.json()["data"]
    events = client.get(f"/jobs/{job_id}?events=true", headers=HEADERS).json()["data"]["events"]
    assert [e["detail"] for e in events] == ["created",
                                             "running/querying -> succeeded/done [server: ok]"]


def test_remote_callback_route(client, write_config, monkeypatch):
    write_config({"execution": {"mode": "remote", "endpoint": "https://jobs.example.com/job",
                                "artifactory_repo": "https://af.example.com/repo",
                                "report_base_url": "https://mcp.example.com",
                                "refresh_min_interval_seconds": 0}})
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "af-secret")
    created = {}

    def fake_create(endpoint, cfg, command):
        created["command"] = command
        return {"outcome": "created", "pod_job_id": "fivportal-abc"}
    monkeypatch.setattr(executor, "create_pod", fake_create)
    monkeypatch.setattr(executor, "job_api_get",
                        lambda endpoint, pod: {"kind": "running", "status": "running", "auth": False})
    monkeypatch.setattr(interfaces, "resolve_stitching", lambda payload: ok(
        {"plan": {"plan_id": "plan-x"}, "plan_id": "plan-x", "warnings": []}))

    job_id = client.post("/stitching", json={"async": True}, headers=HEADERS).json()["data"]["job_id"]
    deadline = time.monotonic() + 5
    while "command" not in created and time.monotonic() < deadline:
        time.sleep(0.01)
    while client.get(f"/jobs/{job_id}", headers=HEADERS).json()["data"]["stage"] == "submitting":
        time.sleep(0.01)
    running = client.get(f"/jobs/{job_id}", headers=HEADERS).json()["data"]
    assert running["stage"] == "running" and running["remote"]["pod_job_id"] == "fivportal-abc"
    token = created["command"].split("--report-token ")[1].split()[0]
    body = {"schema_version": 1, "job_id": job_id, "run_id": running["run_id"], "attempt_id": "a",
            "wrapper": {"version": "1.1.0"}, "worker": {"commit": "c"},
            "result": {"ok": True, "data": {"deliverables": [
                {"kind": "stitched_bin", "name": "o.bin", "url": "https://af/o.bin"}]}}}
    url = f"/internal/jobs/{job_id}/result"
    assert client.post(url, json=body, headers=HEADERS).status_code == 401   # serve.token is refused
    assert client.post(url, content=b"x" * (1024 * 1024 + 1),
                       headers={"Authorization": f"Bearer {token}"}).status_code == 413
    accepted = client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    assert accepted.status_code == 200 and accepted.json()["data"]["accepted"] is True
    done = client.get(f"/jobs/{job_id}", headers=HEADERS).json()["data"]
    assert done["state"] == "succeeded" and done["result_source"] == "callback"
    assert done["result"]["data"]["stitched_bin"]["url"] == "https://af/o.bin"
    assert done["remote"]["worker_commit"] == "c"
