import struct

from ifwi_mcp import interfaces
from ifwi_mcp.result import ErrorCode, err, ok


def _oem_region(version="ap.post-silicon.2026.41.3.01",
                flavor="_IPCleanExternal_Trace_DebugSigned", product="OKSDCRB1"):
    return struct.pack("<16s 48s 128s 32s 32s", product.encode(), version.encode(),
                       flavor.encode(), b"\x90" * 32, b"")


def _serve_oem(monkeypatch, content):
    monkeypatch.setattr(interfaces.artifactory, "read_range",
                        lambda url, start, length: ok({"content": content}))


def test_query_one_and_comparison(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    def lookup(project, phase, version, binary, swimlane):
        assert project == "P"
        return ok({"binary_name": "only", "ingredients": [
            {"name": "MMC", "version": version},
            {"name": "ME", "version": "3" if version == "v1" else "3"},
            *([{"name": "NEW", "version": "1"}] if version == "v2" else [])]})

    monkeypatch.setattr(interfaces.fiv_portal, "get_binary_ingredients", lookup)
    payload = {"fiv_project_id": "77", "phase": "Orange", "versions": ["v1"]}
    assert interfaces.query(payload)["data"]["release"]["ingredients"][0]["version"] == "v1"
    payload["versions"] = ["v1", "v2"]
    result = interfaces.query(payload)
    assert result["data"]["differences"] == [
        {"name": "MMC", "from_version": "v1", "to_version": "v2"},
        {"name": "NEW", "from_version": None, "to_version": "1"}]


def test_stitching_refuses_ambiguous_binary_before_build(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", lambda *a: ok({
        "binaries": [{"binary_name": "a"}, {"binary_name": "b"}]}))
    payload = {"fiv_project_id": "77", "phase": "Orange", "version": "v1",
               "ingredients": {"MMC": "1"}}
    result = interfaces.stitching(payload)
    assert result["error_code"] == ErrorCode.IFWI_BINARY_AMBIGUOUS
    assert result["detail"]["candidates"] == ["a", "b"]


def test_stitching_builds_and_executes(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", lambda *a: ok({
        "binaries": [{"binary_name": "only", "url": "https://art/ifwi.bin"}], "swimlane_branch": "lane"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_stitch_tool", lambda *a: ok({"stitch_url": "https://art/tool.7z"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_ingredient", lambda *a: ok({"ingredient_url": "https://art/mmc.7z"}))
    captured = {}

    def build(*args, **kwargs):
        captured.update(kwargs)
        assert args[2] == [{"name": "MMC", "version": "1"}]
        assert args[3] == "Config_Stitch_only"
        return ok({"plan": {"plan_id": "p"}})

    monkeypatch.setattr(interfaces.plan, "build_plan", build)
    monkeypatch.setattr(interfaces.executor, "execute", lambda p: ok(p))
    result = interfaces.stitching({"fiv_project_id": "77", "phase": "Orange", "version": "v1",
                                   "ingredients": {"MMC": "1"}})
    assert result["data"]["plan_id"] == "p"
    assert captured["swimlane"] == "lane"
    assert captured["ifwi_binary"] == "only"
    assert captured["fiv_project_id"] == "77"
    assert captured["source_urls"] == {"ifwi": "https://art/ifwi.bin",
                                        "stitch_tool": "https://art/tool.7z",
                                        "ingredients": ["https://art/mmc.7z"]}


def test_stitching_uses_custom_ifwi_url(monkeypatch):
    _serve_oem(monkeypatch, b"")  # no OEM region: explicit values are used as given
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    # The release binary still names the config even when it has no URL of its own.
    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", lambda *a: ok({
        "binaries": [{"binary_name": "only"}], "swimlane_branch": "lane"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_stitch_tool", lambda *a: ok({"stitch_url": "https://art/tool.7z"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_ingredient", lambda *a: ok({"ingredient_url": "https://art/mmc.7z"}))
    captured = {}

    def build(*args, **kwargs):
        captured.update(kwargs)
        assert args[3] == "Config_Stitch_only"
        return ok({"plan": {"plan_id": "p"}})

    monkeypatch.setattr(interfaces.plan, "build_plan", build)
    monkeypatch.setattr(interfaces.executor, "execute", lambda p: ok(p))
    result = interfaces.stitching({"fiv_project_id": "77", "phase": "Orange", "version": "v1",
                                   "ingredients": {"MMC": "1", "PMC": "https://art/dev/pmc.7z"},
                                   "ifwi_url": " https://art/dev/my_ifwi.bin "})
    assert result["ok"] is True
    assert captured["source_urls"]["ifwi"] == "https://art/dev/my_ifwi.bin"
    assert captured["source_urls"]["ingredients"] == ["https://art/mmc.7z", "https://art/dev/pmc.7z"]
    assert captured["custom_urls"] == {"https://art/dev/my_ifwi.bin", "https://art/dev/pmc.7z"}
    assert captured["ifwi_binary"] == "only"
    assert "no usable OEM region" in result["data"]["warnings"][0]


def _oem_release(monkeypatch, binaries=("OKSDCRB1_IPCleanExternal_Trace_DebugSigned",
                                        "OKSDCRB1_IPCleanExternal_Trace_DebugSigned_2S")):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_swimlane_hint",
                        lambda project, phase, version, hint: ok({"swimlane": "release/" + hint}))
    calls = []

    def release_binaries(project, phase, version, lane):
        calls.append((version, lane))
        return ok({"binaries": [{"binary_name": name} for name in binaries],
                   "swimlane_branch": lane})

    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", release_binaries)
    monkeypatch.setattr(interfaces.fiv_portal, "find_stitch_tool",
                        lambda *a: ok({"stitch_url": "https://art/tool.7z"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_ingredient",
                        lambda *a: ok({"ingredient_url": "https://art/mmc.7z"}))
    monkeypatch.setattr(interfaces.plan, "build_plan", lambda *a, **k: ok({
        "plan_id": "p", "command_line": "cmd"}))
    return calls


OEM_PAYLOAD = {"fiv_project_id": "232", "phase": "Orange", "ingredients": {"MMC": "1"},
               "ifwi_url": "https://art/dev/custom.bin", "dry_run": True}


def test_stitching_infers_release_from_ifwi_oem(monkeypatch):
    _serve_oem(monkeypatch, _oem_region())
    calls = _oem_release(monkeypatch)
    result = interfaces.stitching(dict(OEM_PAYLOAD))
    assert result["ok"] is True
    data = result["data"]
    assert (data["version"], data["swimlane"], data["binary_name"]) == (
        "2026.41.3.01", "release/ap.post-silicon", "OKSDCRB1_IPCleanExternal_Trace_DebugSigned")
    assert calls == [("2026.41.3.01", "release/ap.post-silicon")]
    assert data["oem"]["product"] == "OKSDCRB1"
    assert data["source_urls"]["ifwi"] == "https://art/dev/custom.bin"


def test_stitching_rejects_explicit_values_that_conflict_with_oem(monkeypatch):
    _serve_oem(monkeypatch, _oem_region())
    _oem_release(monkeypatch)
    result = interfaces.stitching({**OEM_PAYLOAD, "version": "2026.43.3.02"})
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert result["detail"]["oem_version"] == "2026.41.3.01"
    result = interfaces.stitching({**OEM_PAYLOAD, "swimlane": "main"})
    assert result["detail"]["param"] == "swimlane"
    result = interfaces.stitching({**OEM_PAYLOAD,
                                   "binary_name": "OKSDCRB1_IPCleanExternal_Trace_DebugSigned_2S"})
    assert result["detail"]["param"] == "binary_name"
    # Matching explicit values are accepted.
    result = interfaces.stitching({**OEM_PAYLOAD, "version": "2026.41.3.01",
                                   "swimlane": "release/ap.post-silicon"})
    assert result["ok"] is True


def test_stitching_requires_version_when_oem_names_no_release(monkeypatch):
    _serve_oem(monkeypatch, _oem_region(version="", flavor=""))
    _oem_release(monkeypatch)
    result = interfaces.stitching(dict(OEM_PAYLOAD))
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert result["detail"]["param"] == "version"
    _serve_oem(monkeypatch, b"\xff" * 16)
    assert interfaces.stitching(dict(OEM_PAYLOAD))["detail"]["param"] == "version"


def test_stitching_customized_oem_still_needs_binary_name(monkeypatch):
    _serve_oem(monkeypatch, _oem_region(flavor="yyao7.20261020.130527"))
    _oem_release(monkeypatch)
    result = interfaces.stitching(dict(OEM_PAYLOAD))
    assert result["error_code"] == ErrorCode.IFWI_BINARY_AMBIGUOUS
    assert result["detail"]["oem"]["flavor_type"] == "customized"
    result = interfaces.stitching({**OEM_PAYLOAD,
                                   "binary_name": "OKSDCRB1_IPCleanExternal_Trace_DebugSigned_2S"})
    assert result["data"]["version"] == "2026.41.3.01"


def test_stitching_reports_ifwi_url_read_failure(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.artifactory, "read_range", lambda *a: err(
        ErrorCode.AUTH_FAILED, "artifactory rejected credentials"))
    assert interfaces.stitching(dict(OEM_PAYLOAD))["error_code"] == ErrorCode.AUTH_FAILED


def test_stitching_rejects_non_http_ifwi_url():
    result = interfaces.stitching({"fiv_project_id": "77", "phase": "Orange", "version": "v1",
                                   "ingredients": {"MMC": "1"}, "ifwi_url": "C:/ifwi.bin"})
    assert result["detail"]["param"] == "ifwi_url"


def test_invalid_payloads():
    assert interfaces.query({"fiv_project_id": "77", "phase": "Orange", "versions": []})["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert interfaces.stitching({"fiv_project_id": "77", "phase": "Orange",
                                 "version": "v1",
                                 "ingredients": []})["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert interfaces.stitching({"phase": "Orange", "version": "v1",
                                 "ingredients": {"MMC": "1"}})["detail"]["param"] == "fiv_project_id"


def test_stitching_requires_binary_url(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", lambda *a: ok({
        "binaries": [{"binary_name": "only"}], "swimlane_branch": "lane"}))
    result = interfaces.stitching({"fiv_project_id": "77", "phase": "Orange",
                                   "version": "v1", "ingredients": {"MMC": "1"}})
    assert result["error_code"] == ErrorCode.IFWI_BINARY_NOT_FOUND


def test_stitching_dry_run_returns_preview_without_executing(monkeypatch):
    monkeypatch.setattr(interfaces.fiv_portal, "resolve_project_by_id",
                        lambda pid: ok({"project_id": pid, "project": "P"}))
    monkeypatch.setattr(interfaces.fiv_portal, "list_release_binaries", lambda *args: ok({
        "binaries": [{"binary_name": "only", "url": "https://art/ifwi.bin"}], "swimlane_branch": "lane"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_stitch_tool", lambda *a: ok({"stitch_url": "https://art/tool.7z"}))
    monkeypatch.setattr(interfaces.fiv_portal, "find_ingredient", lambda *a: ok({"ingredient_url": "https://art/mmc.7z"}))
    monkeypatch.setattr(interfaces.plan, "build_plan", lambda *args, **kwargs: ok({
        "plan_id": "p", "command_line": "{python} {entry_script} --binary_file {binary_file}"}))

    def must_not_execute(*args):
        raise AssertionError("dry_run must not submit a job")

    monkeypatch.setattr(interfaces.executor, "execute", must_not_execute)
    result = interfaces.stitching({"fiv_project_id": "77", "phase": "Orange",
                                   "version": "v1", "ingredients": {"MMC": "1"}, "dry_run": True})
    assert result["ok"] is True
    assert result["data"]["config_ini"] == "Config_Stitch_only"
    assert result["data"]["binary_name"] == "only"
    assert result["data"]["source_urls"]["stitch_tool"] == "https://art/tool.7z"
    assert "{binary_file}" in result["data"]["command_template"]
    assert interfaces.stitching({"fiv_project_id": "77", "phase": "Orange",
                                 "dry_run": "yes"})["detail"]["param"] == "dry_run"