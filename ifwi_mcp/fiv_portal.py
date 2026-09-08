# ifwi_mcp/fiv_portal.py
"""FIV Portal REST client. Pure HTTP + JSON — no file I/O, no subprocess."""
import json
import re
from typing import Optional

import requests

from . import config
from .result import ok, err, ErrorCode

PHASES = ("Blue", "Orange", "Purple", "Daily")
_VERSION_RE = re.compile(r"^\.?\d{4}\.\d+\.\d+\.\d+$")


def _get(endpoint: str, params: dict, prefix: str = "app/rest") -> dict:
    base = config.get_base_url()
    if not base:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL not set",
                   {"param": "FIV_BASE_URL", "expected": "http(s) URL"})
    auth = config.get_fiv_auth_header()
    if not auth["ok"]:
        return auth
    url = f"{base}/{prefix}/{endpoint}"
    try:
        resp = requests.get(url, params=params,
                            headers={"Authorization": auth["data"]["header_value"]}, timeout=60,
                            verify=False)
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


def classify_project_type(ifwi_type: Optional[str], ifwi_sub_type: Optional[str]) -> dict:
    """Map FIV (ifwi_type, ifwi_sub_type) to a coarse project category.

    Platform  -> IFWI project (domain: server | client | graphic)
    Graphic   -> IFWI project, graphic domain (graphic is its own ifwi_type in FIV)
    Silicon   -> BIOS | UP (Unified Patch)
    """
    t = (ifwi_type or "").strip().lower()
    s = (ifwi_sub_type or "").strip().lower()
    if t == "platform":
        domain = s if s in ("server", "client", "graphic") else (s or "unknown")
        return {"category": "IFWI", "domain": domain, "is_ifwi": True}
    if t == "graphic":
        return {"category": "IFWI", "domain": "graphic", "is_ifwi": True}
    if t == "silicon":
        if s in ("unified_patch", "up"):
            return {"category": "UP", "domain": None, "is_ifwi": False}
        if s == "bios":
            return {"category": "BIOS", "domain": None, "is_ifwi": False}
        return {"category": "SILICON", "domain": s or None, "is_ifwi": False}
    return {"category": "UNKNOWN", "domain": s or None, "is_ifwi": False}


def get_project(project: str) -> dict:
    """Resolve a project and return its FIV type fields plus a coarse category."""
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]
    result = _get("get_project/", {"project_id": project_id})
    if not result["ok"]:
        return result
    detail = result["data"]["json"] or {}
    ifwi_type = detail.get("ifwi_type")
    ifwi_sub_type = detail.get("ifwi_sub_type")
    return ok({
        "project_id": project_id,
        "name": detail.get("name") or pid["data"]["name"],
        "project_name": detail.get("project_name"),
        "ifwi_type": ifwi_type,
        "ifwi_sub_type": ifwi_sub_type,
        **classify_project_type(ifwi_type, ifwi_sub_type),
    })


def resolve_project_id(project: str) -> dict:
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    listing = list_projects()
    if not listing["ok"]:
        return listing
    projects = listing["data"]["projects"]
    needle = project.strip().lower()
    matches = [p for p in projects
               if needle in (str(p.get("name", "")).lower(), str(p.get("project_name", "")).lower())]
    # dedupe by project id (the same project can appear twice if name == project_name)
    by_id = {p["id"]: p for p in matches}
    matches = list(by_id.values())
    if len(matches) > 1:
        return err(ErrorCode.PROJECT_AMBIGUOUS, "multiple projects match this name; ask the user which one",
                   {"candidates": [{"id": p["id"], "name": p["name"], "project_name": p.get("project_name")}
                                    for p in matches]})
    if matches:
        p = matches[0]
        return ok({"project_id": p["id"], "name": p["name"]})
    return err(ErrorCode.PROJECT_NOT_FOUND, "no project matched",
               {"candidates": [p["name"] for p in projects]})


def _release_rows(project_id: int, phase: str, version: str, swimlane: Optional[str]) -> dict:
    params = {"project_id": project_id, "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    return _get("get_ifwi_release/", params)


def _release_swimlanes_for_version(project: str, phase: str, version: str) -> dict:
    """Internal: swimlanes that a SPECIFIC version was released in (from get_ifwi_release).

    Used to auto-resolve a swimlane for find_ifwi/find_stitch_tool/match_build_by_oem
    when the caller does not name one. This is version-scoped and only sees lanes that
    actually have a release for that version — NOT the project's full swimlane list
    (for that, use list_swimlanes, which reads the build-config metadata).
    """
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
    if not isinstance(rows, list) or not rows:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no release rows",
                   {"project": project, "phase": phase, "version": version})
    swimlanes = sorted({r.get("swimlane_branch") for r in rows if r.get("swimlane_branch")})
    return ok({"swimlanes": swimlanes})


def list_swimlanes(project: str, phase: Optional[str] = None) -> dict:
    """List a project's configured build types / swimlanes from build metadata.

    Reads meta_data/build (the authoritative build-config table), keeping only
    visible builds. Optionally filter to those whose phase list includes `phase`.
    Returns each build's name, swimlane branch, phases, stepping/SKU.
    """
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    result = _get("build/", {"proj_id": pid["data"]["project_id"]}, prefix="meta_data")
    if not result["ok"]:
        return result
    payload = result["data"]["json"]
    builds = (payload or {}).get("data", {}).get("list", []) if isinstance(payload, dict) else []
    swimlanes = []
    for b in builds:
        if b.get("visible") != 1:
            continue
        phases = [p.strip() for p in (b.get("phase") or "").split(",") if p.strip()]
        if phase and phase not in phases:
            continue
        meta = {}
        raw = b.get("meta_data_info")
        if isinstance(raw, str) and raw:
            try:
                meta = json.loads(raw)
            except ValueError:
                meta = {}
        swimlanes.append({
            "name": b.get("name"),
            "swimlane_branch": b.get("build_branch"),
            "phases": phases,
            "stepping": meta.get("stepping"),
            "sku": meta.get("SKU"),
        })
    if not swimlanes:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no visible builds for project",
                   {"project": project, "phase": phase})
    return ok({"project_id": pid["data"]["project_id"], "count": len(swimlanes),
               "swimlanes": swimlanes})


def list_releases(project: str, phase: str, swimlane: Optional[str] = None) -> dict:
    """List releases for a project+phase, newest first. No version required."""
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    if phase not in PHASES:
        return err(ErrorCode.INVALID_ARGUMENT, "invalid phase",
                   {"param": "phase", "expected": f"one of {PHASES}"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    # Omit version to enumerate all releases in the phase. The endpoint silently caps
    # at 20 rows unless a large `limit` is passed. The `swimlane` param IS honored by
    # the server; when omitted the server returns only the project's default lane, so
    # to list a non-default lane (e.g. an imh2 branch) the caller must name it.
    params = {"project_id": pid["data"]["project_id"], "phase": phase,
              "version": "", "limit": 1000000}
    if swimlane:
        params["swimlane"] = swimlane
    result = _get("get_ifwi_release/", params)
    if not result["ok"]:
        return result
    rows = result["data"]["json"]
    # FIV returns an error dict (not a list) when no data matches.
    if not isinstance(rows, list) or not rows:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no releases for project/phase",
                   {"project": project, "phase": phase, "swimlane": swimlane})
    releases = [{
        "version": r.get("version"),
        "phase": r.get("phase"),
        "swimlane_branch": r.get("swimlane_branch"),
        "status": r.get("status"),
        "publish_time": r.get("publish_time"),
    } for r in rows]
    # Newest first by publish_time (ISO strings sort correctly); None sinks to bottom.
    releases.sort(key=lambda x: x.get("publish_time") or "", reverse=True)
    return ok({"project_id": pid["data"]["project_id"], "phase": phase,
               "count": len(releases), "latest": releases[0], "releases": releases})


def find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    if not swimlane:
        lanes = _release_swimlanes_for_version(project, phase, version)
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
            # package_path is the .7z package file; release_root+package_path is the
            # downloadable URL. full_binary_name may be a comma-separated list of the
            # .bin files inside it — expose them as a list, don't concatenate onto url.
            files = [fn.strip() for fn in (binaries[0].get("full_binary_name") or "").split(",")
                     if fn.strip()]
            return ok({
                "project_id": project_id,
                "swimlane_branch": data.get("swimlane_branch"),
                "release_root": release_root,
                "ifwi_url": release_root + (target.get("package_path") or ""),
                "binaries": files,
                "build_target": target.get("package_name"),
            })
    return err(ErrorCode.RELEASE_NOT_FOUND, "no IFWI binary in build targets",
               {"project": project, "phase": phase, "version": version})


def list_ifwi_binaries(project: str, phase: str, version: str,
                       swimlane: Optional[str] = None) -> dict:
    """List every IFWI binary across all build targets of a release.

    This is the *fallback* listing: it enumerates every build target and the .7z
    package each one ships. Prefer list_release_binaries, which returns the curated
    IFWI binaries of the release report; only fall back here when the user says none
    of those are the one they want (or no report exists).
    """
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    if not swimlane:
        lanes = _release_swimlanes_for_version(project, phase, version)
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
    targets = []
    total = 0
    for target in data.get("build_target", []):
        package_path = target.get("package_path") or ""
        # full_binary_name may be a comma-separated list of .bin files inside the pkg.
        files = []
        for entry in (target.get("binary_list") or []):
            for fn in (entry.get("full_binary_name") or "").split(","):
                fn = fn.strip()
                if fn:
                    files.append(fn)
        total += len(files)
        targets.append({
            "build_target": target.get("package_name"),
            "package_url": release_root + package_path,
            "binaries": files,
        })
    if not targets:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no build targets in release",
                   {"project": project, "phase": phase, "version": version})
    return ok({"project_id": project_id, "phase": phase, "version": version,
               "swimlane_branch": data.get("swimlane_branch"),
               "release_root": release_root,
               "target_count": len(targets), "binary_count": total,
               "targets": targets})


def _resolve_swimlane_or_multi(project, phase, version, swimlane):
    """Return ok({"swimlane": <str|None>}) or a MULTIPLE_SWIMLANES/error result."""
    if swimlane:
        return ok({"swimlane": swimlane})
    lanes = _release_swimlanes_for_version(project, phase, version)
    if not lanes["ok"]:
        return lanes
    candidates = lanes["data"]["swimlanes"]
    if len(candidates) > 1:
        return err(ErrorCode.MULTIPLE_SWIMLANES, "multiple swimlanes; pick one",
                   {"candidates": candidates})
    return ok({"swimlane": candidates[0] if candidates else None})


def list_release_binaries(project: str, phase: str, version: str,
                          swimlane: Optional[str] = None) -> dict:
    """List the IFWI binaries published in a release's report (get_release_binary).

    This is the preferred way to pick an IFWI: the report holds the curated, validated
    binaries for the release, and each entry links straight to a .bin - no .7z package
    to download and extract. When no report exists the caller should fall back to
    list_ifwi_binaries, which enumerates every build target instead.
    """
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
    result = _get("get_release_binary/", params)
    if not result["ok"]:
        return result
    data = result["data"]["json"]
    # A missing report is reported as HTTP 200 with an error envelope, not a 404.
    if not isinstance(data, dict) or data.get("code") == 1 or "binaries" not in data:
        reason = (data or {}).get("error") if isinstance(data, dict) else "unexpected payload"
        return err(ErrorCode.RELEASE_NOT_FOUND, "no release report for this version",
                   {"project": project, "phase": phase, "version": version,
                    "swimlane": swimlane, "reason": reason})

    binaries = []
    for entry in data.get("binaries") or []:
        urls = [u for u in (entry.get("binary_list") or []) if u]
        binaries.append({
            "binary_id": entry.get("binary_id"),
            "binary_name": entry.get("binary_name"),
            "short_name": entry.get("short_name"),
            "validated": entry.get("validated"),
            "url": urls[0] if urls else None,
            "urls": urls,
        })
    if not binaries:
        return err(ErrorCode.RELEASE_NOT_FOUND, "release report lists no IFWI binaries",
                   {"project": project, "phase": phase, "version": version})
    return ok({"project_id": pid["data"]["project_id"], "phase": phase, "version": version,
               "swimlane_branch": data.get("swimlane_branch"),
               "daily_hash": data.get("daily_hash"),
               "count": len(binaries), "binaries": binaries, "source": "release_report"})


def get_binary_ingredients(project: str, phase: str, version: str, binary_name: Optional[str] = None,
                           swimlane: Optional[str] = None, package_name: Optional[str] = None) -> dict:
    """Look up the ingredients baked into one specific release binary (get_release_binary_info).

    binary_name must match a build's ifwi_target_name exactly (the `binary_name` field from
    list_release_binaries / list_ifwi_binaries). Returns each ingredient's name, version and
    other properties - e.g. to read off which MMC version shipped in a given .bin.

    binary_name may be omitted only when the release report lists exactly one binary; when
    it lists several, this refuses to guess and returns IFWI_BINARY_AMBIGUOUS with every
    candidate name instead - the caller must show them to the user and let them pick before
    asking anything about ingredients (never silently pick one, e.g. the first result).
    """
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid

    if not binary_name:
        report = list_release_binaries(project, phase, version, swimlane)
        if not report["ok"]:
            return report
        binaries = report["data"]["binaries"]
        if len(binaries) > 1:
            return err(ErrorCode.IFWI_BINARY_AMBIGUOUS,
                       "release report lists several IFWI binaries; show them to the user "
                       "and pick one with binary_name before asking about ingredients",
                       {"candidates": [b.get("binary_name") for b in binaries]})
        binary_name = binaries[0]["binary_name"]
        swimlane = swimlane or report["data"]["swimlane_branch"]

    lane = _resolve_swimlane_or_multi(project, phase, version, swimlane)
    if not lane["ok"]:
        return lane
    swimlane = lane["data"]["swimlane"]

    params = {"project_id": pid["data"]["project_id"], "phase": phase, "version": version,
              "binary_name": binary_name}
    if swimlane:
        params["swimlane"] = swimlane
    if package_name:
        params["package_name"] = package_name
    result = _get("get_release_binary_info/", params)
    if not result["ok"]:
        return result
    data = result["data"]["json"]
    # Same error envelope shape as get_release_binary/: HTTP 200 with {"code": 1, ...}.
    if not isinstance(data, dict) or data.get("code") == 1:
        reason = (data or {}).get("error") if isinstance(data, dict) else "unexpected payload"
        return err(ErrorCode.RELEASE_NOT_FOUND, "no release report for this version",
                   {"project": project, "phase": phase, "version": version,
                    "swimlane": swimlane, "reason": reason})
    ingredients = data.get("ingredient_list")
    # binary_name matched nothing: the endpoint returns {} (no ingredient_list at all).
    if not ingredients:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "binary_name matched no build in this release",
                   {"project": project, "phase": phase, "version": version,
                    "binary_name": binary_name, "swimlane": swimlane})
    return ok({"project_id": pid["data"]["project_id"], "phase": phase, "version": version,
               "binary_name": binary_name, "swimlane_branch": data.get("swimlane_branch"),
               "count": len(ingredients), "ingredients": ingredients})


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


def _version_sort_key(v: str):
    parts = re.split(r"[.\-]", v)
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def list_ingredient_versions(project: str, name: str, status: str = "ALL",
                             include_stitch_ingredient: int = 0) -> dict:
    """List every version FIV has ever recorded for a named ingredient.

    Hits get_ingredient_version/ directly - the authoritative source - instead of
    inferring versions by scanning historical release ingredient lists (which misses
    any version never baked into a release binary, and can't be trusted to be complete).
    status defaults to ALL (every VCS check-in status); the endpoint's own default is
    'accept' only, which silently hides Pending/Reject/Revert/Close/Obsoleted/Skipped
    entries. Note: 'ALL' must be this exact uppercase string - the server compares it
    case-sensitively and anything else (including lowercase 'all') is treated as a
    literal single status to filter by, matching nothing.
    """
    if not project or not name:
        return err(ErrorCode.INVALID_ARGUMENT, "project, name are required",
                   {"param": "project|name", "expected": "non-empty"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    result = _get("get_ingredient_version/", {
        "project_id": pid["data"]["project_id"],
        "ingredient_name": name,
        "status": status,
        "include_stitch_ingredient": include_stitch_ingredient,
    })
    if not result["ok"]:
        if result.get("error_code") == ErrorCode.INTERNAL_ERROR and \
                (result.get("detail") or {}).get("http_status") == 404:
            return err(ErrorCode.INGREDIENT_NOT_FOUND, "no data for ingredient",
                       {"ingredient_name": name, "project": project})
        return result
    payload = result["data"]["json"]
    if not isinstance(payload, list):
        return err(ErrorCode.INGREDIENT_NOT_FOUND, "unexpected response shape",
                   {"ingredient_name": name, "project": project, "response": payload})
    versions = sorted({v for v in payload if v}, key=_version_sort_key)
    return ok({"ingredient_name": name, "status": status, "count": len(versions),
               "versions": versions})


def _artifactory_storage_url(url: str) -> Optional[str]:
    """Convert an Artifactory repo-browse URL into its `api/storage` JSON-listing URL."""
    marker = "/artifactory/"
    idx = url.find(marker)
    if idx == -1:
        return None
    prefix = url[:idx + len(marker)]
    rest = url[idx + len(marker):]
    if rest.startswith("api/storage/"):
        return url
    return f"{prefix}api/storage/{rest}"


def _artifactory_list(url: str) -> dict:
    """List the immediate children of an Artifactory folder via its Storage API."""
    storage_url = _artifactory_storage_url(url)
    if not storage_url:
        return err(ErrorCode.INTERNAL_ERROR, "not an artifactory URL", {"url": url})
    token = config.get_artifactory_token()
    if not token["ok"]:
        return token
    try:
        resp = requests.get(storage_url,
                            headers={"Authorization": f"Bearer {token['data']['token']}"},
                            timeout=60, verify=False)
    except requests.RequestException as exc:
        return err(ErrorCode.INTERNAL_ERROR, "artifactory network error",
                   {"url": storage_url, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                   {"service": "artifactory", "http_status": resp.status_code})
    if not (200 <= resp.status_code < 300):
        return err(ErrorCode.INTERNAL_ERROR, "artifactory http error",
                   {"url": storage_url, "http_status": resp.status_code})
    try:
        payload = resp.json()
    except ValueError as exc:
        return err(ErrorCode.INTERNAL_ERROR, "artifactory returned non-JSON", {"reason": str(exc)})
    children = payload.get("children") or []
    folders = [c["uri"].lstrip("/") for c in children if c.get("folder")]
    files = [c["uri"].lstrip("/") for c in children if not c.get("folder")]
    return ok({"folders": folders, "files": files})


def _browse_release_root_for_stitch(release_root: str) -> Optional[dict]:
    """Fallback for stitch tools that don't self-report in build_target metadata.

    Some stitch-tool packages exist on Artifactory but are never listed by
    get_ifwi_release_package_info. Browse release_root directly: one level down is a
    folder per target name, one level below that is the package file itself. Returns a
    find_stitch_tool-shaped ok() result, or None if the fallback found nothing (never
    raises — this is best-effort and must not mask the original not-found error).
    """
    if not release_root:
        return None
    top = _artifactory_list(release_root)
    if not top["ok"]:
        return None
    for folder in top["data"]["folders"]:
        if "stitch" not in folder.lower():
            continue
        sub = _artifactory_list(f"{release_root}{folder}/")
        if not sub["ok"] or not sub["data"]["files"]:
            continue
        return ok({"stitch_url": f"{release_root}{folder}/{sub['data']['files'][0]}",
                   "package_name": folder, "binaries": [], "source": "artifactory_browse"})
    return None


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
        # Stitch-tool packages are collateral and carry no .bin, so do NOT require a
        # non-empty binary_list. Match by package name; list any binaries if present.
        if "stitch" in name.lower():
            files = []
            for entry in (target.get("binary_list") or []):
                for fn in (entry.get("full_binary_name") or "").split(","):
                    fn = fn.strip()
                    if fn:
                        files.append(fn)
            return ok({"stitch_url": release_root + (target.get("package_path") or ""),
                       "package_name": name, "binaries": files, "source": "metadata"})
    fallback = _browse_release_root_for_stitch(release_root)
    if fallback:
        return fallback
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
               {"available_packages": available, "release_root": release_root})


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
        lanes = _release_swimlanes_for_version(project, phase, version)
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
