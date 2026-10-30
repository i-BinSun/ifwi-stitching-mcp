"""Job lifecycle for /query, /stitching and the MCP job tools.

Design: docs/superpowers/specs/2026-10-28-remote-execution-status-design.md. In short:

* every tracked request is a row in a JobStore (SQLite in server mode, memory in stdio);
* a remote run is submitted once (run_id and run_url are stored before the pod exists),
  after which no thread watches it: the pod's stitch_wrapper calls back once, and a
  status query refreshes the job from the Job API and, if the pod ended without a
  callback, from <run_url>/result.json;
* terminal states are written with conditional updates, first writer wins; the only
  exception is that an inferred failure can be replaced by a later authoritative result.
"""
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from typing import Optional

from . import config, executor, interfaces, plan as plan_mod
from .job_store import (ACTIVE_STATES, TERMINAL_STATES, MemoryJobStore, SqliteJobStore,
                        new_row, note)
from .result import ErrorCode, err, ok

_LOG = logging.getLogger(__name__)

INSTANCE_ID = uuid.uuid4().hex
CALLBACK_SCHEMA_VERSION = 1
# A Job API "not found" this soon after creation means the job is not visible yet.
GONE_GRACE_SECONDS = 60
# A later "not found" is only trusted when seen again at least this much later.
GONE_CONFIRM_SECONDS = 30
TOKEN_EXTRA_SECONDS = 600
CLEANUP_RETRY_SECONDS = 60
CLEANUP_MAX_ATTEMPTS = 5
UPGRADE_PROBE_SECONDS = 60
SWEEP_LIMIT = 10
QUOTA_REFRESH_BUDGET = 15
STARTUP_SWEEP_LIMIT = 100
PURGE_INTERVAL = 3600
SUBMIT_RETRY_DELAYS = (2, 4, 8)
POD_ID_WRITE_SECONDS = 60
DEGRADED_AFTER_ERRORS = 2
LEGACY_TERMINATED_WINDOW = 120
# stitch_wrapper versions from this one on honour spec.timeouts.deadline_epoch.
DEADLINE_EPOCH_WRAPPER = (1, 1, 0)
MAX_BODY_CHARS = 2000
MAX_LOG_TAIL = 4096
SERVER_RESULT_KEYS = ("plan_id", "run_id", "artifactory_url", "stitched_bin", "log_url", "warnings")
VIEW_JOB_FIELDS = ("job_id", "plan_id", "run_id", "artifactory_url")
NOT_CANCELLABLE_LOCAL = "local execution cannot be interrupted once it is running"

_RETRYABLE = {ErrorCode.REMOTE_SUBMIT_FAILED, ErrorCode.REMOTE_SCHEDULE_TIMEOUT,
              ErrorCode.REMOTE_TIMEOUT, ErrorCode.REMOTE_TERMINATED,
              ErrorCode.REMOTE_WORKER_CRASHED, ErrorCode.REMOTE_RESULT_MISSING,
              ErrorCode.REMOTE_RESULT_UNREADABLE, ErrorCode.UPLOAD_FAILED,
              ErrorCode.DOWNLOAD_FAILED, ErrorCode.JOB_INTERRUPTED, ErrorCode.JOB_QUEUE_FULL}


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def iso(ts) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _version_tuple(value) -> Optional[tuple]:
    try:
        return tuple(int(part) for part in str(value).split(".")[:3])
    except (TypeError, ValueError):
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_hash(payload: dict) -> str:
    """Stable hash of the business fields (async and client_request_id excluded)."""
    business = {k: v for k, v in payload.items() if k not in ("async", "client_request_id")}
    return _sha256(json.dumps(business, sort_keys=True, separators=(",", ":"), default=str))


_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def check_client_request_id(value) -> Optional[dict]:
    """None when absent or valid, else an INVALID_ARGUMENT result."""
    if value is None:
        return None
    if not isinstance(value, str) or not _CLIENT_REQUEST_ID_RE.match(value):
        return err(ErrorCode.INVALID_ARGUMENT,
                   "client_request_id must be 1-128 characters of [A-Za-z0-9_.:-]",
                   {"param": "client_request_id"})
    return None


def _truncate(value, limit: int = MAX_BODY_CHARS):
    if isinstance(value, str):
        return value[-limit:] if limit == MAX_LOG_TAIL else value[:limit]
    return value


class JobManager:
    def __init__(self, store, *, server_mode: bool):
        self.store = store
        self.server_mode = server_mode
        self._events = {}
        self._events_lock = threading.Lock()
        self._timers = set()
        self._timers_lock = threading.Lock()
        self._closed = False
        self._flights = set()
        self._flight_lock = threading.Lock()
        self._pool = None
        self._pool_slots = None
        self._pool_lock = threading.Lock()
        self._last_purge = 0.0

    # ---- configuration ------------------------------------------------------------------
    @staticmethod
    def _cfg() -> dict:
        cfg = config.get_execution_config()
        if not cfg["ok"]:
            raise _ConfigProblem(cfg)
        return cfg["data"]

    def refresh_pool(self):
        """Dedicated refresh threads: a slow Job API must not starve synchronous requests."""
        with self._pool_lock:
            if self._pool is None:
                try:
                    size = self._cfg()["refresh_workers"]
                except _ConfigProblem:
                    size = 16
                self._pool = ThreadPoolExecutor(max_workers=size, thread_name_prefix="ifwi-refresh")
                self._pool_slots = threading.BoundedSemaphore(size)
            return self._pool, self._pool_slots

    # ---- submission ---------------------------------------------------------------------
    def submit_query(self, payload: dict, client_request_id=None):
        return self._submit("query", None, "querying", payload, client_request_id,
                            lambda job_id: self._run_query(job_id, payload))

    def submit_stitching(self, payload: dict, client_request_id=None):
        try:
            cfg = self._cfg()
        except _ConfigProblem as problem:
            return "error", problem.result
        return self._submit("stitch", cfg["mode"], "resolving", payload, client_request_id,
                            lambda job_id: self._run_stitching(job_id, payload))

    def submit_plan(self, plan_id: str, client_request_id=None):
        loaded = plan_mod.load_plan(plan_id)
        if not loaded["ok"]:
            return "error", loaded
        return self.submit_plan_object(loaded["data"]["plan"], {"plan_id": plan_id},
                                       client_request_id)

    def submit_plan_object(self, plan: dict, request: dict, client_request_id=None):
        try:
            cfg = self._cfg()
        except _ConfigProblem as problem:
            return "error", problem.result
        stage = "submitting" if cfg["mode"] == "remote" else "running"
        return self._submit("stitch", cfg["mode"], stage, request, client_request_id,
                            lambda job_id: self._execute_plan(job_id, plan, []),
                            plan_id=plan.get("plan_id"))

    def run_plan(self, plan: dict) -> dict:
        """Blocking execution used by executor.execute(): submit, then wait for the end."""
        status, job = self.submit_plan_object(plan, {"plan_id": plan.get("plan_id")})
        if status not in ("created", "existing"):
            return job
        return self.sync_result(self.wait(job["job_id"]))

    def _submit(self, kind, executor_mode, stage, request, client_request_id, work, plan_id=None):
        self.maybe_purge()
        now = _now()
        row = new_row(job_id=uuid.uuid4().hex, kind=kind, executor=executor_mode, state="running",
                      stage=stage, client_request_id=client_request_id,
                      request_hash=request_hash(request), created_at=now, updated_at=now,
                      plan_id=plan_id, owner=INSTANCE_ID, request=_clean_request(request))
        try:
            max_active = self._cfg()["max_active_jobs"]
        except _ConfigProblem as problem:
            return "error", problem.result
        try:
            status, found = self.store.create(row, max_active=max_active)
            if status == "full":
                self._refresh_overdue(budget=QUOTA_REFRESH_BUDGET)
                status, found = self.store.create(row, max_active=max_active)
        except Exception as exc:  # noqa: BLE001 - nothing was created
            _LOG.exception("could not record a new job")
            return "error", err(ErrorCode.INTERNAL_ERROR, "could not record the job",
                                {"reason": str(exc), "retryable": True})
        if status == "full":
            return "error", err(ErrorCode.JOB_QUEUE_FULL, "too many active jobs; retry later",
                                {"max_active_jobs": max_active, "active": found, "retryable": True})
        if status == "conflict":
            return "error", err(ErrorCode.IDEMPOTENCY_CONFLICT,
                                "client_request_id was already used for a different request",
                                {"job_id": found["job_id"], "client_request_id": client_request_id})
        if status == "existing":
            return "existing", found
        try:
            threading.Thread(target=self._run, args=(found["job_id"], work),
                             name=f"ifwi-job-{found['job_id'][:8]}", daemon=True).start()
        except RuntimeError as exc:
            self._finish(found["job_id"], err(ErrorCode.INTERNAL_ERROR, "could not start the job",
                                              {"reason": str(exc), "retryable": True}), "server")
            return "created", self.store.get(found["job_id"])
        return "created", found

    def _run(self, job_id, work):
        try:
            work(job_id)
        except _ConfigProblem as problem:
            self._finish(job_id, problem.result, "server")
        except Exception as exc:  # noqa: BLE001 - the job must still end
            _LOG.exception("job %s failed unexpectedly", job_id)
            self._finish(job_id, err(ErrorCode.INTERNAL_ERROR, "job failed unexpectedly",
                                     {"type": type(exc).__name__, "reason": str(exc)}), "server")

    def _run_query(self, job_id, payload):
        self._finish(job_id, interfaces.query(payload), "server")

    def _run_stitching(self, job_id, payload):
        resolved = interfaces.resolve_stitching(payload)
        if not resolved["ok"]:
            self._finish(job_id, resolved, "server")
            return
        data = resolved["data"]
        self.store.mutate(job_id, _fact(plan_id=data["plan_id"]))
        self._execute_plan(job_id, data["plan"], data["warnings"])

    def _execute_plan(self, job_id, plan, warnings):
        cfg = self._cfg()
        if cfg["mode"] == "remote":
            self._submit_remote(job_id, plan, warnings, cfg)
            return
        if not self.store.mutate(job_id, _active(stage="running", plan_id=plan.get("plan_id"))):
            return                                   # cancelled meanwhile
        result = executor.execute_local(plan, cfg)
        if warnings and result["ok"] and isinstance(result.get("data"), dict):
            result["data"]["warnings"] = list(warnings) + list(result["data"].get("warnings") or [])
        self._finish(job_id, result, "server")

    # ---- remote submission (design §4.6) ------------------------------------------------
    def _submit_remote(self, job_id, plan, warnings, cfg):
        plan_id = plan.get("plan_id", "")
        if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
            self._finish(job_id, err(ErrorCode.PLAN_INVALID, "plan_id is missing or unsafe",
                                     {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"}), "server")
            return
        for check in (config.get_artifactory_upload_base(), config.get_artifactory_token()):
            if not check["ok"]:
                self._finish(job_id, check, "server")
                return
        base = config.get_artifactory_upload_base()["data"]["base_url"]
        run_id = f"{plan_id}-{uuid.uuid4().hex[:8]}"
        run_url = f"{base}/{run_id}"
        report_url = None
        if self.server_mode and cfg["report_base_url"]:
            report_url = f"{cfg['report_base_url']}/internal/jobs/{job_id}/result"
        report_token = secrets.token_urlsafe(32) if report_url else ""
        submitted_at = _now()
        deadline_at = submitted_at + cfg["pending_timeout_seconds"] + cfg["timeout_seconds"]

        def to_submitting(row):
            if row["state"] not in ACTIVE_STATES:
                return False
            row.update(stage="submitting", plan_id=plan_id, run_id=run_id, run_url=run_url,
                       report_token_hash=_sha256(report_token) if report_token else None,
                       submitted_at=submitted_at, deadline_at=deadline_at, updated_at=_now())
            row["remote"]["endpoint"] = cfg["endpoint"]
            if warnings:
                row["remote"]["plan_warnings"] = list(warnings)
            return True
        try:
            if not self.store.mutate(job_id, to_submitting):
                return                               # cancelled: never create the pod
        except Exception as exc:  # noqa: BLE001 - E3: no pod without a record
            _LOG.exception("job %s: could not record run_id before submitting", job_id)
            self._finish(job_id, err(ErrorCode.INTERNAL_ERROR, "could not record the run before submitting",
                                     {"reason": str(exc), "retryable": True}), "server")
            return

        spec = executor.build_spec(job_id=job_id, run_id=run_id, run_url=run_url,
                                   report_url=report_url, plan=plan, exec_cfg=cfg,
                                   submitted_at=submitted_at)
        token = plan_mod.encode_plan(spec)
        if len(token) > cfg["max_inline_spec_bytes"]:
            uploaded = executor.upload_spec(spec, run_url)
            if not uploaded["ok"]:
                self._finish(job_id, uploaded, "server")
                return
            command = executor.wrapper_command(cfg, job_id, spec_url=uploaded["data"]["url"],
                                               report_token=report_token)
        else:
            command = executor.wrapper_command(cfg, job_id, spec_token=token,
                                               report_token=report_token)

        created = executor.create_pod(cfg["endpoint"], cfg, command)
        for delay in SUBMIT_RETRY_DELAYS:
            if created["outcome"] != "not_sent":
                break
            _sleep(delay)
            current = self.store.get(job_id)
            if current is None or current["state"] not in ACTIVE_STATES:
                self._record_cleanup(job_id, "done")     # cancelled; nothing was created
                return
            created = executor.create_pod(cfg["endpoint"], cfg, command)

        outcome = created["outcome"]
        if outcome == "created":
            self._after_created(job_id, created["pod_job_id"], cfg)
        elif outcome in ("rejected", "not_sent"):
            if not self._finish(job_id, created["result"], "server"):
                self._record_cleanup(job_id, "done")
        else:
            last_error = created["result"]["detail"].get("reason") or created["result"]["message"]

            def to_unknown(row):
                if row["state"] not in ACTIVE_STATES:
                    return False
                row.update(stage="submit_unknown", updated_at=_now())
                row["remote"]["last_error"] = _truncate(str(last_error))
                return True
            if not self.store.mutate(job_id, to_unknown):
                self._record_cleanup(job_id, "failed", warning=_ORPHAN_WARNING)

    def _after_created(self, job_id, pod_job_id, cfg):
        if not self._write_pod_job_id(job_id, pod_job_id, cfg):
            return
        moved = self.store.mutate(job_id, _active(stage="pending"))
        if moved is None:
            row = self.store.get(job_id)
            if row is not None and row["state"] == "cancelled":
                self._delete_pod(row, cfg)

    def _write_pod_job_id(self, job_id, pod_job_id, cfg) -> bool:
        delay, started, warned = 1, time.monotonic(), False
        while True:
            try:
                self.store.mutate(job_id, _fact(f"pod created: {pod_job_id}", pod_job_id=pod_job_id))
                return True
            except Exception as exc:  # noqa: BLE001 - E3b
                if not warned:
                    row_hint = self.store_safe_get(job_id)
                    _LOG.warning("job %s: pod %s created (run_id %s) but could not be recorded: %s",
                                 job_id, pod_job_id, (row_hint or {}).get("run_id"), exc)
                    warned = True
                if time.monotonic() - started + delay > POD_ID_WRITE_SECONDS:
                    break
                _sleep(delay)
                delay = min(delay * 2, 16)
        executor.job_api_delete(cfg["endpoint"], pod_job_id)
        with contextlib.suppress(Exception):
            self._finish(job_id, err(ErrorCode.INTERNAL_ERROR,
                                     "the pod was created but could not be recorded; it was deleted",
                                     {"pod_job_id": pod_job_id, "retryable": True}), "server")
        return False

    def store_safe_get(self, job_id):
        with contextlib.suppress(Exception):
            return self.store.get(job_id)
        return None

    # ---- terminal writes ----------------------------------------------------------------
    def _finish(self, job_id, result, source, *, allow_override=False, cleanup_pending=None,
                warning=None):
        """Conditional terminal write. Returns the updated row, or None if another path won."""
        def apply(row):
            override = False
            if row["state"] not in ACTIVE_STATES:
                if not (allow_override and row["state"] == "failed"
                        and row["result_source"] == "inferred"):
                    return False
                override = True
            now = _now()
            final = self._decorate(row, result, source)
            if override:
                row["remote"]["superseded_result"] = row["result"]
                upgraded = f"result upgraded from inferred {(row['result'] or {}).get('error_code')}"
                row["warnings"].append(upgraded)
                note(row, f"{upgraded} to {source}: "
                          f"{'ok' if final['ok'] else final.get('error_code')}")
            row.update(state="succeeded" if final["ok"] else "failed", stage="done",
                       result=final, result_source=source, updated_at=now,
                       finished_at=row["finished_at"] if override else now)
            if cleanup_pending is not None:
                row["cleanup_pending"] = 1 if cleanup_pending else 0
                if cleanup_pending:
                    row["cleanup"] = {"pod_delete": "pending", "attempts": 0}
            if warning:
                row["warnings"].append(warning)
            return True
        updated = self.store.mutate(job_id, apply)
        if updated is not None:
            self._notify(job_id)
            self._schedule_pod_cleanup(updated)
        return updated

    def _decorate(self, row, result, source):
        """Every error carries the locating fields (design §8.1)."""
        if result.get("ok"):
            return result
        detail = dict(result.get("detail") or {})
        for key, value in (("job_id", row["job_id"]), ("run_id", row["run_id"]),
                           ("artifactory_url", row["run_url"])):
            if value is not None:
                detail.setdefault(key, value)
        if row["executor"] == "remote":
            detail.setdefault("pod_job_id", row["pod_job_id"])
            detail.setdefault("pod_status", row["pod_status"])
        if "log_tail" in detail:
            detail["log_tail"] = _truncate(detail["log_tail"], MAX_LOG_TAIL)
        if "retryable" not in detail:
            detail["retryable"] = result["error_code"] in _RETRYABLE
        if result["error_code"] == ErrorCode.REMOTE_SUBMIT_UNKNOWN:
            detail.setdefault("hint", "a pod may still be running until its activedeadlineseconds; "
                                      "confirm before submitting again")
        return {**result, "detail": detail}

    # ---- remote results -----------------------------------------------------------------
    def _where(self, row) -> dict:
        run_url = row["run_url"]
        return {"plan_id": row["plan_id"], "run_id": row["run_id"], "artifactory_url": run_url,
                "result_url": f"{run_url}/{executor.REMOTE_RESULT_NAME}" if run_url else None,
                "pod_job_id": row["pod_job_id"],
                "pod_log_url": row["remote"].get("pod_log_url")
                or (f"{run_url}/{executor.POD_LOG_NAME}" if run_url else None),
                "endpoint": row["remote"].get("endpoint")}

    def _normalize(self, row, envelope, *, wrapper_version=None, finished_at=None) -> dict:
        """Envelope from the callback or result.json -> the stored result (design §6.1)."""
        where = self._where(row)
        if envelope["ok"]:
            shaped = executor.shape_remote_success(envelope, where)
            if shaped["ok"]:
                plan_warnings = row["remote"].get("plan_warnings") or []
                shaped["data"]["warnings"] = list(plan_warnings) + shaped["data"]["warnings"]
                if self.server_mode:
                    # Same shape as a server-mode local run; pod details are in the job view
                    # and the full worker output stays in result.json.
                    shaped = ok({key: shaped["data"][key] for key in SERVER_RESULT_KEYS})
                else:
                    shaped = executor.fetch_remote_deliverables(shaped["data"])
            return shaped
        code = envelope["error_code"]
        detail = dict(envelope.get("detail") or {}) if isinstance(envelope.get("detail"), dict) else {}
        if code == ErrorCode.REMOTE_TERMINATED and self._legacy_deadline_kill(
                row, wrapper_version, finished_at):
            detail["original_error_code"] = code
            code = ErrorCode.REMOTE_TIMEOUT
        for key, value in where.items():
            if value is not None:
                detail.setdefault(key, value)
        return err(code, str(envelope.get("message") or "remote stitch failed"), detail)

    def _legacy_deadline_kill(self, row, wrapper_version, finished_at) -> bool:
        version = _version_tuple(wrapper_version)
        if version is None or version >= DEADLINE_EPOCH_WRAPPER or not row["submitted_at"]:
            return False
        try:
            timeout = self._cfg()["timeout_seconds"]
        except _ConfigProblem:
            return False
        ended = finished_at or _now()
        return ended >= row["submitted_at"] + timeout - LEGACY_TERMINATED_WINDOW

    def _from_result_json(self, row, read) -> Optional[dict]:
        """A result for a result.json read, or None when there is nothing to conclude yet."""
        if read["kind"] == "invalid":
            return err(ErrorCode.REMOTE_RESULT_INVALID, "result.json is not a result envelope",
                       {"result_url": read["url"], "body": _truncate(read.get("body"))})
        if read["kind"] != "found":
            return None
        envelope = read["envelope"]
        if envelope.get("run_id") is not None and envelope.get("run_id") != row["run_id"]:
            return err(ErrorCode.REMOTE_RESULT_INVALID, "result.json belongs to another run",
                       {"result_url": read["url"], "result_run_id": envelope.get("run_id")})
        return self._normalize(row, envelope, finished_at=_parse_iso(envelope.get("finished_at")))

    # ---- callback (design §6.1) ---------------------------------------------------------
    def handle_callback(self, job_id: str, authorization: str, body: bytes):
        """Returns (http_status, envelope)."""
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        row = self.store.get(job_id)
        expected = (row or {}).get("report_token_hash")
        if row is None or not expected or not token or not hmac.compare_digest(_sha256(token), expected):
            _LOG.warning("callback for job %s rejected: %s", job_id,
                         "unknown job" if row is None else "bad token")
            return 401, err(ErrorCode.AUTH_FAILED, "invalid callback token")
        if _now() > self._token_expiry(row):
            return 410, err(ErrorCode.AUTH_FAILED, "callback token expired", {"job_id": job_id})
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 422, err(ErrorCode.INVALID_ARGUMENT, "callback body must be JSON")
        problem = _callback_problem(payload, row)
        if problem:
            _LOG.warning("callback for job %s rejected: %s", job_id, problem)
            return 422, err(ErrorCode.INVALID_ARGUMENT, problem, {"job_id": job_id})

        wrapper = payload.get("wrapper") if isinstance(payload.get("wrapper"), dict) else {}
        worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
        attempt_id = payload.get("attempt_id")

        def facts(current):
            remote = current["remote"]
            if remote.get("attempt_id") and attempt_id and remote["attempt_id"] != attempt_id:
                current["warnings"].append(f"callback from another pod attempt ({attempt_id}); "
                                           "the pod may have been restarted")
            else:
                remote["attempt_id"] = attempt_id
            remote.update(wrapper_version=wrapper.get("version"), worker_commit=worker.get("commit"),
                          durations=payload.get("durations"), exit_code=payload.get("exit_code"),
                          signal=payload.get("signal"), callback_at=_now())
            if payload.get("pod_log_url"):
                remote["pod_log_url"] = payload["pod_log_url"]
            reported = payload["result"]
            note(current, f"callback from attempt {attempt_id} (wrapper {wrapper.get('version')}, "
                          f"{payload.get('result_source')}): "
                          f"{'ok' if reported['ok'] else reported.get('error_code')}")
            return True
        row = self.store.mutate(job_id, facts) or row

        envelope = dict(payload["result"])
        if not envelope["ok"] and payload.get("log_tail"):
            detail = dict(envelope.get("detail") or {})
            detail.setdefault("log_tail", payload["log_tail"])
            envelope["detail"] = detail
        result = self._normalize(row, envelope, wrapper_version=wrapper.get("version"),
                                 finished_at=_parse_iso(payload.get("finished_at")))
        if not result["ok"] and result["error_code"] == ErrorCode.WRAPPER_SPEC_INVALID:
            _LOG.error("job %s: the wrapper rejected the spec the server built: %s",
                       job_id, result["message"])
        upgradable = result["ok"] or result["error_code"] != ErrorCode.REMOTE_TERMINATED
        accepted = self._finish(job_id, result, "callback", allow_override=upgradable)
        return 200, ok({"accepted": accepted is not None})

    def _token_expiry(self, row) -> float:
        try:
            grace = self._cfg()["finalize_grace_seconds"]
        except _ConfigProblem:
            grace = 120
        return (row["deadline_at"] or 0) + grace + TOKEN_EXTRA_SECONDS

    # ---- refresh on query (design §6.2) -------------------------------------------------
    @contextlib.contextmanager
    def _single_flight(self, job_id):
        with self._flight_lock:
            acquired = job_id not in self._flights
            if acquired:
                self._flights.add(job_id)
        try:
            yield acquired
        finally:
            if acquired:
                with self._flight_lock:
                    self._flights.discard(job_id)

    def refresh(self, job_id: str) -> Optional[dict]:
        row = self.store.get(job_id)
        if row is None:
            return None
        try:
            cfg = self._cfg()
        except _ConfigProblem:
            return row
        if row["state"] in TERMINAL_STATES:
            self._retry_cleanup(row, cfg)
            self._maybe_upgrade(row)
            return self.store.get(job_id)
        if row["executor"] != "remote" or row["stage"] in ("resolving", "querying", "submitting"):
            return row
        now = _now()
        if row["last_checked_at"] and now - row["last_checked_at"] < cfg["refresh_min_interval_seconds"]:
            return row
        with self._single_flight(job_id) as acquired:
            if not acquired:
                return row
            row = self.store.get(job_id)
            if row is None or row["state"] in TERMINAL_STATES:
                return row
            return self._refresh_active(row, cfg) or self.store.get(job_id)

    def _refresh_active(self, row, cfg):
        if row["stage"] == "finalizing":
            return self._finalize_step(row, cfg)
        now = _now()
        pod = executor.job_api_get(cfg["endpoint"], row["pod_job_id"]) if row["pod_job_id"] else None
        kind = self._classify(row, pod, now)
        row = self._record(row["job_id"], pod, kind, now)
        if row is None or row["state"] in TERMINAL_STATES:
            return row
        if kind in ("ended", "gone"):
            return self._start_finalize(row, "pod_ended" if kind == "ended" else "pod_gone", cfg)
        if kind == "pending" and now - (row["submitted_at"] or now) > cfg["pending_timeout_seconds"]:
            return self._pending_timeout(row, cfg)
        deadline_at = row["deadline_at"] or float("inf")
        if kind in ("pending", "running") and now > deadline_at:
            if not row["deadline_extended"]:
                # The Job API says the pod still runs: trust it once (design §8.5).
                return self.store.mutate(row["job_id"], _active(
                    "deadline passed but the Job API still reports the pod running; extended once",
                    deadline_at=now + cfg["timeout_seconds"], deadline_extended=1))
            return self._start_finalize(row, "deadline", cfg)
        if now > deadline_at:
            reason = "submit_unknown" if row["stage"] == "submit_unknown" else "deadline"
            return self._start_finalize(row, reason, cfg)
        if row["stage"] == "submit_unknown":
            return self._probe_result(row)
        return row

    @staticmethod
    def _classify(row, pod, now):
        if pod is None:
            return None
        early = now - (row["submitted_at"] or now) < GONE_GRACE_SECONDS
        if pod.get("no_pods") and not early:
            # The Linux Job API reports a job whose pod already finished (and failed) as
            # "pending" with no pods; an unscheduled pod would still be listed.
            return "ended"
        if pod["kind"] != "not_found":
            return pod["kind"]
        if early:
            return "pending"
        seen = row["remote"].get("gone_seen_at")
        if seen and now - seen >= GONE_CONFIRM_SECONDS:
            return "gone"
        return "gone_unconfirmed"

    def _record(self, job_id, pod, kind, now):
        def apply(row):
            if row["state"] not in ACTIVE_STATES:
                return False
            remote = row["remote"]
            row["last_checked_at"] = now
            row["updated_at"] = now
            if kind in ("pending", "running", "ended", "unknown", "gone"):
                if remote.get("degraded"):
                    note(row, "Job API answering again")
                remote["consecutive_errors"] = 0
                remote["degraded"] = False
                if pod.get("status") is not None:
                    if pod["status"] != row["pod_status"] and kind == "unknown":
                        note(row, f"unexpected Job API status {pod['status']!r}")
                    row["pod_status"] = pod["status"]
                if kind == "ended" and pod.get("no_pods"):
                    row["pod_status"] = "pending/no-pods"
                    note(row, "Job API reports the job pending with no pods left: the pod has finished")
            if kind in ("pending", "running", "ended", "unknown"):
                remote.pop("gone_seen_at", None)
            if kind in ("pending", "running"):
                row["stage"] = kind
            if kind == "unknown":
                _LOG.warning("job %s: unexpected Job API status %r", row["job_id"], pod.get("status"))
            if kind in ("error", "gone_unconfirmed"):
                was_degraded = bool(remote.get("degraded"))
                remote["consecutive_errors"] = remote.get("consecutive_errors", 0) + 1
                remote["degraded"] = remote["consecutive_errors"] >= DEGRADED_AFTER_ERRORS
                if kind == "gone_unconfirmed":
                    if "gone_seen_at" not in remote:
                        note(row, "Job API: job not found (waiting for confirmation)")
                    remote.setdefault("gone_seen_at", now)
                    remote["last_error"] = "NOT_FOUND"
                elif pod.get("auth"):
                    remote["last_error"] = ErrorCode.AUTH_FAILED
                    _LOG.error("job %s: the Job API rejected the execution token", row["job_id"])
                else:
                    remote["last_error"] = _truncate(str(pod["error"].get("message")))
                if remote["degraded"] and not was_degraded:
                    note(row, f"Job API degraded: {remote['last_error']}")
            return True
        return self.store.mutate(job_id, apply)

    def _start_finalize(self, row, reason, cfg):
        now = _now()

        def apply(current):
            if current["state"] not in ACTIVE_STATES:
                return False
            if current["stage"] != "finalizing":
                current.update(stage="finalizing", finalize_reason=reason,
                               finalize_deadline_at=now + cfg["finalize_grace_seconds"])
            current["updated_at"] = now
            return True
        updated = self.store.mutate(row["job_id"], apply)
        if updated is None:
            return self.store.get(row["job_id"])
        return self._finalize_step(updated, cfg, recheck=False)

    def _finalize_step(self, row, cfg, *, recheck=True):
        """Design §6.3: decide from result.json, or after the grace period by inference."""
        now = _now()
        if recheck and row["finalize_reason"] == "pod_gone" and row["pod_job_id"]:
            pod = executor.job_api_get(cfg["endpoint"], row["pod_job_id"])
            if pod["kind"] in ("pending", "running") and not pod.get("no_pods"):
                def revert(current):
                    if current["state"] not in ACTIVE_STATES or current["stage"] != "finalizing":
                        return False
                    current.update(stage=pod["kind"], pod_status=pod.get("status"),
                                   finalize_reason=None, finalize_deadline_at=None,
                                   last_checked_at=now, updated_at=now)
                    current["remote"].pop("gone_seen_at", None)
                    current["warnings"].append("the Job API reported the pod again after 'not found'")
                    return True
                return self.store.mutate(row["job_id"], revert)
        read = executor.read_result_json(row["run_url"])
        concluded = self._from_result_json(row, read)
        if concluded is not None:
            return self._finish(row["job_id"], concluded, "result_json")
        if now < (row["finalize_deadline_at"] or 0):
            return self.store.mutate(row["job_id"], _active(last_checked_at=now))
        reason = row["finalize_reason"]
        detail = {"finalize_reason": reason, "result_url": read["url"]}
        if row["remote"].get("last_error"):
            detail["last_error"] = row["remote"]["last_error"]
        if read["kind"] == "auth":
            result = err(ErrorCode.AUTH_FAILED, "Artifactory rejected the server's credentials",
                         {**detail, "service": "artifactory"})
        elif read["kind"] == "error":
            result = err(ErrorCode.REMOTE_RESULT_UNREADABLE, "result.json could not be read",
                         {**detail, "reason": read.get("reason") or read.get("http_status")})
        elif reason == "deadline":
            result = err(ErrorCode.REMOTE_TIMEOUT, "the stitch pod did not report before its deadline",
                         detail)
        elif reason == "submit_unknown":
            result = err(ErrorCode.REMOTE_SUBMIT_UNKNOWN,
                         "the create request had an uncertain outcome and no result ever appeared",
                         detail)
        else:
            result = err(ErrorCode.REMOTE_RESULT_MISSING,
                         "the stitch pod ended without publishing a result", detail)
        delete = reason == "deadline" and read["kind"] == "missing"
        updated = self._finish(row["job_id"], result, "inferred", cleanup_pending=delete or None)
        if updated is not None and delete:
            self._delete_pod(updated, cfg)
            return self.store.get(row["job_id"])
        return updated

    def _pending_timeout(self, row, cfg):
        concluded = self._from_result_json(row, executor.read_result_json(row["run_url"]))
        if concluded is not None:
            return self._finish(row["job_id"], concluded, "result_json")
        updated = self._finish(row["job_id"], err(
            ErrorCode.REMOTE_SCHEDULE_TIMEOUT, "the stitch pod was not scheduled in time",
            {"pending_timeout_seconds": cfg["pending_timeout_seconds"]}),
            "inferred", cleanup_pending=True)
        if updated is not None:
            self._delete_pod(updated, cfg)
        return self.store.get(row["job_id"])

    def _probe_result(self, row):
        concluded = self._from_result_json(row, executor.read_result_json(row["run_url"]))
        if concluded is not None:
            return self._finish(row["job_id"], concluded, "result_json")
        return self.store.mutate(row["job_id"], _active(last_checked_at=_now()))

    def _maybe_upgrade(self, row):
        if row["state"] != "failed" or row["result_source"] != "inferred" or not row["run_url"]:
            return
        now = _now()
        if now > self._token_expiry(row):
            return
        if now - (row["remote"].get("upgrade_probe_at") or 0) < UPGRADE_PROBE_SECONDS:
            return

        def mark(current):
            current["remote"]["upgrade_probe_at"] = now
            return True
        self.store.mutate(row["job_id"], mark)
        concluded = self._from_result_json(row, executor.read_result_json(row["run_url"]))
        if concluded is None or (not concluded["ok"] and concluded["error_code"] in (
                ErrorCode.REMOTE_TERMINATED, ErrorCode.REMOTE_RESULT_INVALID)):
            return
        self._finish(row["job_id"], concluded, "result_json", allow_override=True)

    # ---- pod cleanup --------------------------------------------------------------------
    def _schedule_pod_cleanup(self, row):
        """Delete a finished job's pod instead of leaving it to ttlsecondsafterfinished.

        Paths that delete at once (timeouts, cancel) set cleanup themselves; this covers a
        job that ended on its own (callback, result.json, inferred). The delay lets the
        wrapper finish its last uploads after calling back; a missed timer is retried by the
        startup sweep and by queries.
        """
        if row["executor"] != "remote" or not row["pod_job_id"] or row["cleanup"] is not None:
            return
        try:
            delay = self._cfg()["pod_cleanup_delay_seconds"]
        except _ConfigProblem:
            delay = 30
        due = _now() + delay

        def mark(current):
            if current["cleanup"] is not None or not current["pod_job_id"]:
                return False
            current["cleanup_pending"] = 1
            current["cleanup"] = {"pod_delete": "pending", "attempts": 0, "not_before": due}
            note(current, f"pod delete scheduled in {delay}s")
            return True
        if self.store.mutate(row["job_id"], mark) is None:
            return
        timer = threading.Timer(delay, self._cleanup_due, args=(row["job_id"],))
        timer.daemon = True
        with self._timers_lock:
            if self._closed:
                return
            self._timers.add(timer)
        timer.start()

    def _cleanup_due(self, job_id):
        with self._timers_lock:
            self._timers = {t for t in self._timers if t.is_alive() and t is not threading.current_thread()}
            if self._closed:
                return
        try:
            row = self.store.get(job_id)
            cleanup = (row or {}).get("cleanup") or {}
            if row and row["cleanup_pending"] and cleanup.get("pod_delete") == "pending":
                self._delete_pod(row, self._cfg())
        except Exception:  # noqa: BLE001 - a later query or the startup sweep retries
            _LOG.exception("job %s: scheduled pod delete failed", job_id)

    def close(self):
        """Stop pending cleanup timers (tests, shutdown); their pods expire via the TTL."""
        with self._timers_lock:
            self._closed = True
            timers, self._timers = self._timers, set()
        for timer in timers:
            timer.cancel()

    def _record_cleanup(self, job_id, outcome, *, warning=None):
        def apply(row):
            cleanup = dict(row["cleanup"] or {})
            cleanup["pod_delete"] = outcome
            row["cleanup"] = cleanup
            row["cleanup_pending"] = 1 if outcome == "pending" else 0
            if warning:
                row["warnings"].append(warning)
            row["updated_at"] = _now()
            note(row, f"pod delete: {outcome}" + (f" ({warning})" if warning else ""))
            return True
        return self.store.mutate(job_id, apply)

    def _delete_pod(self, row, cfg):
        if not row["pod_job_id"]:
            return self._record_cleanup(row["job_id"], "failed", warning=_ORPHAN_WARNING)
        deleted = executor.job_api_delete(cfg["endpoint"], row["pod_job_id"])
        now = _now()

        def apply(current):
            cleanup = dict(current["cleanup"] or {})
            attempts = int(cleanup.get("attempts") or 0) + 1
            cleanup.update(attempts=attempts, last_attempt_at=now)
            if deleted["ok"]:
                cleanup["pod_delete"] = "done"
                cleanup.pop("last_error", None)
                current["cleanup_pending"] = 0
            else:
                cleanup["last_error"] = _truncate(deleted["message"])
                if attempts >= CLEANUP_MAX_ATTEMPTS:
                    cleanup["pod_delete"] = "failed"
                    current["cleanup_pending"] = 0
                    current["warnings"].append(_ORPHAN_WARNING)
                else:
                    cleanup["pod_delete"] = "pending"
                    current["cleanup_pending"] = 1
            current["cleanup"] = cleanup
            current["updated_at"] = now
            note(current, f"pod delete attempt {attempts}: {cleanup['pod_delete']}"
                          + (f" ({cleanup['last_error']})" if not deleted["ok"] else ""))
            return True
        return self.store.mutate(row["job_id"], apply)

    def _retry_cleanup(self, row, cfg):
        cleanup = row["cleanup"] or {}
        if not row["cleanup_pending"] or cleanup.get("pod_delete") != "pending" or not row["pod_job_id"]:
            return
        now = _now()
        if now < (cleanup.get("not_before") or 0):
            return
        if now - (cleanup.get("last_attempt_at") or 0) < CLEANUP_RETRY_SECONDS:
            return
        self._delete_pod(row, cfg)

    # ---- cancel (design §8.3) -----------------------------------------------------------
    def cancel(self, job_id: str):
        """Returns (http_status, envelope)."""
        row = self.store.get(job_id)
        if row is None:
            return 404, not_found(job_id)
        stage_at_cancel = {}

        def apply(current):
            if current["state"] not in ACTIVE_STATES:
                return False
            if current["executor"] == "local" and current["stage"] == "running":
                return False
            now = _now()
            stage_at_cancel["stage"] = current["stage"]
            remote_run = current["executor"] == "remote" and current["run_id"] is not None
            current.update(state="cancelled", stage="done", finished_at=now, updated_at=now,
                           result_source="server", cleanup_pending=1 if remote_run else 0,
                           result=err(ErrorCode.JOB_CANCELLED, "job cancelled by request",
                                      {"job_id": current["job_id"], "run_id": current["run_id"],
                                       "pod_job_id": current["pod_job_id"],
                                       "stage_at_cancel": current["stage"]}))
            if remote_run:
                current["cleanup"] = {"pod_delete": "pending", "attempts": 0}
            return True
        updated = self.store.mutate(job_id, apply)
        if updated is None:
            current = self.store.get(job_id)
            if current["state"] == "cancelled":
                return 200, ok(self.view(current))
            if current["state"] in ("succeeded", "failed"):
                return 409, err(ErrorCode.JOB_ALREADY_FINISHED, "the job has already finished",
                                {"job_id": job_id, "state": current["state"]})
            return 409, err(ErrorCode.JOB_NOT_CANCELLABLE, NOT_CANCELLABLE_LOCAL,
                            {"job_id": job_id, "stage": current["stage"]})
        self._notify(job_id)
        if updated["cleanup_pending"]:
            stage = stage_at_cancel["stage"]
            if updated["pod_job_id"]:
                try:
                    self._delete_pod(updated, self._cfg())
                except _ConfigProblem:
                    pass
            elif stage == "submit_unknown":
                self._record_cleanup(job_id, "failed", warning=_ORPHAN_WARNING)
            # stage "submitting": the submitting thread deletes or records the outcome.
        return 200, ok(self.view(self.store.get(job_id)))

    # ---- waiting ------------------------------------------------------------------------
    def _notify(self, job_id):
        with self._events_lock:
            event = self._events.get(job_id)
        if event is not None:
            event.set()

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[dict]:
        """Block until the job is terminal: woken by the callback, with a polling fallback."""
        with self._events_lock:
            event = self._events.setdefault(job_id, threading.Event())
        try:
            poll = self._cfg()["poll_interval_seconds"]
        except _ConfigProblem:
            poll = 5
        started = time.monotonic()
        try:
            while True:
                row = self.refresh(job_id)
                if row is None or row["state"] in TERMINAL_STATES:
                    return row
                if timeout is not None and time.monotonic() - started >= timeout:
                    return row
                event.wait(poll)
                event.clear()
        finally:
            with self._events_lock:
                self._events.pop(job_id, None)

    @staticmethod
    def sync_result(row) -> dict:
        """A synchronous caller's envelope, carrying the task id (design §7.1)."""
        if row is None:
            return err(ErrorCode.JOB_NOT_FOUND, "job not found")
        result = row["result"] or err(ErrorCode.INTERNAL_ERROR, "job ended without a result")
        if result["ok"]:
            data = dict(result["data"]) if isinstance(result.get("data"), dict) else {}
            data["task_job_id"] = row["job_id"]
            return {**result, "data": data}
        detail = dict(result.get("detail") or {})
        detail.setdefault("job_id", row["job_id"])
        return {**result, "detail": detail}

    # ---- listing, views -----------------------------------------------------------------
    def get(self, job_id: str, *, refresh: bool = True) -> Optional[dict]:
        self.maybe_purge()
        return self.refresh(job_id) if refresh else self.store.get(job_id)

    def list_jobs(self, *, states=None, kind=None, plan_id=None, limit=20, before=None) -> list:
        self.maybe_purge()
        self._refresh_overdue(budget=None)
        rows = self.store.select(states=states, kind=kind, plan_id=plan_id, before=before,
                                 limit=limit)
        return [self.view(row, include_result=False) for row in rows]

    def _refresh_overdue(self, *, budget, limit=SWEEP_LIMIT):
        rows = self.store.select(states=ACTIVE_STATES, executor="remote",
                                 deadline_before=_now(), limit=limit, newest_first=False)
        if not rows:
            return
        if budget is None:
            for row in rows:
                with contextlib.suppress(Exception):
                    self.refresh(row["job_id"])
            return
        pool, _ = self.refresh_pool()
        futures = [pool.submit(self.refresh, row["job_id"]) for row in rows]
        wait_futures(futures, timeout=budget)

    def latest_succeeded(self, plan_id: str) -> Optional[dict]:
        rows = self.store.select(states=("succeeded",), kind="stitch", plan_id=plan_id, limit=1)
        return rows[0] if rows else None

    def view(self, row, *, include_result: bool = True, include_events: bool = False) -> dict:
        terminal = row["state"] in TERMINAL_STATES
        now = _now()
        end = row["finished_at"] if terminal and row["finished_at"] else now
        remote = None
        if row["executor"] == "remote":
            stored = row["remote"]
            remote = {"pod_job_id": row["pod_job_id"]}
            if terminal:
                # Poll fields go stale once the job ends; a failure's detail keeps pod_status.
                remote.update({key: stored.get(key) for key in
                               ("worker_commit", "durations", "pod_log_url", "exit_code", "signal")})
            else:
                remote.update({"pod_status": row["pod_status"],
                               "last_checked_at": iso(row["last_checked_at"]),
                               "degraded": bool(stored.get("degraded")),
                               "last_error": stored.get("last_error")})
        cleanup = None
        if row["cleanup"] and (row["cleanup_pending"] or terminal):
            cleanup = {"pod_delete": row["cleanup"].get("pod_delete")}
        view = {"job_id": row["job_id"], "kind": row["kind"], "executor": row["executor"],
                "state": row["state"], "stage": row["stage"], "terminal": terminal,
                "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"]),
                "elapsed_seconds": int(max(0, end - row["created_at"])),
                "plan_id": row["plan_id"], "run_id": row["run_id"],
                "artifactory_url": row["run_url"], "remote": remote, "cleanup": cleanup,
                "warnings": list(row["warnings"]), "poll_after_seconds": poll_after(row),
                "result_source": row["result_source"],
                "status_url": f"/jobs/{row['job_id']}", "instance_id": INSTANCE_ID}
        if not terminal:
            view["cancel_url"] = f"/jobs/{row['job_id']}/cancel"
        if include_result:
            view["result"] = _without_job_fields(row["result"], view) if terminal else None
        if include_events:
            view["events"] = [{**event, "at": iso(event["at"])}
                              for event in self.store.events(row["job_id"])]
        return view

    # ---- startup and retention ----------------------------------------------------------
    def recover_on_startup(self) -> dict:
        """Before serving: settle jobs whose owner thread died with the old process (§8.4)."""
        counts = {"interrupted": 0, "pending": 0, "submit_unknown": 0}
        for row in self.store.select(states=ACTIVE_STATES):
            stage = row["stage"]
            if stage in ("querying", "resolving") or row["executor"] == "local":
                detail = {"stage": stage, "retryable": True}
                if row["executor"] == "local":
                    detail["note"] = ("a Windows stand-alone server may leave the stitch subprocess "
                                      "running in its work directory")
                if self._finish(row["job_id"], err(ErrorCode.JOB_INTERRUPTED,
                                                   "the server restarted while the job was running",
                                                   detail), "server"):
                    counts["interrupted"] += 1
            elif stage == "submitting" and row["run_id"] is None:
                # Died before the run was recorded, so no pod can exist yet.
                if self._finish(row["job_id"], err(ErrorCode.JOB_INTERRUPTED,
                                                   "the server restarted before the pod was submitted",
                                                   {"stage": stage, "retryable": True}), "server"):
                    counts["interrupted"] += 1
            elif stage == "submitting":
                target = "pending" if row["pod_job_id"] else "submit_unknown"
                if self.store.mutate(row["job_id"], _active(stage=target)):
                    counts[target] += 1
        return counts

    def startup_sweep(self):
        """After serving starts: catch callbacks lost while the server was down."""
        with contextlib.suppress(Exception):
            self._refresh_overdue(budget=None, limit=STARTUP_SWEEP_LIMIT)
        with contextlib.suppress(Exception):
            cfg = self._cfg()
            for row in self.store.select(cleanup_pending=True, limit=STARTUP_SWEEP_LIMIT):
                if row["state"] not in TERMINAL_STATES or not row["pod_job_id"]:
                    continue
                wait = ((row["cleanup"] or {}).get("not_before") or 0) - _now()
                if wait > 0:
                    # Its timer died with the previous process: arm a new one.
                    timer = threading.Timer(wait, self._cleanup_due, args=(row["job_id"],))
                    timer.daemon = True
                    with self._timers_lock:
                        if self._closed:
                            break
                        self._timers.add(timer)
                    timer.start()
                else:
                    self._delete_pod(row, cfg)
        self.maybe_purge(force=True)

    def maybe_purge(self, *, force: bool = False):
        now = _now()
        if not force and now - self._last_purge < PURGE_INTERVAL:
            return
        self._last_purge = now
        try:
            hours = self._cfg()["job_retention_hours"]
            cutoff = now - hours * 3600
            self.store.purge(cutoff, cutoff)
        except Exception:  # noqa: BLE001 - retention must never break a request
            _LOG.exception("job retention cleanup failed")


_ORPHAN_WARNING = ("the pod could not be deleted; it may keep running until its "
                   "activedeadlineseconds expires")


def _without_job_fields(result, view):
    """The stored result minus the locating fields the view already shows at its top level."""
    if not isinstance(result, dict):
        return result
    part = "data" if result.get("ok") else "detail"
    body = result.get(part)
    if not isinstance(body, dict):
        return result
    return {**result, part: {key: value for key, value in body.items()
                             if not (key in VIEW_JOB_FIELDS and value == view[key])}}


class _ConfigProblem(Exception):
    def __init__(self, result):
        super().__init__(result.get("message"))
        self.result = result


def poll_after(row) -> Optional[int]:
    if row["state"] in TERMINAL_STATES:
        return None
    if row["remote"].get("degraded"):
        return 60
    if row["stage"] == "finalizing":
        return 10
    if row["stage"] in ("querying", "resolving", "submitting"):
        return 5
    return 30


def not_found(job_id) -> dict:
    return err(ErrorCode.JOB_NOT_FOUND, "job not found; it never existed or is past its retention",
               {"job_id": job_id, "instance_id": INSTANCE_ID})


def _clean_request(request: dict) -> dict:
    """Business fields only: never store a token."""
    return {k: v for k, v in (request or {}).items()
            if k not in ("async", "token", "authorization") and "token" not in k.lower()}


def _active(_note=None, **fields):
    def apply(row):
        if row["state"] not in ACTIVE_STATES:
            return False
        row.update(fields)
        row["updated_at"] = _now()
        if _note:
            note(row, _note)
        return True
    return apply


def _fact(_note=None, **fields):
    def apply(row):
        row.update(fields)
        row["updated_at"] = _now()
        if _note:
            note(row, _note)
        return True
    return apply


def _callback_problem(payload, row) -> Optional[str]:
    if not isinstance(payload, dict):
        return "callback body must be a JSON object"
    if payload.get("schema_version") != CALLBACK_SCHEMA_VERSION:
        return "unsupported schema_version"
    if payload.get("job_id") != row["job_id"]:
        return "job_id does not match the path"
    result = payload.get("result")
    if not executor.is_envelope(result):
        return "result is not a result envelope"
    spec_invalid = not result["ok"] and result["error_code"] == ErrorCode.WRAPPER_SPEC_INVALID
    if not spec_invalid and payload.get("run_id") != row["run_id"]:
        return "run_id does not match this job"
    return None


_manager = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    """The process-wide manager: SQLite in server mode, memory in stdio mode."""
    global _manager
    with _manager_lock:
        if _manager is None:
            serving = config.get_serve_config()
            server_mode = serving["ok"] and serving["data"]["mode"] == "server"
            store = SqliteJobStore(config.get_jobs_db_path()) if server_mode else MemoryJobStore()
            _manager = JobManager(store, server_mode=server_mode)
        return _manager


def reset_manager() -> None:
    """Forget the process-wide manager (tests and config reloads)."""
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.close()
        if manager._pool is not None:
            manager._pool.shutdown(wait=False)
        close = getattr(manager.store, "close", None)
        if close:
            with contextlib.suppress(Exception):
                close()
