# tests/test_executor.py
import json
from pathlib import Path

import pytest

from ifwi_mcp import deliverables, executor, plan, stitch_runner
from ifwi_mcp.result import ok, err, ErrorCode


def _plan(tmp_path, **overrides):
    base = {
        "plan_version": 1,
        "plan_id": "plan-test",
        "project": "DMR", "fiv_project_id": "42", "phase": "Orange", "version": "2026.30.2.01", "swimlane": None,
        "sources": {
            "ifwi": {"kind": "fiv", "project": "DMR", "phase": "Orange",
                     "version": "2026.30.2.01", "swimlane": None, "binary_name": None,
                     "from_build_targets": False},
            "ingredients": [{"kind": "fiv", "project": "DMR", "name": "MMC1",
                            "version": "0.907.0"}],
            "stitch_tool": {"kind": "fiv", "project": "DMR", "phase": "Orange",
                            "version": "2026.30.2.01", "swimlane": None},
        },
        "stitch": {"ingredient_names": ["MMC1"], "config_ini": "Config_Stitch_DMR",
                   "soft_strap": None},
        "command_template": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def env_fakes(monkeypatch, tmp_path):
    """Fake the three preparation steps so no network or venv is needed."""
    state = {"prepared": [], "ifwi_ok": True, "stitch_prepared": True}
    ingredient_dir = tmp_path / "ingredient"; ingredient_dir.mkdir()
    binary = tmp_path / "base.bin"; binary.write_bytes(b"b")

    def fake_prepare_ifwi(project, phase, version, swimlane=None, binary_name=None,
                          from_build_targets=False):
        state["prepared"].append("ifwi")
        state["from_build_targets"] = from_build_targets
        if not state["ifwi_ok"]:
            return err(ErrorCode.IFWI_BINARY_AMBIGUOUS, "pick one", {"candidates": ["a", "b"]})
        return ok({"binary_file": str(binary), "source": "release_report",
                   "ifwi_url": "https://art/ifwi.bin", "binaries": ["base.bin"],
                   "swimlane_branch": "main", "warnings": []})

    def fake_prepare_ingredient(project, name, version):
        state["prepared"].append("ingredient")
        return ok({"ingredient_dir": str(ingredient_dir), "extracted_files": [],
                   "ingredient_url": "https://art/ing.7z", "ingredient_name": name,
                   "ingredient_version": version, "warnings": []})

    def fake_prepare_stitch(project, phase, version=None, swimlane=None, search_neighbors=False):
        state["prepared"].append("stitch_tool")
        assert search_neighbors is True
        if not state["stitch_prepared"]:
            return ok({"prepared": False, "selected_version": None,
                       "candidates": [{"version": "2026.30.4.01", "stitch_url": "u"}],
                       "warnings": []})
        return ok({"prepared": True, "selected_version": version,
                   "stitch_dir": str(tmp_path / "tool"),
                   "tool_root": str(tmp_path / "tool"),
                   "venv_python": str(tmp_path / "tool" / "venv" / "python"),
                   "config_targets": ["Config_Stitch_DMR"], "warnings": ["tool warn"]})

    monkeypatch.setattr(executor.prepare, "prepare_ifwi", fake_prepare_ifwi)
    monkeypatch.setattr(executor.prepare, "prepare_ingredient", fake_prepare_ingredient)
    monkeypatch.setattr(executor.prepare, "prepare_stitch", fake_prepare_stitch)
    state["binary"] = binary
    state["ingredient_dir"] = ingredient_dir
    return state


def test_prepare_environment_resolves_all_three_sources(clean_env, env_fakes, tmp_path):
    r = executor.prepare_environment(_plan(tmp_path))
    assert r["ok"] is True
    assert env_fakes["prepared"] == ["ifwi", "ingredient", "stitch_tool"]
    assert r["data"]["binary_file"] == str(env_fakes["binary"])
    assert r["data"]["ingredients"] == [{"name": "MMC1", "path": str(env_fakes["ingredient_dir"])}]
    assert [f["source"] for f in r["data"]["fetched"]] == ["ifwi", "ingredient"]
    assert "tool warn" in r["data"]["warnings"]


def test_prepare_environment_uses_local_sources(clean_env, env_fakes, tmp_path):
    tool = tmp_path / "tool"; tool.mkdir()
    (tool / "cli.py").write_text("")
    (tool / "config").mkdir()
    (tool / "config" / "Config_Stitch_DMR.ini").write_text("")
    p = _plan(tmp_path)
    p["sources"] = {
        "ifwi": {"kind": "local", "local_path": str(env_fakes["binary"])},
        "ingredients": [{"kind": "local", "name": "MMC1",
                        "local_path": str(env_fakes["ingredient_dir"])}],
        "stitch_tool": {"kind": "local", "local_path": str(tool)},
    }
    r = executor.prepare_environment(p)
    assert r["ok"] is True
    assert env_fakes["prepared"] == []          # nothing downloaded
    assert r["data"]["stitch_dir"] == str(tool)
    assert r["data"]["config_targets"] == ["Config_Stitch_DMR"]


def test_prepare_environment_reports_missing_local_file(clean_env, env_fakes, tmp_path):
    p = _plan(tmp_path)
    p["sources"]["ifwi"] = {"kind": "local", "local_path": str(tmp_path / "gone.bin")}
    assert executor.prepare_environment(p)["error_code"] == ErrorCode.ENV_PREPARE_FAILED


def test_prepare_environment_surfaces_stitch_candidates(clean_env, env_fakes, tmp_path):
    env_fakes["stitch_prepared"] = False
    r = executor.prepare_environment(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.ENV_PREPARE_FAILED
    assert r["detail"]["candidates"][0]["version"] == "2026.30.4.01"


def test_worker_uses_pinned_urls_without_fiv_lookups(clean_env, monkeypatch, tmp_path):
    p = _plan(tmp_path)
    p["sources"]["ifwi"]["url"] = "https://art/ifwi.bin"
    p["sources"]["ingredients"][0]["url"] = "https://art/mmc.7z"
    p["sources"]["stitch_tool"]["url"] = "https://art/tool.7z"
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"b")
    ingredient = tmp_path / "mmc"; ingredient.mkdir()
    calls = []

    def download(url, category):
        calls.append((url, category))
        return ok({"local_path": str(binary)})

    monkeypatch.setattr(executor.artifactory, "download", download)
    monkeypatch.setattr(executor.archive, "extract_archive",
                        lambda path: ok({"extract_dir": str(ingredient)}))
    monkeypatch.setattr(executor.stitch_runner, "extract_stitch_tool", lambda path: ok({
        "stitch_dir": str(tmp_path), "tool_root": str(tmp_path),
        "venv_python": "python", "config_targets": ["Config_Stitch_DMR"], "warnings": []}))
    for name in ("prepare_ifwi", "prepare_ingredient", "prepare_stitch"):
        monkeypatch.setattr(executor.prepare, name,
                            lambda *args, **kwargs: pytest.fail("worker must not re-query FIV"))
    result = executor.prepare_environment(p)
    assert result["ok"] is True
    assert calls == [("https://art/ifwi.bin", "ifwi"),
                     ("https://art/mmc.7z", "ingredients"),
                     ("https://art/tool.7z", "stitch")]


def test_custom_urls_get_their_own_cache_dirs(clean_env, monkeypatch, tmp_path):
    p = _plan(tmp_path)
    p["sources"]["ifwi"].update({"url": "https://art/dev/ifwi.bin", "custom": True})
    p["sources"]["ingredients"][0].update({"url": "https://art/dev/mmc.7z", "custom": True})
    p["sources"]["stitch_tool"] = {"kind": "local", "local_path": str(tmp_path / "tool.7z")}
    (tmp_path / "tool.7z").write_bytes(b"t")
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"b")
    ingredient = tmp_path / "mmc"; ingredient.mkdir()
    categories, extract_names = [], []

    def download(url, category):
        categories.append(category)
        return ok({"local_path": str(binary if category.startswith("ifwi") else tmp_path / "mmc.7z")})

    def extract(path, dest_name=None):
        extract_names.append(dest_name)
        return ok({"extract_dir": str(ingredient)})

    monkeypatch.setattr(executor.artifactory, "download", download)
    monkeypatch.setattr(executor.archive, "extract_archive", extract)
    monkeypatch.setattr(executor.stitch_runner, "extract_stitch_tool", lambda path: ok({
        "stitch_dir": str(tmp_path), "tool_root": str(tmp_path),
        "venv_python": "python", "config_targets": [], "warnings": []}))
    assert executor.prepare_environment(p)["ok"] is True
    assert categories[0].startswith("ifwi/custom-")
    assert categories[1].startswith("ingredients/custom-")
    # Extraction is keyed by URL too, or a same-named archive would reuse old contents.
    assert extract_names[0].startswith("mmc-custom-")


def test_worker_auto_config_uses_extracted_targets(clean_env, env_fakes, run_fakes,
                                                   monkeypatch, tmp_path):
    p = _plan(tmp_path)
    p["stitch"]["auto_config"] = True
    p["sources"]["ifwi"]["binary_name"] = "only"
    seen = []
    original = executor.stitch_runner.build_stitch_command

    def build(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None):
        seen.append(config_ini)
        return original(stitch_dir, binary_file, ingredients, config_ini, soft_strap)

    monkeypatch.setattr(executor.stitch_runner, "build_stitch_command", build)
    executor._execute_local(p, {"timeout_seconds": 10})
    assert seen == ["Config_Stitch_DMR"]  # sole target, even when hint differs


@pytest.fixture
def run_fakes(monkeypatch, tmp_path):
    output = tmp_path / "tool" / "output"; output.mkdir(parents=True)
    stitched = output / "stitched.bin"; stitched.write_bytes(b"out")
    extra = output / "report.txt"; extra.write_text("r")
    log = tmp_path / "plan-test.log"; log.write_text("log body")
    state = {"argv": None, "timeout": None, "stitched": stitched, "log": log,
             "fail": False}

    def fake_build(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None):
        return ok({"argv": ["py", "cli.py", "--binary_file", binary_file],
                   "command_line": "py cli.py --binary_file " + binary_file,
                   "cwd": str(tmp_path / "tool"), "config_ini": config_ini,
                   "ingredient_args": [i["path"] for i in ingredients],
                   "warnings": ["ing warn"]})

    def fake_execute(argv, cwd, log_name=None, timeout=None, run_root=None):
        state["argv"], state["timeout"], state["log_name"] = argv, timeout, log_name
        state["run_root"] = run_root
        if state["fail"]:
            return err(ErrorCode.STITCH_RUN_FAILED, "boom",
                       {"exit_code": 2, "full_log_path": str(log), "log_tail": "bad"})
        return ok({"stitched_bin": str(stitched), "log_path": str(log),
                   "output_dir": str(output), "exit_code": 0,
                   "command_line": " ".join(argv)})

    monkeypatch.setattr(executor.stitch_runner, "build_stitch_command", fake_build)
    monkeypatch.setattr(executor.stitch_runner, "execute_command", fake_execute)
    return state


def test_execute_local_returns_command_and_deliverables(clean_env, env_fakes,
                                                        run_fakes, tmp_path):
    r = executor.execute(_plan(tmp_path))
    assert r["ok"] is True
    assert r["data"]["mode"] == "local"
    assert r["data"]["exit_code"] == 0
    assert r["data"]["command_line"].startswith("py cli.py")
    kinds = sorted(item["kind"] for item in r["data"]["deliverables"]["items"])
    assert kinds == ["log", "output", "stitched_bin"]
    assert r["data"]["deliverables"]["archive"].endswith("plan-test.zip")
    assert "ing warn" in r["data"]["warnings"] and "tool warn" in r["data"]["warnings"]
    assert run_fakes["log_name"] == r["data"]["run_id"]
    assert r["data"]["run_id"].startswith("plan-test-")
    assert run_fakes["run_root"] == str(stitch_runner.run_dir(r["data"]["run_id"]))


def test_execute_local_runs_on_private_copies(clean_env, env_fakes, run_fakes,
                                              monkeypatch, tmp_path):
    seen = []
    fake_build = executor.stitch_runner.build_stitch_command

    def build(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None):
        seen.append((stitch_dir, binary_file, ingredients))
        return fake_build(stitch_dir, binary_file, ingredients, config_ini, soft_strap)

    monkeypatch.setattr(executor.stitch_runner, "build_stitch_command", build)
    first, second = executor.execute(_plan(tmp_path)), executor.execute(_plan(tmp_path))
    assert first["ok"] and second["ok"]
    assert first["data"]["run_id"] != second["data"]["run_id"]
    for result, (stitch_dir, binary_file, ingredients) in zip((first, second), seen):
        root = stitch_runner.run_dir(result["data"]["run_id"])
        assert Path(stitch_dir).is_relative_to(root)
        assert Path(binary_file).is_relative_to(root) and Path(binary_file).read_bytes() == b"b"
        assert Path(ingredients[0]["path"]).is_relative_to(root)
        assert Path(ingredients[0]["path"]).is_dir()


@pytest.fixture
def server_local(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "secret")
    monkeypatch.setenv("IFWI_MCP_ARTIFACTORY_REPO", "https://af.example.com/repo/Stitch/")
    uploads = {}

    def fake_upload(local_path, url):
        uploads[url] = Path(local_path).read_bytes()
        return ok({"url": url})

    monkeypatch.setattr(executor.artifactory, "upload_file", fake_upload)
    return uploads


def test_server_mode_local_execution_returns_artifactory_links(server_local, env_fakes,
                                                               run_fakes, tmp_path):
    r = executor.execute(_plan(tmp_path))
    assert r["ok"] is True, r
    data = r["data"]
    run_url = f"https://af.example.com/repo/Stitch/{data['run_id']}"
    assert set(data) == {"plan_id", "run_id", "artifactory_url", "stitched_bin",
                         "log_url", "warnings"}
    assert data["artifactory_url"] == run_url
    assert data["stitched_bin"]["url"] == f"{run_url}/deliverables/stitched.bin"
    assert data["stitched_bin"]["sha256"] and data["stitched_bin"]["bytes"] == 3
    assert server_local[data["stitched_bin"]["url"]] == b"out"
    assert data["log_url"] == f"{run_url}/deliverables/plan-test.log"
    run_info = json.loads(server_local[f"{run_url}/run.json"])
    assert run_info["command_line"].startswith("py cli.py")
    assert {a["kind"] for a in run_info["artifacts"]} == {"stitched_bin", "log", "output"}
    assert run_info["fetched"] and run_info["warnings"] == data["warnings"]
    assert not stitch_runner.run_dir(data["run_id"]).exists()
    assert not deliverables.deliverables_dir(data["run_id"]).exists()


def test_server_mode_retention_keeps_run_and_prunes_expired(server_local, env_fakes, run_fakes,
                                                            monkeypatch, tmp_path):
    import os, time
    monkeypatch.setenv("IFWI_MCP_RUN_RETENTION_HOURS", "1")
    expired = stitch_runner.run_dir("plan-old-run"); expired.mkdir(parents=True)
    in_flight = stitch_runner.run_dir("plan-busy-run"); in_flight.mkdir(parents=True)
    two_hours_ago = time.time() - 7200
    for path in (expired, in_flight):
        os.utime(path, (two_hours_ago, two_hours_ago))
    executor._ACTIVE_RUNS.add("plan-busy-run")
    try:
        r = executor.execute(_plan(tmp_path))
    finally:
        executor._ACTIVE_RUNS.discard("plan-busy-run")
    assert r["ok"] is True, r
    kept = stitch_runner.run_dir(r["data"]["run_id"])
    assert kept.name == r["data"]["run_id"]
    assert (kept / "tool").is_dir() and (kept / "inputs" / "ingredients" / "0_MMC1").is_dir()
    assert not deliverables.deliverables_dir(r["data"]["run_id"]).exists()
    assert not expired.exists()
    assert in_flight.exists()


def test_server_mode_local_failure_returns_log_link(server_local, env_fakes, run_fakes,
                                                    tmp_path):
    run_fakes["fail"] = True
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["artifacts"][0]["url"].endswith("/deliverables/plan-test.log")
    assert "full_log_path" not in r["detail"] and "deliverables" not in r["detail"]
    assert r["detail"]["run_id"].startswith("plan-test-")


def test_server_mode_local_upload_failure_keeps_local_files(server_local, env_fakes,
                                                            run_fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.artifactory, "upload_file",
                        lambda path, url: err(ErrorCode.UPLOAD_FAILED, "nope", {"url": url}))
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.UPLOAD_FAILED
    assert deliverables.deliverables_dir(r["detail"]["run_id"]).is_dir()


def test_server_mode_local_requires_artifactory_repo(server_local, env_fakes, monkeypatch,
                                                     tmp_path):
    monkeypatch.delenv("IFWI_MCP_ARTIFACTORY_REPO")
    r = executor.execute(_plan(tmp_path))
    assert r["detail"]["param"] == "execution.artifactory_repo"
    assert env_fakes["prepared"] == []


def test_execute_local_honours_timeout_from_config(clean_env, env_fakes, run_fakes,
                                                   write_config, tmp_path):
    write_config({"execution": {"mode": "local", "timeout_seconds": 42}})
    executor.execute(_plan(tmp_path))
    assert run_fakes["timeout"] == 42


def test_execute_local_failure_still_keeps_the_log(clean_env, env_fakes, run_fakes,
                                                   tmp_path):
    run_fakes["fail"] = True
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["deliverables"]["items"][0]["kind"] == "log"


def test_execute_plan_id_loads_the_saved_plan(clean_env, env_fakes, run_fakes, tmp_path):
    built = plan.build_plan("DMR", "Orange", [{"name": "MMC1", "version": "0.907.0"}],
                            "Config_Stitch_DMR", version="2026.30.2.01")
    r = executor.execute_plan_id(built["data"]["plan_id"])
    assert r["ok"] is True
    assert r["data"]["plan_id"] == built["data"]["plan_id"]


def test_execute_plan_id_unknown(clean_env):
    assert executor.execute_plan_id("plan-nope")["error_code"] == ErrorCode.PLAN_NOT_FOUND




class _Resp:
    def __init__(self, payload, status_code=200, text=None):
        self._payload, self.status_code = payload, status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def remote_env(clean_env, write_config, monkeypatch):
    write_config({"execution": {"mode": "remote", "token": "secret",
                                "endpoint": "https://jobs.example.com/v1/fivportal/job",
                                "artifactory_repo": "https://af.example.com/repo/RemoteStitch/",
                                "timeout_seconds": 3600}})
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "af-secret")
    return executor.config.get_execution_config()["data"]


def _post_returning(monkeypatch, response=None, raises=None):
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "headers": headers,
                      "timeout": timeout})
        if raises is not None:
            raise raises
        return response
    monkeypatch.setattr(executor.requests, "request", fake_request)
    return calls


def test_create_pod_posts_a_linux_job(remote_env, monkeypatch):
    calls = _post_returning(monkeypatch, _Resp({"Code": 0, "Data": {"jobid": "fivportal-abc"}}))
    created = executor.create_pod(remote_env["endpoint"], remote_env, "python3 /app/stitch_wrapper.py")
    assert created == {"outcome": "created", "pod_job_id": "fivportal-abc"}
    payload = calls[0]["json"]
    assert payload["os"] == "linux" and payload["activedeadlineseconds"] == 3600
    assert payload["ttlsecondsafterfinished"] == 43200
    assert payload["image"].endswith("fiv-ubuntu24.04-python3.13:v1.1.1")
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.parametrize("response, outcome, code", [
    (_Resp({"Code": 1, "Message": "CreateFail"}), "rejected", ErrorCode.REMOTE_SUBMIT_FAILED),
    (_Resp({"error": "bad"}, status_code=400), "rejected", ErrorCode.REMOTE_SUBMIT_FAILED),
    (_Resp({"error": "nope"}, status_code=401), "rejected", ErrorCode.AUTH_FAILED),
    (_Resp({"Code": -1, "Message": "Unauthorized"}, status_code=500), "rejected", ErrorCode.AUTH_FAILED),
    (_Resp({"Code": -1}, status_code=503), "unknown", ErrorCode.REMOTE_SUBMIT_UNKNOWN),
    (_Resp(ValueError("html"), status_code=502, text="<html>"), "unknown", ErrorCode.REMOTE_SUBMIT_UNKNOWN),
    (_Resp(ValueError("html"), status_code=200, text="<html>"), "unknown", ErrorCode.REMOTE_SUBMIT_UNKNOWN),
    (_Resp({"Code": 0, "Data": {}}), "unknown", ErrorCode.REMOTE_SUBMIT_UNKNOWN),
])
def test_create_pod_classifies_answers(remote_env, monkeypatch, response, outcome, code):
    _post_returning(monkeypatch, response)
    created = executor.create_pod(remote_env["endpoint"], remote_env, "cmd")
    assert created["outcome"] == outcome and created["result"]["error_code"] == code
    assert "secret" not in json.dumps(created)


def test_create_pod_distinguishes_unsent_from_uncertain(remote_env, monkeypatch):
    import requests
    import urllib3

    refused = requests.exceptions.ConnectionError(urllib3.exceptions.MaxRetryError(
        None, "/", urllib3.exceptions.NewConnectionError(None, "refused")))
    for exc, outcome in ((requests.exceptions.ConnectTimeout("slow"), "not_sent"),
                         (refused, "not_sent"),
                         (requests.exceptions.ReadTimeout("no answer"), "unknown"),
                         (requests.exceptions.ConnectionError("Connection aborted"), "unknown")):
        _post_returning(monkeypatch, raises=exc)
        assert executor.create_pod(remote_env["endpoint"], remote_env, "cmd")["outcome"] == outcome


@pytest.mark.parametrize("body, kind", [
    ({"Code": 0, "Data": {"status": "pending"}}, "pending"),
    ({"Code": 0, "Data": {"status": ""}}, "pending"),
    ({"Code": 0, "Data": {"status": "Running"}}, "running"),
    ({"Code": 0, "Data": {"status": "succeeded"}}, "ended"),
    ({"Code": 0, "Data": {"status": "failed"}}, "ended"),
    ({"Code": 0, "Data": {"status": "evicted"}}, "unknown"),
    # Shapes recorded from the real Linux Job API (2026-10-29).
    ({"Code": 0, "Message": "jobpending", "Data": {"status": "pending",
                                                   "pods": [{"name": "p", "status": "Pending"}]}}, "pending"),
    ({"Code": 0, "Message": "jobactive", "Data": {"status": "active",
                                                  "pods": [{"name": "p", "status": "Running"}]}}, "running"),
    ({"Code": 0, "Message": "jobsucceeded", "Data": {"status": "succeeded",
                                                     "pods": [{"name": "p", "status": "Succeeded"}]}}, "ended"),
    ({"Code": 0, "Message": "jobactive", "Data": {"status": "active",
                                                  "pods": [{"name": "p", "status": "Failed"}]}}, "ended"),
    ({"Code": 0, "Message": "jobpending", "Data": {"status": "pending", "pods": None}}, "pending"),
    ({"Code": -1, "Message": 'jobs.batch "fivportal-abc" not found'}, "not_found"),
    ({"Code": -1, "Message": "internal"}, "error"),
])
def test_job_api_get_classifies_status(remote_env, monkeypatch, body, kind):
    _post_returning(monkeypatch, _Resp(body))
    assert executor.job_api_get(remote_env["endpoint"], "fivportal-abc")["kind"] == kind


def test_job_api_get_flags_auth_errors(remote_env, monkeypatch):
    _post_returning(monkeypatch, _Resp({"error": "no"}, status_code=403))
    polled = executor.job_api_get(remote_env["endpoint"], "fivportal-abc")
    assert polled["kind"] == "error" and polled["auth"] is True


def test_job_api_delete_accepts_204(remote_env, monkeypatch):
    calls = _post_returning(monkeypatch, _Resp(ValueError("empty"), status_code=204, text=""))
    assert executor.job_api_delete(remote_env["endpoint"], "fivportal-abc")["ok"] is True
    assert calls[0]["method"] == "DELETE" and calls[0]["url"].endswith("/fivportal-abc")


def test_job_api_unauthorized_envelope_on_http_500(remote_env, monkeypatch):
    _post_returning(monkeypatch, _Resp({"Code": -1, "Message": "Unauthorized", "Data": None},
                                       status_code=500))
    result = executor._job_api("GET", "https://jobs.example.com/job/unknown")
    assert result["error_code"] == ErrorCode.AUTH_FAILED
    assert result["detail"]["status"] == 500
    assert result["detail"]["job_api_code"] == -1
    assert result["detail"]["has_exec_token"] is True
    assert "secret" not in str(result)


def test_job_api_unauthorized_without_exec_token(clean_env, monkeypatch):
    _post_returning(monkeypatch, _Resp({"Code": -1, "Message": "Unauthorized", "Data": None},
                                       status_code=500))
    result = executor._job_api("GET", "https://jobs.example.com/job/unknown")
    assert result["error_code"] == ErrorCode.AUTH_FAILED
    assert result["detail"]["has_exec_token"] is False


@pytest.mark.parametrize("response, kind", [
    (_Resp({"ok": True, "data": {}}), "found"),
    (_Resp({"ok": False, "error_code": "X", "run_id": "r", "schema_version": 1}), "found"),
    (_Resp({"ok": "maybe"}), "invalid"),
    (_Resp(ValueError("html"), text="<html>"), "invalid"),
    (_Resp({}, status_code=404), "missing"),
    (_Resp({}, status_code=403), "auth"),
    (_Resp({}, status_code=500), "error"),
])
def test_read_result_json(remote_env, monkeypatch, response, kind):
    seen = {}

    def fake_get(url, headers=None, **kwargs):
        seen.update(url=url, headers=headers)
        return response
    monkeypatch.setattr(executor.requests, "get", fake_get)
    read = executor.read_result_json("https://af.example.com/repo/run")
    assert read["kind"] == kind
    assert seen["url"] == "https://af.example.com/repo/run/result.json"
    assert seen["headers"]["Authorization"] == "Bearer af-secret"


def test_spec_and_command_are_shell_safe_and_credential_free(remote_env, tmp_path):
    spec = executor.build_spec(job_id="j1", run_id="plan-test-1a2b3c4d",
                               run_url="https://af.example.com/repo/plan-test-1a2b3c4d",
                               report_url="https://mcp.example.com/internal/jobs/j1/result",
                               plan=_plan(tmp_path), exec_cfg=remote_env, submitted_at=1000.0)
    assert spec["spec_version"] == 1 and spec["worker"]["repo_ref"] == "main"
    assert spec["timeouts"] == {"budget_seconds": 3600, "deadline_epoch": 1000 + 3600 - 30}
    token = plan.encode_plan(spec)
    command = executor.wrapper_command(remote_env, "j1", spec_token=token, report_token="tok_-x")
    assert command == f"exec python3 /app/stitch_wrapper.py --job-id j1 --spec {token} --report-token tok_-x"
    import re
    assert all(re.fullmatch(r"[A-Za-z0-9_.:/=-]+", part) for part in command.split(" "))
    assert "secret" not in json.dumps(spec)
    by_url = executor.wrapper_command(remote_env, "j1", spec_url="https://af/run/spec.json")
    assert by_url.endswith("--spec-url https://af/run/spec.json")


def test_shape_remote_success_keeps_v1_aliases():
    where = {"plan_id": "p", "run_id": "p-1", "artifactory_url": "https://af/p-1",
             "result_url": "https://af/p-1/result.json", "pod_job_id": "fivportal-abc",
             "pod_log_url": "https://af/p-1/pod.log", "endpoint": "https://jobs"}
    envelope = {"ok": True, "data": {
        "exit_code": 0, "command_line": "py", "stitched_bin": "/pod/local/out.bin",
        "deliverables": [{"url": "https://af/p-1/deliverables/out.bin", "name": "out.bin",
                          "kind": "stitched_bin", "sha256": "abc", "bytes": 3, "path": "/pod"}],
        "environment": {"stitch_dir": "/pod/tool", "fetched": [{"source": "ifwi"}]}}}
    data = executor.shape_remote_success(envelope, where)["data"]
    assert data["stitched_bin"] == {"url": "https://af/p-1/deliverables/out.bin", "sha256": "abc", "bytes": 3}
    assert data["stitched_bin_url"] == data["stitched_bin"]["url"]
    assert data["job_id"] == data["pod_job_id"] == "fivportal-abc"
    assert data["artifacts"] == [{"name": "out.bin", "url": "https://af/p-1/deliverables/out.bin",
                                  "kind": "stitched_bin"}]
    assert data["environment"] == {"fetched": [{"source": "ifwi"}]}
    assert "/pod/" not in json.dumps(data)
    missing = executor.shape_remote_success({"ok": True, "data": {"deliverables": []}}, where)
    assert missing["error_code"] == ErrorCode.DELIVERABLE_MISSING


def test_execute_remote_goes_through_the_job_manager(remote_env, monkeypatch, tmp_path):
    from ifwi_mcp import jobs
    seen = {}

    class FakeManager:
        def run_plan(self, p):
            seen["plan_id"] = p["plan_id"]
            return ok({"task_job_id": "j"})
    monkeypatch.setattr(jobs, "get_manager", lambda: FakeManager())
    assert executor.execute(_plan(tmp_path))["data"]["task_job_id"] == "j"
    assert seen["plan_id"] == "plan-test"


def _case_sensitive(path):
    probe = path / "case_probe"
    probe.mkdir()
    try:
        return not (path / "CASE_PROBE").exists()
    finally:
        probe.rmdir()


def _stitch_writing(tmp_path, *files):
    """A fake stitch tool that writes each (relative path, bytes) and exits 0."""
    import sys
    body = "; ".join(f"os.makedirs(os.path.dirname({name!r}), exist_ok=True); "
                     f"open({name!r}, 'wb').write({data!r})" for name, data in files)
    return stitch_runner.execute_command([sys.executable, "-c", "import os; " + body], str(tmp_path))


def test_output_dir_is_found_case_insensitively(tmp_path):
    (tmp_path / "Output").mkdir()
    found = stitch_runner._child_dir(tmp_path, "output")
    assert found.is_dir() and found.name.casefold() == "output"
    assert stitch_runner._child_dir(tmp_path, "release") == tmp_path / "release"
    assert stitch_runner._child_dirs(tmp_path, "release") == [tmp_path / "release"]


@pytest.mark.parametrize("folder", ["output", "Output"])
def test_execute_command_finds_either_output_spelling(clean_env, tmp_path, folder):
    run = _stitch_writing(tmp_path, (f"{folder}/out.bin", b"x"))
    assert run["ok"] is True, run
    assert run["data"]["stitched_bin"].endswith("out.bin")
    assert run["data"]["output_dir"].casefold().endswith(folder.casefold())


@pytest.mark.parametrize("with_bin, other", [("Output", "output"), ("output", "Output")])
def test_execute_command_searches_both_spellings_when_both_exist(clean_env, tmp_path, with_bin, other):
    if not _case_sensitive(tmp_path):
        pytest.skip("output/ and Output/ cannot coexist on a case-insensitive file system")
    run = _stitch_writing(tmp_path, (f"{other}/stitch.log", b"log"), (f"{with_bin}/out.bin", b"x"))
    assert run["ok"] is True, run
    assert run["data"]["stitched_bin"] == str(tmp_path / with_bin / "out.bin")
    assert sorted(stitch_runner._child_dirs(tmp_path, "output")) == sorted(
        [tmp_path / "output", tmp_path / "Output"])
    assert stitch_runner._child_dirs(tmp_path, "output")[0] == tmp_path / "output"


def test_job_api_get_flags_a_job_left_without_pods(remote_env, monkeypatch):
    _post_returning(monkeypatch, _Resp({"Code": 0, "Message": "jobpending",
                                        "Data": {"status": "pending", "pods": None}}))
    polled = executor.job_api_get(remote_env["endpoint"], "fivportal-abc")
    assert polled["kind"] == "pending" and polled["no_pods"] is True and polled["has_pods"] is False
    _post_returning(monkeypatch, _Resp({"Code": 0, "Data": {"status": "pending",
                                                            "pods": [{"status": "Pending"}]}}))
    polled = executor.job_api_get(remote_env["endpoint"], "fivportal-abc")
    assert polled["no_pods"] is False and polled["has_pods"] is True