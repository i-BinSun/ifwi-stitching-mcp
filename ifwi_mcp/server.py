"""FastMCP server: thin tool wrappers over the ifwi_mcp modules."""
import functools
import json
import sys
from typing import Optional

from fastmcp import FastMCP

from . import (archive, config, deliverables, downloader, executor, fiv_portal,
               oem_parser, plan as plan_mod, prepare, stitch_runner)
from .result import err, ErrorCode

mcp = FastMCP("ifwi-stitching-mcp")


def _guard(fn):
    """Map any escaped exception to a unified INTERNAL_ERROR result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return err(ErrorCode.INTERNAL_ERROR, "unexpected error", {"exception": str(exc)})
    return wrapper


@_guard
def fiv_list_projects() -> dict:
    """List every FIV Portal project with its id, name and project_name."""
    return fiv_portal.list_projects()


@_guard
def fiv_get_project(project: str) -> dict:
    """Get one project's detail and its category (IFWI / UP / BIOS) from its FIV type fields."""
    return fiv_portal.get_project(project)


@_guard
def fiv_list_swimlanes(project: str, phase: Optional[str] = None) -> dict:
    """List a project's configured swimlane branches, optionally filtered to one phase.

    Use this to resolve a MULTIPLE_SWIMLANES error by asking the user which lane to use.
    """
    return fiv_portal.list_swimlanes(project, phase)


@_guard
def fiv_list_releases(project: str, phase: str, swimlane: Optional[str] = None) -> dict:
    """List releases for a phase, newest first. No version needed - use it to discover one."""
    return fiv_portal.list_releases(project, phase, swimlane)


@_guard
def fiv_find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    """Resolve one release's IFWI package: the downloadable .7z URL and the .bin names inside it.

    Returns MULTIPLE_SWIMLANES with candidates when swimlane is omitted and several exist.
    """
    return fiv_portal.find_ifwi(project, phase, version, swimlane)


@_guard
def fiv_list_release_binaries(project: str, phase: str, version: str,
                              swimlane: Optional[str] = None) -> dict:
    """List the IFWI binaries from a release's report. Try this FIRST to pick an IFWI.

    These are the curated, validated binaries of the release, each linking straight to a
    .bin. Show them to the user; only if they say none is the right one, fall back to
    fiv_list_ifwi_binaries, which enumerates every build target of the release.
    """
    return fiv_portal.list_release_binaries(project, phase, version, swimlane)


@_guard
def fiv_list_ifwi_binaries(project: str, phase: str, version: str,
                           swimlane: Optional[str] = None) -> dict:
    """List every build target of a release with its package URL and contained .bin files.

    This is the fallback listing - prefer fiv_list_release_binaries, and come here only
    when the user rejects every binary in the release report, or no report exists.
    """
    return fiv_portal.list_ifwi_binaries(project, phase, version, swimlane)


@_guard
def fiv_get_binary_ingredients(project: str, phase: str, version: str, binary_name: str,
                               swimlane: Optional[str] = None,
                               package_name: Optional[str] = None) -> dict:
    """List the ingredients (name, version, ...) baked into one specific release binary.

    binary_name must match exactly (the `binary_name` field from fiv_list_release_binaries /
    fiv_list_ifwi_binaries), e.g. to read off which MMC version shipped in a given .bin.
    """
    return fiv_portal.get_binary_ingredients(project, phase, version, binary_name,
                                             swimlane, package_name)


@_guard
def fiv_find_ingredient(project: str, name: str, version: str) -> dict:
    """Resolve an ingredient's download URL by exact name and version. No version fallback."""
    return fiv_portal.find_ingredient(project, name, version)


@_guard
def fiv_list_ingredient_versions(project: str, name: str, status: str = "ALL",
                                 include_stitch_ingredient: int = 0) -> dict:
    """List every version FIV has ever recorded for a named ingredient.

    Authoritative - hits FIV's dedicated get_ingredient_version/ endpoint instead of
    inferring versions by scanning historical release binaries. status defaults to ALL
    (every VCS check-in status); pass e.g. 'Accept' to narrow to just checked-in versions.
    """
    return fiv_portal.list_ingredient_versions(project, name, status, include_stitch_ingredient)


@_guard
def fiv_find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    """Resolve a release's stitch-tool package URL and its file list."""
    return fiv_portal.find_stitch_tool(project, phase, version, swimlane)


@_guard
def fiv_match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                           phase: str, swimlane: Optional[str] = None) -> dict:
    """Reverse-match a FIV build from OEM fields parsed out of a local IFWI image.

    Feed it the output of parse_ifwi_oem to identify which release an unknown .bin came from.
    """
    return fiv_portal.match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane)


@_guard
def fiv_prepare_stitch(project: str, phase: str, version: Optional[str] = None,
                       swimlane: Optional[str] = None,
                       search_neighbors: bool = False) -> dict:
    """Download and extract the stitch tool and build its venv, in one call.

    Omit version to auto-pick the newest release that ships one. If a specific version has
    no stitch tool, this fails fast (no candidates) so the caller can tell the user first;
    re-call with search_neighbors=True to check adjacent versions and get candidates back.
    """
    return prepare.prepare_stitch(project, phase, version, swimlane, search_neighbors)


@_guard
def fiv_prepare_ingredient(project: str, name: str, version: str) -> dict:
    """Download and extract an ingredient, in one call. Exact version only, never falls back."""
    return prepare.prepare_ingredient(project, name, version)


@_guard
def fiv_prepare_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None,
                     binary_name: Optional[str] = None,
                     from_build_targets: bool = False) -> dict:
    """Fetch the base IFWI .bin, in one call.

    Reads the release report by default, which downloads the .bin directly. Pass
    from_build_targets=True when the user rejected every binary in that report; it then
    downloads and extracts the build target's .7z package instead. binary_name may be
    omitted only when there is exactly one candidate; otherwise the result lists them.
    """
    return prepare.prepare_ifwi(project, phase, version, swimlane, binary_name,
                                from_build_targets)


@_guard
def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    """Decode a local IFWI image's OEM region into product, ifwi_version, flavor and hash."""
    return oem_parser.parse_ifwi_oem(local_ifwi_path)


@_guard
def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    """Download an Artifactory URL, or copy a local file, into the cache.

    category is one of ifwi, ingredients or stitch.
    """
    return downloader.download(url_or_path, category, dest_name)


@_guard
def list_local_files() -> dict:
    """List the files already cached, across all categories."""
    return downloader.list_local_files()


@_guard
def extract_archive(archive_path: str, dest_name: Optional[str] = None) -> dict:
    """Extract a .zip / .tar* / .7z into the cache and report the .bin files it contained."""
    return archive.extract_archive(archive_path, dest_name)


@_guard
def extract_stitch_tool(archive_path: str) -> dict:
    """Extract a stitch-tool archive, build its venv, and list its Config_Stitch_*.ini targets."""
    return stitch_runner.extract_stitch_tool(archive_path)


def _parse_ingredients(ingredients: str):
    """Parse the ingredients JSON-array-string param. Returns (list, None) or (None, err_dict)."""
    try:
        parsed = json.loads(ingredients)
    except (ValueError, TypeError) as exc:
        return None, err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a JSON array string",
                         {"param": "ingredients", "reason": str(exc)})
    if not isinstance(parsed, list):
        return None, err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a JSON array",
                         {"param": "ingredients", "got": type(parsed).__name__})
    return parsed, None


@_guard
def run_stitch(stitch_dir: str, binary_file: str, ingredients: str, config_ini: str,
               soft_strap: Optional[str] = None) -> dict:
    """Run an already-prepared stitch tool's cli.py and return the stitched .bin.

    Low-level: everything must already be on disk. Prefer stitch_execute_plan, which
    prepares the environment and collects deliverables for you.

    ingredients is a JSON array string, one entry per ingredient to stitch together in a
    single run (pipe-joined internally, as cli.py's own --ingredient_name/--ingredient_path
    expect): [{"name": "PowerOn_DMRAP_MMC1", "path": "/abs/dir"}, {"name": "...", "path": "..."}].
    """
    parsed, error = _parse_ingredients(ingredients)
    if error:
        return error
    return stitch_runner.run_stitch(stitch_dir, binary_file, parsed, config_ini, soft_strap)


@_guard
def get_execution_config() -> dict:
    """Report where stitch jobs run (local or remote) and which endpoint is used."""
    return config.get_execution_config()


@_guard
def stitch_build_plan(project: str, phase: str, ingredients: str, config_ini: str,
                      version: Optional[str] = None, swimlane: Optional[str] = None,
                      ifwi_binary: Optional[str] = None, ifwi_path: Optional[str] = None,
                      ifwi_from_build_targets: bool = False,
                      stitch_version: Optional[str] = None,
                      stitch_path: Optional[str] = None,
                      soft_strap: Optional[str] = None) -> dict:
    """Turn the answers confirmed with the user into a saved plan plus its command line.

    Call this once every parameter is settled. Nothing is downloaded and nothing runs, so
    the returned command_line can be shown to the user for a final confirmation before
    stitch_execute_plan. Each input is taken either from FIV (version fields) or from a
    local path (the *_path arguments). Set ifwi_from_build_targets=True if the chosen
    ifwi_binary came from fiv_list_ifwi_binaries rather than the release report.

    ingredients is a JSON array string, one entry per ingredient to stitch together in a
    single run: [{"name": "PowerOn_DMRAP_MMC1", "version": "0.958.0"},
    {"name": "PowerOn_DMRAP_MMC2", "path": "/abs/dir"}]. Each entry needs a name and
    either version (fetched from FIV) or path (an existing local dir/file).
    """
    parsed, error = _parse_ingredients(ingredients)
    if error:
        return error
    return plan_mod.build_plan(project, phase, parsed, config_ini, version,
                               swimlane, ifwi_binary, ifwi_path, ifwi_from_build_targets,
                               stitch_version, stitch_path, soft_strap)


@_guard
def stitch_get_plan(plan_id: str) -> dict:
    """Re-read a saved plan and its command line."""
    return plan_mod.load_plan(plan_id)


@_guard
def stitch_list_plans() -> dict:
    """List the saved plans, newest inputs first, with their project / phase / version."""
    return plan_mod.list_plans()


@_guard
def stitch_prepare_environment(plan_id: str) -> dict:
    """Download and unpack everything the plan needs, without running the command."""
    loaded = plan_mod.load_plan(plan_id)
    if not loaded["ok"]:
        return loaded
    return executor.prepare_environment(loaded["data"]["plan"])


@_guard
def stitch_execute_plan(plan_id: str) -> dict:
    """Prepare the environment, run the plan locally or remotely, return deliverables.

    Where it runs is decided by the execution.mode config switch - see get_execution_config.
    Returns the command line that actually ran, the exit code, and a deliverables manifest.
    """
    return executor.execute_plan_id(plan_id)


@_guard
def stitch_get_deliverables(plan_id: str) -> dict:
    """Re-read the deliverables manifest of a finished plan: artifacts, sizes and sha256."""
    return deliverables.load_manifest(plan_id)


# Register each plain function as an MCP tool (keeps the plain callable importable for tests).
for _fn in (fiv_list_projects, fiv_get_project, fiv_list_swimlanes, fiv_list_releases,
            fiv_find_ifwi, fiv_list_release_binaries, fiv_list_ifwi_binaries,
            fiv_get_binary_ingredients, fiv_find_ingredient, fiv_list_ingredient_versions,
            fiv_find_stitch_tool, fiv_match_build_by_oem,
            fiv_prepare_stitch, fiv_prepare_ingredient, fiv_prepare_ifwi,
            parse_ifwi_oem, download,
            list_local_files, extract_archive, extract_stitch_tool, run_stitch,
            get_execution_config, stitch_build_plan, stitch_get_plan, stitch_list_plans,
            stitch_prepare_environment, stitch_execute_plan, stitch_get_deliverables):
    mcp.tool(_fn)


def main() -> None:
    startup = config.validate_startup()
    if not startup["ok"]:
        print(json.dumps(startup), file=sys.stderr)
        sys.exit(1)
    mcp.run()


if __name__ == "__main__":
    main()
