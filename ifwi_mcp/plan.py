"""Turn the answers confirmed during Q&A into a validated, executable stitch plan.

A plan is the hand-off artifact between "the user has confirmed everything" and
"something runs it". It records where each input comes from (FIV or a local path)
and the command line that will be run, with placeholders for the paths that only
exist after the environment is prepared. Plans are JSON, so the very same plan can
be executed locally or POSTed to a remote runner.
"""
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, fiv_portal, prepare, stitch_runner
from .result import ok, err, ErrorCode

PLAN_VERSION = 1
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Placeholders resolved against the prepared environment right before execution.
P_PYTHON = "{python}"
P_BINARY = "{binary_file}"
P_INGREDIENT = "{ingredient_path}"
P_CONFIG_INI = "{config_ini}"

_SOFT_STRAP_RE = re.compile(r"^(\w+:\w+=[^,\s]+)([ ,]\w+:\w+=[^,\s]+)*$")


def _new_plan_id() -> str:
    return "plan-{0}-{1}".format(datetime.now().strftime("%Y%m%d-%H%M%S"),
                                 uuid.uuid4().hex[:6])


def _invalid(message: str, detail: dict) -> dict:
    return err(ErrorCode.PLAN_INVALID, message, detail)


def _resolve_stitch_dir(stitch_source: dict) -> dict:
    """Locate the stitch tool's cli.py directory, preparing it (from cache, if already
    fetched) if it's FIV-sourced. Used only for the advisory ingredient-match preview
    below -- execution re-resolves this itself."""
    if stitch_source["kind"] == "local":
        local = Path(stitch_source["local_path"])
        if local.is_file():
            return stitch_runner.extract_stitch_tool(str(local))
        cli_dir = stitch_runner.find_cli_dir(local)
        if cli_dir is None:
            return err(ErrorCode.ENV_PREPARE_FAILED, "no cli.py or stitch2.py under stitch_path",
                       {"path": str(local)})
        return ok({"stitch_dir": str(cli_dir)})
    res = prepare.prepare_stitch(stitch_source["project"], stitch_source["phase"],
                                 stitch_source["version"], stitch_source.get("swimlane"),
                                 search_neighbors=True)
    if not res["ok"]:
        return res
    if not res["data"].get("prepared"):
        return err(ErrorCode.ENV_PREPARE_FAILED, "no stitch tool for the planned version",
                   {"version": stitch_source["version"],
                    "candidates": res["data"].get("candidates", [])})
    return ok({"stitch_dir": res["data"]["stitch_dir"]})


def _preview_local_ingredients(stitch_source: dict, config_ini: str,
                               local_ingredients: list) -> tuple:
    """Advisory regex-match preview for every local-path ingredient, so the caller can
    show the user what file was actually found before confirming the plan -- instead of
    asking them to pick a candidate blindly. Never blocks plan building: failures become
    warnings."""
    if not local_ingredients:
        return [], []
    stitch_dir_res = _resolve_stitch_dir(stitch_source)
    if not stitch_dir_res["ok"]:
        return [], [f"could not prepare the stitch tool to preview ingredient regex match: "
                    f"{stitch_dir_res['message']}"]
    stitch_dir = stitch_dir_res["data"]["stitch_dir"]
    previews, warnings = [], []
    for item in local_ingredients:
        preview = stitch_runner.preview_ingredient_match(
            stitch_dir, config_ini, item["name"], item["path"])
        if preview["ok"]:
            previews.append(preview["data"])
        else:
            warnings.append(f"{item['name']}: could not preview ingredient match: "
                            f"{preview['message']}")
    return previews, warnings


def build_plan(project: str, phase: str, ingredients: list, config_ini: str,
               version: Optional[str] = None, swimlane: Optional[str] = None,
               ifwi_binary: Optional[str] = None, ifwi_path: Optional[str] = None,
               ifwi_from_build_targets: bool = False,
               stitch_version: Optional[str] = None,
               stitch_path: Optional[str] = None,
               soft_strap: Optional[str] = None) -> dict:
    """Validate the confirmed answers and render the command line for them.

    Every source is either "fiv" (fetched during environment preparation) or
    "local" (an existing path the user supplied).

    If any ingredient is a local path, the stitch tool is prepared right away (a no-op
    if it was already fetched, e.g. via fiv_prepare_stitch) so its config_ini's
    regex_mandatory_dict can be advisory-matched against that path. The result is
    returned as ingredient_match_preview — show it to the user before they confirm the
    plan, instead of asking them to pick a candidate file blindly. It never blocks plan
    building: an unresolved or non-matching regex only adds to the response's warnings.

    ingredients is a non-empty list of {"name": str, "version": str} (fetched from FIV)
    or {"name": str, "path": str} (existing local dir/file) — one entry per ingredient.
    All of them are stitched together in a single cli.py invocation, matching the vendor
    tool's own pipe-separated --ingredient_name/--ingredient_path syntax.
    """
    if not project or not project.strip():
        return _invalid("project is required", {"param": "project"})
    if phase not in fiv_portal.PHASES:
        return _invalid("phase is not recognised",
                        {"param": "phase", "expected": list(fiv_portal.PHASES), "got": phase})
    if not ingredients:
        return _invalid("ingredients is required",
                        {"param": "ingredients",
                         "expected": "non-empty list of {name, version|path}"})
    for i, item in enumerate(ingredients):
        if not (item.get("name") or "").strip():
            return _invalid("each ingredient needs a name",
                            {"param": f"ingredients[{i}].name"})
    if not config_ini or not config_ini.strip():
        return _invalid("config_ini is required",
                        {"param": "config_ini", "expected": "e.g. Config_Stitch_DMR"})
    if soft_strap and not _SOFT_STRAP_RE.match(soft_strap):
        return _invalid("soft_strap has invalid syntax",
                        {"param": "soft_strap", "expected": "k:v=val[,k:v=val]"})

    # --- IFWI source -----------------------------------------------------
    if ifwi_path:
        if not Path(ifwi_path).is_file():
            return _invalid("ifwi_path does not exist",
                            {"param": "ifwi_path", "expected": "existing .bin file"})
        ifwi_source = {"kind": "local", "local_path": str(Path(ifwi_path).resolve())}
    else:
        if not version:
            return _invalid("version is required unless ifwi_path is given",
                            {"param": "version", "expected": "e.g. 2026.28.3.01"})
        ifwi_source = {"kind": "fiv", "project": project, "phase": phase,
                       "version": version, "swimlane": swimlane,
                       "binary_name": ifwi_binary,
                       "from_build_targets": bool(ifwi_from_build_targets)}

    # --- Ingredient sources ------------------------------------------------
    ingredient_sources = []
    ingredient_names = []
    local_ingredients = []
    for i, item in enumerate(ingredients):
        name = item["name"].strip()
        ing_path = item.get("path")
        if ing_path:
            if not Path(ing_path).exists():
                return _invalid("ingredient path does not exist",
                                {"param": f"ingredients[{i}].path",
                                 "expected": "existing dir or file"})
            resolved_path = str(Path(ing_path).resolve())
            ingredient_sources.append({"kind": "local", "name": name,
                                       "local_path": resolved_path})
            local_ingredients.append({"name": name, "path": resolved_path})
        else:
            ing_version = item.get("version")
            if not ing_version:
                return _invalid("ingredient needs version or path",
                                {"param": f"ingredients[{i}].version"})
            ingredient_sources.append({"kind": "fiv", "project": project, "name": name,
                                       "version": ing_version})
        ingredient_names.append(name)

    # --- Stitch tool source ----------------------------------------------
    if stitch_path:
        if not Path(stitch_path).exists():
            return _invalid("stitch_path does not exist",
                            {"param": "stitch_path",
                             "expected": "extracted stitch dir or tool archive"})
        stitch_source = {"kind": "local", "local_path": str(Path(stitch_path).resolve())}
    else:
        tool_version = stitch_version or version
        if not tool_version:
            return _invalid("stitch_version is required unless stitch_path or version is given",
                            {"param": "stitch_version"})
        stitch_source = {"kind": "fiv", "project": project, "phase": phase,
                         "version": tool_version, "swimlane": swimlane}

    ingredient_match_preview, preview_warnings = _preview_local_ingredients(
        stitch_source, config_ini, local_ingredients)

    argv = [P_PYTHON, "cli.py",
            "--binary_file", P_BINARY,
            "--ingredient_name", "|".join(ingredient_names),
            "--ingredient_path", P_INGREDIENT,
            "--config_ini", P_CONFIG_INI]
    if soft_strap:
        argv += ["--soft_strap", soft_strap]

    plan = {
        "plan_version": PLAN_VERSION,
        "plan_id": _new_plan_id(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "phase": phase,
        "version": version,
        "swimlane": swimlane,
        "sources": {"ifwi": ifwi_source, "ingredients": ingredient_sources,
                    "stitch_tool": stitch_source},
        "stitch": {"ingredient_names": ingredient_names, "config_ini": config_ini,
                   "soft_strap": soft_strap},
        "command_template": argv,
    }
    saved = save_plan(plan)
    if not saved["ok"]:
        return saved
    return ok({"plan_id": plan["plan_id"], "plan": plan,
               "plan_path": saved["data"]["plan_path"],
               "command_line": render_command_line(plan),
               "placeholders": [P_PYTHON, P_BINARY, P_INGREDIENT, P_CONFIG_INI],
               "ingredient_match_preview": ingredient_match_preview,
               "warnings": preview_warnings})


def render_command_line(plan: dict) -> str:
    """Human-readable preview of the command; placeholders stay unresolved."""
    return " ".join(plan.get("command_template", []))


def plan_dir() -> Path:
    return config.cache_subdir("plans")


def save_plan(plan: dict) -> dict:
    plan_id = plan.get("plan_id", "")
    if not plan_id or not PLAN_ID_RE.match(plan_id):
        return _invalid("plan_id is missing or unsafe",
                        {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    path = plan_dir() / f"{plan_id}.json"
    try:
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    except OSError as exc:
        return err(ErrorCode.INTERNAL_ERROR, "could not save plan",
                   {"plan_id": plan_id, "reason": str(exc)})
    return ok({"plan_path": str(path)})


def load_plan(plan_id: str) -> dict:
    if not plan_id or not PLAN_ID_RE.match(plan_id):
        return _invalid("plan_id is missing or unsafe",
                        {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    path = plan_dir() / f"{plan_id}.json"
    if not path.is_file():
        return err(ErrorCode.PLAN_NOT_FOUND, "no such plan",
                   {"plan_id": plan_id, "expected": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return err(ErrorCode.PLAN_INVALID, "plan file is not readable JSON",
                   {"plan_id": plan_id, "reason": str(exc)})
    return ok({"plan": data, "plan_path": str(path),
               "command_line": render_command_line(data)})


def list_plans() -> dict:
    plans = []
    for path in sorted(plan_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plans.append({"plan_id": data.get("plan_id", path.stem),
                      "created_at": data.get("created_at"),
                      "project": data.get("project"), "phase": data.get("phase"),
                      "version": data.get("version")})
    return ok({"count": len(plans), "plans": plans})
