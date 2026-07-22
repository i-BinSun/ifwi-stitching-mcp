"""Turn Artifactory URLs or local paths into files in the local cache."""
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from . import config
from .result import ok, err, ErrorCode

_CATEGORIES = ("ifwi", "ingredients", "stitch")


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in ("http", "https")


def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    if not url_or_path:
        return err(ErrorCode.INVALID_ARGUMENT, "url_or_path is required",
                   {"param": "url_or_path", "expected": "non-empty URL or path"})
    if dest_name and (("/" in dest_name) or ("\\" in dest_name) or (".." in dest_name)):
        return err(ErrorCode.INVALID_ARGUMENT, "dest_name must be a bare filename",
                   {"param": "dest_name", "expected": "no path separators or .."})

    name = dest_name or Path(urlparse(url_or_path).path if _is_url(url_or_path) else url_or_path).name
    target = config.cache_subdir(category) / name

    if _is_url(url_or_path):
        token_result = config.get_artifactory_token()
        if not token_result["ok"]:
            return token_result
        headers = {"Authorization": f"Bearer {token_result['data']['token']}"}
        try:
            resp = requests.get(url_or_path, headers=headers, stream=True, timeout=120)
        except requests.RequestException as exc:
            return err(ErrorCode.DOWNLOAD_FAILED, "network error during download",
                       {"url": url_or_path, "reason": str(exc)})
        if resp.status_code in (401, 403):
            return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                       {"service": "artifactory", "http_status": resp.status_code})
        if not (200 <= resp.status_code < 300):
            return err(ErrorCode.DOWNLOAD_FAILED, "download returned error status",
                       {"url": url_or_path, "http_status": resp.status_code})
        written = 0
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        return ok({"local_path": str(target), "source": "download", "bytes": written})

    src = Path(url_or_path)
    if not src.is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "local path does not exist",
                   {"param": "url_or_path", "expected": "existing file or http(s) URL"})
    shutil.copyfile(src, target)
    return ok({"local_path": str(target), "source": "copy", "bytes": target.stat().st_size})


def list_local_files() -> dict:
    files = []
    for category in _CATEGORIES:
        base = config.cache_subdir(category)
        for path in sorted(base.glob("*")):
            if path.is_file():
                files.append({"path": str(path), "category": category, "bytes": path.stat().st_size})
    return ok({"files": files})
