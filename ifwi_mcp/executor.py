"""Execute a stitch plan: prepare the environment, run the command, return results.

The execution.mode switch in the config file decides where the work happens:

* local  - download/extract everything into the cache, then run cli.py in a private
           per-run directory holding its own copy of the tool, venv and inputs. In
           server mode the deliverables are uploaded to Artifactory and returned as links.
* remote - create a Windows pod through the FIV Job API (the same API AUTO_INTEGRATION_SERVICE
           uses, minus its database). The pod clones this repo and runs ifwi_mcp.worker, which
           reuses this exact local-execution codepath, uploads the deliverables to Artifactory
           and writes result.json next to them. We poll the Job API until the pod ends, then
           read result.json and fetch the deliverables back.

Either way the caller gets the same result shape: the exact command line that ran,
the exit code, and a deliverables manifest.
"""
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from . import archive, artifactory, config, deliverables, plan as plan_mod, prepare, stitch_runner
from .result import ok, err, ErrorCode

REMOTE_RESULT_NAME = "result.json"
REMOTE_DELIVERABLES_DIR = "deliverables"
RUN_INFO_NAME = "run.json"
_JOB_API_TIMEOUT = 60
_POD_OS = "windows"
_POD_TTL_SECONDS = 12 * 3600
_JOB_SUCCEEDED = ("succeeded", "complete", "completed")
_JOB_FAILED = ("failed", "error")
_ACTIVE_RUNS = set()
_ACTIVE_LOCK = threading.Lock()


def _auth_headers() -> dict:
    token = config.get_execution_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def prepare_environment(plan: dict) -> dict:
    """Fetch and unpack everything the plan needs, before any command runs.

    Resolves the three sources (IFWI binary, ingredient, stitch tool) into concrete
    local paths, creating the stitch tool's venv on the way.
    """
    sources = plan.get("sources") or {}
    warnings = []
    fetched = []

    ifwi = sources.get("ifwi") or {}
    binary_file = ""
    if ifwi and ifwi.get("kind") != "none":
        if ifwi.get("kind") == "local":
            binary_file = ifwi.get("local_path", "")
            if not Path(binary_file).is_file():
                return err(ErrorCode.ENV_PREPARE_FAILED, "IFWI binary no longer exists",
                           {"source": "ifwi", "path": binary_file})
        else:
            if ifwi.get("url"):
                dl = artifactory.download(ifwi["url"], category="ifwi")
                if not dl["ok"]:
                    return dl
                res = ok({"binary_file": dl["data"]["local_path"],
                          "ifwi_url": ifwi["url"], "warnings": []})
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
            if ingredient.get("url"):
                dl = artifactory.download(ingredient["url"], category="ingredients")
                if not dl["ok"]:
                    return dl
                extracted = archive.extract_archive(dl["data"]["local_path"])
                if not extracted["ok"]:
                    return extracted
                res = ok({"ingredient_dir": extracted["data"]["extract_dir"],
                          "ingredient_url": ingredient["url"], "warnings": []})
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
            cli_dir = stitch_runner.find_cli_dir(local, project=plan.get("project"))
            if cli_dir is None:
                return err(ErrorCode.ENV_PREPARE_FAILED,
                           "no cli.py, muppet.py or stitch2.py under stitch_path",
                           {"source": "stitch_tool", "path": str(local)})
            stitch_dir = str(cli_dir)
            venv_python = str(stitch_runner.find_venv_python(cli_dir))
            config_dir = stitch_runner.find_config_dir(cli_dir)
            config_targets = []
            if config_dir:
                config_targets = sorted(p.stem for p in config_dir.glob("Config_Stitch_*.ini"))
                if not config_targets:
                    config_targets = sorted(p.stem for p in config_dir.glob("*.ini")) + sorted(p.stem for p in config_dir.glob("*.json"))
            tool_root = str(local)
        else:
            return err(ErrorCode.ENV_PREPARE_FAILED, "stitch path no longer exists",
                       {"source": "stitch_tool", "path": str(local)})
    else:
        if tool.get("url"):
            dl = artifactory.download(tool["url"], category="stitch")
            if not dl["ok"]:
                return dl
            res = stitch_runner.extract_stitch_tool(dl["data"]["local_path"])
            if res["ok"]:
                res["data"]["prepared"] = True
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
    serving = config.get_serve_config()
    if not serving["ok"]:
        return serving
    if serving["data"]["mode"] == "server":
        return _execute_local_published(plan, cfg["data"])
    return _execute_local(plan, cfg["data"])


def execute_plan_id(plan_id: str) -> dict:
    loaded = plan_mod.load_plan(plan_id)
    if not loaded["ok"]:
        return loaded
    return execute(loaded["data"]["plan"])


def _collect_local(key: str, stitched_bin: str, log_path: str, output_dir: str) -> dict:
    entries = [{"path": stitched_bin, "kind": "stitched_bin", "required": True},
               {"path": log_path, "kind": "log", "required": False}]
    out = Path(output_dir)
    if out.is_dir():
        for extra in sorted(out.iterdir()):
            if extra.is_file() and str(extra) != stitched_bin:
                entries.append({"path": str(extra), "kind": "output", "required": False})
    return deliverables.collect(key, entries)


def _isolated_stitch_dir(env_data: dict, key: str) -> dict:
    """Copy the tool into this run's private directory and re-root stitch_dir onto it,
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


def _isolated_inputs(env_data: dict, inputs_dir: Path) -> dict:
    """Private copies of the IFWI binary and ingredients, so no run reads or writes
    files another run can touch."""
    binary_file = env_data["binary_file"]
    ingredients = []
    try:
        if binary_file:
            target = inputs_dir / "ifwi" / Path(binary_file).name
            target.parent.mkdir(parents=True, exist_ok=True)
            binary_file = str(shutil.copy2(binary_file, target))
        for index, item in enumerate(env_data["ingredients"]):
            src = Path(item["path"])
            label = re.sub(r"[^A-Za-z0-9_.-]", "_", item["name"])
            target = inputs_dir / "ingredients" / f"{index}_{label}"
            if src.is_dir():
                shutil.copytree(src, target, symlinks=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
                target = Path(shutil.copy2(src, target / src.name))
            ingredients.append({"name": item["name"], "path": str(target)})
    except OSError as exc:
        return err(ErrorCode.WORKSPACE_SETUP_FAILED, "could not copy stitch inputs into the run directory",
                   {"inputs_dir": str(inputs_dir), "reason": str(exc)})
    return ok({"binary_file": binary_file, "ingredients": ingredients})


def _execute_local(plan: dict, exec_cfg: dict, run_id: str = "") -> dict:
    plan_id = plan.get("plan_id", "")
    # A caller-owned run_id also keys the deliverables, so each run's files stay separate.
    deliverables_key = run_id or plan_id
    run_id = run_id or f"{plan_id or 'run'}-{uuid.uuid4().hex[:8]}"
    env = prepare_environment(plan)
    if not env["ok"]:
        return env
    stitch = plan.get("stitch") or {}
    config_ini = stitch.get("config_ini", "")
    if stitch.get("auto_config"):
        targets = env["data"]["config_targets"]
        binary = (plan.get("sources") or {}).get("ifwi", {}).get("binary_name", "")
        exact = [name for name in targets
                 if name.casefold() == f"Config_Stitch_{binary}".casefold()]
        if len(exact) == 1:
            config_ini = exact[0]
        elif len(targets) == 1:
            config_ini = targets[0]
        else:
            return err(ErrorCode.CONFIG_AMBIGUOUS, "cannot select config for binary on worker",
                       {"binary_name": binary, "candidates": targets})

    isolated = _isolated_stitch_dir(env["data"], run_id)
    if not isolated["ok"]:
        return isolated
    run_root = stitch_runner.run_dir(run_id)
    inputs = _isolated_inputs(env["data"], run_root / "inputs")
    if not inputs["ok"]:
        return inputs

    built = stitch_runner.build_stitch_command(
        isolated["data"]["stitch_dir"], inputs["data"]["binary_file"],
        inputs["data"]["ingredients"],
        config_ini, stitch.get("soft_strap"))
    if not built["ok"]:
        return built

    run = stitch_runner.execute_command(built["data"]["argv"], built["data"]["cwd"],
                                        log_name=run_id,
                                        timeout=exec_cfg["timeout_seconds"],
                                        run_root=str(run_root))
    if not run["ok"]:
        log = run["detail"].get("full_log_path")
        if deliverables_key and log:
            collected = deliverables.collect(
                deliverables_key, [{"path": log, "kind": "log", "required": False}])
            if collected["ok"]:
                run["detail"]["deliverables"] = collected["data"]
        return run

    collected = _collect_local(deliverables_key, run["data"]["stitched_bin"],
                               run["data"]["log_path"], run["data"]["output_dir"])
    if not collected["ok"]:
        return collected
    return ok({"plan_id": plan_id, "mode": "local",
               "run_id": run_id,
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


def publish_deliverables(items: list, upload_url: str) -> dict:
    """Upload collected deliverables to <upload_url>/deliverables/ and return their links."""
    uploaded = []
    for item in items:
        dest = f"{upload_url}/{REMOTE_DELIVERABLES_DIR}/{quote(item['name'])}"
        res = artifactory.upload_file(item["path"], dest)
        if not res["ok"]:
            return res
        uploaded.append({"url": res["data"]["url"], "name": item["name"], "kind": item["kind"],
                         **{key: item[key] for key in ("bytes", "sha256") if key in item}})
    return ok({"items": uploaded})


def _finish_local_run(run_id: str, retention_hours: int) -> None:
    """Drop the run's local deliverable copies (they are on Artifactory), then either
    delete its run directory or keep it and prune runs older than the retention."""
    shutil.rmtree(deliverables.deliverables_dir(run_id), ignore_errors=True)
    if not retention_hours:
        stitch_runner.discard_run(run_id)
        return
    cutoff = time.time() - retention_hours * 3600
    with _ACTIVE_LOCK:
        active = set(_ACTIVE_RUNS)
    for entry in config.cache_subdir("work").iterdir():
        try:
            expired = entry.is_dir() and entry.stat().st_mtime < cutoff
        except OSError:
            continue
        if expired and entry.name not in active:
            stitch_runner.discard_run(entry.name)


def _execute_local_published(plan: dict, exec_cfg: dict) -> dict:
    """Server mode: run locally in an isolated run directory, then hand back only
    Artifactory links. Local copies are removed once everything is uploaded, except
    that the run directory is kept for execution.run_retention_hours."""
    plan_id = plan.get("plan_id", "")
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.PLAN_INVALID, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    base = config.get_artifactory_upload_base()
    if not base["ok"]:
        return base
    run_id = f"{plan_id}-{uuid.uuid4().hex[:8]}"
    run_url = f"{base['data']['base_url']}/{run_id}"
    where = {"run_id": run_id, "artifactory_url": run_url}
    retention = exec_cfg.get("run_retention_hours", 0)

    with _ACTIVE_LOCK:
        _ACTIVE_RUNS.add(run_id)
    try:
        result = _execute_local(plan, exec_cfg, run_id=run_id)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.discard(run_id)
    if not result["ok"]:
        detail = dict(result["detail"])
        items = (detail.pop("deliverables", None) or {}).get("items") or []
        published = publish_deliverables(items, run_url) if items else ok({"items": []})
        if published["ok"]:
            detail.pop("full_log_path", None)
            detail["artifacts"] = published["data"]["items"]
            _finish_local_run(run_id, retention)
        else:
            detail["upload_error"] = published["message"]
        detail.update(where)
        return err(result["error_code"], result["message"], detail)

    data = result["data"]
    published = publish_deliverables(data["deliverables"]["items"], run_url)
    if not published["ok"]:
        published["detail"].update(where)
        return published
    artifacts = published["data"]["items"]
    stitched = next(a for a in artifacts if a["kind"] == "stitched_bin")

    run_info = stitch_runner.run_dir(run_id) / RUN_INFO_NAME
    run_info.parent.mkdir(parents=True, exist_ok=True)
    run_info.write_text(json.dumps({
        "plan_id": plan_id, "run_id": run_id, "command_line": data["command_line"],
        "exit_code": data["exit_code"], "fetched": data["environment"]["fetched"],
        "artifacts": artifacts, "warnings": data["warnings"]}, indent=2), encoding="utf-8")
    uploaded = artifactory.upload_file(str(run_info), f"{run_url}/{RUN_INFO_NAME}")
    if not uploaded["ok"]:
        uploaded["detail"].update(where)
        return uploaded

    _finish_local_run(run_id, retention)
    return ok({"plan_id": plan_id, **where,
               "stitched_bin": {key: stitched.get(key) for key in ("url", "sha256", "bytes")},
               "log_url": next((a["url"] for a in artifacts if a["kind"] == "log"), None),
               "warnings": data["warnings"]})


def _job_api(method: str, url: str, **kwargs) -> dict:
    try:
        resp = requests.request(method, url, headers=_auth_headers(),
                                timeout=_JOB_API_TIMEOUT, verify=False, **kwargs)
    except requests.RequestException as exc:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "could not reach the Job API",
                   {"url": url, "reason": str(exc)})
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and str(body.get("Message", "")).lower() == "unauthorized":
        return err(ErrorCode.AUTH_FAILED, "Job API rejected this caller",
                   {"url": url, "status": resp.status_code,
                    "job_api_code": body.get("Code"),
                    "has_exec_token": bool(config.get_execution_token())})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "Job API rejected the request",
                   {"url": url, "status": resp.status_code})
    if resp.status_code >= 400:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API returned an error",
                   {"url": url, "status": resp.status_code, "body": resp.text[:2000]})
    if body is None:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API returned invalid JSON",
                   {"url": url, "body": resp.text[:2000]})
    if not isinstance(body, dict):
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API returned an unexpected body",
                   {"url": url, "body": str(body)[:2000]})
    return ok(body)


def _pod_command(exec_cfg: dict, run_id: str, plan: dict, run_url: str) -> str:
    """PowerShell for the pod. Every statement stands alone (no cwd, no $vars, no quotes)
    so it behaves the same whether the Job API runs it via powershell, cmd or argv split."""
    checkout = f"C:/ifwi-mcp-{run_id}"
    return (f"powershell -NoProfile -Command "
            f"git clone --quiet --depth 1 --branch {exec_cfg['repo_ref']} "
            f"{exec_cfg['repo_url']} {checkout}; "
            f"python -m pip install --quiet --disable-pip-version-check {checkout}; "
            f"python -m ifwi_mcp.worker --plan-b64 {plan_mod.encode_plan(plan)} "
            f"--upload-url {run_url}")


def _create_job(endpoint: str, exec_cfg: dict, command: str) -> dict:
    created = _job_api("POST", endpoint, json={
        "image": exec_cfg["image"], "command": command, "os": _POD_OS,
        "activedeadlineseconds": exec_cfg["timeout_seconds"],
        "ttlsecondsafterfinished": _POD_TTL_SECONDS})
    if not created["ok"]:
        return created
    body = created["data"]
    job_id = (body.get("Data") or {}).get("jobid")
    if body.get("Code") != 0 or not job_id:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API did not create the stitch pod",
                   {"endpoint": endpoint, "body": body})
    return ok({"job_id": str(job_id)})


def _wait_job(endpoint: str, job_id: str, timeout_seconds: int, poll_interval: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    status = ""
    while True:
        polled = _job_api("GET", f"{endpoint}/{job_id}")
        if not polled["ok"]:
            return polled
        # A Code != 0 (e.g. job not visible yet) is treated as still pending.
        if polled["data"].get("Code") == 0:
            status = str((polled["data"].get("Data") or {}).get("status") or "").lower()
            if status in _JOB_SUCCEEDED or status in _JOB_FAILED:
                return ok({"status": status})
        if time.monotonic() >= deadline:
            return err(ErrorCode.REMOTE_TIMEOUT, "stitch pod did not finish in time",
                       {"status": status, "timeout_seconds": timeout_seconds})
        time.sleep(poll_interval)


def _read_remote_result(result_url: str, token: str) -> dict:
    try:
        resp = requests.get(result_url, headers={"Authorization": f"Bearer {token}"},
                            timeout=_JOB_API_TIMEOUT, verify=False)
    except requests.RequestException as exc:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "could not read the pod's result.json",
                   {"result_url": result_url, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                   {"service": "artifactory", "http_status": resp.status_code})
    if resp.status_code == 404:
        return err(ErrorCode.REMOTE_EXEC_FAILED,
                   "stitch pod ended without publishing result.json (clone/pip/startup failure?)",
                   {"result_url": result_url})
    if resp.status_code >= 400:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "could not read the pod's result.json",
                   {"result_url": result_url, "http_status": resp.status_code})
    try:
        result = resp.json()
    except ValueError:
        result = None
    if not isinstance(result, dict) or "ok" not in result:
        return err(ErrorCode.REMOTE_EXEC_FAILED, "pod result.json is not a result envelope",
                   {"result_url": result_url, "body": resp.text[:2000]})
    return ok(result)


def _execute_remote(plan: dict, exec_cfg: dict) -> dict:
    plan_id = plan.get("plan_id", "")
    endpoint = exec_cfg["endpoint"]
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.PLAN_INVALID, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    base = config.get_artifactory_upload_base()
    if not base["ok"]:
        return base
    token = config.get_artifactory_token()
    if not token["ok"]:
        return token

    run_id = f"{plan_id}-{uuid.uuid4().hex[:8]}"
    run_url = f"{base['data']['base_url']}/{run_id}"
    result_url = f"{run_url}/{REMOTE_RESULT_NAME}"
    created = _create_job(endpoint, exec_cfg, _pod_command(exec_cfg, run_id, plan, run_url))
    if not created["ok"]:
        return created
    job_id = created["data"]["job_id"]
    where = {"job_id": job_id, "endpoint": endpoint, "artifactory_url": run_url}

    waited = _wait_job(endpoint, job_id, exec_cfg["timeout_seconds"],
                       exec_cfg["poll_interval_seconds"])
    if not waited["ok"]:
        waited["detail"].update(where)
        return waited

    read = _read_remote_result(result_url, token["data"]["token"])
    if not read["ok"]:
        read["detail"].update(where, job_status=waited["data"]["status"])
        return read
    result = read["data"]

    if not result["ok"]:
        detail = dict(result.get("detail") or {})
        detail.update(where, result_url=result_url)
        return err(result.get("error_code") or ErrorCode.STITCH_RUN_FAILED,
                   result.get("message") or "remote stitch failed", detail)

    data = result.get("data") or {}
    artifacts = data.get("deliverables") or []
    stitched_url = next((a.get("url") for a in artifacts
                         if a.get("kind") == "stitched_bin" and a.get("url")), None)
    if not stitched_url:
        return err(ErrorCode.DELIVERABLE_MISSING,
                   "remote stitch reported success without an uploaded stitched binary",
                   {**where, "result_url": result_url, "artifacts": artifacts})

    fetched = deliverables.fetch(plan_id, artifacts, token["data"]["token"])
    if not fetched["ok"]:
        fetched["detail"].update(where, artifacts=artifacts)
        return fetched
    stitched = next((item["path"] for item in fetched["data"]["items"]
                     if item["kind"] == "stitched_bin"), None)
    log = next((item["path"] for item in fetched["data"]["items"]
                if item["kind"] == "log"), None)
    return ok({"plan_id": plan_id, "mode": "remote", **where,
               "result_url": result_url,
               "stitched_bin_url": stitched_url, "artifacts": artifacts,
               "command_line": data.get("command_line"),
               "exit_code": data.get("exit_code", 0),
               "stitched_bin": stitched, "log_path": log,
               "environment": data.get("environment", {}),
               "deliverables": fetched["data"],
               "warnings": data.get("warnings", [])})
