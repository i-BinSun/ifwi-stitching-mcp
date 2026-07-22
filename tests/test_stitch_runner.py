# tests/test_stitch_runner.py
import os
import stat
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


def test_run_stitch_success(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00" * 16)
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_TARGET_A")
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
        stitch_dir, str(binary), "BIOS", str(ing), "../cli.py")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_run_stitch_bad_soft_strap(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_TARGET_A",
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
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_T")
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["exit_code"] == 1
    assert "boom" in r["detail"]["log_tail"]
