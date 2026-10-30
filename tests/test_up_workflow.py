# tests/test_up_workflow.py
import configparser
from pathlib import Path
import pytest
import responses

from ifwi_mcp import executor, fiv_portal, plan, server, stitch_runner, up_config
from ifwi_mcp.result import ErrorCode

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "xyz")
    return clean_env


def test_generate_up_config_ini_default(tmp_path):
    out = tmp_path / "test.ini"
    res = up_config.generate_up_config_ini(
        str(out),
        default_params={"build_id": "99", "version": "0xF0010002", "build_path": "c:\\patch\\F0010002"},
        ingredients={
            "mcheck": "c:\\patch\\mcheck.patchlet",
            "s3m": "c:\\patch\\s3m.bin",
            "pcode-io": "c:\\patch\\pcode_io.bin",
            "ucode": "c:\\patch\\ucode.patch",
        }
    )
    assert res["ok"] is True
    assert out.is_file()

    cp = configparser.ConfigParser()
    cp.read(str(out))
    assert cp["default"]["build_id"] == "99"
    assert cp["default"]["version"] == "0xF0010002"
    assert cp["FIT1"]["mcheck"] == "c:\\patch\\mcheck.patchlet"
    assert cp["FIT1"]["ucode"] == "c:\\patch\\ucode.patch"
    assert cp["SOC"]["s3m"] == "c:\\patch\\s3m.bin"
    assert cp["SOC"]["pcode-io"] == "c:\\patch\\pcode_io.bin"


def test_generate_up_config_ini_with_template(tmp_path):
    tpl = tmp_path / "template.ini"
    tpl.write_text("""[default]
build_id = 01
version = 0x0
[FIT1]
fit1_sign = css
[SOC]
soc_sign = css
""", encoding="utf-8")

    out = tmp_path / "generated.ini"
    res = up_config.generate_up_config_ini(
        str(out),
        default_params={"version": "0xF0010099"},
        ingredients={"oobmsm": "c:\\patch\\oobmsm.bin"},
        template_path=str(tpl)
    )
    assert res["ok"] is True

    cp = configparser.ConfigParser()
    cp.read(str(out))
    assert cp["default"]["version"] == "0xF0010099"
    assert cp["FIT1"]["fit1_sign"] == "css"
    assert cp["SOC"]["oobmsm"] == "c:\\patch\\oobmsm.bin"


def test_find_cli_dir_prioritizes_muppet_project_folder(tmp_path):
    tool_root = tmp_path / "env_pkg"
    (tool_root / "MUPPET").mkdir(parents=True)
    (tool_root / "MUPPET" / "muppet.py").write_text("# main muppet", encoding="utf-8")

    (tool_root / "MUPPT_DMR").mkdir(parents=True)
    (tool_root / "MUPPT_DMR" / "muppet.py").write_text("# dmr muppet", encoding="utf-8")

    # With project="DMR", should select MUPPT_DMR
    found_dmr = stitch_runner.find_cli_dir(tool_root, project="DMR")
    assert found_dmr == tool_root / "MUPPT_DMR"

    # With other project or None, should find MUPPET or project folder
    found_default = stitch_runner.find_cli_dir(tool_root)
    assert found_default in (tool_root / "MUPPET", tool_root / "MUPPT_DMR")


def test_build_stitch_command_for_muppet(tmp_path):
    muppet_dir = tmp_path / "MUPPET"
    muppet_dir.mkdir(parents=True)
    (muppet_dir / "muppet.py").write_text("#", encoding="utf-8")

    cfg_file = tmp_path / "F0010002.ini"
    cfg_file.write_text("[default]\nversion=0xF0010002\n", encoding="utf-8")

    res = stitch_runner.build_stitch_command(
        str(muppet_dir),
        binary_file=None,
        ingredients=[],
        config_ini=str(cfg_file)
    )
    assert res["ok"] is True
    assert "-c" in res["data"]["argv"]
    assert str(cfg_file.resolve()) in res["data"]["argv"]


def test_execute_command_finds_release_dir_outputs(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    rel_dir = work_dir / "release"
    rel_dir.mkdir()
    out_bin = rel_dir / "F0010002.bin"
    out_bin.write_bytes(b"UP_BINARY_DATA")
    out_log = rel_dir / "PatchBuild_F0010002.log"
    out_log.write_text("build log", encoding="utf-8")

    def fake_subprocess_run(argv, cwd, capture_output, text, timeout, env, **kwargs):
        class Result:
            returncode = 0
            stdout = "MUPPT Build Stages Completed!"
            stderr = ""
        return Result()

    monkeypatch.setattr(stitch_runner.subprocess, "run", fake_subprocess_run)

    res = stitch_runner.execute_command(["python", "muppet.py", "-c", "test.ini"], cwd=str(work_dir))
    assert res["ok"] is True
    assert res["data"]["stitched_bin"] == str(out_bin)
    assert res["data"]["output_dir"] == str(rel_dir)


def test_execute_command_errors_when_exit_zero_but_no_output(clean_env, tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def fake_subprocess_run(argv, cwd, capture_output, text, timeout, env, **kwargs):
        class Result:
            returncode = 0
            stdout = "nothing was produced"
            stderr = ""
        return Result()

    monkeypatch.setattr(stitch_runner.subprocess, "run", fake_subprocess_run)

    res = stitch_runner.execute_command(["python", "cli.py"], cwd=str(work_dir))
    assert res["error_code"] == ErrorCode.STITCH_NO_OUTPUT
    assert Path(res["detail"]["full_log_path"]).is_file()


@responses.activate
def test_up_plan_building_and_execution(fiv_env, tmp_path, monkeypatch):
    # Setup mock FIV responses
    responses.add(responses.GET, f"{BASE}/app/rest/get_project_info/",
                  json=[{"id": 101, "name": "DMR_UP", "project_name": "DMR Unified Patch"}], status=200)
    responses.add(responses.GET, f"{BASE}/app/rest/get_project/",
                  json={"name": "DMR_UP", "ifwi_type": "Silicon", "ifwi_sub_type": "Unified_Patch"}, status=200)
    responses.add(responses.GET, f"{BASE}/app/rest/get_ifwi_release_detail/",
                  json={"version": "2026.28.3.01", "swimlane_branch": "main",
                        "env_atf_link": "https://art/up/dmr_env.7z"}, status=200)

    cfg_file = tmp_path / "F0010002.ini"
    cfg_file.write_text("[default]\nversion=0xF0010002\n", encoding="utf-8")

    # Build plan for UP project
    p = plan.build_plan(
        project="DMR_UP",
        phase="Orange",
        ingredients=[{"name": "s3m", "path": str(tmp_path)}],
        config_ini=str(cfg_file),
        version="2026.28.3.01",
        stitch_path=str(tmp_path),
    )
    assert p["ok"] is True
    assert p["data"]["plan"]["category"] == "UP"
    assert "{entry_script}" in p["data"]["command_line"]
