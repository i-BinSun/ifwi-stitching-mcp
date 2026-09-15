"""Standalone command-line entry point: `ifwi-cli`.

Thin argparse wrapper over the plain functions already defined in `server.py` --
reuses them directly so the CLI and the MCP tools can never behave differently.
Every command prints the same unified result envelope the MCP tools return
(`{"ok": ...}`) as JSON and exits 0/1 to match.
"""
import argparse
import json
import sys

from . import config, server


def _dest(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def _opt(*flags: str, **kwargs) -> tuple:
    return list(flags), kwargs


def _pos(name: str, **kwargs) -> tuple:
    return [name], kwargs


class Spec:
    __slots__ = ("group", "name", "func", "args")

    def __init__(self, group, name, func, args):
        self.group = group
        self.name = name
        self.func = func
        self.args = args


SPECS = [
    Spec(None, "download", server.download, [
        _pos("url_or_path"),
        _opt("--category", default="ifwi"),
        _opt("--dest-name", default=None),
    ]),
    Spec(None, "list-local-files", server.list_local_files, []),
    Spec(None, "extract-archive", server.extract_archive, [
        _pos("archive_path"),
        _opt("--dest-name", default=None),
    ]),

    Spec("oem", "parse", server.parse_ifwi_oem, [
        _pos("local_ifwi_path"),
    ]),

    Spec("fiv", "list-projects", server.fiv_list_projects, []),
    Spec("fiv", "get-project", server.fiv_get_project, [
        _opt("--project", required=True),
    ]),
    Spec("fiv", "list-swimlanes", server.fiv_list_swimlanes, [
        _opt("--project", required=True),
        _opt("--phase", default=None),
    ]),
    Spec("fiv", "list-releases", server.fiv_list_releases, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "find-ifwi", server.fiv_find_ifwi, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "list-release-binaries", server.fiv_list_release_binaries, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "list-ifwi-binaries", server.fiv_list_ifwi_binaries, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "get-binary-ingredients", server.fiv_get_binary_ingredients, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--binary-name", default=None),
        _opt("--swimlane", default=None),
        _opt("--package-name", default=None),
    ]),
    Spec("fiv", "find-ingredient", server.fiv_find_ingredient, [
        _opt("--project", required=True),
        _opt("--name", required=True),
        _opt("--version", required=True),
    ]),
    Spec("fiv", "list-ingredient-versions", server.fiv_list_ingredient_versions, [
        _opt("--project", required=True),
        _opt("--name", required=True),
        _opt("--status", default="ALL"),
        _opt("--include-stitch-ingredient", type=int, default=0),
    ]),
    Spec("fiv", "find-stitch-tool", server.fiv_find_stitch_tool, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "find-env-package", server.fiv_find_env_package, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "match-build-by-oem", server.fiv_match_build_by_oem, [
        _opt("--project", required=True),
        _opt("--product", required=True),
        _opt("--ifwi-version", required=True),
        _opt("--flavor", required=True),
        _opt("--phase", required=True),
        _opt("--swimlane", default=None),
    ]),
    Spec("fiv", "prepare-stitch", server.fiv_prepare_stitch, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", default=None),
        _opt("--swimlane", default=None),
        _opt("--search-neighbors", action="store_true"),
    ]),
    Spec("fiv", "prepare-ingredient", server.fiv_prepare_ingredient, [
        _opt("--project", required=True),
        _opt("--name", required=True),
        _opt("--version", required=True),
    ]),
    Spec("fiv", "prepare-ifwi", server.fiv_prepare_ifwi, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--version", required=True),
        _opt("--swimlane", default=None),
        _opt("--binary-name", default=None),
        _opt("--from-build-targets", action="store_true"),
    ]),

    Spec("stitch", "extract-tool", server.extract_stitch_tool, [
        _opt("--archive-path", required=True),
    ]),
    Spec("stitch", "run", server.run_stitch, [
        _opt("--stitch-dir", required=True),
        _opt("--binary-file", required=True),
        _opt("--ingredients", required=True),
        _opt("--config-ini", required=True),
        _opt("--soft-strap", default=None),
    ]),
    Spec("stitch", "build-plan", server.stitch_build_plan, [
        _opt("--project", required=True),
        _opt("--phase", required=True),
        _opt("--ingredients", required=True),
        _opt("--config-ini", required=True),
        _opt("--version", default=None),
        _opt("--swimlane", default=None),
        _opt("--ifwi-binary", default=None),
        _opt("--ifwi-path", default=None),
        _opt("--ifwi-from-build-targets", action="store_true"),
        _opt("--stitch-version", default=None),
        _opt("--stitch-path", default=None),
        _opt("--soft-strap", default=None),
    ]),
    Spec("stitch", "get-plan", server.stitch_get_plan, [
        _opt("--plan-id", required=True),
    ]),
    Spec("stitch", "list-plans", server.stitch_list_plans, []),
    Spec("stitch", "prepare-environment", server.stitch_prepare_environment, [
        _opt("--plan-id", required=True),
    ]),
    Spec("stitch", "execute-plan", server.stitch_execute_plan, [
        _opt("--plan-id", required=True),
    ]),
    Spec("stitch", "get-deliverables", server.stitch_get_deliverables, [
        _opt("--plan-id", required=True),
    ]),
    Spec("stitch", "execution-config", server.get_execution_config, []),
    Spec("stitch", "create-up-config", server.create_up_config, [
        _opt("--output-path", required=True),
        _opt("--default-params", default=None),
        _opt("--ingredients", default=None),
        _opt("--template-path", default=None),
        _opt("--fit1-params", default=None),
        _opt("--soc-params", default=None),
    ]),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ifwi-cli")
    top_sub = parser.add_subparsers(dest="_top")
    top_sub.required = True

    group_subs = {}
    for spec in SPECS:
        if spec.group is None:
            leaf = top_sub.add_parser(spec.name)
        else:
            group_sub = group_subs.get(spec.group)
            if group_sub is None:
                group_parser = top_sub.add_parser(spec.group)
                group_sub = group_parser.add_subparsers(dest="_sub")
                group_sub.required = True
                group_subs[spec.group] = group_sub
            leaf = group_sub.add_parser(spec.name)
        for flags, kwargs in spec.args:
            leaf.add_argument(*flags, **kwargs)
        leaf.set_defaults(_spec=spec)
    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    spec = args._spec

    startup = config.validate_startup()
    if not startup["ok"]:
        print(json.dumps(startup, indent=2), file=sys.stderr)
        sys.exit(1)

    kwargs = {_dest(flags[0]): getattr(args, _dest(flags[0])) for flags, _ in spec.args}
    result = spec.func(**kwargs)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
