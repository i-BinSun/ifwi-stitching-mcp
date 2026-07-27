# tests/test_archive.py
import tarfile
import zipfile
from pathlib import Path

import py7zr

from ifwi_mcp import archive
from ifwi_mcp.result import ErrorCode


def _seed(tmp_path: Path) -> Path:
    """Lay out a tree with a .bin and a text file; return the staging dir."""
    staging = tmp_path / "staging"
    (staging / "sub").mkdir(parents=True)
    (staging / "fw.bin").write_bytes(b"BIN")
    (staging / "sub" / "readme.txt").write_text("hi")
    return staging


def test_extract_missing_archive(clean_env):
    r = archive.extract_archive("/no/such.7z")
    assert r["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_bad_format(clean_env, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    assert archive.extract_archive(str(bad))["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_rejects_bad_dest_name(clean_env, tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("f.bin", b"x")
    r = archive.extract_archive(str(z), dest_name="../escape")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_extract_zip_reports_bins(clean_env, tmp_path):
    staging = _seed(tmp_path)
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in staging.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(staging))
    r = archive.extract_archive(str(z))
    assert r["ok"] is True
    assert r["data"]["file_count"] == 2
    assert [Path(b).name for b in r["data"]["bins"]] == ["fw.bin"]


def test_extract_tar_reports_bins(clean_env, tmp_path):
    staging = _seed(tmp_path)
    t = tmp_path / "pkg.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        tf.add(staging, arcname=".")
    r = archive.extract_archive(str(t))
    assert r["ok"] is True
    assert [Path(b).name for b in r["data"]["bins"]] == ["fw.bin"]


def test_extract_7z_reports_bins(clean_env, tmp_path):
    staging = _seed(tmp_path)
    sevenz = tmp_path / "pkg.7z"
    with py7zr.SevenZipFile(sevenz, "w") as z:
        for p in staging.rglob("*"):
            if p.is_file():
                z.write(p, str(p.relative_to(staging)))
    r = archive.extract_archive(str(sevenz), dest_name="out")
    assert r["ok"] is True
    assert r["data"]["extract_dir"].endswith("/out")
    assert [Path(b).name for b in r["data"]["bins"]] == ["fw.bin"]
