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


def _resolve_swimlane_or_multi(project, phase, version, swimlane):
    """Return ok({"swimlane": <str|None>}) or a MULTIPLE_SWIMLANES/error result."""
    if swimlane:
        return ok({"swimlane": swimlane})
    lanes = list_swimlanes(project, phase, version)
    if not lanes["ok"]:
        return lanes
    candidates = lanes["data"]["swimlanes"]
    if len(candidates) > 1:
        return err(ErrorCode.MULTIPLE_SWIMLANES, "multiple swimlanes; pick one",
                   {"candidates": candidates})
    return ok({"swimlane": candidates[0] if candidates else None})


def find_ingredient(project: str, name: str, version: str) -> dict:
    if not project or not name or not version:
        return err(ErrorCode.INVALID_ARGUMENT, "project, name, version are required",
                   {"param": "project|name|version", "expected": "non-empty"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    result = _get("get_ingredient_detail/", {
        "project_id": pid["data"]["project_id"],
        "ingredient_name": name,
        "ingredient_version": version,
    })
    if not result["ok"]:
        return result
    link = result["data"]["json"].get("ingredient_link")
    if not link:
        return err(ErrorCode.INGREDIENT_NOT_FOUND, "no ingredient link",
                   {"candidates": [], "ingredient_name": name, "ingredient_version": version})
    return ok({"ingredient_url": link, "ingredient_name": name, "ingredient_version": version})


def find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    lane = _resolve_swimlane_or_multi(project, phase, version, swimlane)
    if not lane["ok"]:
        return lane
    swimlane = lane["data"]["swimlane"]

    params = {"project_id": pid["data"]["project_id"], "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    pkg = _get("get_ifwi_release_package_info/", params)
    if not pkg["ok"]:
        return pkg
    data = pkg["data"]["json"]
    release_root = data.get("release_root") or ""
    available = []
    for target in data.get("build_target", []):
        name = target.get("package_name") or ""
        available.append(name)
        binaries = target.get("binary_list") or []
        if "stitch" in name.lower() and binaries:
            full = binaries[0]["full_binary_name"]
            return ok({"stitch_url": release_root + (target.get("package_path") or "") + full,
                       "package_name": name, "full_binary_name": full})
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
               {"available_packages": available})


def match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                       phase: str, swimlane: Optional[str] = None) -> dict:
    if not project or not product or not ifwi_version or not flavor:
        return err(ErrorCode.INVALID_ARGUMENT, "project, product, ifwi_version, flavor are required",
                   {"param": "project|product|ifwi_version|flavor", "expected": "non-empty"})
    if phase not in PHASES:
        return err(ErrorCode.INVALID_ARGUMENT, "invalid phase",
                   {"param": "phase", "expected": f"one of {PHASES}"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    # Determine which swimlanes to scan.
    if swimlane:
        lanes_to_scan = [swimlane]
    else:
        # Need a version to enumerate swimlanes; derive from OEM ifwi_version by
        # stripping the leading dot (e.g. ".2026.28.3.01" -> "2026.28.3.01").
        version = ifwi_version.lstrip(".")
        if not _VERSION_RE.match(version):
            return err(ErrorCode.INVALID_ARGUMENT, "OEM ifwi_version not usable as version",
                       {"param": "ifwi_version", "expected": "YYYY.WW.D.NN"})
        lanes = list_swimlanes(project, phase, version)
        if not lanes["ok"]:
            return lanes
        lanes_to_scan = lanes["data"]["swimlanes"] or [None]

    product_l = product.lower()
    flavor_l = flavor.strip("_").lower()
    matches = []  # list of {"package_name","swimlane"}
    version = ifwi_version.lstrip(".")
    for lane in lanes_to_scan:
        params = {"project_id": project_id, "phase": phase, "version": version}
        if lane:
            params["swimlane"] = lane
        pkg = _get("get_ifwi_release_package_info/", params)
        if not pkg["ok"]:
            return pkg
        data = pkg["data"]["json"]
        lane_branch = data.get("swimlane_branch") or lane
        for target in data.get("build_target", []):
            name = (target.get("package_name") or "")
            haystack = name.lower()
            for b in (target.get("binary_list") or []):
                haystack += " " + (b.get("full_binary_name") or "").lower()
            if product_l in haystack and flavor_l in haystack:
                matches.append({"package_name": name, "swimlane": lane_branch})

    if not matches:
        return err(ErrorCode.OEM_MATCH_NONE, "no build matched the OEM info",
                   {"parsed": {"product": product, "ifwi_version": ifwi_version, "flavor": flavor}})
    distinct_lanes = sorted({m["swimlane"] for m in matches if m["swimlane"]})
    if not swimlane and len(distinct_lanes) > 1:
        return err(ErrorCode.MULTIPLE_SWIMLANES, "OEM matched builds in multiple swimlanes",
                   {"candidates": distinct_lanes})
    if len(matches) > 1:
        return err(ErrorCode.OEM_MATCH_AMBIGUOUS, "OEM matched more than one build",
                   {"candidates": matches})
    return ok({"project_id": project_id, "phase": phase,
               "swimlane_branch": matches[0]["swimlane"], "matched_package": matches[0]["package_name"]})
