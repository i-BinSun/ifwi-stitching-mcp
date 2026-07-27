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
                   swimlane: str = None) -> dict:
    if version:
        found = fiv_portal.find_stitch_tool(project, phase, version, swimlane)
        if found["ok"]:
            return _download_and_extract_stitch(found["data"]["stitch_url"], version)
        # No stitch tool at the requested version -> offer adjacent candidates.
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
    files = sorted(str(p) for p in Path(extract_dir).rglob("*") if p.is_file())
    return ok({"ingredient_dir": extract_dir, "extracted_files": files,
               "ingredient_url": found["data"]["ingredient_url"],
               "ingredient_name": name, "ingredient_version": version, "warnings": []})
