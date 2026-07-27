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


@pytest.mark.parametrize("ifwi_type,sub,category,domain,is_ifwi", [
    ("Platform", "server", "IFWI", "server", True),
    ("Platform", "client", "IFWI", "client", True),
    ("Graphic", "client", "IFWI", "graphic", True),
    ("Silicon", "Unified_Patch", "UP", None, False),
    ("Silicon", "BIOS", "BIOS", None, False),
    (None, None, "UNKNOWN", None, False),
])
def test_classify_project_type(ifwi_type, sub, category, domain, is_ifwi):
    r = fiv_portal.classify_project_type(ifwi_type, sub)
    assert r == {"category": category, "domain": domain, "is_ifwi": is_ifwi}


@responses.activate
def test_get_project_ifwi(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_project/"), status=200,
                  json={"name": "OakStreamAP", "project_name": "SiEn-OakStream-DiamonRapids-AP",
                        "ifwi_type": "Platform", "ifwi_sub_type": "server"})
    r = fiv_portal.get_project("OakStreamAP")
    assert r["ok"] is True
    assert r["data"]["project_id"] == 232
    assert r["data"]["category"] == "IFWI"
    assert r["data"]["domain"] == "server"
    assert r["data"]["is_ifwi"] is True


@responses.activate
def test_get_project_unified_patch(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_project/"), status=200,
                  json={"name": "OakStreamAP", "ifwi_type": "Silicon",
                        "ifwi_sub_type": "Unified_Patch"})
    r = fiv_portal.get_project("OakStreamAP")
    assert r["data"]["category"] == "UP"
    assert r["data"]["is_ifwi"] is False


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
def test_release_swimlanes_for_version_distinct(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
        {"version": "2026.28.3.01", "swimlane_branch": "release-ww28"},
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
    ], status=200)
    r = fiv_portal._release_swimlanes_for_version("OakStreamAP", "Orange", "2026.28.3.01")
    assert r["ok"] is True
    assert sorted(r["data"]["swimlanes"]) == ["main", "release-ww28"]


def _meta_url(ep):
    return f"{BASE}/meta_data/{ep}"


BUILD_LIST = {"code": 0, "msg": "ok", "data": {"list": [
    {"name": "Pre-Silicon_DMRAP", "build_branch": "release/ap.pre-silicon", "visible": 1,
     "phase": "engineering,Orange,Purple,Blue",
     "meta_data_info": '{"stepping": "Pre_Silicon", "swimlane": "DMRAP"}'},
    {"name": "IMH2_Pre_Silicon_DMRAP", "build_branch": "release/ap.imh2.pre_silicon", "visible": 1,
     "phase": "engineering,Orange,Purple,Blue",
     "meta_data_info": '{"stepping": "IMH2_Pre_Silicon", "swimlane": "DMRAP"}'},
    {"name": "Post-Silicon_DMRAP", "build_branch": "release/ap.post-silicon", "visible": 1,
     "phase": "Purple", "meta_data_info": '{"stepping": "Post_Silicon", "SKU": "X"}'},
    {"name": "Hidden_Build", "build_branch": "release/ap.hidden", "visible": 0,
     "phase": "Orange", "meta_data_info": ""},
]}}


@responses.activate
def test_list_swimlanes_filters_hidden(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _meta_url("build/"), json=BUILD_LIST, status=200)
    r = fiv_portal.list_swimlanes("OakStreamAP")
    assert r["ok"] is True
    branches = [s["swimlane_branch"] for s in r["data"]["swimlanes"]]
    assert "release/ap.imh2.pre_silicon" in branches
    assert "release/ap.hidden" not in branches   # visible != 1 dropped
    assert r["data"]["count"] == 3
    imh2 = next(s for s in r["data"]["swimlanes"] if "imh2" in s["swimlane_branch"])
    assert imh2["stepping"] == "IMH2_Pre_Silicon"


@responses.activate
def test_list_swimlanes_phase_filter(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _meta_url("build/"), json=BUILD_LIST, status=200)
    r = fiv_portal.list_swimlanes("OakStreamAP", "Orange")
    assert r["ok"] is True
    # Post-Silicon (Purple only) excluded; hidden already excluded
    names = {s["name"] for s in r["data"]["swimlanes"]}
    assert names == {"Pre-Silicon_DMRAP", "IMH2_Pre_Silicon_DMRAP"}


@responses.activate
def test_list_swimlanes_none_visible(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _meta_url("build/"),
                  json={"code": 0, "data": {"list": []}}, status=200)
    r = fiv_portal.list_swimlanes("OakStreamAP")
    assert r["error_code"] == ErrorCode.RELEASE_NOT_FOUND


@responses.activate
def test_list_releases_sorted_newest_first(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.23.5.01", "phase": "Purple", "swimlane_branch": "main",
         "status": "published", "publish_time": "2026-06-06T00:08:19"},
        {"version": "2026.26.5.01", "phase": "Purple", "swimlane_branch": "main",
         "status": "published", "publish_time": "2026-07-04T00:00:16"},
        {"version": "2026.25.3.01", "phase": "Purple", "swimlane_branch": "main",
         "status": "published", "publish_time": "2026-06-22T14:51:24"},
    ], status=200)
    r = fiv_portal.list_releases("OakStreamAP", "Purple")
    assert r["ok"] is True
    assert r["data"]["count"] == 3
    assert r["data"]["latest"]["version"] == "2026.26.5.01"
    assert [x["version"] for x in r["data"]["releases"]] == \
        ["2026.26.5.01", "2026.25.3.01", "2026.23.5.01"]
    # a large limit must be sent to defeat the endpoint's silent 20-row cap
    rel_call = [c for c in responses.calls if "get_ifwi_release" in c.request.url][0]
    assert "limit=1000000" in rel_call.request.url


@responses.activate
def test_list_releases_passes_swimlane_to_server(fiv_env):
    # the server honors the swimlane param; list_releases must forward it (not filter locally)
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.20.3.03", "phase": "Orange", "swimlane_branch": "release/ap.imh2.pre_silicon",
         "status": "published", "publish_time": "2026-05-16T02:16:42"},
    ], status=200)
    r = fiv_portal.list_releases("OakStreamAP", "Orange", "release/ap.imh2.pre_silicon")
    assert r["ok"] is True
    assert r["data"]["latest"]["version"] == "2026.20.3.03"
    rel_call = [c for c in responses.calls if "get_ifwi_release" in c.request.url][0]
    assert "swimlane=release%2Fap.imh2.pre_silicon" in rel_call.request.url


@responses.activate
def test_list_releases_empty_list(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[], status=200)
    r = fiv_portal.list_releases("OakStreamAP", "Orange")
    assert r["error_code"] == ErrorCode.RELEASE_NOT_FOUND


@responses.activate
def test_list_releases_error_dict(fiv_env):
    # FIV returns an error dict (not a list) when no data matches.
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"),
                  json={"code": 1, "msg": "fail", "error": "No data has found"}, status=200)
    r = fiv_portal.list_releases("OakStreamAP", "Daily")
    assert r["error_code"] == ErrorCode.RELEASE_NOT_FOUND


def test_list_releases_bad_phase(fiv_env):
    r = fiv_portal.list_releases("OakStreamAP", "Green")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


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
def test_list_ifwi_binaries_all_targets(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "release/ap.imh2.pre_silicon",
        "build_target": [
            {"package_name": "empty", "package_path": "p0/pkg0.7z", "binary_list": []},
            {"package_name": "debug", "package_path": "d/pkg1.7z", "binary_list": [
                # comma-separated: a _64M variant + standard, in one entry
                {"binary_name": "crb", "full_binary_name": "A_64M.bin, A.bin", "target_id": 1},
            ]},
            {"package_name": "release", "package_path": "r/pkg2.7z", "binary_list": [
                {"binary_name": "rel", "full_binary_name": "B.bin", "target_id": 2},
            ]},
        ],
    }, status=200)
    r = fiv_portal.list_ifwi_binaries("OakStreamAP", "Orange", "2026.20.3.03",
                                      swimlane="release/ap.imh2.pre_silicon")
    assert r["ok"] is True
    assert r["data"]["target_count"] == 3
    assert r["data"]["binary_count"] == 3          # 2 (comma-split) + 1
    debug = next(t for t in r["data"]["targets"] if t["build_target"] == "debug")
    assert debug["binaries"] == ["A_64M.bin", "A.bin"]   # comma string split
    assert debug["package_url"] == "https://art/root/d/pkg1.7z"
    empty = next(t for t in r["data"]["targets"] if t["build_target"] == "empty")
    assert empty["binaries"] == []


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
