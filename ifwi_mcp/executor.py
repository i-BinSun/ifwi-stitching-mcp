"""Execute a stitch plan: prepare the environment, run the command, return results.

The execution.mode switch in the config file decides where the work happens:

* local  - download/extract everything into the cache, run cli.py in its venv.
* remote - POST the plan to execution.endpoint, poll for completion, then pull the
           deliverables back. The runner is expected to perform the same steps.

Either way the caller gets the same result shape: the exact command line that ran,
the exit code, and a deliverables manifest.
"""
import time
import uuid
from pathlib import Path

import requests

from . import config, deliverables, plan as plan_mod, prepare, stitch_runner
from .result import ok, err, ErrorCode

_SUBMIT_TIMEOUT = 60
_POLL_TIMEOUT = 60
_DONE = {"succeeded": True, "success": True, "completed": True, "done": True,
         "failed": False, "error": False, "cancelled": False, "canceled": False}
_FAILED_STATES = ("failed", "error", "cancelled", "canceled")


def _headers(token: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def prepare_environment(plan: dict) -> dict:
    """Fetch and unpack everything the plan needs, before any command runs.

    Resolves the three sources (IFWI binary, ingredient, stitch tool) into concrete
    local paths, creating the stitch tool's venv on the way.
    """
    sources = plan.get("sources") or {}
    warnings = []
    fetched = []

    ifwi = sources.get("ifwi") or {}
    if ifwi.get("kind") == "local":
        binary_file = ifwi.get("local_path", "")
        if not Path(binary_file).is_file():
            return err(ErrorCode.ENV_PREPARE_FAILED, "IFWI binary no longer exists",
                       {"source": "ifwi", "path": binary_file})
    else:
        res = prepare.prepare_ifwi(ifwi.get("project"), ifwi.get("phase"),
                                   ifwi.get("version"), ifwi.get("swimlane"),
                                   ifwi.get("binary_name"),
                                   bool(ifwi.get("from_build_targets")))
        if not res["ok"]:
            return res
        binary_file = res["data"]["binary_file"]
        warnings += res["data"].get("warnings", [])
        fetched.append({"source": "ifwi", "url": res["data"]["ifwi_url"]})

    ingredients = []
    for ingredient in (sources.get("ingredients") or []):
        name = ingredient.get("name")
        if ingredient.get("kind") == "local":
            ingredient_dir = ingredient.get("local_path", "")
            if not Path(ingredient_dir).exists():
                return err(ErrorCode.ENV_PREPARE_FAILED, "ingredient path no longer exists",
                           {"source": "ingredient", "name": name, "path": ingredient_dir})
        else:
            res = prepare.prepare_ingredient(ingredient.get("project"), name,
                                             ingredient.get("version"))
            if not res["ok"]:
                return res
            ingredient_dir = res["data"]["ingredient_dir"]
            warnings += res["data"].get("warnings", [])
            fetched.append({"source": "ingredient", "name": name,
                            "url": res["data"]["ingredient_url"]})
        ingredients.append({"name": name, "path": ingredient_dir})

    tool = sources.get("stitch_tool") or {}
    if tool.get("kind") == "local":
        local = Path(tool.get("local_path", ""))
        if local.is_file():
            res = stitch_runner.extract_stitch_tool(str(local))
            if not res["ok"]:
                return res
            stitch_dir, venv_python = res["data"]["stitch_dir"], res["data"]["venv_python"]
            config_targets = res["data"]["config_targets"]
            tool_root = res["data"]["tool_root"]
        elif local.is_dir():
            cli_dir = stitch_runner.find_cli_dir(local)
            if cli_dir is None:
                return err(ErrorCode.ENV_PREPARE_FAILED,
                           "no cli.py or stitch2.py under stitch_path",
                           {"source": "stitch_tool", "path": str(local)})
            stitch_dir = str(cli_dir)
            venv_python = str(stitch_runner.find_venv_python(cli_dir))
            config_targets = sorted(p.stem for p in (cli_dir / "config").glob("Config_Stitch_*.ini"))
            tool_root = str(local)
        else:
            return err(ErrorCode.ENV_PREPARE_FAILED, "stitch path no longer exists",
                       {"source": "stitch_tool", "path": str(local)})
    else:
        res = prepare.prepare_stitch(tool.get("project"), tool.get("phase"),
                                     tool.get("version"), tool.get("swimlane"),
                                     search_neighbors=True)
        if not res["ok"]:
            return res
        if not res["data"].get("prepared"):
            return err(ErrorCode.ENV_PREPARE_FAILED,
                       "no stitch tool for the planned version",
                       {"source": "stitch_tool", "version": tool.get("version"),
                        "candidates": res["data"].get("candidates", [])})
        stitch_dir, venv_python = res["data"]["stitch_dir"], res["data"]["venv_python"]
        config_targets = res["data"]["config_targets"]
        tool_root = res["data"]["tool_root"]
        warnings += res["data"].get("warnings", [])

    return ok({"binary_file": binary_file, "ingredients": ingredients,
               "stitch_dir": stitch_dir, "venv_python": venv_python,
               "tool_root": tool_root,
               "config_targets": config_targets, "fetched": fetched, "warnings": warnings})


def execute(plan: dict) -> dict:
    """Run the plan wherever execution.mode points."""
    cfg = config.get_execution_config()
    if not cfg["ok"]:
        return cfg
    if cfg["data"]["mode"] == "remote":
        return _execute_remote(plan, cfg["data"])
    return _execute_local(plan, cfg["data"])


def execute_plan_id(plan_id: str) -> dict:
    loaded = plan_mod.load_plan(plan_id)
    if not loaded["ok"]:
        return loaded
    return execute(loaded["data"]["plan"])


def _collect_local(plan_id: str, stitched_bin: str, log_path: str, output_dir: str) -> dict:
    entries = [{"path": stitched_bin, "kind": "stitched_bin", "required": True},
               {"path": log_path, "kind": "log", "required": False}]
    out = Path(output_dir)
    if out.is_dir():
        for extra in sorted(out.iterdir()):
            if extra.is_file() and str(extra) != stitched_bin:
                entries.append({"path": str(extra), "kind": "output", "required": False})
    return deliverables.collect(plan_id, entries)


def _isolated_stitch_dir(env_data: dict, key: str) -> dict:
    """Materialize a private workspace for this run and re-root stitch_dir onto it,
    so concurrent runs sharing the same underlying tool never collide."""
    tool_root = Path(env_data["tool_root"])
    stitch_dir = Path(env_data["stitch_dir"])
    try:
        rel = stitch_dir.relative_to(tool_root)
    except ValueError:
        return ok({"stitch_dir": str(stitch_dir), "workspace_dir": None})
    workspace = stitch_runner.materialize_workspace(tool_root, key)
    if not workspace["ok"]:
        return workspace
    workspace_root = Path(workspace["data"]["workspace_root"])
    return ok({"stitch_dir": str(workspace_root / rel), "workspace_dir": str(workspace_root)})


def _execute_local(plan: dict, exec_cfg: dict) -> dict:
    plan_id = plan.get("plan_id", "")
    env = prepare_environment(plan)
    if not env["ok"]:
        return env
    stitch = plan.get("stitch") or {}

    isolated = _isolated_stitch_dir(env["data"], plan_id or uuid.uuid4().hex)
    if not isolated["ok"]:
        return isolated

    built = stitch_runner.build_stitch_command(
        isolated["data"]["stitch_dir"], env["data"]["binary_file"],
        env["data"]["ingredients"],
        stitch.get("config_ini", ""), stitch.get("soft_strap"))
    if not built["ok"]:
        return built

    run = stitch_runner.execute_command(built["data"]["argv"], built["data"]["cwd"],
                                        log_name=plan_id or None,
                                        timeout=exec_cfg["timeout_seconds"])
    if not run["ok"]:
        log = run["detail"].get("full_log_path")
        if plan_id and log:
            collected = deliverables.collect(
                plan_id, [{"path": log, "kind": "log", "required": False}])
            if collected["ok"]:
                run["detail"]["deliverables"] = collected["data"]
        return run

    collected = _collect_local(plan_id, run["data"]["stitched_bin"],
                               run["data"]["log_path"], run["data"]["output_dir"])
    if not collected["ok"]:
        return collected
    return ok({"plan_id": plan_id, "mode": "local",
               "command_line": run["data"]["command_line"],
               "exit_code": run["data"]["exit_code"],
               "stitched_bin": run["data"]["stitched_bin"],
               "log_path": run["data"]["log_path"],
               "workspace_dir": isolated["data"]["workspace_dir"],
               "environment": {k: env["data"][k] for k in
                               ("binary_file", "ingredients", "stitch_dir",
                                "venv_python", "fetched")},
               "deliverables": collected["data"],
               "warnings": env["data"]["warnings"] + built["data"]["warnings"]})


def _submit(endpoint: str, token: str, plan: dict) -> dict:
    try:
        resp = requests.post(f"{endpoint}/jobs", json={"plan": plan},
                             headers=_headers(token), timeout=_SUBMIT_TIMEOUT,
                             verify=False)
    except requests.RequestException as exc:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "could not reach the stitch runner",
                   {"endpoint": endpoint, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "stitch runner rejected the credentials",
                   {"endpoint": endpoint, "status": resp.status_code})
    if resp.status_code >= 400:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "stitch runner refused the job",
                   {"endpoint": endpoint, "status": resp.status_code,
                    "body": resp.text[:2000]})
    try:
        body = resp.json()
    except ValueError:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "stitch runner returned invalid JSON",
                   {"endpoint": endpoint, "body": resp.text[:2000]})
    job_id = body.get("job_id") or body.get("id")
    if not job_id:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "stitch runner returned no job_id",
                   {"endpoint": endpoint, "body": body})
    return ok({"job_id": str(job_id)})


def _poll(endpoint: str, token: str, job_id: str, timeout_seconds: int,
          poll_interval: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while True:
        try:
            resp = requests.get(f"{endpoint}/jobs/{job_id}", headers=_headers(token),
                                timeout=_POLL_TIMEOUT, verify=False)
        except requests.RequestException as exc:
            return err(ErrorCode.REMOTE_EXEC_FAILED, "lost contact with the stitch runner",
                       {"endpoint": endpoint, "job_id": job_id, "reason": str(exc)})
        if resp.status_code in (401, 403):
            return err(ErrorCode.AUTH_FAILED, "stitch runner rejected the credentials",
                       {"endpoint": endpoint, "status": resp.status_code})
        if resp.status_code >= 400:
            return err(ErrorCode.REMOTE_EXEC_FAILED, "stitch runner returned an error",
                       {"endpoint": endpoint, "job_id": job_id,
                        "status": resp.status_code, "body": resp.text[:2000]})
        try:
            last = resp.json()
        except ValueError:
            return err(ErrorCode.REMOTE_EXEC_FAILED, "stitch runner returned invalid JSON",
                       {"endpoint": endpoint, "job_id": job_id, "body": resp.text[:2000]})
        status = str(last.get("status", "")).lower()
        if status in _FAILED_STATES:
            return err(ErrorCode.STITCH_RUN_FAILED, "remote stitch job failed",
                       {"job_id": job_id, "status": status,
                        "exit_code": last.get("exit_code"),
                        "command_line": last.get("command_line"),
                        "log_tail": last.get("log_tail")})
        if _DONE.get(status):
            return ok({"job": last})
        if time.monotonic() >= deadline:
            return err(ErrorCode.REMOTE_TIMEOUT, "remote stitch job did not finish in time",
                       {"job_id": job_id, "status": status,
                        "timeout_seconds": timeout_seconds})
        time.sleep(poll_interval)


def _execute_remote(plan: dict, exec_cfg: dict) -> dict:
    plan_id = plan.get("plan_id", "")
    endpoint, token = exec_cfg["endpoint"], config.get_execution_token()

    submitted = _submit(endpoint, token, plan)
    if not submitted["ok"]:
        return submitted
    job_id = submitted["data"]["job_id"]

    polled = _poll(endpoint, token, job_id, exec_cfg["timeout_seconds"],
                   exec_cfg["poll_interval_seconds"])
    if not polled["ok"]:
        polled["detail"]["endpoint"] = endpoint
        return polled
    job = polled["data"]["job"]

    entries = job.get("deliverables") or []
    fetched = deliverables.fetch(plan_id, entries, token) if entries else \
        deliverables.collect(plan_id, [])
    if not fetched["ok"]:
        return fetched
    stitched = next((item["path"] for item in fetched["data"]["items"]
                     if item["kind"] == "stitched_bin"), None)
    log = next((item["path"] for item in fetched["data"]["items"]
                if item["kind"] == "log"), None)
    return ok({"plan_id": plan_id, "mode": "remote", "job_id": job_id,
               "endpoint": endpoint,
               "command_line": job.get("command_line"),
               "exit_code": job.get("exit_code", 0),
               "stitched_bin": stitched, "log_path": log,
               "environment": job.get("environment", {}),
               "deliverables": fetched["data"],
               "warnings": job.get("warnings", [])})
