# tests/test_binary_ingredients.py
import pytest

from ifwi_mcp import fiv_portal
from ifwi_mcp.result import ok, ErrorCode


INFO = {
    "ingredient_list": [
        {"name": "MMC", "version": "1.2.3", "url": "https://af/mmc.bin",
         "project_id": "232", "ingredient_ipx_id": "ipx-1"},
        {"name": "ME", "version": "4.5.6", "url": "https://af/me.bin",
         "project_id": "232", "ingredient_ipx_id": "ipx-2"},
    ],
    "swimlane_branch": "release/ap.b0.poweron",
}


@pytest.fixture
def portal(monkeypatch):
    """Fake the raw HTTP layer plus project/swimlane resolution."""
    state = {"payload": INFO, "params": None}

    def fake_get(endpoint, params, prefix="app/rest"):
        state["params"] = dict(params)
        state["endpoint"] = endpoint
        return ok({"json": state["payload"]})

    monkeypatch.setattr(fiv_portal, "_get", fake_get)
    monkeypatch.setattr(fiv_portal, "resolve_project_id",
                        lambda project: ok({"project_id": 232, "name": project}))
    monkeypatch.setattr(fiv_portal, "_resolve_swimlane_or_multi",
                        lambda p, ph, v, sw: ok({"swimlane": sw or "release/ap.b0.poweron"}))
    return state


def test_get_binary_ingredients_returns_list(portal):
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "2026.35.3.01",
                                          "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned")
    assert r["ok"] is True
    assert portal["endpoint"] == "get_release_binary_info/"
    assert portal["params"]["binary_name"] == "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned"
    assert r["data"]["count"] == 2
    assert r["data"]["swimlane_branch"] == "release/ap.b0.poweron"
    names = {i["name"]: i["version"] for i in r["data"]["ingredients"]}
    assert names["MMC"] == "1.2.3"


def test_omitted_binary_name_auto_resolves_single_candidate(monkeypatch):
    monkeypatch.setattr(fiv_portal, "list_release_binaries", lambda p, ph, v, sw: ok({
        "binaries": [{"binary_name": "only_one", "url": "https://af/only_one.bin"}],
        "swimlane_branch": "release/ap.b0.poweron",
    }))
    monkeypatch.setattr(fiv_portal, "resolve_project_id",
                        lambda project: ok({"project_id": 232, "name": project}))
    monkeypatch.setattr(fiv_portal, "_resolve_swimlane_or_multi",
                        lambda p, ph, v, sw: ok({"swimlane": sw or "release/ap.b0.poweron"}))
    monkeypatch.setattr(fiv_portal, "_get", lambda endpoint, params, prefix="app/rest": ok({"json": INFO}))
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "2026.35.3.01")
    assert r["ok"] is True
    assert r["data"]["binary_name"] == "only_one"


def test_omitted_binary_name_is_ambiguous_with_several_candidates(monkeypatch):
    monkeypatch.setattr(fiv_portal, "list_release_binaries", lambda p, ph, v, sw: ok({
        "binaries": [{"binary_name": "a"}, {"binary_name": "b"}],
        "swimlane_branch": "release/ap.b0.poweron",
    }))
    monkeypatch.setattr(fiv_portal, "resolve_project_id",
                        lambda project: ok({"project_id": 232, "name": project}))
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "2026.35.3.01")
    assert r["error_code"] == ErrorCode.IFWI_BINARY_AMBIGUOUS
    assert r["detail"]["candidates"] == ["a", "b"]


def test_unmatched_binary_name_is_not_found(portal):
    portal["payload"] = {}
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "2026.35.3.01", "nope")
    assert r["error_code"] == ErrorCode.IFWI_BINARY_NOT_FOUND


def test_error_envelope_is_release_not_found(portal):
    portal["payload"] = {"code": 1, "msg": "fail", "error": "report is not available"}
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "2026.35.3.01", "x")
    assert r["error_code"] == ErrorCode.RELEASE_NOT_FOUND
    assert r["detail"]["reason"] == "report is not available"


def test_validates_version(portal):
    r = fiv_portal.get_binary_ingredients("DMR", "Orange", "not-a-version", "x")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
