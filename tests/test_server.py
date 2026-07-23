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
    for name in ["fiv_list_projects", "fiv_list_swimlanes", "fiv_find_ifwi",
                 "fiv_find_ingredient", "fiv_find_stitch_tool", "fiv_match_build_by_oem",
                 "parse_ifwi_oem", "download", "list_local_files",
                 "extract_stitch_tool", "run_stitch", "main"]:
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
