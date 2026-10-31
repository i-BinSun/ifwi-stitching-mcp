import json
import struct
import zipfile
from pathlib import Path

import pytest

from ifwi_mcp import bios_package, executor, stitch_runner
from ifwi_mcp.result import ErrorCode

BIOS_ID = "OKSDCRB1.JEN.0040.D62.2610290336"
BIOSID_REGEXES = {
    "main_fd": "Trunk_1P0_Debug\\bios_fd\\MAIN.fd$",
    "secpei_bin": "Trunk_1P0_Debug\\bios_fd\\SECPEI.fd$",
    "BinaryFd_fdf": "Trunk_1P0_Debug\\fdf\\BinaryFd.fdf$",
    "BuildConfig": "Trunk_1P0_Debug\\Config\\BuildConfig.ini$",
    "report": "Trunk_1P0_Debug\\report\\BuildReport.log$",
    "romfile": "Trunk_B0PO_Debug\\romfile\\.*.rom$",
    "milestone": "1P0",
}
MAIN = b"MAIN" + b"$IBIOSI$" + BIOS_ID.encode("utf-16-le") + b"\x00\x00" + b"\xff" * 16
SECPEI = b"SECPEI" * 4
PKG_MEMBERS = {
    "BiosPkg/BiosFile/MAIN.fd": MAIN,
    "BiosPkg/BiosFile/SECPEI.fd": SECPEI,
    "BiosPkg/ConfigInfo/BuildConfig.ini": b"[VabdConfig]\nINTEGRATE_VAB = FALSE\n",
    "BiosPkg/SourceFile/BinaryFd.fdf": b"[FD.Binary]\nSize = 0x1000\n",
    "BiosPkg/BaseTools/Bin/GnuNoteBti.bin": b"noise",
}


def _config(path: Path, regexes=BIOSID_REGEXES, section="BIOSID") -> str:
    path.write_text("[Configuration]\nINTEGRATE_VAB = FALSE\n"
                    f"[{section}]\nregex_mandatory_dict = {json.dumps(regexes)}\n", encoding="utf-8")
    return str(path)


def _zip_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "IFWIStitch20.bin", "w") as z:
        for name, data in PKG_MEMBERS.items():
            z.writestr(name, data)
    return root


@pytest.fixture
def small_ifwi(monkeypatch):
    """Shrink the '64MB' IFWI size so tests write tiny images."""
    monkeypatch.setattr(bios_package, "IFWI_SIZE", 0x100)

    def write(path: Path, signed=True):
        data = bytearray(0x100)
        if signed:
            struct.pack_into("<I", data, 0x10, bios_package.FD_SIGNATURE)
        path.write_bytes(bytes(data))
        return path
    return write


def test_ifwistitch_zip_becomes_biosid_folder(tmp_path):
    pkg = _zip_package(tmp_path / "pkg")
    r = bios_package.resolve_bios_ingredients(
        [{"name": "MMC1", "path": "x"}, {"name": "BIOS", "path": str(pkg)}],
        _config(tmp_path / "cfg.ini"), str(tmp_path / "work"))
    assert r["ok"], r
    mmc, bios = r["data"]["ingredients"]
    assert mmc == {"name": "MMC1", "path": "x"}
    assert bios["name"] == "BIOSID" and bios["verbatim"] is True
    version_dir = Path(bios["path"])
    assert version_dir.name == "0040.D62"
    assert (version_dir / "Trunk_1P0_Debug/bios_fd/MAIN.fd").read_bytes() == MAIN
    assert (version_dir / "Trunk_1P0_Debug/bios_fd/SECPEI.fd").read_bytes() == SECPEI
    assert (version_dir / "Trunk_1P0_Debug/Config/BuildConfig.ini").is_file()
    assert (version_dir / "Trunk_1P0_Debug/fdf/BinaryFd.fdf").is_file()
    assert not (version_dir / "Trunk_B0PO_Debug").exists()  # no romfile: the raw FDs win
    assert "BIOSID 0040.D62" in r["data"]["warnings"][0]


def test_zip_wins_over_64mb_images(tmp_path, small_ifwi):
    pkg = _zip_package(tmp_path / "pkg")
    small_ifwi(pkg / "IMG.bin")
    assert bios_package.detect(str(pkg))["kind"] == "zip"


def test_extracted_biospkg_folder_is_used(tmp_path):
    pkg = tmp_path / "pkg"
    for name, data in PKG_MEMBERS.items():
        target = pkg / "IFWIStitch20" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    r = bios_package.resolve_bios_ingredients([{"name": "bios", "path": str(pkg)}],
                                              _config(tmp_path / "cfg.ini"), str(tmp_path / "work"))
    assert r["ok"], r
    assert r["data"]["ingredients"][0]["name"] == "BIOSID"
    assert Path(r["data"]["ingredients"][0]["path"], "Trunk_1P0_Debug/bios_fd/MAIN.fd").read_bytes() == MAIN


def test_64mb_image_selection_rule(tmp_path, small_ifwi):
    pkg = tmp_path / "pkg"; pkg.mkdir()
    for name in ("A_X64_Simics.bin", "A_X64_PO_SAFE.bin", "A_X64_jc_2s_b.bin", "B_X64.bin",
                 "A_X64_Simics_jc_2s_b_PO_SAFE.bin"):
        small_ifwi(pkg / name)
    small_ifwi(pkg / "A_unsigned.bin", signed=False)
    (pkg / "SDU_CAPSULE.bin").write_bytes(b"small")
    r = bios_package.resolve_bios_ingredients([{"name": "BIOS", "path": str(pkg)}], None,
                                              str(tmp_path / "work"))
    assert r["ok"], r
    item = r["data"]["ingredients"][0]
    assert item == {"name": "BIOS", "path": str(pkg / "B_X64.bin"), "verbatim": True}
    assert "B_X64.bin" in r["data"]["warnings"][0]


def test_64mb_prefers_simics_over_po_safe_only_when_needed(tmp_path, small_ifwi):
    pkg = tmp_path / "pkg"; pkg.mkdir()
    small_ifwi(pkg / "A_PO_SAFE.bin")
    small_ifwi(pkg / "A_Simics_jc_2s_b.bin")
    small_ifwi(pkg / "A_Simics.bin")
    picked = bios_package.pick_ifwi_bin([str(p) for p in sorted(pkg.iterdir())])
    assert Path(picked["data"]["bin"]).name == "A_Simics.bin"


def test_only_po_safe_images_is_an_error(tmp_path, small_ifwi):
    pkg = tmp_path / "pkg"; pkg.mkdir()
    small_ifwi(pkg / "A_PO_SAFE.bin")
    r = bios_package.resolve_bios_ingredients([{"name": "BIOS", "path": str(pkg)}], None,
                                              str(tmp_path / "work"))
    assert r["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID
    assert r["detail"]["candidates"] == ["A_PO_SAFE.bin"]


def test_non_package_paths_pass_through(tmp_path):
    image = tmp_path / "bios.rom"; image.write_bytes(b"r")
    empty = tmp_path / "empty"; empty.mkdir()
    items = [{"name": "BIOS", "path": str(image)}, {"name": "BIOS", "path": str(empty)}]
    r = bios_package.resolve_bios_ingredients(items, None, str(tmp_path / "work"))
    assert r["ok"] and r["data"]["ingredients"] == items and r["data"]["warnings"] == []


def test_config_without_biosid_section_is_an_error(tmp_path):
    pkg = _zip_package(tmp_path / "pkg")
    r = bios_package.resolve_bios_ingredients([{"name": "BIOS", "path": str(pkg)}],
                                              _config(tmp_path / "cfg.ini", section="MMC1"),
                                              str(tmp_path / "work"))
    assert r["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID
    assert "[BIOSID]" in r["message"]


def test_non_literal_biosid_pattern_is_an_error(tmp_path):
    pkg = _zip_package(tmp_path / "pkg")
    regexes = dict(BIOSID_REGEXES, main_fd="Trunk_1P0_Debug\\bios_fd\\.*MAIN.fd$")
    r = bios_package.resolve_bios_ingredients([{"name": "BIOS", "path": str(pkg)}],
                                              _config(tmp_path / "cfg.ini", regexes),
                                              str(tmp_path / "work"))
    assert r["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID
    assert r["detail"]["key"] == "main_fd"


def test_fd_route_needs_the_target_config(tmp_path):
    pkg = _zip_package(tmp_path / "pkg")
    r = bios_package.resolve_bios_ingredients([{"name": "BIOS", "path": str(pkg)}], None,
                                              str(tmp_path / "work"))
    assert r["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID


def test_bios_version_parsing():
    assert bios_package.parse_bios_version(MAIN)["data"]["version"] == "0040.D62"
    other = b"$IBIOSI$" + "OKSDCRB1.JEN.0040.D63.2610290336".encode("utf-16-le") + b"\x00\x00"
    assert bios_package.parse_bios_version(MAIN + other)["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID
    assert bios_package.parse_bios_version(b"none")["error_code"] == ErrorCode.BIOS_PACKAGE_INVALID


def test_build_command_passes_bios_paths_verbatim(clean_env, tmp_path):
    tool = tmp_path / "tool"; (tool / "config").mkdir(parents=True)
    (tool / "cli.py").write_text("")
    _config(tool / "config" / "Config_Stitch_T.ini")
    binary = tmp_path / "base.bin"; binary.write_bytes(b"b")
    biosid = tmp_path / "BIOSID" / "0040.D62" / "Trunk_1P0_Debug" / "bios_fd"
    biosid.mkdir(parents=True)
    (biosid / "MAIN.fd").write_bytes(MAIN)
    folder = str(tmp_path / "BIOSID" / "0040.D62")
    r = stitch_runner.build_stitch_command(str(tool), str(binary),
                                           [{"name": "BIOSID", "path": folder}], "Config_Stitch_T")
    assert r["ok"], r
    assert r["data"]["ingredient_args"] == [folder]


def test_execute_local_stitches_bios_package_as_biosid(clean_env, monkeypatch, tmp_path):
    tool = tmp_path / "tool"; (tool / "config").mkdir(parents=True)
    (tool / "cli.py").write_text("")
    _config(tool / "config" / "Config_Stitch_DMR.ini")
    binary = tmp_path / "base.bin"; binary.write_bytes(b"b")
    pkg = _zip_package(tmp_path / "pkg")
    plan = {"plan_id": "plan-bios", "project": "DMR",
            "sources": {"ifwi": {"kind": "local", "local_path": str(binary)},
                        "ingredients": [{"kind": "local", "name": "BIOS", "local_path": str(pkg)}],
                        "stitch_tool": {"kind": "local", "local_path": str(tool)}},
            "stitch": {"ingredient_names": ["BIOS"], "config_ini": "Config_Stitch_DMR"}}
    seen = {}

    def fake_execute(argv, cwd, log_name=None, timeout=None, run_root=None):
        seen["argv"], seen["run_root"] = argv, run_root
        return {"ok": False, "error_code": ErrorCode.STITCH_RUN_FAILED, "message": "stop",
                "detail": {}}

    monkeypatch.setattr(executor.stitch_runner, "execute_command", fake_execute)
    executor._execute_local(plan, {"timeout_seconds": 10})
    argv = seen["argv"]
    assert argv[argv.index("--ingredient_name") + 1] == "BIOSID"
    path = Path(argv[argv.index("--ingredient_path") + 1])
    assert path.name == "0040.D62" and path.is_relative_to(Path(seen["run_root"]))
    assert (path / "Trunk_1P0_Debug/bios_fd/SECPEI.fd").read_bytes() == SECPEI
