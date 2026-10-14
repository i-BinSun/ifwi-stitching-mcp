from ifwi_mcp import interfaces
from ifwi_mcp.result import ErrorCode, ok


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