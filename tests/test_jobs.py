"""JobManager: submission, callback, refresh-on-query, finalization, cancel, restart."""
import json
import threading
import time

import pytest

from ifwi_mcp import executor, jobs, plan as plan_mod
from ifwi_mcp.job_store import MemoryJobStore, SqliteJobStore
from ifwi_mcp.result import ErrorCode, err, ok

RUN_BASE = "https://af.example.com/repo/stitch_task"
ENDPOINT = "https://jobs.example.com/v1/fivportal/job"


def _plan(plan_id="plan-test"):
    return {"plan_version": 1, "plan_id": plan_id, "sources": {}, "stitch": {}}


def _success(run_id=None):
    envelope = {"ok": True, "data": {
        "exit_code": 0, "command_line": "python cli.py",
        "deliverables": [
            {"url": f"{RUN_BASE}/r/deliverables/out.bin", "name": "out.bin",
             "kind": "stitched_bin", "sha256": "abc", "bytes": 3},
            {"url": f"{RUN_BASE}/r/deliverables/p.log", "name": "p.log", "kind": "log"}],
        "environment": {"binary_file": "/tmp/x.bin", "fetched": [{"source": "ifwi"}]},
        "warnings": ["worker warn"]}}
    if run_id:
        envelope["run_id"] = run_id
    return envelope


class Remote:
    """Fake Job API + Artifactory as seen through the executor primitives."""

    def __init__(self, monkeypatch, store):
        self.store = store
        self.outcomes = []
        self.created = []
        self.pod = {"kind": "running", "status": "running", "auth": False}
        self.result = {"kind": "missing"}
        self.deleted = []
        self.uploaded_specs = []
        self.create_gate = None
        self.gets = 0
        monkeypatch.setattr(executor, "create_pod", self.create_pod)
        monkeypatch.setattr(executor, "job_api_get", self.job_api_get)
        monkeypatch.setattr(executor, "job_api_delete", self.job_api_delete)
        monkeypatch.setattr(executor, "read_result_json", self.read_result_json)
        monkeypatch.setattr(executor, "upload_spec", self.upload_spec)

    def create_pod(self, endpoint, cfg, command):
        if self.create_gate is not None:
            self.create_gate.wait(5)
        job_id = command.split("--job-id ")[1].split()[0]
        self.created.append({"command": command, "row": self.store.get(job_id)})
        if self.outcomes:
            return self.outcomes.pop(0)
        return {"outcome": "created", "pod_job_id": "fivportal-abc"}

    def job_api_get(self, endpoint, pod_job_id):
        self.gets += 1
        return dict(self.pod)

    def job_api_delete(self, endpoint, pod_job_id):
        self.deleted.append(pod_job_id)
        return ok({"pod_job_id": pod_job_id})

    def read_result_json(self, run_url):
        return {"url": f"{run_url}/result.json", **self.result}

    def upload_spec(self, spec, run_url):
        self.uploaded_specs.append(spec)
        return ok({"url": f"{run_url}/spec.json"})

    @property
    def token(self):
        command = self.created[-1]["command"]
        return command.split("--report-token ")[1].split()[0]


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(jobs, "_now", lambda: state["now"])
    monkeypatch.setattr(jobs, "_sleep", lambda s: state.__setitem__("now", state["now"] + s))
    return state


@pytest.fixture
def remote_cfg(clean_env, write_config, monkeypatch):
    def _write(**overrides):
        execution = {"mode": "remote", "endpoint": ENDPOINT, "token": "exec-secret",
                     "artifactory_repo": RUN_BASE, "report_base_url": "https://mcp.example.com",
                     "timeout_seconds": 3600, "pending_timeout_seconds": 900,
                     "finalize_grace_seconds": 120, "refresh_min_interval_seconds": 0,
                     "poll_interval_seconds": 1}
        execution.update(overrides)
        write_config({"execution": execution})
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "af-secret")
    _write()
    return _write


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryJobStore()
    else:
        sqlite = SqliteJobStore(tmp_path / "jobs.db")
        yield sqlite
        sqlite.close()


@pytest.fixture
def setup(remote_cfg, store, clock, monkeypatch):
    remote = Remote(monkeypatch, store)
    manager = jobs.JobManager(store, server_mode=True)
    yield manager, remote
    manager.close()


def _settle(manager, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = manager.store.get(job_id)
        if row["stage"] not in ("resolving", "submitting", "querying"):
            return row
        time.sleep(0.01)
    raise AssertionError(f"job stuck in {row['stage']}")


def _submit(manager, plan_id="plan-test"):
    status, job = manager.submit_plan_object(_plan(plan_id), {"plan_id": plan_id})
    assert status == "created", job
    return _settle(manager, job["job_id"])


def _callback(manager, row, token, result, **extra):
    body = {"schema_version": 1, "job_id": row["job_id"], "run_id": row["run_id"],
            "attempt_id": extra.pop("attempt_id", "a1"),
            "wrapper": {"name": "stitch_wrapper", "version": extra.pop("version", "1.1.0"), "protocol": 1},
            "worker": {"repo_url": "r", "repo_ref": "main", "commit": "c0ffee", "module": "ifwi_mcp.worker"},
            "result_source": "worker_file", "result": result, "exit_code": 0, "signal": None,
            "terminated": False, "started_at": "2026-10-28T00:00:00Z",
            "finished_at": extra.pop("finished_at", "2026-10-28T00:03:00Z"),
            "durations": {"bootstrap_seconds": 60}, "pod_log_url": f"{row['run_url']}/pod.log",
            "log_tail": "tail"}
    body.update(extra)
    return manager.handle_callback(row["job_id"], f"Bearer {token}", json.dumps(body).encode())


# ---- submission ------------------------------------------------------------------------

def test_submit_records_run_before_creating_the_pod(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    assert row["stage"] == "pending" and row["pod_job_id"] == "fivportal-abc"
    before = remote.created[0]["row"]
    assert before["stage"] == "submitting" and before["run_id"] == row["run_id"]
    assert before["submitted_at"] == clock["now"] and before["pod_job_id"] is None
    assert row["deadline_at"] == clock["now"] + 900 + 3600
    command = remote.created[0]["command"]
    assert command.startswith("exec python3 /app/stitch_wrapper.py --job-id " + row["job_id"])
    assert "sh -c" not in command and "af-secret" not in command and "exec-secret" not in command
    spec = plan_mod.decode_plan(command.split("--spec ")[1].split()[0])
    assert spec["run_id"] == row["run_id"] and spec["run_url"] == f"{RUN_BASE}/{row['run_id']}"
    assert spec["report_url"] == f"https://mcp.example.com/internal/jobs/{row['job_id']}/result"
    assert spec["timeouts"] == {"budget_seconds": 3600,
                                "deadline_epoch": int(clock["now"] + 3600 - 30)}
    assert remote.token not in json.dumps(spec)
    assert row["report_token_hash"] and remote.token not in json.dumps(row)


def test_long_spec_is_uploaded_and_passed_by_url(setup, remote_cfg):
    remote_cfg(max_inline_spec_bytes=256)
    manager, remote = setup
    row = _submit(manager)
    assert remote.uploaded_specs and "--spec-url " + f"{row['run_url']}/spec.json" in remote.created[0]["command"]


def test_spec_upload_failure_creates_no_pod(setup, remote_cfg, monkeypatch):
    remote_cfg(max_inline_spec_bytes=256)
    manager, remote = setup
    monkeypatch.setattr(executor, "upload_spec",
                        lambda spec, url: err(ErrorCode.UPLOAD_FAILED, "nope", {"url": url}))
    row = _submit(manager)
    assert row["state"] == "failed" and row["result"]["error_code"] == ErrorCode.UPLOAD_FAILED
    assert remote.created == []


def test_unsent_create_is_retried_with_the_same_run(setup):
    manager, remote = setup
    failed = {"outcome": "not_sent", "result": err(ErrorCode.REMOTE_SUBMIT_FAILED, "down")}
    remote.outcomes = [failed, failed]
    row = _submit(manager)
    assert row["stage"] == "pending" and len(remote.created) == 3
    assert {c["row"]["run_id"] for c in remote.created} == {row["run_id"]}


def test_unsent_create_gives_up_after_three_retries(setup):
    manager, remote = setup
    remote.outcomes = [{"outcome": "not_sent",
                        "result": err(ErrorCode.REMOTE_SUBMIT_FAILED, "down")}] * 4
    row = _submit(manager)
    assert row["result"]["error_code"] == ErrorCode.REMOTE_SUBMIT_FAILED
    assert row["result"]["detail"]["retryable"] is True and len(remote.created) == 4


def test_rejected_create_fails_without_retry(setup):
    manager, remote = setup
    remote.outcomes = [{"outcome": "rejected", "result": err(ErrorCode.AUTH_FAILED, "no")}]
    row = _submit(manager)
    assert row["result"]["error_code"] == ErrorCode.AUTH_FAILED and len(remote.created) == 1


def test_uncertain_create_waits_for_result_json(setup, clock):
    manager, remote = setup
    remote.outcomes = [{"outcome": "unknown",
                        "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN, "?", {"reason": "read timeout"})}]
    row = _submit(manager)
    assert row["stage"] == "submit_unknown" and row["pod_job_id"] is None
    assert len(remote.created) == 1
    assert manager.refresh(row["job_id"])["stage"] == "submit_unknown"
    remote.result = {"kind": "found", "envelope": _success(row["run_id"])}
    done = manager.refresh(row["job_id"])
    assert done["state"] == "succeeded" and done["result_source"] == "result_json"


def test_uncertain_create_concludes_after_the_deadline(setup, clock):
    manager, remote = setup
    remote.outcomes = [{"outcome": "unknown", "result": err(ErrorCode.REMOTE_SUBMIT_UNKNOWN, "?")}]
    row = _submit(manager)
    clock["now"] = row["deadline_at"] + 1
    assert manager.refresh(row["job_id"])["stage"] == "finalizing"
    clock["now"] += 121
    done = manager.refresh(row["job_id"])
    assert done["result"]["error_code"] == ErrorCode.REMOTE_SUBMIT_UNKNOWN
    assert done["result_source"] == "inferred" and "hint" in done["result"]["detail"]


# ---- callback --------------------------------------------------------------------------

def test_callback_success_finishes_the_job(setup):
    manager, remote = setup
    row = _submit(manager)
    status, body = _callback(manager, row, remote.token, _success())
    assert (status, body["data"]["accepted"]) == (200, True)
    done = manager.store.get(row["job_id"])
    assert done["state"] == "succeeded" and done["result_source"] == "callback"
    data = done["result"]["data"]
    assert data["stitched_bin"] == {"url": f"{RUN_BASE}/r/deliverables/out.bin", "sha256": "abc", "bytes": 3}
    assert set(data) == set(jobs.SERVER_RESULT_KEYS)
    assert data["artifactory_url"] == row["run_url"]
    assert done["remote"]["worker_commit"] == "c0ffee"
    view = manager.view(done)
    assert "pod_status" not in view["remote"] and view["remote"]["worker_commit"] == "c0ffee"
    assert set(view["result"]["data"]) == {"stitched_bin", "log_url", "warnings"}
    again = _callback(manager, row, remote.token, _success())
    assert again[1]["data"]["accepted"] is False


def test_callback_requires_the_job_token(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    assert _callback(manager, row, "wrong", _success())[0] == 401
    assert manager.handle_callback("nope", f"Bearer {remote.token}", b"{}")[0] == 401
    assert _callback(manager, {**row, "run_id": "other"}, remote.token, _success())[0] == 422
    assert _callback(manager, row, remote.token, {"ok": "yes"})[0] == 422
    clock["now"] = row["deadline_at"] + 120 + 601
    assert _callback(manager, row, remote.token, _success())[0] == 410
    assert manager.store.get(row["job_id"])["state"] == "running"


def test_spec_invalid_callback_is_accepted_without_run_id(setup):
    manager, remote = setup
    row = _submit(manager)
    status, body = _callback(manager, {**row, "run_id": None}, remote.token,
                             {"ok": False, "error_code": "WRAPPER_SPEC_INVALID", "message": "bad",
                              "detail": {}})
    assert body["data"]["accepted"] is True
    assert manager.store.get(row["job_id"])["result"]["error_code"] == "WRAPPER_SPEC_INVALID"


def test_success_without_uploaded_binary_is_deliverable_missing(setup):
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, {"ok": True, "data": {"deliverables": []}})
    assert manager.store.get(row["job_id"])["result"]["error_code"] == ErrorCode.DELIVERABLE_MISSING


def test_terminated_is_kept_unless_a_legacy_wrapper_hit_the_deadline(setup, clock):
    manager, remote = setup
    terminated = {"ok": False, "error_code": "REMOTE_TERMINATED", "message": "sigterm", "detail": {}}
    row = _submit(manager)
    _callback(manager, row, remote.token, terminated)
    assert manager.store.get(row["job_id"])["result"]["error_code"] == ErrorCode.REMOTE_TERMINATED

    row = _submit(manager)
    late = jobs.iso(row["submitted_at"] + 3600 - 60)
    _callback(manager, row, remote.token, terminated, version="1.0.0", finished_at=late)
    result = manager.store.get(row["job_id"])["result"]
    assert result["error_code"] == ErrorCode.REMOTE_TIMEOUT
    assert result["detail"]["original_error_code"] == "REMOTE_TERMINATED"


def test_restarted_pod_attempt_adds_a_warning(setup):
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, _success(), attempt_id="a1")
    _callback(manager, row, remote.token, _success(), attempt_id="a2")
    assert any("another pod attempt" in w for w in manager.store.get(row["job_id"])["warnings"])


# ---- refresh on query ------------------------------------------------------------------

def test_refresh_tracks_pod_status_and_throttles(setup, remote_cfg, clock):
    manager, remote = setup
    row = _submit(manager)
    assert manager.refresh(row["job_id"])["stage"] == "running"
    remote_cfg(refresh_min_interval_seconds=5)
    gets = remote.gets
    manager.refresh(row["job_id"])
    assert remote.gets == gets                     # within 5s of the last check
    clock["now"] += 6
    manager.refresh(row["job_id"])
    assert remote.gets == gets + 1


def test_job_api_errors_never_fail_the_job(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "error", "status": None, "auth": True,
                  "error": err(ErrorCode.AUTH_FAILED, "no")}
    manager.refresh(row["job_id"])
    after = manager.refresh(row["job_id"])
    assert after["state"] == "running" and after["remote"]["degraded"] is True
    assert after["remote"]["last_error"] == ErrorCode.AUTH_FAILED
    assert manager.view(after)["poll_after_seconds"] == 60


def test_pending_timeout_needs_an_explicit_pending_answer(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "pending", "status": "pending", "auth": False}
    manager.refresh(row["job_id"])
    clock["now"] += 901
    remote.pod = {"kind": "error", "status": None, "auth": False, "error": err("X", "down")}
    stale = manager.refresh(row["job_id"])
    assert stale["state"] == "running" and stale["stage"] == "pending" and remote.deleted == []
    remote.pod = {"kind": "pending", "status": "pending", "auth": False}
    done = manager.refresh(row["job_id"])
    assert done["result"]["error_code"] == ErrorCode.REMOTE_SCHEDULE_TIMEOUT
    assert remote.deleted == ["fivportal-abc"] and done["cleanup"]["pod_delete"] == "done"


def test_ended_pod_uses_result_json(setup):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "succeeded", "auth": False}
    remote.result = {"kind": "found", "envelope": _success(row["run_id"])}
    done = manager.refresh(row["job_id"])
    assert done["state"] == "succeeded" and done["result_source"] == "result_json"


def test_result_json_of_another_run_is_invalid(setup):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    remote.result = {"kind": "found", "envelope": _success("other-run")}
    assert manager.refresh(row["job_id"])["result"]["error_code"] == ErrorCode.REMOTE_RESULT_INVALID


def test_missing_result_is_inferred_after_the_grace_period(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    waiting = manager.refresh(row["job_id"])
    assert waiting["stage"] == "finalizing" and waiting["finalize_reason"] == "pod_ended"
    assert manager.view(waiting)["poll_after_seconds"] == 10
    clock["now"] += 60
    assert manager.refresh(row["job_id"])["stage"] == "finalizing"
    clock["now"] += 61
    done = manager.refresh(row["job_id"])
    assert done["result"]["error_code"] == ErrorCode.REMOTE_RESULT_MISSING
    assert done["result_source"] == "inferred"
    assert done["result"]["detail"]["finalize_reason"] == "pod_ended"


def test_gone_needs_two_answers_and_reverts_when_the_pod_reappears(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "not_found", "status": None, "auth": False}
    clock["now"] += 30
    assert manager.refresh(row["job_id"])["stage"] == "pending"     # just created
    clock["now"] += 60
    first = manager.refresh(row["job_id"])
    assert first["stage"] == "pending" and first["remote"]["gone_seen_at"] == clock["now"]
    clock["now"] += 30
    gone = manager.refresh(row["job_id"])
    assert gone["stage"] == "finalizing" and gone["finalize_reason"] == "pod_gone"
    remote.pod = {"kind": "running", "status": "running", "auth": False}
    back = manager.refresh(row["job_id"])
    assert back["stage"] == "running" and back["finalize_reason"] is None
    assert any("not found" in w for w in back["warnings"])


def test_inferred_failure_is_upgraded_by_a_late_callback(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    manager.refresh(row["job_id"])
    clock["now"] += 121
    assert manager.refresh(row["job_id"])["result_source"] == "inferred"
    status, body = _callback(manager, row, remote.token, _success())
    assert body["data"]["accepted"] is True
    done = manager.store.get(row["job_id"])
    assert done["state"] == "succeeded" and done["result_source"] == "callback"
    assert done["remote"]["superseded_result"]["error_code"] == ErrorCode.REMOTE_RESULT_MISSING
    assert any("upgraded from inferred" in w for w in done["warnings"])


def test_inferred_failure_is_upgraded_by_a_later_result_json(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    manager.refresh(row["job_id"])
    clock["now"] += 121
    manager.refresh(row["job_id"])
    remote.result = {"kind": "found", "envelope": _success(row["run_id"])}
    assert manager.refresh(row["job_id"])["state"] == "succeeded"


def test_terminated_never_replaces_an_inferred_failure(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    manager.refresh(row["job_id"])
    clock["now"] += 121
    manager.refresh(row["job_id"])
    _callback(manager, row, remote.token,
              {"ok": False, "error_code": "REMOTE_TERMINATED", "message": "x", "detail": {}})
    assert manager.store.get(row["job_id"])["result"]["error_code"] == ErrorCode.REMOTE_RESULT_MISSING


def test_deadline_is_extended_once_then_the_pod_is_deleted(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    clock["now"] = row["deadline_at"] + 1
    extended = manager.refresh(row["job_id"])
    assert extended["deadline_extended"] == 1 and extended["state"] == "running"
    assert extended["deadline_at"] == clock["now"] + 3600
    clock["now"] = extended["deadline_at"] + 1
    assert manager.refresh(row["job_id"])["finalize_reason"] == "deadline"
    clock["now"] += 121
    done = manager.refresh(row["job_id"])
    assert done["result"]["error_code"] == ErrorCode.REMOTE_TIMEOUT
    assert remote.deleted == ["fivportal-abc"]


# ---- cancel ----------------------------------------------------------------------------

def test_cancel_deletes_the_pod_and_ignores_the_late_callback(setup):
    manager, remote = setup
    row = _submit(manager)
    status, body = manager.cancel(row["job_id"])
    assert status == 200 and body["data"]["state"] == "cancelled"
    assert body["data"]["cleanup"] == {"pod_delete": "done"} and remote.deleted == ["fivportal-abc"]
    late = _callback(manager, row, remote.token,
                     {"ok": False, "error_code": "REMOTE_TERMINATED", "message": "x", "detail": {}})
    assert late[1]["data"]["accepted"] is False
    assert manager.cancel(row["job_id"])[0] == 200
    assert manager.cancel("missing")[0] == 404


def test_cancel_after_the_end_is_a_conflict(setup):
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, _success())
    status, body = manager.cancel(row["job_id"])
    assert status == 409 and body["error_code"] == ErrorCode.JOB_ALREADY_FINISHED


def test_cancel_while_submitting_deletes_the_new_pod(setup):
    manager, remote = setup
    remote.create_gate = threading.Event()
    status, job = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"})
    deadline = time.monotonic() + 5
    while manager.store.get(job["job_id"])["run_id"] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.cancel(job["job_id"])[0] == 200
    remote.create_gate.set()
    while not remote.deleted and time.monotonic() < deadline:
        time.sleep(0.01)
    row = manager.store.get(job["job_id"])
    assert remote.deleted == ["fivportal-abc"] and row["pod_job_id"] == "fivportal-abc"
    assert row["state"] == "cancelled"


def test_local_running_job_cannot_be_cancelled(clean_env, write_config, monkeypatch):
    write_config({"execution": {"mode": "local"}})
    gate = threading.Event()
    monkeypatch.setattr(executor, "execute_local", lambda plan, cfg: gate.wait(5) and ok({"x": 1}))
    manager = jobs.JobManager(MemoryJobStore(), server_mode=False)
    status, job = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"})
    status, body = manager.cancel(job["job_id"])
    assert status == 409 and body["error_code"] == ErrorCode.JOB_NOT_CANCELLABLE
    gate.set()
    assert manager.wait(job["job_id"], timeout=5)["state"] == "succeeded"


# ---- idempotency, quota, waiting -------------------------------------------------------

def test_idempotent_resubmission_and_conflict(setup):
    manager, remote = setup
    first = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"}, "req-1")
    again = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"}, "req-1")
    assert again[0] == "existing" and again[1]["job_id"] == first[1]["job_id"]
    status, conflict = manager.submit_plan_object(_plan("plan-x"), {"plan_id": "plan-x"}, "req-1")
    assert conflict["error_code"] == ErrorCode.IDEMPOTENCY_CONFLICT
    assert conflict["detail"]["job_id"] == first[1]["job_id"]
    _settle(manager, first[1]["job_id"])
    assert len(remote.created) == 1


def test_queue_full_is_reported(setup, remote_cfg):
    remote_cfg(max_active_jobs=1)
    manager, remote = setup
    _submit(manager)
    status, result = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"})
    assert result["error_code"] == ErrorCode.JOB_QUEUE_FULL and result["detail"]["retryable"]


def test_wait_returns_when_the_callback_arrives(setup):
    manager, remote = setup
    row = _submit(manager)
    threading.Timer(0.2, lambda: _callback(manager, row, remote.token, _success())).start()
    done = manager.wait(row["job_id"], timeout=5)
    assert done["state"] == "succeeded"
    synced = manager.sync_result(done)
    assert synced["data"]["task_job_id"] == row["job_id"]
    assert "job_id" not in synced["data"]


def test_stdio_mode_downloads_remote_deliverables(remote_cfg, clock, monkeypatch):
    store = MemoryJobStore()
    remote = Remote(monkeypatch, store)
    monkeypatch.setattr(executor, "fetch_remote_deliverables",
                        lambda shaped: ok({**shaped, "stitched_bin": "/local/out.bin"}))
    manager = jobs.JobManager(store, server_mode=False)
    row = _submit(manager)
    assert "--report-token" not in remote.created[0]["command"]
    assert plan_mod.decode_plan(remote.created[0]["command"].split("--spec ")[1].split()[0])["report_url"] is None
    remote.pod = {"kind": "ended", "status": "succeeded", "auth": False}
    remote.result = {"kind": "found", "envelope": _success(row["run_id"])}
    assert manager.refresh(row["job_id"])["result"]["data"]["stitched_bin"] == "/local/out.bin"


# ---- restart, retention ----------------------------------------------------------------

def test_sqlite_store_survives_a_restart(remote_cfg, clock, monkeypatch, tmp_path):
    path = tmp_path / "jobs.db"
    first = SqliteJobStore(path)
    remote = Remote(monkeypatch, first)
    row = _submit(jobs.JobManager(first, server_mode=True))
    token = remote.token
    first.close()

    second = SqliteJobStore(path)
    remote.store = second
    manager = jobs.JobManager(second, server_mode=True)
    assert manager.store.get(row["job_id"])["stage"] == "pending"
    assert _callback(manager, row, token, _success())[1]["data"]["accepted"] is True
    second.close()


def test_recover_on_startup(store, clock):
    now = clock["now"]
    for job_id, stage, executor_mode, pod, run_id in (("a", "resolving", "remote", None, None),
                                                      ("b", "submitting", "remote", "pod-b", "rb"),
                                                      ("c", "submitting", "remote", None, "rc"),
                                                      ("d", "running", "local", None, None),
                                                      ("e", "running", "remote", "pod-e", "re"),
                                                      ("f", "submitting", "remote", None, None)):
        store.create(jobs.new_row(job_id=job_id, kind="stitch", executor=executor_mode,
                                  state="running", stage=stage, created_at=now, updated_at=now,
                                  pod_job_id=pod, run_id=run_id,
                                  submitted_at=now if run_id else None,
                                  deadline_at=now + 4500 if run_id else None), max_active=10)
    manager = jobs.JobManager(store, server_mode=True)
    assert manager.recover_on_startup() == {"interrupted": 3, "pending": 1, "submit_unknown": 1}
    assert store.get("a")["result"]["error_code"] == ErrorCode.JOB_INTERRUPTED
    assert store.get("a")["result"]["detail"]["retryable"] is True
    assert store.get("b")["stage"] == "pending" and store.get("c")["stage"] == "submit_unknown"
    assert store.get("d")["state"] == "failed" and store.get("e")["stage"] == "running"
    assert store.get("f")["result"]["error_code"] == ErrorCode.JOB_INTERRUPTED


def test_recover_on_startup_interrupts_a_job_waiting_for_a_pod_slot(store, clock):
    store.create(jobs.new_row(job_id="q", kind="stitch", executor="remote", state="queued",
                              stage="queued", created_at=clock["now"], updated_at=clock["now"]),
                 max_active=10)
    manager = jobs.JobManager(store, server_mode=True)
    assert manager.recover_on_startup()["interrupted"] == 1
    assert store.get("q")["result"]["error_code"] == ErrorCode.JOB_INTERRUPTED


def test_retention_purges_old_jobs(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, _success())
    clock["now"] += 169 * 3600
    manager.maybe_purge(force=True)
    assert manager.store.get(row["job_id"]) is None


# ---- MCP tools -------------------------------------------------------------------------

def test_mcp_job_tools(clean_env, monkeypatch):
    from ifwi_mcp import server
    assert plan_mod.save_plan(_plan("plan-mcp"))["ok"]
    gate = threading.Event()
    monkeypatch.setattr(executor, "execute_local",
                        lambda plan, cfg: gate.wait(5) and ok({"plan_id": plan["plan_id"]}))
    submitted = server.stitch_submit_plan("plan-mcp", client_request_id="mcp-1")
    job_id = submitted["data"]["job_id"]
    assert submitted["data"]["state"] == "running" and submitted["data"]["terminal"] is False
    assert server.stitch_submit_plan("plan-mcp", client_request_id="mcp-1")["data"]["job_id"] == job_id
    assert server.stitch_submit_plan("plan-mcp", client_request_id="bad id")["error_code"] == ErrorCode.INVALID_ARGUMENT
    listed = server.stitch_list_jobs(state="running")["data"]["jobs"]
    assert [j["job_id"] for j in listed] == [job_id]
    assert server.stitch_cancel_job(job_id)["error_code"] == ErrorCode.JOB_NOT_CANCELLABLE
    gate.set()
    done = jobs.get_manager().wait(job_id, timeout=5)
    view = server.stitch_get_job(job_id)["data"]
    assert done["state"] == view["state"] == "succeeded" and view["plan_id"] == "plan-mcp"
    assert "plan_id" not in view["result"]["data"]
    assert server.stitch_get_job("nope")["error_code"] == ErrorCode.JOB_NOT_FOUND
    assert server.stitch_submit_plan("plan-missing")["error_code"] == ErrorCode.PLAN_NOT_FOUND


def test_mcp_execute_plan_blocks_and_names_the_job(clean_env, monkeypatch):
    from ifwi_mcp import server
    assert plan_mod.save_plan(_plan("plan-mcp"))["ok"]
    monkeypatch.setattr(executor, "execute_local", lambda plan, cfg: err(ErrorCode.STITCH_RUN_FAILED, "boom"))
    result = server.stitch_execute_plan("plan-mcp")
    assert result["error_code"] == ErrorCode.STITCH_RUN_FAILED and result["detail"]["job_id"]


def test_get_deliverables_in_server_mode_reads_the_latest_job(clean_env, monkeypatch):
    from ifwi_mcp import server
    monkeypatch.setenv("IFWI_MCP_SERVER_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVER_TOKEN", "t")
    assert server.stitch_get_deliverables("plan-a")["error_code"] == ErrorCode.DELIVERABLE_MISSING
    store = jobs.get_manager().store
    now = time.time()
    store.create(jobs.new_row(job_id="j1", kind="stitch", executor="remote", state="succeeded",
                              stage="done", created_at=now, updated_at=now, plan_id="plan-a",
                              run_id="plan-a-1",
                              result=ok({"stitched_bin": {"url": "u"}, "log_url": "l",
                                         "artifacts": [], "artifactory_url": "a"})),
                 max_active=10)
    data = server.stitch_get_deliverables("plan-a")["data"]
    assert data["job_id"] == "j1" and data["stitched_bin"] == {"url": "u"} and data["run_id"] == "plan-a-1"

# ---- event log -------------------------------------------------------------------------

def test_event_log_records_transitions_and_notes(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "error", "status": None, "auth": False, "error": err("X", "down")}
    manager.refresh(row["job_id"])
    manager.refresh(row["job_id"])
    remote.pod = {"kind": "running", "status": "running", "auth": False}
    manager.refresh(row["job_id"])
    _callback(manager, row, remote.token, _success())
    events = manager.store.events(row["job_id"])
    details = [e["detail"] for e in events]
    assert details[0] == "created"
    assert "running/submitting -> running/pending" in details
    assert any(d.startswith("pod created: fivportal-abc") for d in details)
    assert any(d.startswith("Job API degraded") for d in details)
    assert "Job API answering again" in details
    assert any(d.startswith("callback from attempt a1") and d.endswith(": ok") for d in details)
    assert "running/running -> succeeded/done [callback: ok]" in details
    assert details[-1] == "pod delete scheduled in 30s"
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    view = manager.view(manager.store.get(row["job_id"]), include_events=True)
    assert view["events"][-1]["state"] == "succeeded" and view["events"][0]["at"].endswith("Z")
    assert "events" not in manager.view(manager.store.get(row["job_id"]))


def test_event_log_records_cleanup_and_upgrade(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "failed", "auth": False}
    manager.refresh(row["job_id"])
    clock["now"] += 121
    manager.refresh(row["job_id"])
    _callback(manager, row, remote.token, _success())
    details = [e["detail"] for e in manager.store.events(row["job_id"])]
    assert "running/pending -> running/finalizing (pod_ended)" in details
    assert any(d.endswith("[inferred: REMOTE_RESULT_MISSING]") for d in details)
    assert any(d.startswith("result upgraded from inferred REMOTE_RESULT_MISSING to callback: ok")
               for d in details)

    other = _submit(manager)
    manager.cancel(other["job_id"])
    details = [e["detail"] for e in manager.store.events(other["job_id"])]
    assert "running/pending -> cancelled/done [server: JOB_CANCELLED]" in details
    assert "pod delete attempt 1: done" in details


def test_events_are_purged_with_their_job(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, _success())
    assert manager.store.events(row["job_id"])
    clock["now"] += 169 * 3600
    manager.maybe_purge(force=True)
    assert manager.store.events(row["job_id"]) == []


def test_version_1_database_gains_the_event_table(tmp_path):
    import sqlite3
    from ifwi_mcp import job_store
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(job_store._SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    store = SqliteJobStore(path)
    assert store._write.execute("PRAGMA user_version").fetchone()[0] == 2
    store.create(jobs.new_row(job_id="j", kind="query", state="running", stage="querying",
                              created_at=1.0, updated_at=1.0), max_active=1)
    assert store.events("j") == [{"at": 1.0, "state": "running", "stage": "querying", "detail": "created"}]
    store.close()

# ---- real Linux Job API shapes (2026-10-29) -------------------------------------------

def test_a_failed_pod_reported_pending_without_pods_ends_the_job(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "running", "status": "active", "auth": False, "no_pods": False,
                  "has_pods": True}
    clock["now"] += 10
    assert manager.refresh(row["job_id"])["stage"] == "running"
    remote.pod = {"kind": "pending", "status": "pending", "auth": False, "no_pods": True}
    clock["now"] += 20
    assert manager.refresh(row["job_id"])["stage"] == "pending"      # still inside the grace
    clock["now"] += 60
    ended = manager.refresh(row["job_id"])
    assert ended["stage"] == "finalizing" and ended["finalize_reason"] == "pod_ended"
    assert ended["pod_status"] == "pending/no-pods"
    remote.result = {"kind": "found", "envelope": {"ok": False, "error_code": "STITCH_RUN_FAILED",
                                                   "message": "boom", "detail": {},
                                                   "run_id": row["run_id"]}}
    done = manager.refresh(row["job_id"])
    assert done["state"] == "failed" and done["result"]["error_code"] == "STITCH_RUN_FAILED"
    details = [e["detail"] for e in manager.store.events(row["job_id"])]
    assert any(d.startswith("Job API reports the job pending with no pods left") for d in details)


def test_a_job_whose_pod_was_never_listed_stays_queued(setup, clock):
    # Namespace quota full: the Job controller has not created the pod yet (2026-10-30).
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "pending", "status": "pending", "auth": False, "no_pods": True,
                  "has_pods": False}
    for _ in range(5):
        clock["now"] += 60
        queued = manager.refresh(row["job_id"])
        assert queued["state"] == "running" and queued["stage"] == "pending"
    details = [e["detail"] for e in manager.store.events(row["job_id"])]
    assert details.count("Job API reports the job pending with no pod yet: "
                         "waiting for cluster capacity") == 1
    remote.pod = {"kind": "running", "status": "active", "auth": False, "no_pods": False,
                  "has_pods": True}
    clock["now"] += 10
    assert manager.refresh(row["job_id"])["stage"] == "running"
    remote.pod = {"kind": "pending", "status": "pending", "auth": False, "no_pods": True,
                  "has_pods": False}
    clock["now"] += 10
    assert manager.refresh(row["job_id"])["finalize_reason"] == "pod_ended"


def test_a_job_never_given_a_pod_hits_the_pending_timeout(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "pending", "status": "pending", "auth": False, "no_pods": True,
                  "has_pods": False}
    clock["now"] += 901
    done = manager.refresh(row["job_id"])
    assert done["state"] == "failed" and done["result"]["error_code"] == "REMOTE_SCHEDULE_TIMEOUT"


def test_pending_without_pods_is_not_taken_as_the_pod_coming_back(setup, clock):
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "not_found", "status": None, "auth": False}
    clock["now"] += 90
    manager.refresh(row["job_id"])
    clock["now"] += 30
    assert manager.refresh(row["job_id"])["finalize_reason"] == "pod_gone"
    remote.pod = {"kind": "pending", "status": "pending", "auth": False, "no_pods": True}
    assert manager.refresh(row["job_id"])["stage"] == "finalizing"

# ---- pod slots (max_running_pods) ------------------------------------------------------

def _token_of(remote, job_id):
    command = next(c["command"] for c in remote.created if f"--job-id {job_id} " in c["command"])
    return command.split("--report-token ")[1].split()[0]


def _submit_queued(manager):
    status, job = manager.submit_plan_object(_plan(), {"plan_id": "plan-test"})
    assert status == "created", job
    assert _wait_for(lambda: manager.store.get(job["job_id"])["stage"] == "queued")
    return manager.store.get(job["job_id"])


def test_jobs_beyond_max_running_pods_wait_queued_then_submit_in_order(setup, remote_cfg, clock):
    remote_cfg(max_running_pods=2)
    manager, remote = setup
    first, second = _submit(manager), _submit(manager)
    third = _submit_queued(manager)
    fourth = _submit_queued(manager)
    assert len(remote.created) == 2
    assert third["state"] == "queued" and third["run_id"] is None and third["submitted_at"] is None
    view = manager.view(manager.refresh(third["job_id"]))
    assert view["state"] == "queued" and not view["terminal"] and view["poll_after_seconds"] == 15
    details = [e["detail"] for e in manager.store.events(fourth["job_id"])]
    assert "waiting for a pod slot (2/2 in use, 1 ahead)" in details

    clock["now"] += 600
    _callback(manager, first, _token_of(remote, first["job_id"]), _success())
    assert _wait_for(lambda: manager.store.get(third["job_id"])["stage"] == "pending")
    assert manager.store.get(fourth["job_id"])["stage"] == "queued" and len(remote.created) == 3
    started = manager.store.get(third["job_id"])
    assert started["state"] == "running" and started["submitted_at"] == clock["now"]
    assert started["deadline_at"] == clock["now"] + 900 + 3600

    manager.cancel(second["job_id"])
    assert _wait_for(lambda: manager.store.get(fourth["job_id"])["stage"] == "pending")
    assert len(remote.created) == 4


def test_a_cancelled_queued_job_never_creates_a_pod(setup, remote_cfg):
    remote_cfg(max_running_pods=1)
    manager, remote = setup
    _submit(manager)
    queued = _submit_queued(manager)
    status, body = manager.cancel(queued["job_id"])
    assert status == 200 and body["data"]["state"] == "cancelled"
    assert _wait_for(lambda: not manager._slot_queue)
    assert len(remote.created) == 1 and manager.store.get(queued["job_id"])["cleanup"] is None


def test_the_queue_head_refreshes_running_jobs_whose_callback_was_lost(setup, remote_cfg,
                                                                       monkeypatch):
    monkeypatch.setattr(jobs, "SLOT_RECHECK_SECONDS", 0.05)
    remote_cfg(max_running_pods=1)
    manager, remote = setup
    running = _submit(manager)
    queued = _submit_queued(manager)
    remote.result = {"kind": "found", "envelope": {**_success(), "run_id": running["run_id"]}}
    remote.pod = {"kind": "ended", "status": "succeeded", "auth": False}
    assert _wait_for(lambda: manager.store.get(queued["job_id"])["stage"] == "pending")
    assert manager.store.get(running["job_id"])["state"] == "succeeded"


def test_a_finalizing_job_whose_pod_ended_frees_its_slot(setup, remote_cfg, clock):
    remote_cfg(max_running_pods=1)
    manager, remote = setup
    row = _submit(manager)
    queued = _submit_queued(manager)
    remote.pod = {"kind": "ended", "status": "succeeded", "auth": False}
    assert manager.refresh(row["job_id"])["stage"] == "finalizing"
    assert _wait_for(lambda: manager.store.get(queued["job_id"])["stage"] == "pending")


def test_zero_max_running_pods_means_no_limit(setup, remote_cfg):
    remote_cfg(max_running_pods=0)
    manager, remote = setup
    for _ in range(7):
        _submit(manager)
    assert len(remote.created) == 7

# ---- pod cleanup after a normal end ----------------------------------------------------

def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_finished_job_deletes_its_pod_after_the_delay(setup, remote_cfg):
    remote_cfg(pod_cleanup_delay_seconds=0)
    manager, remote = setup
    row = _submit(manager)
    _callback(manager, row, remote.token, _success())
    assert _wait_for(lambda: remote.deleted == ["fivportal-abc"])
    assert _wait_for(lambda: manager.store.get(row["job_id"])["cleanup"]["pod_delete"] == "done")
    done = manager.store.get(row["job_id"])
    assert done["cleanup_pending"] == 0 and manager.view(done)["cleanup"] == {"pod_delete": "done"}
    details = [e["detail"] for e in manager.store.events(row["job_id"])]
    assert "pod delete scheduled in 0s" in details and "pod delete attempt 1: done" in details


def test_inferred_and_result_json_endings_also_clean_up(setup, remote_cfg, clock):
    remote_cfg(pod_cleanup_delay_seconds=0)
    manager, remote = setup
    row = _submit(manager)
    remote.pod = {"kind": "ended", "status": "succeeded", "auth": False}
    remote.result = {"kind": "found", "envelope": _success(row["run_id"])}
    assert manager.refresh(row["job_id"])["result_source"] == "result_json"
    assert _wait_for(lambda: remote.deleted == ["fivportal-abc"])


def test_cleanup_waits_for_the_delay_and_is_not_rescheduled(setup, clock):
    manager, remote = setup                                   # default 30 s delay
    row = _submit(manager)
    _callback(manager, row, remote.token, _success())
    waiting = manager.store.get(row["job_id"])
    assert waiting["cleanup"]["not_before"] == clock["now"] + 30 and remote.deleted == []
    manager.refresh(row["job_id"])                            # a query before the delay
    assert remote.deleted == []
    _callback(manager, row, remote.token, _success())         # duplicate: no second schedule
    assert [e["detail"] for e in manager.store.events(row["job_id"])].count(
        "pod delete scheduled in 30s") == 1
    clock["now"] += 31
    manager.refresh(row["job_id"])                            # timer lost: the query retries
    assert remote.deleted == ["fivportal-abc"]


def test_timeouts_and_cancel_do_not_schedule_a_second_delete(setup, remote_cfg, clock):
    remote_cfg(pod_cleanup_delay_seconds=0)
    manager, remote = setup
    row = _submit(manager)
    manager.cancel(row["job_id"])
    time.sleep(0.2)
    assert remote.deleted == ["fivportal-abc"]


def test_startup_sweep_deletes_due_pods_and_rearms_the_rest(remote_cfg, store, clock, monkeypatch):
    remote = Remote(monkeypatch, store)
    first = jobs.JobManager(store, server_mode=True)
    a = _submit(first, "plan-a")
    token_a = remote.token
    b = _submit(first, "plan-b")
    _callback(first, a, token_a, _success())
    clock["now"] += 20
    _callback(first, b, remote.token, _success())
    first.close()                                             # the process dies, timers with it
    clock["now"] += 15                                        # a is due, b is not
    second = jobs.JobManager(store, server_mode=True)
    try:
        monkeypatch.setattr(jobs.threading, "Timer", _InstantTimer)
        monkeypatch.setattr(_InstantTimer, "delays", [])
        second.startup_sweep()
        assert sorted(remote.deleted) == ["fivportal-abc", "fivportal-abc"]
        assert store.get(a["job_id"])["cleanup"]["pod_delete"] == "done"
        assert _InstantTimer.delays == [pytest.approx(15)]
    finally:
        second.close()


class _InstantTimer(threading.Thread):
    """threading.Timer stand-in that records its delay and fires at once."""
    delays = []

    def __init__(self, interval, function, args=()):
        super().__init__(daemon=True)
        _InstantTimer.delays.append(interval)
        self._function, self._args = function, args

    def run(self):
        self._function(*self._args)

    def cancel(self):
        pass

    def start(self):
        self.run()