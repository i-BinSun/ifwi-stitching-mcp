"""Turn Artifactory URLs or local paths into files in the local cache."""
import os
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from . import config, lock
from .result import ok, err, ErrorCode

_CATEGORIES = ("ifwi", "ingredients", "stitch")


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in ("http", "https")


def _cached(target: Path) -> Optional[dict]:
    if target.is_file() and target.stat().st_size > 0:
        return ok({"local_path": str(target), "source": "cache", "bytes": target.stat().st_size})
    return None


def _fetch_url(url_or_path: str, target: Path) -> dict:
    token_result = config.get_artifactory_token()
    if not token_result["ok"]:
        return token_result
    headers = {"Authorization": f"Bearer {token_result['data']['token']}"}
    try:
        resp = requests.get(url_or_path, headers=headers, stream=True, timeout=120,
                             verify=False)
    except requests.RequestException as exc:
        return err(ErrorCode.DOWNLOAD_FAILED, "network error during download",
                   {"url": url_or_path, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                   {"service": "artifactory", "http_status": resp.status_code})
    if not (200 <= resp.status_code < 300):
        return err(ErrorCode.DOWNLOAD_FAILED, "download returned error status",
                   {"url": url_or_path, "http_status": resp.status_code})
    part = target.with_name(target.name + ".part")
    written = 0
    try:
        with open(part, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return ok({"local_path": str(target), "source": "download", "bytes": written})


def _copy_local(src: Path, target: Path) -> dict:
    part = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(src, part)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return ok({"local_path": str(target), "source": "copy", "bytes": target.stat().st_size})


def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    if not url_or_path:
        return err(ErrorCode.INVALID_ARGUMENT, "url_or_path is required",
                   {"param": "url_or_path", "expected": "non-empty URL or path"})
    if dest_name and (("/" in dest_name) or ("\\" in dest_name) or (".." in dest_name)):
        return err(ErrorCode.INVALID_ARGUMENT, "dest_name must be a bare filename",
                   {"param": "dest_name", "expected": "no path separators or .."})

    name = dest_name or Path(urlparse(url_or_path).path if _is_url(url_or_path) else url_or_path).name
    target = config.cache_subdir(category) / name

    cached = _cached(target)
    if cached:
        return cached

    is_url = _is_url(url_or_path)
    if not is_url and not Path(url_or_path).is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "local path does not exist",
                   {"param": "url_or_path", "expected": "existing file or http(s) URL"})

    try:
        with lock.acquire(str(target)):
            cached = _cached(target)
            if cached:
                return cached
            if is_url:
                return _fetch_url(url_or_path, target)
            return _copy_local(Path(url_or_path), target)
    except TimeoutError as exc:
        return err(ErrorCode.LOCK_TIMEOUT, "timed out waiting for another download of the same file",
                   {"target": str(target), "reason": str(exc)})


def list_local_files() -> dict:
    files = []
    for category in _CATEGORIES:
        base = config.cache_subdir(category)
        for path in sorted(base.glob("*")):
            if path.is_file():
                files.append({"path": path.as_posix(), "category": category, "bytes": path.stat().st_size})
    return ok({"files": files})
