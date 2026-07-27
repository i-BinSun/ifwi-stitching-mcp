"""FastMCP server: thin tool wrappers over the ifwi_mcp modules."""
import functools
import json
import sys
from typing import Optional

from fastmcp import FastMCP

from . import archive, config, downloader, fiv_portal, oem_parser, prepare, stitch_runner
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
    return fiv_portal.list_projects()


@_guard
def fiv_get_project(project: str) -> dict:
    return fiv_portal.get_project(project)


@_guard
def fiv_list_swimlanes(project: str, phase: Optional[str] = None) -> dict:
    return fiv_portal.list_swimlanes(project, phase)


@_guard
def fiv_list_releases(project: str, phase: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.list_releases(project, phase, swimlane)


@_guard
def fiv_find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.find_ifwi(project, phase, version, swimlane)


@_guard
def fiv_list_ifwi_binaries(project: str, phase: str, version: str,
                           swimlane: Optional[str] = None) -> dict:
    return fiv_portal.list_ifwi_binaries(project, phase, version, swimlane)


@_guard
def fiv_find_ingredient(project: str, name: str, version: str) -> dict:
    return fiv_portal.find_ingredient(project, name, version)


@_guard
def fiv_find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.find_stitch_tool(project, phase, version, swimlane)


@_guard
def fiv_match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                           phase: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane)


@_guard
def fiv_prepare_stitch(project: str, phase: str, version: Optional[str] = None,
                       swimlane: Optional[str] = None) -> dict:
    return prepare.prepare_stitch(project, phase, version, swimlane)


@_guard
def fiv_prepare_ingredient(project: str, name: str, version: str) -> dict:
    return prepare.prepare_ingredient(project, name, version)


@_guard
def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    return oem_parser.parse_ifwi_oem(local_ifwi_path)


@_guard
def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    return downloader.download(url_or_path, category, dest_name)


@_guard
def list_local_files() -> dict:
    return downloader.list_local_files()


@_guard
def extract_archive(archive_path: str, dest_name: Optional[str] = None) -> dict:
    return archive.extract_archive(archive_path, dest_name)


@_guard
def extract_stitch_tool(archive_path: str) -> dict:
    return stitch_runner.extract_stitch_tool(archive_path)


@_guard
def run_stitch(stitch_dir: str, binary_file: str, ingredient_name: str,
               ingredient_path: str, config_ini: str, soft_strap: Optional[str] = None) -> dict:
    return stitch_runner.run_stitch(stitch_dir, binary_file, ingredient_name,
                                    ingredient_path, config_ini, soft_strap)


# Register each plain function as an MCP tool (keeps the plain callable importable for tests).
for _fn in (fiv_list_projects, fiv_get_project, fiv_list_swimlanes, fiv_list_releases,
            fiv_find_ifwi, fiv_list_ifwi_binaries, fiv_find_ingredient,
            fiv_find_stitch_tool, fiv_match_build_by_oem,
            fiv_prepare_stitch, fiv_prepare_ingredient,
            parse_ifwi_oem, download,
            list_local_files, extract_archive, extract_stitch_tool, run_stitch):
    mcp.tool(_fn)


def main() -> None:
    startup = config.validate_startup()
    if not startup["ok"]:
        print(json.dumps(startup), file=sys.stderr)
        sys.exit(1)
    mcp.run()


if __name__ == "__main__":
    main()
