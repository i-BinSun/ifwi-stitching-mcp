"""Common archive extraction (.zip / .tar* / .7z). Pure file I/O — no network."""
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

import py7zr

from . import config, lock
from .result import ok, err, ErrorCode


def extract_into(src: Path, dest: Path) -> Optional[dict]:
    """Extract a .zip / .tar* / .7z archive into dest.

    Returns an err dict on failure, or None on success. Format is detected by
    content (zip -> tar -> 7z); py7zr.is_7zfile reads the file's magic.
    """
    try:
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as z:
                z.extractall(dest)
        elif tarfile.is_tarfile(src):
            with tarfile.open(src) as t:
                # filter="data" (py>=3.12, backported to recent 3.9+) blocks path
                # traversal / unsafe members; fall back if the runtime lacks it.
                try:
                    t.extractall(dest, filter="data")
                except TypeError:
                    t.extractall(dest)
        elif py7zr.is_7zfile(src):
            with py7zr.SevenZipFile(src, mode="r") as z:
                z.extractall(dest)
        else:
            return err(ErrorCode.EXTRACT_FAILED, "unsupported archive format",
                       {"archive": str(src), "reason": "not zip, tar, or 7z"})
    except (zipfile.BadZipFile, tarfile.TarError, py7zr.exceptions.ArchiveError, OSError) as exc:
        return err(ErrorCode.EXTRACT_FAILED, "extraction failed",
                   {"archive": str(src), "reason": str(exc)})
    return None


def extract_archive(archive_path: str, dest_name: Optional[str] = None) -> dict:
    """Extract any supported archive into the cache and report its contents.

    dest_name (optional) names the output subdir; defaults to the archive stem.
    Returns extract_dir, the file count, and the extracted .bin files (the common
    payload of interest for IFWI packages).
    """
    if dest_name and (("/" in dest_name) or ("\\" in dest_name) or (".." in dest_name)):
        return err(ErrorCode.INVALID_ARGUMENT, "dest_name must be a bare filename",
                   {"param": "dest_name", "expected": "no path separators or .."})
    src = Path(archive_path)
    dest = config.cache_subdir("extracted") / (dest_name or src.name.split(".")[0])

    def _describe(source: str) -> dict:
        files = [p for p in dest.rglob("*") if p.is_file()]
        bins = sorted(str(p) for p in files if p.suffix.lower() == ".bin")
        return ok({"extract_dir": dest.as_posix(), "file_count": len(files), "bins": bins,
                   "source": source})

    if dest.is_dir() and any(dest.rglob("*")):
        return _describe("cache")

    if not src.is_file():
        return err(ErrorCode.EXTRACT_FAILED, "archive not found",
                   {"archive": archive_path, "reason": "not a file"})

    try:
        with lock.acquire(str(dest)):
            if dest.is_dir() and any(dest.rglob("*")):
                return _describe("cache")
            dest.mkdir(parents=True, exist_ok=True)
            failure = extract_into(src, dest)
            if failure:
                return failure
            return _describe("extract")
    except TimeoutError as exc:
        return err(ErrorCode.LOCK_TIMEOUT, "timed out waiting for another extraction of the same archive",
                   {"dest": str(dest), "reason": str(exc)})
