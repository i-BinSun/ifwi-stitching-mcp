# ifwi_mcp/fiv_portal.py
"""FIV Portal REST client. Pure HTTP + JSON — no file I/O, no subprocess."""
import re
from typing import Optional

import requests

from . import config
from .result import ok, err, ErrorCode

PHASES = ("Blue", "Orange", "Purple", "Daily")
_VERSION_RE = re.compile(r"^\.?\d{4}\.\d+\.\d+\.\d+$")


def _get(endpoint: str, params: dict) -> dict:
    base = config.get_base_url()
    if not base:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL not set",
                   {"param": "FIV_BASE_URL", "expected": "http(s) URL"})
    auth = config.get_fiv_auth_header()
    if not auth["ok"]:
        return auth
    url = f"{base}/app/rest/{endpoint}"
    try:
        resp = requests.get(url, params=params,
                            headers={"Authorization": auth["data"]["header_value"]}, timeout=60)
    except requests.RequestException as exc:
        return err(ErrorCode.INTERNAL_ERROR, "fiv network error", {"url": url, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "FIV Portal rejected credentials",
                   {"service": "fiv", "http_status": resp.status_code})
    if not (200 <= resp.status_code < 300):
        return err(ErrorCode.INTERNAL_ERROR, "fiv http error",
                   {"url": url, "http_status": resp.status_code})
    try:
        return ok({"json": resp.json()})
    except ValueError as exc:
        return err(ErrorCode.INTERNAL_ERROR, "fiv returned non-JSON", {"reason": str(exc)})


def _validate_common(project: str, phase: str, version: str) -> Optional[dict]:
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    if phase not in PHASES:
        return err(ErrorCode.INVALID_ARGUMENT, "invalid phase",
                   {"param": "phase", "expected": f"one of {PHASES}"})
    if not _VERSION_RE.match(version or ""):
        return err(ErrorCode.INVALID_ARGUMENT, "invalid version",
                   {"param": "version", "expected": "YYYY.WW.D.NN (leading dot tolerated)"})
    return None


def list_projects() -> dict:
    result = _get("get_project_info/", {})
    if not result["ok"]:
        return result
    projects = [{"id": p.get("id"), "name": p.get("name"), "project_name": p.get("project_name")}
                for p in result["data"]["json"]]
    return ok({"projects": projects})


def resolve_project_id(project: str) -> dict:
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    listing = list_projects()
    if not listing["ok"]:
        return listing
    projects = listing["data"]["projects"]
    needle = project.strip().lower()
    for p in projects:
        if needle in (str(p.get("name", "")).lower(), str(p.get("project_name", "")).lower()):
            return ok({"project_id": p["id"], "name": p["name"]})
    return err(ErrorCode.PROJECT_NOT_FOUND, "no project matched",
               {"candidates": [p["name"] for p in projects]})


def _release_rows(project_id: int, phase: str, version: str, swimlane: Optional[str]) -> dict:
    params = {"project_id": project_id, "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    return _get("get_ifwi_release/", params)


def list_swimlanes(project: str, phase: str, version: str) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    rows_result = _release_rows(pid["data"]["project_id"], phase, version, None)
    if not rows_result["ok"]:
        return rows_result
    rows = rows_result["data"]["json"]
    if not rows:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no release rows",
                   {"project": project, "phase": phase, "version": version})
    swimlanes = sorted({r.get("swimlane_branch") for r in rows if r.get("swimlane_branch")})
    return ok({"swimlanes": swimlanes})


def find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    if not swimlane:
        lanes = list_swimlanes(project, phase, version)
        if not lanes["ok"]:
            return lanes
        candidates = lanes["data"]["swimlanes"]
        if len(candidates) > 1:
            return err(ErrorCode.MULTIPLE_SWIMLANES, "multiple swimlanes; pick one",
                       {"candidates": candidates})
        swimlane = candidates[0] if candidates else None

    params = {"project_id": project_id, "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    pkg = _get("get_ifwi_release_package_info/", params)
    if not pkg["ok"]:
        return pkg
    data = pkg["data"]["json"]
    release_root = data.get("release_root") or ""
    for target in data.get("build_target", []):
        binaries = target.get("binary_list") or []
        if binaries:
            full = binaries[0]["full_binary_name"]
            return ok({
                "project_id": project_id,
                "swimlane_branch": data.get("swimlane_branch"),
                "release_root": release_root,
                "ifwi_url": release_root + (target.get("package_path") or "") + full,
                "full_binary_name": full,
                "build_target": target.get("package_name"),
            })
    return err(ErrorCode.RELEASE_NOT_FOUND, "no IFWI binary in build targets",
               {"project": project, "phase": phase, "version": version})
