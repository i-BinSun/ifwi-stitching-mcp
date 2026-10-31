"""JSON contracts for the independent Query and Stitching HTTP operations."""
from . import artifactory, executor, fiv_portal, oem_parser, plan
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


def _custom_ifwi_oem(url: str) -> dict:
    """OEM fields of a caller-supplied IFWI, read without downloading the whole image."""
    head = artifactory.read_range(url, oem_parser.OEM_INFO_START, oem_parser.OEM_INFO_LEN)
    if not head["ok"]:
        return head
    if len(head["data"]["content"]) < oem_parser.OEM_INFO_LEN:
        return err(ErrorCode.OEM_REGION_NOT_FOUND, "file too small to contain the OEM region",
                   {"url": url})
    return oem_parser.parse_oem_region(head["data"]["content"])


def stitching(payload: dict) -> dict:
    """Resolve a release and plan it; execute it unless dry_run is true."""
    resolved = resolve_stitching(payload)
    if not resolved["ok"] or resolved["data"].get("dry_run"):
        return resolved
    result = executor.execute(resolved["data"]["plan"])
    warnings = resolved["data"]["warnings"]
    if warnings and result["ok"] and isinstance(result.get("data"), dict):
        result["data"]["warnings"] = warnings + list(result["data"].get("warnings") or [])
    return result


def resolve_stitching(payload: dict) -> dict:
    """Validate a /stitching payload and build its plan without executing it.

    Returns the dry-run preview when dry_run is true, else {"plan", "plan_id", "warnings"}.
    """
    bad = _common(payload)
    if bad:
        return bad
    if type(payload.get("dry_run", False)) is not bool:
        return err(ErrorCode.INVALID_ARGUMENT, "dry_run must be a boolean",
                   {"param": "dry_run"})
    custom_ifwi = _text(payload, "ifwi_url", False)
    if isinstance(custom_ifwi, dict):
        return custom_ifwi
    if custom_ifwi and not custom_ifwi.lower().startswith(("https://", "http://")):
        return err(ErrorCode.INVALID_ARGUMENT, "ifwi_url must be an http(s) download link",
                   {"param": "ifwi_url"})
    # With ifwi_url, version may come from the IFWI's OEM region instead.
    version = _text(payload, "version", not custom_ifwi)
    if isinstance(version, dict):
        return version
    ingredients = payload.get("ingredients")
    if not isinstance(ingredients, dict) or not ingredients or any(
            not isinstance(name, str) or not name.strip() or
            not isinstance(value, str) or not value.strip()
            for name, value in ingredients.items()):
        return err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a non-empty name-to-version object",
                   {"param": "ingredients"})

    project = fiv_portal.resolve_project_by_id(payload["fiv_project_id"])
    if not project["ok"]:
        return project
    project_name = project["data"]["project"]
    phase = payload["phase"]
    lane = payload.get("swimlane")
    binary = payload.get("binary_name")
    warnings = []

    oem = None
    if custom_ifwi:
        parsed = _custom_ifwi_oem(custom_ifwi)
        if not parsed["ok"] and parsed["error_code"] not in (ErrorCode.OEM_REGION_NOT_FOUND,
                                                             ErrorCode.OEM_PARSE_FAILED):
            return parsed
        if parsed["ok"]:
            oem = parsed["data"]
        else:
            warnings.append(f"ifwi_url has no usable OEM region ({parsed['message']}); "
                            "version, swimlane and binary_name are not inferred")
        oem_version = oem and oem["version"]
        if version and oem_version and version.lstrip(".") != oem_version:
            return err(ErrorCode.INVALID_ARGUMENT, "version conflicts with the IFWI OEM region",
                       {"param": "version", "version": version, "oem_version": oem_version,
                        "oem": oem})
        version = version or oem_version
        if not version:
            return err(ErrorCode.INVALID_ARGUMENT,
                       "version is required: the IFWI OEM region names no FIV release version",
                       {"param": "version", "oem": oem})
        hint = oem and oem["swimlane_hint"]
        if hint and not (lane and fiv_portal.swimlane_matches_hint(lane, hint)):
            hinted = fiv_portal.resolve_swimlane_hint(project_name, phase, version, hint)
            if lane and hinted["ok"]:
                return err(ErrorCode.INVALID_ARGUMENT, "swimlane conflicts with the IFWI OEM region",
                           {"param": "swimlane", "swimlane": lane,
                            "oem_swimlane": hinted["data"]["swimlane"], "oem": oem})
            if lane:
                warnings.append(f"OEM swimlane {hint} matches no release swimlane; using {lane}")
            else:
                if not hinted["ok"]:
                    return hinted
                lane = hinted["data"]["swimlane"]

    # Plan building does not resolve IFWI binary ambiguity. Do so before persisting
    # the plan or submitting a potentially costly stitch task.
    binaries = fiv_portal.list_release_binaries(project_name, phase, version, lane)
    if not binaries["ok"]:
        return binaries
    candidates = [entry["binary_name"] for entry in binaries["data"]["binaries"]]
    if oem and oem["flavor_type"] == "official":
        # Official builds write the release binary name as product + flavor.
        oem_binary = oem["product"] + oem["flavor_value"]
        if oem_binary in candidates:
            if binary and binary != oem_binary:
                return err(ErrorCode.INVALID_ARGUMENT, "binary_name conflicts with the IFWI OEM region",
                           {"param": "binary_name", "binary_name": binary,
                            "oem_binary_name": oem_binary, "oem": oem})
            binary = oem_binary
        else:
            warnings.append(f"OEM binary name {oem_binary} is not in the release report")
    if binary is None and len(candidates) != 1:
        return err(ErrorCode.IFWI_BINARY_AMBIGUOUS, "select binary_name for this release",
                   {"candidates": candidates, "oem": oem})
    binary = binary or candidates[0]
    if binary not in candidates:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "binary_name is not in the release report",
                   {"binary_name": binary, "candidates": candidates})

    lane = lane or binaries["data"].get("swimlane_branch")
    chosen = next(entry for entry in binaries["data"]["binaries"]
                  if entry["binary_name"] == binary)
    # A customized baseline replaces only the release binary download; the release
    # still names the binary (for config selection) and supplies the stitch tool.
    ifwi_url = custom_ifwi or chosen.get("url")
    if not ifwi_url:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "release binary has no download URL",
                   {"binary_name": binary})
    tool = fiv_portal.find_stitch_tool(project_name, phase, version, lane)
    if not tool["ok"]:
        return tool
    ingredient_urls = []
    custom_urls = {custom_ifwi} if custom_ifwi else set()
    for name, value in ingredients.items():
        if value.strip().lower().startswith(("https://", "http://")):
            # A direct download link bypasses the FIV lookup.
            ingredient_urls.append(value.strip())
            custom_urls.add(value.strip())
            continue
        found = fiv_portal.find_ingredient(project_name, name, value)
        if not found["ok"]:
            return found
        ingredient_urls.append(found["data"]["ingredient_url"])

    # Config names inside the archive are unknown without downloading it. This
    # conventional name is only a template hint; execution checks the actual
    # extracted targets before invoking the vendor tool.
    config_ini = f"Config_Stitch_{binary}"
    urls = {"ifwi": ifwi_url, "stitch_tool": tool["data"]["stitch_url"],
            "ingredients": ingredient_urls}

    built = plan.build_plan(project_name, phase,
                            [{"name": name, "version": value} for name, value in ingredients.items()],
                            config_ini, version=version,
                            swimlane=lane, ifwi_binary=binary,
                            fiv_project_id=project["data"]["project_id"],
                            source_urls=urls, custom_urls=custom_urls)
    if not built["ok"]:
        return built
    if payload.get("dry_run"):
        if any(name.strip().lower() == "bios" for name in ingredients):
            warnings.append("BIOS: a BIOS build package is resolved during execution: "
                            "IFWIStitch20.bin MAIN.fd/SECPEI.fd are stitched as BIOSID, otherwise a "
                            "64MB IFWI .bin (not PO_SAFE, preferably not Simics/jc_2s_b) as BIOS")
        return ok({"dry_run": True, "plan_id": built["data"]["plan_id"],
                   "project": project_name, "fiv_project_id": project["data"]["project_id"],
                   "phase": phase, "version": version,
                   "swimlane": lane, "binary_name": binary,
                   "custom_ifwi": bool(custom_ifwi), "oem": oem,
                   "config_ini": config_ini, "ingredients": ingredients,
                   "config_status": "validated against the extracted stitch tool during execution",
                   "source_urls": urls,
                   "command_template": built["data"]["command_line"],
                   "warnings": warnings + built["data"].get("warnings", []),
                   "note": "Preview only: FIV URLs resolved and plan saved; nothing downloaded or executed. "
                       "Execution validates the config and resolves command paths."})
    plan_data = built["data"]["plan"]
    return ok({"plan": plan_data,
               "plan_id": built["data"].get("plan_id") or plan_data.get("plan_id"),
               "warnings": warnings})