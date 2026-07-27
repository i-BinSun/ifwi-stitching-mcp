# tests/test_prepare.py
import pytest
from ifwi_mcp import prepare
from ifwi_mcp.result import ok, err, ErrorCode


@pytest.fixture
def fakes(monkeypatch):
    """Replace prepare's collaborators with in-memory fakes."""
    state = {
        "stitch_by_version": {},   # version -> stitch_url (present == has stitch tool)
        "releases": [],            # newest-first list of version strings
        "downloaded": [],
        "extracted": [],
    }

    def fake_find_stitch_tool(project, phase, version, swimlane=None):
        url = state["stitch_by_version"].get(version)
        if url:
            return ok({"stitch_url": url, "package_name": "IFWI_Stitch_Tool_Release",
                       "binaries": []})
        return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
                   {"available_packages": []})

    def fake_list_releases(project, phase, swimlane=None):
        if not state["releases"]:
            return err(ErrorCode.RELEASE_NOT_FOUND, "no releases", {})
        rel = [{"version": v} for v in state["releases"]]
        return ok({"releases": rel, "latest": rel[0], "count": len(rel)})

    def fake_download(url, category="ifwi", dest_name=None):
        state["downloaded"].append((url, category))
        return ok({"local_path": f"/cache/{category}/{url.split('/')[-1]}",
                   "source": "download", "bytes": 1})

    def fake_extract_stitch_tool(local_path):
        state["extracted"].append(local_path)
        return ok({"stitch_dir": f"/x/{local_path.split('/')[-1]}",
                   "venv_python": "/x/venv/bin/python",
                   "config_targets": ["Config_Stitch_A"]})

    monkeypatch.setattr(prepare.fiv_portal, "find_stitch_tool", fake_find_stitch_tool)
    monkeypatch.setattr(prepare.fiv_portal, "list_releases", fake_list_releases)
    monkeypatch.setattr(prepare.downloader, "download", fake_download)
    monkeypatch.setattr(prepare.stitch_runner, "extract_stitch_tool", fake_extract_stitch_tool)
    return state


def test_prepare_stitch_version_present(fakes):
    fakes["stitch_by_version"] = {"2026.30.2.01": "https://art/s/stitch.7z"}
    r = prepare.prepare_stitch("P", "Orange", "2026.30.2.01")
    assert r["ok"] is True
    assert r["data"]["prepared"] is True
    assert r["data"]["selected_version"] == "2026.30.2.01"
    assert r["data"]["config_targets"] == ["Config_Stitch_A"]
    assert fakes["downloaded"] == [("https://art/s/stitch.7z", "stitch")]


def test_prepare_stitch_version_absent_offers_adjacent(fakes):
    # center 2026.30.2.01 has no stitch tool; a newer and an older neighbor do.
    fakes["releases"] = ["2026.31.1.01", "2026.30.4.01", "2026.30.2.01",
                         "2026.29.6.02", "2026.28.3.05"]
    fakes["stitch_by_version"] = {"2026.30.4.01": "https://art/a.7z",
                                  "2026.29.6.02": "https://art/b.7z"}
    r = prepare.prepare_stitch("P", "Orange", "2026.30.2.01")
    assert r["ok"] is True
    assert r["data"]["prepared"] is False
    vers = [c["version"] for c in r["data"]["candidates"]]
    assert vers == ["2026.30.4.01", "2026.29.6.02"]  # nearest-first
    assert fakes["downloaded"] == []                  # nothing downloaded on selection
    assert r["data"]["warnings"]


def test_prepare_stitch_no_version_scans_backward(fakes):
    fakes["releases"] = ["2026.30.2.01", "2026.30.4.01", "2026.29.6.02"]
    # latest has none; second one does
    fakes["stitch_by_version"] = {"2026.30.4.01": "https://art/x.7z"}
    r = prepare.prepare_stitch("P", "Orange")
    assert r["ok"] is True
    assert r["data"]["prepared"] is True
    assert r["data"]["selected_version"] == "2026.30.4.01"
    assert any("2026.30.2.01" in w for w in r["data"]["warnings"])


def test_prepare_stitch_no_version_none_found(fakes):
    fakes["releases"] = ["2026.30.2.01", "2026.30.4.01"]
    fakes["stitch_by_version"] = {}
    r = prepare.prepare_stitch("P", "Orange")
    assert r["error_code"] == ErrorCode.STITCH_TOOL_NOT_FOUND
