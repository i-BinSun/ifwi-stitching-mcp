# tests/test_stitch_runner.py
import ast
import os
import stat
import sys
import zipfile
from pathlib import Path
import pytest
from ifwi_mcp import stitch_runner
from ifwi_mcp.result import ErrorCode


def _make_stitch_zip(zip_path: Path, with_requirements=False):
    """A minimal stitch tool: cli.py that writes output/out_stitched.bin and exits 0."""
    cli_src = (
        "import argparse, os, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--binary_file', required=True)\n"
        "p.add_argument('--ingredient_name', required=True)\n"
        "p.add_argument('--ingredient_path', required=True)\n"
        "p.add_argument('--config_ini', required=True)\n"
        "p.add_argument('--soft_strap', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open(os.path.join('output', 'out_stitched.bin'), 'wb').write(b'STITCHED')\n"
        "print('done')\n"
        "sys.exit(0)\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/cli.py", cli_src)
        z.writestr("Output/config/Config_Stitch_TARGET_A.ini", "[Configuration]\n")
        z.writestr("Output/config/Config_Stitch_TARGET_B.ini", "[Configuration]\n")
        if with_requirements:
            z.writestr("Output/requirements.txt", "")  # empty: pip install is a no-op-ish


def test_extract_missing_archive(clean_env):
    assert stitch_runner.extract_stitch_tool("/no/such.zip")["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_bad_format(clean_env, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    assert stitch_runner.extract_stitch_tool(str(bad))["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_success_lists_config_targets(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True
    assert Path(r["data"]["stitch_dir"], "cli.py").is_file()
    assert sorted(r["data"]["config_targets"]) == [
        "Config_Stitch_TARGET_A", "Config_Stitch_TARGET_B"]


def test_extract_success_7z(clean_env, tmp_path):
    import py7zr
    # build a .7z with the same layout by first laying files on disk
    staging = tmp_path / "staging"
    (staging / "Output" / "config").mkdir(parents=True)
    (staging / "Output" / "cli.py").write_text("print('hi')\n")
    (staging / "Output" / "config" / "Config_Stitch_TARGET_A.ini").write_text("[Configuration]\n")
    sevenz = tmp_path / "stitch_tool.7z"
    with py7zr.SevenZipFile(sevenz, "w") as z:
        z.writeall(staging / "Output", "Output")
    r = stitch_runner.extract_stitch_tool(str(sevenz))
    assert r["ok"] is True
    assert Path(r["data"]["stitch_dir"], "cli.py").is_file()
    assert r["data"]["config_targets"] == ["Config_Stitch_TARGET_A"]


def test_run_stitch_success(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00" * 16)
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "Config_Stitch_TARGET_A")
    assert r["ok"] is True, r
    assert Path(r["data"]["stitched_bin"]).read_bytes() == b"STITCHED"
    assert r["data"]["exit_code"] == 0


def test_run_stitch_rejects_config_traversal(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "../cli.py")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_run_stitch_bad_soft_strap(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "Config_Stitch_TARGET_A",
        soft_strap="this is not valid")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_run_stitch_nonzero_exit(clean_env, tmp_path):
    # cli.py that exits 1
    zpath = tmp_path / "fail_tool.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Output/cli.py",
                   "import argparse,sys\n"
                   "p=argparse.ArgumentParser()\n"
                   "[p.add_argument(f'--{a}') for a in "
                   "['binary_file','ingredient_name','ingredient_path','config_ini','soft_strap']]\n"
                   "p.parse_args()\n"
                   "print('boom', file=sys.stderr)\n"
                   "sys.exit(1)\n")
        z.writestr("Output/config/Config_Stitch_T.ini", "[Configuration]\n")
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "Config_Stitch_T")
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["exit_code"] == 1
    assert "boom" in r["detail"]["log_tail"]


def _make_legacy_stitch_zip(zip_path: Path):
    """A minimal legacy stitch tool: no cli.py, Source/stitch2.py instead."""
    stitch2_src = (
        "import argparse, os, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--binary_file', required=True)\n"
        "p.add_argument('--ingredient_name', required=True)\n"
        "p.add_argument('--ingredient_path', required=True)\n"
        "p.add_argument('--config_ini', required=True)\n"
        "p.add_argument('--soft_strap', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open(os.path.join('output', 'out_stitched.bin'), 'wb').write(b'STITCHED')\n"
        "print('done')\n"
        "sys.exit(0)\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/Source/stitch2.py", stitch2_src)
        z.writestr("Output/Source/config/Config_Stitch_TARGET_A.ini", "[Configuration]\n")


def test_extract_legacy_tool_finds_stitch2(clean_env, tmp_path):
    zpath = tmp_path / "legacy_tool.zip"
    _make_legacy_stitch_zip(zpath)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True, r
    assert Path(r["data"]["stitch_dir"]).name == "Source"
    assert Path(r["data"]["stitch_dir"], "stitch2.py").is_file()
    assert r["data"]["config_targets"] == ["Config_Stitch_TARGET_A"]


def test_run_stitch_legacy_tool_uses_stitch2(clean_env, tmp_path):
    zpath = tmp_path / "legacy_tool.zip"
    _make_legacy_stitch_zip(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00" * 16)
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "Config_Stitch_TARGET_A")
    assert r["ok"] is True, r
    assert Path(r["data"]["stitched_bin"]).read_bytes() == b"STITCHED"
    assert "stitch2.py" in r["data"]["command_line"]


def _make_legacy_stitch_zip_sibling_config(zip_path: Path):
    """Legacy layout seen in real DMR-AP packages: Config/ is a *sibling* of
    Source/, not nested under it (Source/config does not exist)."""
    stitch2_src = (
        "import argparse, os, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--binary_file', required=True)\n"
        "p.add_argument('--ingredient_name', required=True)\n"
        "p.add_argument('--ingredient_path', required=True)\n"
        "p.add_argument('--config_ini', required=True)\n"
        "p.add_argument('--soft_strap', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open(os.path.join('output', 'out_stitched.bin'), 'wb').write(b'STITCHED')\n"
        "print('done')\n"
        "sys.exit(0)\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/Source/stitch2.py", stitch2_src)
        z.writestr("Output/Config/Config_Stitch_TARGET_A.ini", "[Configuration]\n")


def test_extract_legacy_tool_finds_sibling_config(clean_env, tmp_path):
    zpath = tmp_path / "legacy_sibling.zip"
    _make_legacy_stitch_zip_sibling_config(zpath)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True, r
    assert Path(r["data"]["stitch_dir"]).name == "Source"
    assert r["data"]["config_targets"] == ["Config_Stitch_TARGET_A"]


def test_extract_with_no_requirements_anywhere_installs_nothing(clean_env, tmp_path):
    zpath = tmp_path / "no_reqs.zip"
    _make_legacy_stitch_zip(zpath)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True, r
    assert r["data"]["deps_source"] == "none"
    assert not (Path(r["data"]["tool_root"]) / "venv" / ".deps_installed").exists()


def test_extract_finds_nested_platform_requirements_file(clean_env, tmp_path):
    """Legacy packages ship the FIT tool's own deps as requirements_<platform>.txt,
    nested under a FITm_Py subdir rather than next to stitch2.py."""
    zpath = tmp_path / "nested_reqs.zip"
    variant = "requirements_windows.txt" if sys.platform.startswith("win") else "requirements_linux.txt"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Output/Source/stitch2.py", "import sys\nsys.exit(0)\n")
        z.writestr("Output/Source/config/Config_Stitch_TARGET_A.ini", "[Configuration]\n")
        z.writestr(f"Output/FITm_Py/ModularFIT_cmd_1.0.0/{variant}", "")
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True, r
    assert r["data"]["deps_source"] == "requirements_file"
    assert (Path(r["data"]["tool_root"]) / "venv" / ".deps_installed").is_file()


def test_extract_uses_configured_fallback_deps_when_none_found(clean_env, tmp_path, monkeypatch):
    zpath = tmp_path / "no_reqs_fallback.zip"
    _make_legacy_stitch_zip(zpath)
    monkeypatch.setenv("IFWI_MCP_STITCH_FALLBACK_DEPS", "colorama,cbor2")
    calls = []
    real_run = stitch_runner.subprocess.run

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return real_run([argv[0], "-c", "pass"], **kwargs)

    monkeypatch.setattr(stitch_runner.subprocess, "run", fake_run)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True, r
    assert r["data"]["deps_source"] == "fallback"
    install_calls = [c for c in calls if "install" in c]
    assert install_calls and install_calls[0][-2:] == ["colorama", "cbor2"]
    assert (Path(r["data"]["tool_root"]) / "venv" / ".deps_installed").is_file()


def test_run_stitch_legacy_tool_sibling_config(clean_env, tmp_path):
    zpath = tmp_path / "legacy_sibling.zip"
    _make_legacy_stitch_zip_sibling_config(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00" * 16)
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), [{"name": "BIOS", "path": str(ing)}], "Config_Stitch_TARGET_A")
    assert r["ok"] is True, r
    assert Path(r["data"]["stitched_bin"]).read_bytes() == b"STITCHED"


def test_find_config_dir_sibling(clean_env, tmp_path):
    root = tmp_path / "root"
    (root / "Source").mkdir(parents=True)
    (root / "Config").mkdir()
    (root / "Config" / "Config_Stitch_TARGET_A.ini").write_text("[Configuration]\n")
    found = stitch_runner.find_config_dir(root / "Source")
    assert found == root / "Config"


def test_find_config_dir_none(clean_env, tmp_path):
    d = tmp_path / "lonely"; d.mkdir()
    assert stitch_runner.find_config_dir(d) is None


def test_find_entry_script_prefers_cli_py(clean_env, tmp_path):
    d = tmp_path / "both"; d.mkdir()
    (d / "cli.py").write_text("")
    (d / "stitch2.py").write_text("")
    assert stitch_runner.find_entry_script(d) == "cli.py"


def test_find_entry_script_none(clean_env, tmp_path):
    d = tmp_path / "neither"; d.mkdir()
    assert stitch_runner.find_entry_script(d) is None


def test_read_regex_mandatory_reads_section(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n"
        "[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*_imh1-a0\\\\.bin$\"}\n"
    )
    d = stitch_runner._read_regex_mandatory(ini, "MMC1")
    assert d == {"MMC1_file": ".*mmc_pkg_.*_imh1-a0\\.bin$"}
    assert stitch_runner._read_regex_mandatory(ini, "NoSuchIngredient") is None


def test_resolve_ingredient_arg_exact_regex_match(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*imh1-a0\\\\.bin$\"}\n"
    )
    d = tmp_path / "ing"; d.mkdir()
    good = d / "mmc_pkg_0.915.0_00_10_sign-prod-debug_encrypt-prod_imh1-a0.bin"
    good.write_bytes(b"x")
    (d / "readme.txt").write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", str(d))
    assert ast.literal_eval(arg) == {"MMC1_file": str(good)}
    assert warnings == []


def test_resolve_ingredient_arg_best_effort_when_no_regex_match(clean_env, tmp_path):
    # 0.907.0 package lacks the _imh1-a0 suffix the regex requires -> best-effort + warning.
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*_sign-prod-debug_encrypt-prod_imh1-a0\\\\.bin$\"}\n"
    )
    d = tmp_path / "ing"; d.mkdir()
    close = d / "mmc_pkg_0.907.0_00_10_sign-prod-debug_encrypt-prod.bin"
    close.write_bytes(b"x")
    (d / "mmc_pkg_0.907.0_00_00_unsigned_unencrypted.bin").write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", str(d))
    assert ast.literal_eval(arg)["MMC1_file"] == str(close)  # most token overlap
    assert any("best-effort" in w for w in warnings)


def test_resolve_ingredient_arg_passthrough_dict_string(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"; ini.write_text("[Configuration]\n")
    dict_str = '{"MMC1_file": "/abs/path/to/file.bin"}'
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", dict_str)
    assert arg == dict_str
    assert warnings == []


def test_resolve_ingredient_arg_passthrough_when_no_config_section(clean_env, tmp_path):
    # Back-compat: ingredient not in config -> path forwarded unchanged.
    ini = tmp_path / "cfg.ini"; ini.write_text("[Configuration]\n")
    f = tmp_path / "ing.bin"; f.write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "BIOS", str(f))
    assert arg == str(f)
    assert warnings == []


def _make_stitch_zip_echo_ingredient(zip_path):
    """cli.py that echoes --ingredient_path into output/ingredient_arg.txt and a .bin."""
    cli_src = (
        "import argparse, os\n"
        "p = argparse.ArgumentParser()\n"
        "for a in ['binary_file','ingredient_name','ingredient_path','config_ini','soft_strap']:\n"
        "    p.add_argument(f'--{a}', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open('output/ingredient_arg.txt','w').write(a.ingredient_path)\n"
        "open('output/out.bin','wb').write(b'STITCHED')\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/cli.py", cli_src)
        z.writestr("Output/config/Config_Stitch_MMC.ini",
                   "[Configuration]\n[MMC1]\n"
                   "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*imh1-a0\\\\.bin$\"}\n")


def test_run_stitch_auto_assembles_ingredient_dict(clean_env, tmp_path):
    zpath = tmp_path / "tool.zip"
    _make_stitch_zip_echo_ingredient(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing"; ing.mkdir()
    mmc = ing / "mmc_pkg_0.915.0_00_10_sign-prod-debug_encrypt-prod_imh1-a0.bin"
    mmc.write_bytes(b"m")
    r = stitch_runner.run_stitch(stitch_dir, str(binary), [{"name": "MMC1", "path": str(ing)}],
                                 "Config_Stitch_MMC")
    assert r["ok"] is True, r
    echoed = Path(stitch_dir, "output", "ingredient_arg.txt").read_text()
    assert ast.literal_eval(echoed) == {"MMC1_file": str(mmc)}
    assert r["data"]["warnings"] == []


def test_build_stitch_command_joins_multiple_ingredients_with_pipe(clean_env, tmp_path):
    zpath = tmp_path / "tool.zip"
    _make_stitch_zip(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing1 = tmp_path / "ing1.bin"; ing1.write_bytes(b"\x01")
    ing2 = tmp_path / "ing2.bin"; ing2.write_bytes(b"\x02")
    r = stitch_runner.build_stitch_command(
        stitch_dir, str(binary),
        [{"name": "MMC1", "path": str(ing1)}, {"name": "MMC2", "path": str(ing2)}],
        "Config_Stitch_TARGET_A")
    assert r["ok"] is True, r
    argv = r["data"]["argv"]
    assert argv[argv.index("--ingredient_name") + 1] == "MMC1|MMC2"
    assert argv[argv.index("--ingredient_path") + 1] == f"{ing1}|{ing2}"
    assert r["data"]["ingredient_args"] == [str(ing1), str(ing2)]


def test_build_stitch_command_rejects_empty_ingredients(clean_env, tmp_path):
    zpath = tmp_path / "tool.zip"
    _make_stitch_zip(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    r = stitch_runner.build_stitch_command(stitch_dir, str(binary), [], "Config_Stitch_TARGET_A")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert r["detail"]["param"] == "ingredients"


def _make_tool_root(tmp_path: Path, name: str) -> Path:
    """A minimal, already-materialized-looking tool install: cli.py + config/ next
    to a venv/ dir, with no Stitching/Output/output runtime dirs yet."""
    root = tmp_path / name
    (root / "config").mkdir(parents=True)
    (root / "cli.py").write_text("print('hi')\n")
    (root / "config" / "Config_Stitch_A.ini").write_text("[Configuration]\n")
    (root / "venv").mkdir()
    (root / "venv" / "marker.txt").write_text("venv")
    return root


def test_materialize_workspace_is_idempotent_for_same_key(clean_env, tmp_path):
    tool_root = _make_tool_root(tmp_path, "tool")
    first = stitch_runner.materialize_workspace(tool_root, "same-key")
    second = stitch_runner.materialize_workspace(tool_root, "same-key")
    assert first["ok"] is True and second["ok"] is True
    assert first["data"]["workspace_root"] == second["data"]["workspace_root"]


def test_materialize_workspace_links_code_without_copying(clean_env, tmp_path):
    tool_root = _make_tool_root(tmp_path, "tool")
    workspace = stitch_runner.materialize_workspace(tool_root, "key-1")
    assert workspace["ok"] is True
    ws_root = Path(workspace["data"]["workspace_root"])
    assert (ws_root / "cli.py").read_text() == "print('hi')\n"
    assert (ws_root / "config" / "Config_Stitch_A.ini").is_file()
    # a hardlinked file is the *same* inode: editing through tool_root must be
    # visible through the workspace, proving it isn't a full copy.
    (tool_root / "cli.py").write_text("print('changed')\n")
    assert (ws_root / "cli.py").read_text() == "print('changed')\n"


def test_materialize_workspace_isolates_runtime_dirs_between_keys(clean_env, tmp_path):
    tool_root = _make_tool_root(tmp_path, "tool")
    ws1 = Path(stitch_runner.materialize_workspace(tool_root, "run-1")["data"]["workspace_root"])
    ws2 = Path(stitch_runner.materialize_workspace(tool_root, "run-2")["data"]["workspace_root"])
    assert ws1 != ws2

    (ws1 / "output").mkdir()
    (ws1 / "output" / "stitched.bin").write_bytes(b"one")
    (ws1 / "Stitching").mkdir()

    # run-2's workspace must not see run-1's runtime output/Stitching dirs, and
    # tool_root itself (the shared source) must never have gained them either.
    assert not (ws2 / "output").exists()
    assert not (ws2 / "Stitching").exists()
    assert not (tool_root / "output").exists()
    assert not (tool_root / "Stitching").exists()
