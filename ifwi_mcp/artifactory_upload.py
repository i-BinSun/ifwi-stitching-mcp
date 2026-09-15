"""Upload remote-stitch deliverables to Artifactory.

Mirrors the Bearer-auth GET pattern already used by downloader.py's _fetch_url,
just in the PUT direction.
"""
from pathlib import Path

import requests

from . import config
from .result import ok, err, ErrorCode

_UPLOAD_TIMEOUT = 300


def upload_file(local_path: str, dest_name: str) -> dict:
    src = Path(local_path)
    if not src.is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "local_path does not exist",
                   {"param": "local_path", "expected": "existing file"})

    base_result = config.get_artifactory_upload_base()
    if not base_result["ok"]:
        return base_result
    token_result = config.get_artifactory_token()
    if not token_result["ok"]:
        return token_result

    url = f"{base_result['data']['base_url']}/{dest_name}"
    headers = {"Authorization": f"Bearer {token_result['data']['token']}"}
    try:
        with src.open("rb") as fh:
            resp = requests.put(url, data=fh, headers=headers,
                                timeout=_UPLOAD_TIMEOUT, verify=False)
    except requests.RequestException as exc:
        return err(ErrorCode.UPLOAD_FAILED, "network error during upload",
                   {"url": url, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                   {"service": "artifactory", "http_status": resp.status_code})
    if not (200 <= resp.status_code < 300):
        return err(ErrorCode.UPLOAD_FAILED, "upload returned error status",
                   {"url": url, "http_status": resp.status_code, "body": resp.text[:2000]})
    return ok({"url": url, "name": dest_name})
