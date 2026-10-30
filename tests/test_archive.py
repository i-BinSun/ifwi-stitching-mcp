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


def test_extract_is_skipped_on_second_call(clean_env, tmp_path):
    staging = _seed(tmp_path)
    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in staging.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(staging))
    first = archive.extract_archive(str(z))
    assert first["data"]["source"] == "extract"
    z.unlink()  # prove the second call doesn't need the archive at all
    second = archive.extract_archive(str(tmp_path / "pkg.zip"))
    assert second["ok"] is True
    assert second["data"]["source"] == "cache"
    assert second["data"]["extract_dir"] == first["data"]["extract_dir"]


def _zip_with(tmp_path: Path, name: str, member: str) -> Path:
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(member, b"payload")
    return z


def test_cache_name_keeps_the_version():
    assert archive.cache_name("PowerOn_MMC1_0.958.0.7z") == "PowerOn_MMC1_0.958.0"
    assert archive.cache_name("tool.tar.gz") == "tool"
    assert archive.cache_name("no_suffix") == "no_suffix"


def test_versioned_packages_do_not_share_an_extract_dir(clean_env, tmp_path):
    a = archive.extract_archive(str(_zip_with(tmp_path, "MMC1_0.958.0.zip", "a.bin")))
    b = archive.extract_archive(str(_zip_with(tmp_path, "MMC1_0.959.0.zip", "b.bin")))
    assert a["data"]["extract_dir"] != b["data"]["extract_dir"]
    assert [Path(p).name for p in a["data"]["bins"]] == ["a.bin"]
    assert [Path(p).name for p in b["data"]["bins"]] == ["b.bin"]


def test_half_written_extraction_is_not_served_as_cache(clean_env, tmp_path):
    z = _zip_with(tmp_path, "pkg.zip", "fw.bin")
    first = archive.extract_archive(str(z))
    dest = Path(first["data"]["extract_dir"])
    # simulate a crash mid-extraction: content present, completion marker absent
    archive._marker(dest).unlink()
    (dest / "fw.bin").write_bytes(b"truncated")
    second = archive.extract_archive(str(z))
    assert second["data"]["source"] == "extract"
    assert (dest / "fw.bin").read_bytes() == b"payload"


def test_failed_extraction_leaves_no_cached_dir(clean_env, tmp_path):
    bad = tmp_path / "pkg.zip"
    bad.write_bytes(b"not an archive at all")
    r = archive.extract_archive(str(bad))
    assert r["error_code"] == ErrorCode.EXTRACT_FAILED
    assert not archive.is_extracted(clean_env / "cache" / "extracted" / "pkg")
