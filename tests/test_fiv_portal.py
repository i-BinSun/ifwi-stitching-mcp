# tests/test_fiv_portal.py
import responses
import pytest
from ifwi_mcp import fiv_portal
from ifwi_mcp.result import ErrorCode

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "xyz")
    return clean_env


def _url(ep):
    return f"{BASE}/app/rest/{ep}"


PROJECTS = [
    {"id": 232, "name": "OakStreamAP", "project_name": "SiEn-OakStream-DiamonRapids-AP"},
    {"id": 99, "name": "OtherProj", "project_name": "Other"},
]


def test_validate_rejects_bad_phase(fiv_env):
    r = fiv_portal.find_ifwi("OakStreamAP", "Green", "2026.28.3.01")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_validate_rejects_bad_version(fiv_env):
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "not-a-version")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_validate_accepts_leading_dot_version(fiv_env):
    # leading-dot version passes validation (will fail later on network, that's fine)
    assert fiv_portal._validate_common("p", "Orange", ".2026.28.3.01") is None


@responses.activate
def test_list_projects(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.list_projects()
    assert r["ok"] is True
    assert r["data"]["projects"][0]["id"] == 232
    assert responses.calls[0].request.headers["Authorization"] == "Bearer xyz"


@responses.activate
def test_resolve_project_id_case_insensitive(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.resolve_project_id("oakstreamap")
    assert r["data"]["project_id"] == 232


@responses.activate
def test_resolve_project_not_found(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.resolve_project_id("nope")
    assert r["error_code"] == ErrorCode.PROJECT_NOT_FOUND
    assert "OakStreamAP" in r["detail"]["candidates"]


@responses.activate
def test_projects_401_is_auth_failed(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), status=401)
    r = fiv_portal.list_projects()
    assert r["error_code"] == ErrorCode.AUTH_FAILED


@responses.activate
def test_list_swimlanes_distinct(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
        {"version": "2026.28.3.01", "swimlane_branch": "release-ww28"},
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
    ], status=200)
    r = fiv_portal.list_swimlanes("OakStreamAP", "Orange", "2026.28.3.01")
    assert r["ok"] is True
    assert sorted(r["data"]["swimlanes"]) == ["main", "release-ww28"]


@responses.activate
def test_find_ifwi_multiple_swimlanes(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
        {"version": "2026.28.3.01", "swimlane_branch": "release-ww28"},
    ], status=200)
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "2026.28.3.01")
    assert r["error_code"] == ErrorCode.MULTIPLE_SWIMLANES
    assert sorted(r["detail"]["candidates"]) == ["main", "release-ww28"]


@responses.activate
def test_find_ifwi_success_with_swimlane(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [
            {"package_name": "empty", "package_path": "p0/", "binary_list": []},
            {"package_name": "ifwi_pkg", "package_path": "pkg/",
             "binary_list": [{"binary_name": "ifwi", "full_binary_name": "OakStreamAP.bin", "target_id": "t1"}]},
        ],
    }, status=200)
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["ok"] is True
    assert r["data"]["ifwi_url"] == "https://art/root/pkg/OakStreamAP.bin"
    assert r["data"]["full_binary_name"] == "OakStreamAP.bin"


@responses.activate
def test_find_ingredient_success(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ingredient_detail/"),
                  json={"ingredient_link": "https://art/ing/B-1.0.bin",
                        "ingredient_name": "B", "ingredient_version": "1.0"}, status=200)
    r = fiv_portal.find_ingredient("OakStreamAP", "B", "1.0")
    assert r["ok"] is True
    assert r["data"]["ingredient_url"] == "https://art/ing/B-1.0.bin"


@responses.activate
def test_find_ingredient_not_found(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ingredient_detail/"),
                  json={"ingredient_link": None}, status=200)
    r = fiv_portal.find_ingredient("OakStreamAP", "B", "9.9")
    assert r["error_code"] == ErrorCode.INGREDIENT_NOT_FOUND


@responses.activate
def test_find_stitch_tool_success(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [
            {"package_name": "IFWI_Main", "package_path": "a/",
             "binary_list": [{"full_binary_name": "ifwi.bin"}]},
            {"package_name": "IFWI_Stitch_Tool", "package_path": "s/",
             "binary_list": [{"full_binary_name": "stitch_tool.zip"}]},
        ],
    }, status=200)
    r = fiv_portal.find_stitch_tool("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["ok"] is True
    assert r["data"]["stitch_url"] == "https://art/root/s/stitch_tool.zip"
    assert r["data"]["package_name"] == "IFWI_Stitch_Tool"


@responses.activate
def test_find_stitch_tool_none(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [{"package_name": "IFWI_Main", "package_path": "a/",
                          "binary_list": [{"full_binary_name": "ifwi.bin"}]}],
    }, status=200)
    r = fiv_portal.find_stitch_tool("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["error_code"] == ErrorCode.STITCH_TOOL_NOT_FOUND
    assert r["detail"]["available_packages"] == ["IFWI_Main"]


@responses.activate
def test_match_build_by_oem_single(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    # list_swimlanes call (no swimlane) -> one lane
    responses.add(responses.GET, _url("get_ifwi_release/"),
                  json=[{"version": "2026.28.3.01", "swimlane_branch": "main"}], status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/", "swimlane_branch": "main",
        "build_target": [
            {"package_name": "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned", "package_path": "p/",
             "binary_list": [{"full_binary_name": "ifwi.bin"}]},
        ],
    }, status=200)
    r = fiv_portal.match_build_by_oem(
        "OakStreamAP", "OKSDCRB1", ".2026.28.3.01",
        "_1P0_NonIPClean_Trace_DebugSigned", "Orange")
    assert r["ok"] is True
    assert r["data"]["matched_package"] == "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned"
    assert r["data"]["swimlane_branch"] == "main"


@responses.activate
def test_match_build_by_oem_none(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"),
                  json=[{"version": "2026.28.3.01", "swimlane_branch": "main"}], status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/", "swimlane_branch": "main",
        "build_target": [{"package_name": "SOMETHING_ELSE", "package_path": "p/",
                          "binary_list": [{"full_binary_name": "x.bin"}]}],
    }, status=200)
    r = fiv_portal.match_build_by_oem(
        "OakStreamAP", "OKSDCRB1", ".2026.28.3.01", "_1P0_NonIPClean", "Orange")
    assert r["error_code"] == ErrorCode.OEM_MATCH_NONE
    assert r["detail"]["parsed"]["product"] == "OKSDCRB1"
