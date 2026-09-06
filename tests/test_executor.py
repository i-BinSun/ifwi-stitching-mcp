# tests/test_executor.py
import pytest

from ifwi_mcp import executor, plan
from ifwi_mcp.result import ok, err, ErrorCode


def _plan(tmp_path, **overrides):
    base = {
        "plan_version": 1,
        "plan_id": "plan-test",
        "project": "DMR", "phase": "Orange", "version": "2026.30.2.01", "swimlane": None,
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

    def fake_execute(argv, cwd, log_name=None, timeout=None):
        state["argv"], state["timeout"], state["log_name"] = argv, timeout, log_name
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
    assert run_fakes["log_name"] == "plan-test"


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
                                "endpoint": "https://runner.example.com/api",
                                "timeout_seconds": 10, "poll_interval_seconds": 1}})
    calls = {"post": [], "get": []}

    # Virtual clock: sleeping advances time instead of blocking.
    clock = {"now": 0.0}
    monkeypatch.setattr(executor.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(executor.time, "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    return calls


def test_execute_remote_submits_polls_and_fetches(remote_env, monkeypatch, tmp_path):
    job = {"status": "succeeded", "exit_code": 0, "command_line": "python cli.py ...",
           "deliverables": [{"url": "https://runner.example.com/f/out.bin",
                             "name": "out.bin", "kind": "stitched_bin"}],
           "warnings": ["remote warn"]}

    def fake_post(url, json=None, headers=None, timeout=None, verify=None):
        remote_env["post"].append((url, json, headers))
        return _Resp({"job_id": "j-1"})

    def fake_get(url, stream=False, **kwargs):
        if stream:                       # deliverable download, not a status poll
            return _Download()
        remote_env["get"].append(url)
        return _Resp(job)

    monkeypatch.setattr(executor.requests, "post", fake_post)
    monkeypatch.setattr(executor.requests, "get", fake_get)

    r = executor.execute(_plan(tmp_path))
    assert r["ok"] is True
    assert r["data"]["mode"] == "remote" and r["data"]["job_id"] == "j-1"
    assert remote_env["post"][0][0] == "https://runner.example.com/api/jobs"
    assert remote_env["post"][0][1]["plan"]["plan_id"] == "plan-test"
    assert remote_env["post"][0][2]["Authorization"] == "Bearer secret"
    assert remote_env["get"] == ["https://runner.example.com/api/jobs/j-1"]
    assert r["data"]["command_line"] == "python cli.py ..."
    assert r["data"]["stitched_bin"].endswith("out.bin")
    assert r["data"]["deliverables"]["item_count"] == 1


def test_execute_remote_reports_job_failure(remote_env, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.requests, "post",
                        lambda *a, **kw: _Resp({"job_id": "j-2"}))
    monkeypatch.setattr(executor.requests, "get",
                        lambda *a, **kw: _Resp({"status": "failed", "exit_code": 3,
                                                "log_tail": "nope"}))
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["exit_code"] == 3


def test_execute_remote_rejects_bad_credentials(remote_env, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.requests, "post",
                        lambda *a, **kw: _Resp({"error": "nope"}, status_code=401))
    assert executor.execute(_plan(tmp_path))["error_code"] == ErrorCode.AUTH_FAILED


def test_execute_remote_times_out(remote_env, monkeypatch, tmp_path):
    monkeypatch.setattr(executor.requests, "post",
                        lambda *a, **kw: _Resp({"job_id": "j-3"}))
    monkeypatch.setattr(executor.requests, "get",
                        lambda *a, **kw: _Resp({"status": "running"}))
    r = executor.execute(_plan(tmp_path))
    assert r["error_code"] == ErrorCode.REMOTE_TIMEOUT
    assert r["detail"]["timeout_seconds"] == 10
