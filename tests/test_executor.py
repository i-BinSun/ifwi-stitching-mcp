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
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code, self.text = payload, status_code, str(payload)

    def json(self):
        return self._payload


class _Download:
    """Streaming response used when a deliverable file is fetched."""
    status_code = 200

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=0):
        yield b"stitched"


@pytest.fixture
def remote_env(clean_env, write_config, monkeypatch):
    write_config({"execution": {"mode": "remote", "token": "secret",
                                "endpoint": "https://jobs.example.com/v1/fivportal/job",
                                "artifactory_repo": "https://af.example.com/repo/RemoteStitch/",
                                "timeout_seconds": 10, "poll_interval_seconds": 1}})
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "af-secret")
    state = {"jobs": [], "status": ["running", "succeeded"], "result": None,
             "result_status": 200, "result_urls": [], "downloads": []}

    def fake_request(method, url, json=None, headers=None, **kwargs):
        state["jobs"].append((method, url, json, headers))
        if method == "POST":
            return _Resp({"Code": 0, "Message": "CreateSuccess",
                          "Data": {"jobid": "fivportal-abc", "status": ""}})
        status = state["status"].pop(0) if len(state["status"]) > 1 else state["status"][0]
        return _Resp({"Code": 0, "Data": {"jobid": "fivportal-abc", "status": status}})

    def fake_get(url, stream=False, headers=None, **kwargs):
        if stream:                       # deliverable download
            state["downloads"].append(url)
            return _Download()
        state["result_urls"].append((url, headers))
        return _Resp(state["result"], status_code=state["result_status"])

    monkeypatch.setattr(executor.requests, "request", fake_request)
    monkeypatch.setattr(executor.requests, "get", fake_get)

    # Virtual clock: sleeping advances time instead of blocking.
    clock = {"now": 0.0}
    monkeypatch.setattr(executor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(executor.time, "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    return state


def _success_result():
    return {"ok": True, "data": {
        "exit_code": 0, "command_line": "python cli.py ...",
        "deliverables": [
            {"url": "https://af.example.com/repo/RemoteStitch/r/deliverables/out.bin",
             "name": "out.bin", "kind": "stitched_bin"},
            {"url": "https://af.example.com/repo/RemoteStitch/r/deliverables/plan-test.log",
             "name": "plan-test.log", "kind": "log"}],
        "warnings": ["remote warn"]}}


def test_execute_remote_creates_pod_polls_and_reads_result(remote_env, tmp_path):
    remote_env["result"] = _success_result()
    r = executor.execute(_plan(tmp_path))
    assert r["ok"] is True, r

    method, url, payload, headers = remote_env["jobs"][0]
    assert (method, url) == ("POST", "https://jobs.example.com/v1/fivportal/job")
    assert headers["Authorization"] == "Bearer secret"
    assert payload["os"] == "windows" and payload["activedeadlineseconds"] == 10
    assert payload["image"].startswith("amr-registry.caas.intel.com/")
    command = payload["command"]
    assert "git clone" in command and "python -m ifwi_mcp.worker" in command
    assert "af-secret" not in command          # tokens never travel on the command line
    encoded = command.split("--plan-b64 ")[1].split()[0]
    assert plan.decode_plan(encoded)["plan_id"] == "plan-test"
    run_url = command.split("--upload-url ")[1].split()[0]
    assert run_url.startswith("https://af.example.com/repo/RemoteStitch/plan-test-")

    assert [j[:2] for j in remote_env["jobs"][1:]] == [
        ("GET", "https://jobs.example.com/v1/fivportal/job/fivportal-abc")] * 2
    assert remote_env["result_urls"][0][0] == f"{run_url}/result.json"
    assert remote_env["result_urls"][0][1]["Authorization"] == "Bearer af-secret"

    data = r["data"]
    assert data["mode"] == "remote" and data["job_id"] == "fivportal-abc"
    assert data["artifactory_url"] == run_url
    assert data["stitched_bin_url"].endswith("/deliverables/out.bin")
    assert [a["kind"] for a in data["artifacts"]] == ["stitched_bin", "log"]
    assert data["stitched_bin"].endswith("out.bin")
    assert data["deliverables"]["item_count"] == 2
    assert data["warnings"] == ["remote warn"]


def test_execute_remote_reports_worker_failure_with_log_links(remote_env, tmp_path):
    remote_env["status"] = ["failed"]
    remote_env["result"] = {"ok": False, "error_code": "STITCH_RUN_FAILED", "message": "boom",
                            "detail": {"exit_code": 3, "deliverables": [
                                {"url": "https://af/x.log", "name": "x.log", "kind": "log"}]}}
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["exit_code"] == 3
    assert r["detail"]["job_id"] == "fivportal-abc"
    assert r["detail"]["deliverables"][0]["url"] == "https://af/x.log"
    assert r["detail"]["result_url"].endswith("/result.json")


def test_execute_remote_pod_died_without_result(remote_env, tmp_path):
    remote_env["status"] = ["failed"]
    remote_env["result_status"] = 404
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.REMOTE_EXEC_FAILED
    assert r["detail"]["job_status"] == "failed"
    assert r["detail"]["job_id"] == "fivportal-abc"


def test_execute_remote_success_requires_uploaded_stitched_bin(remote_env, tmp_path):
    remote_env["result"] = {"ok": True, "data": {"deliverables": [
        {"url": "https://af/x.log", "name": "x.log", "kind": "log"}]}}
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.DELIVERABLE_MISSING
    assert remote_env["downloads"] == []


def test_execute_remote_requires_artifactory_repo(remote_env, write_config, tmp_path):
    write_config({"execution": {"mode": "remote", "endpoint": "https://jobs.example.com/job"}})
    r = executor.execute(_plan(tmp_path))
    assert r["detail"]["param"] == "execution.artifactory_repo"
    assert remote_env["jobs"] == []


def test_execute_remote_rejects_unsafe_plan_id(remote_env, tmp_path):
    r = executor.execute(_plan(tmp_path, plan_id="x; whoami"))
    assert r["error_code"] == ErrorCode.PLAN_INVALID
    assert remote_env["jobs"] == []


def test_execute_remote_job_api_refuses(remote_env, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.requests, "request",
                        lambda *a, **kw: _Resp({"Code": 1, "Message": "CreateFail"}))
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.REMOTE_EXEC_FAILED


def test_execute_remote_rejects_bad_credentials(remote_env, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.requests, "request",
                        lambda *a, **kw: _Resp({"error": "nope"}, status_code=401))
    assert executor.execute(_plan(tmp_path))["error_code"] == ErrorCode.AUTH_FAILED


def test_job_api_unauthorized_envelope_on_http_500(remote_env, monkeypatch):
    monkeypatch.setattr(executor.requests, "request",
                        lambda *a, **kw: _Resp({"Code": -1, "Message": "Unauthorized", "Data": None},
                                              status_code=500))
    result = executor._job_api("GET", "https://jobs.example.com/job/unknown")
    assert result["error_code"] == ErrorCode.AUTH_FAILED
    assert result["detail"]["status"] == 500
    assert result["detail"]["job_api_code"] == -1
    assert result["detail"]["has_exec_token"] is True
    assert "secret" not in str(result)


def test_job_api_unauthorized_without_exec_token(clean_env, monkeypatch):
    monkeypatch.setattr(executor.requests, "request",
                        lambda *a, **kw: _Resp({"Code": -1, "Message": "Unauthorized", "Data": None},
                                              status_code=500))
    result = executor._job_api("GET", "https://jobs.example.com/job/unknown")
    assert result["error_code"] == ErrorCode.AUTH_FAILED
    assert result["detail"]["has_exec_token"] is False


def test_execute_remote_times_out(remote_env, tmp_path):
    remote_env["status"] = ["running"]
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.REMOTE_TIMEOUT
    assert r["detail"]["timeout_seconds"] == 10
    assert r["detail"]["job_id"] == "fivportal-abc"
