"""Execute a stitch plan: prepare the environment, run the command, return results.

The execution.mode switch in the config file decides where the work happens:

* local  - download/extract everything into the cache, then run cli.py in a private
           per-run directory holding its own copy of the tool, venv and inputs. In
           server mode the deliverables are uploaded to Artifactory and returned as links.
* remote - create a Linux pod through the FIV Job API. The pod runs the image's
           /app/stitch_wrapper.py, which installs this repo and runs ifwi_mcp.worker. The
           worker reuses this exact local-execution codepath, uploads the deliverables to
           Artifactory and writes result.json next to them; the wrapper then calls the server
           back once. jobs.JobManager tracks the run (callback first, result.json as the
           fallback); this module only holds the Job API and Artifactory primitives.

Either way the caller gets the same result shape: the exact command line that ran,
the exit code, and a deliverables manifest (or Artifactory links in server mode).
"""
import hashlib
import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

from . import archive, artifactory, config, deliverables, plan as plan_mod, prepare, stitch_runner
from .result import ok, err, ErrorCode

REMOTE_RESULT_NAME = "result.json"
REMOTE_DELIVERABLES_DIR = "deliverables"
RUN_INFO_NAME = "run.json"
SPEC_NAME = "spec.json"
POD_LOG_NAME = "pod.log"
WRAPPER_SPEC_VERSION = 1
# Margin for clock skew between the server and the pod in spec.timeouts.deadline_epoch.
CLOCK_SKEW_SECONDS = 30
_JOB_API_TIMEOUT = 10
_CREATE_TIMEOUT = (10, 30)
# The create call may or may not have created a pod when it answers with these.
_CREATE_UNCERTAIN_STATUS = (429, 502, 503, 504)
_POD_OS = "linux"
_POD_TTL_SECONDS = 12 * 3600
_JOB_PENDING = ("pending", "waiting", "created", "")
_JOB_RUNNING = ("running", "active")
_JOB_SUCCEEDED = ("succeeded", "complete", "completed")
_JOB_FAILED = ("failed", "error")
_POD_FINISHED = ("failed", "succeeded")
_ACTIVE_RUNS = set()
_ACTIVE_LOCK = threading.Lock()


def _auth_headers() -> dict:
    token = config.get_execution_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _custom_key(source: dict) -> str:
    """Caller-supplied links often reuse release file names; key their cache by URL."""
    if not source.get("custom"):
        return ""
    return "custom-" + hashlib.sha256(source["url"].encode()).hexdigest()[:16]


def _cache_category(category: str, source: dict) -> str:
    key = _custom_key(source)
    return f"{category}/{key}" if key else category


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
                dl = artifactory.download(ifwi["url"], category=_cache_category("ifwi", ifwi))
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
                dl = artifactory.download(ingredient["url"],
                                          category=_cache_category("ingredients", ingredient))
                if not dl["ok"]:
                    return dl
                local_path = dl["data"]["local_path"]
                key = _custom_key(ingredient)
                extracted = (archive.extract_archive(
                    local_path, f"{archive.cache_name(Path(local_path).name)}-{key}")
                             if key else archive.extract_archive(local_path))
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
    """Run the plan wherever execution.mode points and block until it has a result.

    Remote runs go through the job manager (submit, then wait for the callback or the
    result.json fallback); local runs execute right here.
    """
    cfg = config.get_execution_config()
    if not cfg["ok"]:
        return cfg
    if cfg["data"]["mode"] == "remote":
        from . import jobs
        return jobs.get_manager().run_plan(plan)
    return execute_local(plan, cfg["data"])


def execute_local(plan: dict, exec_cfg: dict) -> dict:
    """Local execution; server mode publishes the deliverables to Artifactory."""
    serving = config.get_serve_config()
    if not serving["ok"]:
        return serving
    if serving["data"]["mode"] == "server":
        return _execute_local_published(plan, exec_cfg)
    return _execute_local(plan, exec_cfg)


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


# ---- remote execution primitives --------------------------------------------------------
# The job lifecycle (submit, refresh, callback, finalize) lives in jobs.JobManager; these
# helpers only talk to the Job API and Artifactory and never block for a whole run.

def _job_api(method: str, url: str, timeout=_JOB_API_TIMEOUT, **kwargs) -> dict:
    try:
        resp = requests.request(method, url, headers=_auth_headers(),
                                timeout=timeout, verify=False, **kwargs)
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
                   {"url": url, "status": resp.status_code, "body": resp.text[:2000]})
    if not isinstance(body, dict):
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API returned an unexpected body",
                   {"url": url, "body": str(body)[:2000]})
    return ok(body)


def _never_sent(exc: Exception) -> bool:
    """True when the request certainly never reached the server (no TCP connection)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    seen, stack = set(), [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (urllib3.exceptions.NewConnectionError,
                                urllib3.exceptions.ConnectTimeoutError)):
            return True
        stack.extend([getattr(current, "reason", None), current.__cause__, current.__context__,
                      *[arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException)]])
    return False


def create_pod(endpoint: str, exec_cfg: dict, command: str) -> dict:
    """POST the pod. outcome: created | rejected | not_sent | unknown (design §8.2 E4-E6)."""
    url = endpoint
    payload = {"image": exec_cfg["image"], "command": command, "os": _POD_OS,
               "activedeadlineseconds": exec_cfg["timeout_seconds"],
               "ttlsecondsafterfinished": _POD_TTL_SECONDS}
    try:
        resp = requests.request("POST", url, headers=_auth_headers(), json=payload,
                                timeout=_CREATE_TIMEOUT, verify=False)
    except requests.RequestException as exc:
        detail = {"endpoint": endpoint, "reason": str(exc)}
        if _never_sent(exc):
            return {"outcome": "not_sent",
                    "result": err(ErrorCode.REMOTE_SUBMIT_FAILED, "could not reach the Job API", detail)}
        return {"outcome": "unknown",
                "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN,
                              "the Job API did not answer the create request; a pod may exist", detail)}
    try:
        body = resp.json()
    except ValueError:
        body = None
    detail = {"endpoint": endpoint, "status": resp.status_code,
              "body": body if isinstance(body, dict) else resp.text[:2000]}
    if resp.status_code in (401, 403) or (
            isinstance(body, dict) and str(body.get("Message", "")).lower() == "unauthorized"):
        detail["has_exec_token"] = bool(config.get_execution_token())
        return {"outcome": "rejected",
                "result": err(ErrorCode.AUTH_FAILED, "Job API rejected this caller", detail)}
    if resp.status_code in _CREATE_UNCERTAIN_STATUS or (resp.status_code >= 500 and not isinstance(body, dict)):
        return {"outcome": "unknown",
                "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN,
                              "the Job API create result is uncertain; a pod may exist", detail)}
    if not isinstance(body, dict):
        if resp.status_code < 300:
            return {"outcome": "unknown",
                    "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN,
                                  "the Job API answered without JSON; a pod may exist", detail)}
        return {"outcome": "rejected",
                "result": err(ErrorCode.REMOTE_SUBMIT_FAILED, "Job API returned an error", detail)}
    job_id = (body.get("Data") or {}).get("jobid") if isinstance(body.get("Data"), dict) else None
    if resp.status_code < 300 and body.get("Code") == 0 and job_id:
        return {"outcome": "created", "pod_job_id": str(job_id)}
    if resp.status_code < 300 and body.get("Code") == 0:
        return {"outcome": "unknown",
                "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN,
                              "the Job API accepted the request without a job id", detail)}
    return {"outcome": "rejected",
            "result": err(ErrorCode.REMOTE_SUBMIT_FAILED, "Job API did not create the stitch pod", detail)}


def job_api_get(endpoint: str, pod_job_id: str) -> dict:
    """Classify the pod: pending | running | ended | unknown | not_found | error (design §6.2)."""
    polled = _job_api("GET", f"{endpoint}/{pod_job_id}")
    if not polled["ok"]:
        return {"kind": "error", "status": None, "auth": polled["error_code"] == ErrorCode.AUTH_FAILED,
                "error": polled}
    body = polled["data"]
    if body.get("Code") != 0:
        message = str(body.get("Message") or "")
        if "not found" in message.lower():
            return {"kind": "not_found", "status": None, "auth": False, "message": message[:500]}
        return {"kind": "error", "status": None, "auth": False,
                "error": err(ErrorCode.REMOTE_EXEC_FAILED, "Job API returned an error code",
                             {"code": body.get("Code"), "message": message[:500]})}
    data = body.get("Data") or {}
    status = str(data.get("status") or "").strip().lower()
    pods = data.get("pods") if isinstance(data.get("pods"), list) else []
    pod_states = [str((p or {}).get("status") or "").strip().lower() for p in pods]
    if status in _JOB_SUCCEEDED or status in _JOB_FAILED:
        kind = "ended"
    elif pod_states and all(s in _POD_FINISHED for s in pod_states):
        # The Linux Job API keeps a job whose only pod failed "active" (backoffLimit=0, so
        # nothing will retry it); the pod list is the reliable signal that it is over.
        kind, status = "ended", f"{status or 'unknown'}/pod-{pod_states[-1]}"
    elif status in _JOB_PENDING:
        kind = "pending"
    elif status in _JOB_RUNNING:
        kind = "running"
    else:
        kind = "unknown"
    # It later reports such a job as "pending" with no pods at all. A job whose pod could
    # not be created yet (namespace quota full) looks the same, so jobs.py only treats it
    # as ended once a pod was listed earlier (see JobManager._classify).
    return {"kind": kind, "status": status, "auth": False,
            "no_pods": kind == "pending" and not pods, "has_pods": bool(pods)}


def job_api_delete(endpoint: str, pod_job_id: str) -> dict:
    deleted = _job_api("DELETE", f"{endpoint}/{pod_job_id}")
    if deleted["ok"]:
        body = deleted["data"]
        if body.get("Code") in (0, None) or "not found" in str(body.get("Message", "")).lower():
            return ok({"pod_job_id": pod_job_id})
        return err(ErrorCode.REMOTE_EXEC_FAILED, "Job API refused to delete the pod",
                   {"pod_job_id": pod_job_id, "body": body})
    if 200 <= int(deleted["detail"].get("status") or 0) < 300:
        return ok({"pod_job_id": pod_job_id})       # 204 No Content has no JSON body
    return deleted


def read_result_json(run_url: str) -> dict:
    """GET <run_url>/result.json. kind: found | missing | invalid | auth | error."""
    url = f"{run_url}/{REMOTE_RESULT_NAME}"
    token = config.get_artifactory_token()
    if not token["ok"]:
        return {"kind": "auth", "url": url, "error": token}
    try:
        resp = requests.get(url, headers={"Authorization": "Bearer " + token["data"]["token"]},
                            timeout=_JOB_API_TIMEOUT, verify=False)
    except requests.RequestException as exc:
        return {"kind": "error", "url": url, "reason": str(exc)}
    if resp.status_code in (401, 403):
        return {"kind": "auth", "url": url, "http_status": resp.status_code}
    if resp.status_code == 404:
        return {"kind": "missing", "url": url}
    if resp.status_code >= 400:
        return {"kind": "error", "url": url, "http_status": resp.status_code}
    try:
        body = resp.json()
    except ValueError:
        return {"kind": "invalid", "url": url, "body": resp.text[:2000]}
    if not is_envelope(body):
        return {"kind": "invalid", "url": url, "body": str(body)[:2000]}
    return {"kind": "found", "url": url, "envelope": body}


def is_envelope(value) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return False
    if value["ok"]:
        return isinstance(value.get("data"), dict)
    return isinstance(value.get("error_code"), str)


def build_spec(*, job_id: str, run_id: str, run_url: str, report_url, plan: dict,
               exec_cfg: dict, submitted_at: float) -> dict:
    """The stitch_wrapper spec (contract protocol 1). Never holds a credential."""
    deadline_epoch = int(submitted_at + exec_cfg["timeout_seconds"] - CLOCK_SKEW_SECONDS)
    return {"spec_version": WRAPPER_SPEC_VERSION, "job_id": job_id, "run_id": run_id,
            "run_url": run_url, "report_url": report_url or None,
            "worker": {"repo_url": exec_cfg["repo_url"], "repo_ref": exec_cfg["repo_ref"]},
            "timeouts": {"budget_seconds": exec_cfg["timeout_seconds"],
                         "deadline_epoch": deadline_epoch},
            "plan": plan}


def wrapper_command(exec_cfg: dict, job_id: str, *, spec_token: str = "", spec_url: str = "",
                    report_token: str = "") -> str:
    """`exec python3 /app/stitch_wrapper.py ...` with values limited to [A-Za-z0-9_.:/=-], so
    nothing needs quoting.

    The Linux Job API runs the command as `sh -c "<command>"` with that sh as PID 1. A PID 1
    sh does not handle SIGTERM, so without `exec` a pod delete or deadline would never reach
    the wrapper and it could not report. `exec` makes the wrapper itself PID 1.
    """
    parts = ["exec", "python3", exec_cfg["wrapper_path"], "--job-id", job_id]
    parts += ["--spec-url", spec_url] if spec_url else ["--spec", spec_token]
    if report_token:
        parts += ["--report-token", report_token]
    return " ".join(parts)


def upload_spec(spec: dict, run_url: str) -> dict:
    url = f"{run_url}/{SPEC_NAME}"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / SPEC_NAME
        path.write_text(json.dumps(spec), encoding="utf-8")
        uploaded = artifactory.upload_file(str(path), url)
    if not uploaded["ok"]:
        code = uploaded["error_code"]
        return err(code if code in (ErrorCode.AUTH_FAILED, ErrorCode.MISSING_TOKEN) else ErrorCode.UPLOAD_FAILED,
                   "could not upload the wrapper spec", {**uploaded["detail"], "url": url})
    return ok({"url": url})


def shape_remote_success(envelope: dict, where: dict) -> dict:
    """Map a worker's ok envelope to the public remote result (design §6.5)."""
    data = envelope.get("data") or {}
    artifacts = [a for a in (data.get("deliverables") or []) if isinstance(a, dict)]
    stitched = next((a for a in artifacts if a.get("kind") == "stitched_bin" and a.get("url")), None)
    if stitched is None:
        return err(ErrorCode.DELIVERABLE_MISSING,
                   "remote stitch reported success without an uploaded stitched binary",
                   {**where, "artifacts": artifacts})
    environment = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    return ok({"plan_id": where.get("plan_id"), "run_id": where.get("run_id"), "mode": "remote",
               "artifactory_url": where.get("artifactory_url"), "result_url": where.get("result_url"),
               "pod_job_id": where.get("pod_job_id"), "pod_log_url": where.get("pod_log_url"),
               "stitched_bin": {key: stitched.get(key) for key in ("url", "sha256", "bytes")},
               "log_url": next((a.get("url") for a in artifacts if a.get("kind") == "log"), None),
               "warnings": list(data.get("warnings") or []),
               # v1 compatibility aliases; job_id here is the pod's id, not the task's.
               "job_id": where.get("pod_job_id"), "endpoint": where.get("endpoint"),
               "stitched_bin_url": stitched["url"],
               "artifacts": [{key: a.get(key) for key in ("name", "url", "kind")} for a in artifacts],
               "command_line": data.get("command_line"), "exit_code": data.get("exit_code", 0),
               "environment": {"fetched": environment.get("fetched", [])}})


def fetch_remote_deliverables(shaped: dict) -> dict:
    """stdio mode: download a remote run's deliverables so stitch_get_deliverables works."""
    data = dict(shaped)
    token = config.get_artifactory_token()
    fetched = deliverables.fetch(data["plan_id"], data["artifacts"],
                                 token["data"]["token"] if token["ok"] else "")
    if not fetched["ok"]:
        fetched["detail"].update({key: data.get(key) for key in
                                  ("run_id", "pod_job_id", "artifactory_url", "stitched_bin_url")})
        return fetched
    items = fetched["data"]["items"]
    data["stitched_bin"] = next((i["path"] for i in items if i["kind"] == "stitched_bin"), None)
    data["log_path"] = next((i["path"] for i in items if i["kind"] == "log"), None)
    data["deliverables"] = fetched["data"]
    return ok(data)


