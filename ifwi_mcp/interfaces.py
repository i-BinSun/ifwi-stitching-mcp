"""JSON contracts for the independent Query and Stitching HTTP operations."""
from . import executor, fiv_portal, plan
from .result import ErrorCode, err, ok


def _text(payload: dict, key: str, required: bool = True):
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        return err(ErrorCode.INVALID_ARGUMENT, f"{key} must be a non-empty string",
                   {"param": key})
    return value.strip()


def _common(payload: dict):
    if not isinstance(payload, dict):
        return err(ErrorCode.INVALID_ARGUMENT, "payload must be a JSON object")
    values = [_text(payload, key) for key in ("fiv_project_id", "phase")]
    values.append(_text(payload, "swimlane", False))
    values.append(_text(payload, "binary_name", False))
    return next((value for value in values if isinstance(value, dict)), None)


def query(payload: dict) -> dict:
    """Query one release's ingredients or compare two releases by ingredient name."""
    bad = _common(payload)
    if bad:
        return bad
    versions = payload.get("versions")
    if not isinstance(versions, list) or len(versions) not in (1, 2) or any(
            not isinstance(v, str) or not v.strip() for v in versions):
        return err(ErrorCode.INVALID_ARGUMENT, "versions must contain one or two version strings",
                   {"param": "versions"})
    if len(versions) == 2 and versions[0] == versions[1]:
        return err(ErrorCode.INVALID_ARGUMENT, "comparison versions must differ",
                   {"param": "versions"})

    project = fiv_portal.resolve_project_by_id(payload["fiv_project_id"])
    if not project["ok"]:
        return project

    releases = []
    for version in versions:
        result = fiv_portal.get_binary_ingredients(
            project["data"]["project"], payload["phase"], version.strip(),
            payload.get("binary_name"), payload.get("swimlane"))
        if not result["ok"]:
            result["detail"]["version"] = version
            return result
        releases.append(result["data"])
    if len(releases) == 1:
        return ok({"release": releases[0]})

    left, right = releases
    a = {entry["name"]: entry.get("version") for entry in left["ingredients"]}
    b = {entry["name"]: entry.get("version") for entry in right["ingredients"]}
    differences = [{"name": name, "from_version": a.get(name), "to_version": b.get(name)}
                   for name in sorted(a.keys() | b.keys()) if a.get(name) != b.get(name)]
    return ok({"versions": versions, "binaries": [left["binary_name"], right["binary_name"]],
               "differences": differences, "count": len(differences)})


def stitching(payload: dict) -> dict:
    """Resolve a release and plan it; execute remotely unless dry_run is true."""
    bad = _common(payload)
    if bad:
        return bad
    if type(payload.get("dry_run", False)) is not bool:
        return err(ErrorCode.INVALID_ARGUMENT, "dry_run must be a boolean",
                   {"param": "dry_run"})
    value = _text(payload, "version")
    if isinstance(value, dict):
        return value
    ingredients = payload.get("ingredients")
    if not isinstance(ingredients, dict) or not ingredients or any(
            not isinstance(name, str) or not name.strip() or
            not isinstance(version, str) or not version.strip()
            for name, version in ingredients.items()):
        return err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a non-empty name-to-version object",
                   {"param": "ingredients"})

    project = fiv_portal.resolve_project_by_id(payload["fiv_project_id"])
    if not project["ok"]:
        return project
    project_name = project["data"]["project"]

    # Plan building does not resolve IFWI binary ambiguity. Do so before persisting
    # the plan or submitting a potentially costly stitch task.
    binaries = fiv_portal.list_release_binaries(project_name, payload["phase"],
                                               payload["version"], payload.get("swimlane"))
    if not binaries["ok"]:
        return binaries
    candidates = [entry["binary_name"] for entry in binaries["data"]["binaries"]]
    binary = payload.get("binary_name")
    if binary is None and len(candidates) != 1:
        return err(ErrorCode.IFWI_BINARY_AMBIGUOUS, "select binary_name for this release",
                   {"candidates": candidates})
    binary = binary or candidates[0]
    if binary not in candidates:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "binary_name is not in the release report",
                   {"binary_name": binary, "candidates": candidates})

    lane = payload.get("swimlane") or binaries["data"].get("swimlane_branch")
    chosen = next(entry for entry in binaries["data"]["binaries"]
                  if entry["binary_name"] == binary)
    ifwi_url = chosen.get("url")
    if not ifwi_url:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "release binary has no download URL",
                   {"binary_name": binary})
    tool = fiv_portal.find_stitch_tool(project_name, payload["phase"], payload["version"], lane)
    if not tool["ok"]:
        return tool
    ingredient_urls = []
    for name, version in ingredients.items():
        found = fiv_portal.find_ingredient(project_name, name, version)
        if not found["ok"]:
            return found
        ingredient_urls.append(found["data"]["ingredient_url"])

    # Config names inside the archive are unknown without downloading it. This
    # conventional name is only a template hint; the worker checks the actual
    # extracted targets before invoking the vendor tool.
    config_ini = f"Config_Stitch_{binary}"
    urls = {"ifwi": ifwi_url, "stitch_tool": tool["data"]["stitch_url"],
            "ingredients": ingredient_urls}

    built = plan.build_plan(project_name, payload["phase"],
                            [{"name": name, "version": version} for name, version in ingredients.items()],
                            config_ini, version=payload["version"],
                            swimlane=lane, ifwi_binary=binary,
                            fiv_project_id=project["data"]["project_id"],
                            source_urls=urls)
    if not built["ok"]:
        return built
    if payload.get("dry_run"):
        return ok({"dry_run": True, "plan_id": built["data"]["plan_id"],
                   "project": project_name, "fiv_project_id": project["data"]["project_id"],
                   "phase": payload["phase"], "version": payload["version"],
                   "swimlane": lane, "binary_name": binary,
                   "config_ini": config_ini, "ingredients": ingredients,
                   "config_status": "worker will validate config from the extracted stitch tool",
                   "source_urls": urls,
                   "command_template": built["data"]["command_line"],
                   "warnings": built["data"].get("warnings", []),
                   "note": "Preview only: FIV URLs resolved and plan saved; no files downloaded or remote job submitted. "
                       "The worker validates config and resolves command paths during execution."})
    return executor.execute(built["data"]["plan"])