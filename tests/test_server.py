# tests/test_server.py
import responses
import pytest
from ifwi_mcp import server
from ifwi_mcp.result import ErrorCode

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "xyz")
    return clean_env


def test_all_tools_exist():
    for name in ["fiv_list_projects", "fiv_get_project", "fiv_list_swimlanes",
                 "fiv_list_releases", "fiv_find_ifwi", "fiv_list_ifwi_binaries",
                 "fiv_find_ingredient", "fiv_find_stitch_tool", "fiv_find_env_package",
                 "fiv_match_build_by_oem",
                 "fiv_prepare_stitch", "fiv_prepare_ingredient",
                 "parse_ifwi_oem", "download", "list_local_files",
                 "extract_archive", "extract_stitch_tool", "run_stitch",
                 "create_up_config", "main"]:
        assert hasattr(server, name), name


def test_parse_ifwi_oem_wrapper_passes_through(fiv_env, tmp_path):
    result = server.parse_ifwi_oem(str(tmp_path / "missing.bin"))
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT


@responses.activate
def test_fiv_list_projects_wrapper(fiv_env):
    responses.add(responses.GET, f"{BASE}/app/rest/get_project_info/",
                  json=[{"id": 1, "name": "P", "project_name": "Proj"}], status=200)
    result = server.fiv_list_projects()
    assert result["ok"] is True
    assert result["data"]["projects"][0]["id"] == 1


def test_download_wrapper_validation(fiv_env):
    assert server.download("")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_wrapper_maps_unexpected_exception(fiv_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(server.oem_parser, "parse_ifwi_oem", boom)
    result = server.parse_ifwi_oem("/whatever")
    assert result["error_code"] == ErrorCode.INTERNAL_ERROR
    assert "kaboom" in result["detail"]["exception"]


def test_fiv_prepare_stitch_wrapper_passes_through(fiv_env, monkeypatch):
    from ifwi_mcp.result import ok
    monkeypatch.setattr(server.prepare, "prepare_stitch",
                        lambda *a, **k: ok({"prepared": False, "candidates": []}))
    r = server.fiv_prepare_stitch("P", "Orange")
    assert r["ok"] is True
    assert r["data"]["prepared"] is False


def test_main_local_uses_stdio(clean_env, monkeypatch):
    called = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: called.update(kwargs))
    server.main()
    assert called == {}


def test_server_mode_cannot_use_low_level_local_stitch(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    result = server.run_stitch("/tool", "/base.bin", "[]", "cfg")
    assert result["error_code"] == ErrorCode.CONFIG_INVALID


def test_main_server_uses_authenticated_http(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "test-bearer")
    monkeypatch.setenv("IFWI_MCP_EXEC_MODE", "remote")
    monkeypatch.setenv("IFWI_MCP_EXEC_ENDPOINT", "https://runner.example.com")
    called = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: called.update(kwargs))
    original_auth = server.mcp.auth
    try:
        server.main()
        assert called == {"host": "127.0.0.1", "port": 8000}
        assert "test-bearer" in server.mcp.auth.tokens
    finally:
        server.mcp.auth = original_auth


def test_server_http_requires_bearer_token(clean_env, monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "test-bearer")
    monkeypatch.setenv("IFWI_MCP_EXEC_MODE", "remote")
    monkeypatch.setenv("IFWI_MCP_EXEC_ENDPOINT", "https://runner.example.com")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    original_auth = server.mcp.auth
    try:
        server.main()
        with TestClient(server.mcp.http_app()) as client:
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}}
            assert client.post("/mcp", json=request).status_code == 401
            assert client.post("/mcp", json=request,
                               headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.post("/mcp", json=request,
                               headers={"Authorization": "Bearer test-bearer"}).status_code == 200
    finally:
        server.mcp.auth = original_auth
