"""High-level orchestration: prepare a stitch tool or an ingredient end-to-end.

Composes the pure-HTTP fiv_portal with the I/O modules (downloader, archive,
stitch_runner). This is the only place those layers are chained together.
"""
from pathlib import Path

from . import archive, downloader, fiv_portal, stitch_runner
from .result import ok, err, ErrorCode

_SCAN_CAP = 30
_NEIGHBORS = 3


def _download_and_extract_stitch(stitch_url: str, version: str) -> dict:
    dl = downloader.download(stitch_url, category="stitch")
    if not dl["ok"]:
        return dl
    ext = stitch_runner.extract_stitch_tool(dl["data"]["local_path"])
    if not ext["ok"]:
        return ext
    data = dict(ext["data"])
    data["prepared"] = True
    data["selected_version"] = version
    data.setdefault("warnings", [])
    return ok(data)


def prepare_stitch(project: str, phase: str, version: str = None,
                   swimlane: str = None, search_neighbors: bool = False) -> dict:
    if version:
        found = fiv_portal.find_stitch_tool(project, phase, version, swimlane)
        if found["ok"]:
            return _download_and_extract_stitch(found["data"]["stitch_url"], version)
        if not search_neighbors:
            # Fail fast: let the caller tell the user before paying for a neighbor scan.
            return err(ErrorCode.STITCH_TOOL_NOT_FOUND,
                       f"no stitch tool for version {version}; "
                       "retry with search_neighbors=True to check adjacent versions",
                       {"project": project, "phase": phase, "version": version,
                        "swimlane": swimlane})
        # Caller asked to check adjacent versions -> offer candidates.
        rels = fiv_portal.list_releases(project, phase, swimlane)
        if not rels["ok"]:
            return rels
        versions = [r["version"] for r in rels["data"]["releases"]]
        if version not in versions:
            return err(ErrorCode.RELEASE_NOT_FOUND, "version not found in release list",
                       {"project": project, "phase": phase, "version": version})
        i = versions.index(version)
        neighbors = []
        for j in range(1, _NEIGHBORS + 1):
            for k in (i - j, i + j):          # nearest-first, newer then older
                if 0 <= k < len(versions):
                    neighbors.append(versions[k])
        candidates = []
        for v in neighbors:
            f = fiv_portal.find_stitch_tool(project, phase, v, swimlane)
            if f["ok"]:
                candidates.append({"version": v, "stitch_url": f["data"]["stitch_url"]})
        return ok({"prepared": False, "selected_version": None, "candidates": candidates,
                   "warnings": [f"version {version} has no stitch tool; "
                                f"{len(candidates)} adjacent candidate(s) found"]})

    # No version: scan latest -> oldest, prepare the first with a stitch tool.
    rels = fiv_portal.list_releases(project, phase, swimlane)
    if not rels["ok"]:
        return rels
    versions = [r["version"] for r in rels["data"]["releases"]]
    for v in versions[:_SCAN_CAP]:
        f = fiv_portal.find_stitch_tool(project, phase, v, swimlane)
        if f["ok"]:
            res = _download_and_extract_stitch(f["data"]["stitch_url"], v)
            if res["ok"] and v != versions[0]:
                res["data"]["warnings"].append(
                    f"latest release {versions[0]} had no stitch tool; used {v}")
            return res
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND,
               "no stitch tool in the latest releases scanned",
               {"project": project, "phase": phase, "scanned": len(versions[:_SCAN_CAP])})


def prepare_ingredient(project: str, name: str, version: str) -> dict:
    found = fiv_portal.find_ingredient(project, name, version)
    if not found["ok"]:
        return found  # INGREDIENT_NOT_FOUND passes through; never auto-fall-back
    dl = downloader.download(found["data"]["ingredient_url"], category="ingredients")
    if not dl["ok"]:
        return dl
    ext = archive.extract_archive(dl["data"]["local_path"])
    if not ext["ok"]:
        return ext
    extract_dir = ext["data"]["extract_dir"]
    files = sorted(p.as_posix() for p in Path(extract_dir).rglob("*") if p.is_file())
    return ok({"ingredient_dir": extract_dir, "extracted_files": files,
               "ingredient_url": found["data"]["ingredient_url"],
               "ingredient_name": name, "ingredient_version": version, "warnings": []})


def _pick_report_binary(binaries: list, binary_name: str = None) -> dict:
    """Select one release-report binary, or report the choice back to the caller."""
    if binary_name:
        needle = binary_name.strip().lower()
        for entry in binaries:
            names = [entry.get("binary_name"), entry.get("short_name")]
            if entry.get("url"):
                names.append(entry["url"].rsplit("/", 1)[-1])
            if needle in [str(n).lower() for n in names if n]:
                return ok({"binary": entry})
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "binary not in the release report",
                   {"binary_name": binary_name,
                    "available": [b.get("binary_name") for b in binaries]})
    if len(binaries) == 1:
        return ok({"binary": binaries[0]})
    return err(ErrorCode.IFWI_BINARY_AMBIGUOUS,
               "release report lists several IFWI binaries; pick one with binary_name",
               {"candidates": [b.get("binary_name") for b in binaries]})


def prepare_ifwi(project: str, phase: str, version: str, swimlane: str = None,
                 binary_name: str = None, from_build_targets: bool = False) -> dict:
    """Fetch the base IFWI .bin to stitch onto.

    By default this reads the release report (fiv_list_release_binaries), which links
    straight to a .bin - nothing to extract. Set from_build_targets=True when the user
    says none of the report's binaries are the one they want; that enumerates the
    release's build targets instead, downloading and extracting the .7z package.
    The build-target path is also used automatically when a release has no report.
    """
    if not from_build_targets:
        report = fiv_portal.list_release_binaries(project, phase, version, swimlane)
        if report["ok"]:
            picked = _pick_report_binary(report["data"]["binaries"], binary_name)
            if not picked["ok"]:
                return picked
            entry = picked["data"]["binary"]
            if not entry.get("url"):
                return err(ErrorCode.IFWI_BINARY_NOT_FOUND,
                           "release report entry has no download URL",
                           {"binary_name": entry.get("binary_name")})
            dl = downloader.download(entry["url"], category="ifwi")
            if not dl["ok"]:
                return dl
            return ok({"binary_file": dl["data"]["local_path"],
                       "source": "release_report",
                       "binary_name": entry.get("binary_name"),
                       "ifwi_url": entry["url"],
                       "swimlane_branch": report["data"]["swimlane_branch"],
                       "binaries": [b.get("binary_name") for b in report["data"]["binaries"]],
                       "warnings": []})
        # Only a missing report falls through; real errors (auth, ambiguous lane) surface.
        if report["error_code"] != ErrorCode.RELEASE_NOT_FOUND:
            return report

    found = fiv_portal.find_ifwi(project, phase, version, swimlane)
    if not found["ok"]:
        return found
    dl = downloader.download(found["data"]["ifwi_url"], category="ifwi")
    if not dl["ok"]:
        return dl
    ext = archive.extract_archive(dl["data"]["local_path"])
    if not ext["ok"]:
        return ext
    bins = [Path(p) for p in ext["data"]["bins"]]
    names = sorted(p.name for p in bins)
    if binary_name:
        matches = [p for p in bins if p.name == binary_name]
        if not matches:
            return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "binary not found in IFWI package",
                       {"binary_name": binary_name, "available": names})
        chosen = matches[0]
    elif len(bins) == 1:
        chosen = bins[0]
    elif not bins:
        return err(ErrorCode.IFWI_BINARY_NOT_FOUND, "IFWI package contains no .bin",
                   {"ifwi_url": found["data"]["ifwi_url"]})
    else:
        return err(ErrorCode.IFWI_BINARY_AMBIGUOUS, "IFWI package has several .bin files; "
                                                    "pick one with binary_name",
                   {"candidates": names})
    return ok({"binary_file": str(chosen), "source": "build_targets",
               "ifwi_dir": ext["data"]["extract_dir"],
               "ifwi_url": found["data"]["ifwi_url"], "binaries": names,
               "swimlane_branch": found["data"]["swimlane_branch"], "warnings": []})
