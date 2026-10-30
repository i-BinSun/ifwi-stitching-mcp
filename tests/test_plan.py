# tests/test_plan.py
import pytest
from ifwi_mcp import plan
from ifwi_mcp.result import ErrorCode


ANSWERS = dict(project="DMR", phase="Orange",
               ingredients=[{"name": "MMC1", "version": "0.907.0"}],
               config_ini="Config_Stitch_DMR", version="2026.30.2.01")


def test_build_plan_renders_command_line(clean_env):
    r = plan.build_plan(**ANSWERS)
    assert r["ok"] is True
    cmd = r["data"]["command_line"]
    assert cmd.startswith("{python} {entry_script} ")
    assert "--binary_file {binary_file}" in cmd
    assert "--ingredient_name MMC1" in cmd
    assert "--ingredient_path {ingredient_path}" in cmd
    assert "--config_ini {config_ini}" in cmd
    assert "--soft_strap" not in cmd


def test_build_plan_persists_per_request_project_id(clean_env):
    built = plan.build_plan(fiv_project_id="77", **ANSWERS)
    assert built["data"]["plan"]["fiv_project_id"] == "77"
    assert plan.load_plan(built["data"]["plan_id"])["data"]["plan"]["fiv_project_id"] == "77"


def test_build_plan_renders_multiple_ingredients_pipe_joined(clean_env):
    answers = dict(ANSWERS)
    answers["ingredients"] = [{"name": "MMC1", "version": "0.907.0"},
                              {"name": "MMC2", "version": "0.907.0"}]
    r = plan.build_plan(**answers)
    assert r["ok"] is True
    assert "--ingredient_name MMC1|MMC2" in r["data"]["command_line"]
    assert r["data"]["plan"]["stitch"]["ingredient_names"] == ["MMC1", "MMC2"]
    assert len(r["data"]["plan"]["sources"]["ingredients"]) == 2


def test_build_plan_includes_soft_strap(clean_env):
    r = plan.build_plan(soft_strap="btg:Dma=1", **ANSWERS)
    assert "--soft_strap btg:Dma=1" in r["data"]["command_line"]


def test_build_plan_records_fiv_sources(clean_env):
    sources = plan.build_plan(swimlane="main", ifwi_binary="a.bin", **ANSWERS)["data"]["plan"]["sources"]
    assert sources["ifwi"] == {"kind": "fiv", "project": "DMR", "phase": "Orange",
                               "version": "2026.30.2.01", "swimlane": "main",
                               "binary_name": "a.bin", "from_build_targets": False}
    assert sources["ingredients"][0]["kind"] == "fiv"
    assert sources["ingredients"][0]["version"] == "0.907.0"
    # stitch tool defaults to the IFWI release version
    assert sources["stitch_tool"]["version"] == "2026.30.2.01"


def test_build_plan_marks_custom_urls(clean_env):
    urls = {"ifwi": "https://art/dev/ifwi.bin", "stitch_tool": "https://art/tool.7z",
            "ingredients": ["https://art/mmc.7z"]}
    sources = plan.build_plan(source_urls=urls, custom_urls={"https://art/dev/ifwi.bin"},
                              **ANSWERS)["data"]["plan"]["sources"]
    assert sources["ifwi"]["custom"] is True
    assert "custom" not in sources["ingredients"][0]
    assert "custom" not in sources["stitch_tool"]


def test_build_plan_records_build_target_fallback(clean_env):
    sources = plan.build_plan(ifwi_binary="a.bin", ifwi_from_build_targets=True,
                              **ANSWERS)["data"]["plan"]["sources"]
    assert sources["ifwi"]["from_build_targets"] is True


def test_build_plan_records_local_sources(clean_env, tmp_path, monkeypatch):
    ifwi = tmp_path / "base.bin"; ifwi.write_bytes(b"x")
    ing = tmp_path / "ing"; ing.mkdir()
    answers = dict(ANSWERS)
    answers["ingredients"] = [{"name": "MMC1", "path": str(ing)}]
    # No stitch tool cached/reachable here: preview resolution should fail soft.
    monkeypatch.setattr(plan.prepare, "prepare_stitch",
                        lambda *a, **k: {"ok": False, "error_code": "STITCH_TOOL_NOT_FOUND",
                                         "message": "no stitch tool", "detail": {}})
    r = plan.build_plan(ifwi_path=str(ifwi), **answers)
    sources = r["data"]["plan"]["sources"]
    assert sources["ifwi"] == {"kind": "local", "local_path": str(ifwi.resolve())}
    assert sources["ingredients"][0]["kind"] == "local"
    assert r["data"]["ingredient_match_preview"] == []
    assert any("could not prepare the stitch tool" in w for w in r["data"]["warnings"])


def _make_stitch_dir(tmp_path, ini_name, section, regex_dict_literal):
    stitch_dir = tmp_path / "stitch"
    (stitch_dir / "config").mkdir(parents=True)
    (stitch_dir / "cli.py").write_text("# fake entry script")
    ini_path = stitch_dir / "config" / ini_name
    ini_path.write_text(f"[{section}]\nregex_mandatory_dict = {regex_dict_literal}\n")
    return stitch_dir


def test_build_plan_local_ingredient_preview_matches_regex(clean_env, tmp_path):
    stitch_dir = _make_stitch_dir(tmp_path, "Config_Stitch_Test.ini", "MMC1",
                                  '{"MMC1_file": ".*mmc_pkg.*"}')
    ing = tmp_path / "ing"; ing.mkdir()
    (ing / "mmc_pkg_1.0.bin").write_bytes(b"x")
    answers = dict(ANSWERS)
    answers["ingredients"] = [{"name": "MMC1", "path": str(ing)}]
    answers["config_ini"] = "Config_Stitch_Test"
    r = plan.build_plan(stitch_path=str(stitch_dir), **answers)
    assert r["ok"] is True
    preview = r["data"]["ingredient_match_preview"]
    assert len(preview) == 1
    assert preview[0]["name"] == "MMC1"
    assert preview[0]["matches"]["MMC1_file"].endswith("mmc_pkg_1.0.bin")
    assert preview[0]["warnings"] == []


def test_build_plan_local_ingredient_preview_no_match_still_builds(clean_env, tmp_path):
    stitch_dir = _make_stitch_dir(tmp_path, "Config_Stitch_Test.ini", "MMC1",
                                  '{"MMC1_file": ".*nothing_matches_this.*"}')
    ing = tmp_path / "ing"; ing.mkdir()
    (ing / "unrelated.bin").write_bytes(b"x")
    answers = dict(ANSWERS)
    answers["ingredients"] = [{"name": "MMC1", "path": str(ing)}]
    answers["config_ini"] = "Config_Stitch_Test"
    r = plan.build_plan(stitch_path=str(stitch_dir), **answers)
    assert r["ok"] is True
    preview = r["data"]["ingredient_match_preview"]
    assert preview[0]["matches"]["MMC1_file"] is None
    assert any("no regex match" in w or "no files available" in w for w in preview[0]["warnings"])


def test_build_plan_is_saved_and_reloadable(clean_env):
    built = plan.build_plan(**ANSWERS)
    plan_id = built["data"]["plan_id"]
    loaded = plan.load_plan(plan_id)
    assert loaded["ok"] is True
    assert loaded["data"]["plan"] == built["data"]["plan"]
    assert plan.list_plans()["data"]["count"] == 1


def test_load_plan_unknown_id(clean_env):
    assert plan.load_plan("plan-nope")["error_code"] == ErrorCode.PLAN_NOT_FOUND


def test_load_plan_rejects_traversal(clean_env):
    assert plan.load_plan("../../etc/passwd")["error_code"] == ErrorCode.PLAN_INVALID


@pytest.mark.parametrize("override,param", [
    ({"project": ""}, "project"),
    ({"phase": "Green"}, "phase"),
    ({"ingredients": []}, "ingredients"),
    ({"ingredients": [{"name": ""}]}, "ingredients[0].name"),
    ({"ingredients": [{"name": "MMC1"}]}, "ingredients[0].version"),
    ({"config_ini": ""}, "config_ini"),
    ({"version": None}, "version"),
])
def test_build_plan_rejects_incomplete_answers(clean_env, override, param):
    answers = dict(ANSWERS); answers.update(override)
    r = plan.build_plan(**answers)
    assert r["error_code"] == ErrorCode.PLAN_INVALID
    assert r["detail"]["param"] == param


def test_build_plan_rejects_bad_soft_strap(clean_env):
    r = plan.build_plan(soft_strap="nope", **ANSWERS)
    assert r["detail"]["param"] == "soft_strap"


def test_build_plan_rejects_missing_local_paths(clean_env, tmp_path):
    r = plan.build_plan(ifwi_path=str(tmp_path / "gone.bin"), **ANSWERS)
    assert r["detail"]["param"] == "ifwi_path"
