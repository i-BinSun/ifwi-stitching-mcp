from starlette.testclient import TestClient

from ifwi_mcp import http_api, interfaces
from ifwi_mcp.result import ErrorCode, err, ok


def test_authenticated_query_and_async_stitching(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    monkeypatch.setattr(interfaces, "query", lambda payload: ok({"versions": payload["versions"]}))
    monkeypatch.setattr(interfaces, "stitching", lambda payload: ok({"plan_id": "p"}))
    from ifwi_mcp.server import mcp
    with TestClient(http_api.create_app(mcp)) as client:
        assert client.post("/query", json={"versions": ["v1"]}).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert client.post("/query", json={"versions": ["v1"]}, headers=headers).json()["data"] == {"versions": ["v1"]}
        response = client.post("/stitching", json={"async": True}, headers=headers)
        assert response.status_code == 202
        submitted = response.json()["data"]
        status_url = submitted["status_url"]
        assert submitted["instance_id"]
        assert client.get(status_url).status_code == 401
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = client.get(status_url, headers=headers).json()["data"]
            assert status["instance_id"] == submitted["instance_id"]
            if status["state"] != "running":
                break
        assert status["state"] == "succeeded"
        assert status["result"]["data"]["plan_id"] == "p"
        missing = client.get("/jobs/unknown", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error_code"] == ErrorCode.JOB_NOT_FOUND
        assert missing.json()["detail"]["instance_id"] == submitted["instance_id"]


def test_query_async_and_stitching_sync_errors(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    monkeypatch.setattr(interfaces, "query", lambda payload: ok({"count": 1}))
    monkeypatch.setattr(interfaces, "stitching", lambda payload: err(
        ErrorCode.IFWI_BINARY_AMBIGUOUS, "select binary", {"candidates": ["a", "b"]}))
    from ifwi_mcp.server import mcp
    with TestClient(http_api.create_app(mcp)) as client:
        headers = {"Authorization": "Bearer secret"}
        queued = client.post("/query", json={"async": True}, headers=headers)
        assert queued.status_code == 202
        assert client.post("/stitching", json={}, headers=headers).status_code == 409
        assert client.post("/query", json={"async": "yes"}, headers=headers).status_code == 400
        assert client.post("/query", data="not-json", headers=headers).status_code == 400


def test_stitching_dry_run_http_contract(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    monkeypatch.setattr(interfaces, "stitching", lambda payload: ok({
        "dry_run": True, "command_template": "{python} {entry_script}"}))
    from ifwi_mcp.server import mcp
    with TestClient(http_api.create_app(mcp)) as client:
        headers = {"Authorization": "Bearer secret"}
        response = client.post("/stitching", json={"dry_run": True}, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["dry_run"] is True
        queued = client.post("/stitching", json={"dry_run": True, "async": True},
                     headers=headers)
        assert queued.status_code == 200
        assert queued.json()["data"]["command_template"] == "{python} {entry_script}"
        assert "job_id" not in queued.json()["data"]