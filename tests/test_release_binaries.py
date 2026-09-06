# tests/test_release_binaries.py
import pytest

from ifwi_mcp import fiv_portal, prepare
from ifwi_mcp.result import ok, err, ErrorCode


REPORT = {
    "project_id": "232",
    "version": "2026.35.3.02",
    "phase": "Orange",
    "swimlane_branch": "release/ap.post-silicon",
    "daily_hash": "7dab591",
    "binaries": [
        {"binary_id": "59", "binary_name": "OKSDREL1_NonIPClean", "short_name": "REL1_NonIP",
         "binary_list": ["https://af/a/OKSDREL1_NonIPClean.bin"], "validated": "YES"},
        {"binary_id": "7", "binary_name": "OKSDREL1_IPCleanDFX", "short_name": "REL1_DFX",
         "binary_list": ["https://af/a/OKSDREL1_IPCleanDFX.bin"], "validated": "YES"},
    ],
}


@pytest.fixture
def portal(monkeypatch):
    """Fake the raw HTTP layer plus project/swimlane resolution."""
    state = {"payload": REPORT, "params": None}

    def fake_get(endpoint, params, prefix="app/rest"):
        state["params"] = dict(params)
        state["endpoint"] = endpoint
        return ok({"json": state["payload"]})

    monkeypatch.setattr(fiv_portal, "_get", fake_get)
    monkeypatch.setattr(fiv_portal, "resolve_project_id",
                        lambda project: ok({"project_id": 232, "name": project}))
    monkeypatch.setattr(fiv_portal, "_resolve_swimlane_or_multi",
                        lambda p, ph, v, sw: ok({"swimlane": sw or "release/ap.post-silicon"}))
    return state


def test_list_release_binaries_flattens_entries(portal):
    r = fiv_portal.list_release_binaries("DMR", "Orange", "2026.35.3.02")
    assert r["ok"] is True
    assert portal["endpoint"] == "get_release_binary/"
    assert r["data"]["count"] == 2
    assert r["data"]["source"] == "release_report"
    assert r["data"]["swimlane_branch"] == "release/ap.post-silicon"
    first = r["data"]["binaries"][0]
    assert first["binary_name"] == "OKSDREL1_NonIPClean"
    assert first["url"] == "https://af/a/OKSDREL1_NonIPClean.bin"
    assert first["validated"] == "YES"


def test_missing_report_is_release_not_found(portal):
    # FIV answers HTTP 200 with an error envelope when there is no report.
    portal["payload"] = {"code": 1, "msg": "fail", "error": "report is not available"}
    r = fiv_portal.list_release_binaries("DMR", "Orange", "2026.35.3.02")
    assert r["error_code"] == ErrorCode.RELEASE_NOT_FOUND
    assert r["detail"]["reason"] == "report is not available"


def test_empty_report_is_release_not_found(portal):
    portal["payload"] = dict(REPORT, binaries=[])
    assert fiv_portal.list_release_binaries("DMR", "Orange",
                                            "2026.35.3.02")["error_code"] == ErrorCode.RELEASE_NOT_FOUND


def test_list_release_binaries_validates_version(portal):
    r = fiv_portal.list_release_binaries("DMR", "Orange", "not-a-version")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


@pytest.fixture
def prep(monkeypatch):
    state = {"report": ok({"swimlane_branch": "lane", "count": 2,
                           "binaries": REPORT["binaries"] and [
                               {"binary_name": b["binary_name"], "short_name": b["short_name"],
                                "url": b["binary_list"][0], "urls": b["binary_list"],
                                "validated": "YES", "binary_id": b["binary_id"]}
                               for b in REPORT["binaries"]]}),
             "downloaded": [], "find_ifwi_called": False}

    monkeypatch.setattr(prepare.fiv_portal, "list_release_binaries",
                        lambda *a, **kw: state["report"])

    def fake_find_ifwi(project, phase, version, swimlane=None):
        state["find_ifwi_called"] = True
        return ok({"ifwi_url": "https://af/pkg.7z", "swimlane_branch": "lane",
                   "binaries": ["a.bin"], "release_root": "", "build_target": "t"})

    def fake_download(url, category="ifwi", dest_name=None):
        state["downloaded"].append((url, category))
        return ok({"local_path": "/cache/ifwi/" + url.rsplit("/", 1)[-1],
                   "source": "download", "bytes": 1})

    def fake_extract(path, dest_name=None):
        return ok({"extract_dir": "/x", "file_count": 1, "bins": ["/x/a.bin"]})

    monkeypatch.setattr(prepare.fiv_portal, "find_ifwi", fake_find_ifwi)
    monkeypatch.setattr(prepare.downloader, "download", fake_download)
    monkeypatch.setattr(prepare.archive, "extract_archive", fake_extract)
    return state


def test_prepare_ifwi_prefers_the_release_report(prep):
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02",
                             binary_name="OKSDREL1_IPCleanDFX")
    assert r["ok"] is True
    assert r["data"]["source"] == "release_report"
    assert r["data"]["binary_file"].endswith("OKSDREL1_IPCleanDFX.bin")
    assert prep["downloaded"] == [("https://af/a/OKSDREL1_IPCleanDFX.bin", "ifwi")]
    assert prep["find_ifwi_called"] is False      # never touched the .7z path


def test_prepare_ifwi_matches_report_by_short_name_or_filename(prep):
    for needle in ("REL1_DFX", "OKSDREL1_IPCleanDFX.bin"):
        r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02", binary_name=needle)
        assert r["ok"] is True and r["data"]["source"] == "release_report"


def test_prepare_ifwi_report_ambiguous_lists_candidates(prep):
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02")
    assert r["error_code"] == ErrorCode.IFWI_BINARY_AMBIGUOUS
    assert r["detail"]["candidates"] == ["OKSDREL1_NonIPClean", "OKSDREL1_IPCleanDFX"]
    assert prep["downloaded"] == []


def test_prepare_ifwi_unknown_report_binary(prep):
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02", binary_name="nope")
    assert r["error_code"] == ErrorCode.IFWI_BINARY_NOT_FOUND
    assert "OKSDREL1_NonIPClean" in r["detail"]["available"]


def test_from_build_targets_skips_the_report(prep):
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02", from_build_targets=True)
    assert r["ok"] is True
    assert r["data"]["source"] == "build_targets"
    assert prep["find_ifwi_called"] is True
    assert prep["downloaded"] == [("https://af/pkg.7z", "ifwi")]


def test_missing_report_falls_back_automatically(prep):
    prep["report"] = err(ErrorCode.RELEASE_NOT_FOUND, "no release report", {})
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02")
    assert r["ok"] is True
    assert r["data"]["source"] == "build_targets"
    assert prep["find_ifwi_called"] is True


def test_real_errors_do_not_fall_back(prep):
    prep["report"] = err(ErrorCode.MULTIPLE_SWIMLANES, "pick one", {"candidates": ["a", "b"]})
    r = prepare.prepare_ifwi("DMR", "Orange", "2026.35.3.02")
    assert r["error_code"] == ErrorCode.MULTIPLE_SWIMLANES
    assert prep["find_ifwi_called"] is False
