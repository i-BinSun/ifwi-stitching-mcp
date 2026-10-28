"""Job records: a SQLite store for server mode and an in-memory store for stdio mode.

Both stores expose the same small interface. Every state change goes through
mutate(), which applies a caller-supplied function to the current row atomically,
so conditional writes ("only while the job is still active") are race-free.
"""
import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

SCHEMA_VERSION = 1
# Plain columns, in table order. JSON columns are exposed on rows without the _json suffix.
_PLAIN = ("job_id", "kind", "executor", "state", "stage", "client_request_id", "request_hash",
          "created_at", "updated_at", "finished_at", "submitted_at", "deadline_at",
          "deadline_extended", "finalize_deadline_at", "finalize_reason", "last_checked_at",
          "plan_id", "run_id", "run_url", "pod_job_id", "pod_status", "report_token_hash",
          "result_source", "cleanup_pending", "owner")
_JSON = {"request": dict, "remote": dict, "cleanup": None, "warnings": list, "result": None}
COLUMNS = _PLAIN + tuple(f"{name}_json" for name in _JSON)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    executor          TEXT,
    state             TEXT NOT NULL,
    stage             TEXT NOT NULL,
    client_request_id TEXT,
    request_hash      TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    finished_at       REAL,
    submitted_at      REAL,
    deadline_at       REAL,
    deadline_extended INTEGER NOT NULL DEFAULT 0,
    finalize_deadline_at REAL,
    finalize_reason   TEXT,
    last_checked_at   REAL,
    plan_id           TEXT,
    run_id            TEXT UNIQUE,
    run_url           TEXT,
    pod_job_id        TEXT,
    pod_status        TEXT,
    report_token_hash TEXT,
    result_source     TEXT,
    cleanup_pending   INTEGER NOT NULL DEFAULT 0,
    owner             TEXT,
    request_json      TEXT,
    remote_json       TEXT,
    cleanup_json      TEXT,
    warnings_json     TEXT,
    result_json       TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_active  ON jobs(state, deadline_at);
CREATE INDEX IF NOT EXISTS ix_jobs_cleanup ON jobs(cleanup_pending);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_plan    ON jobs(plan_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idem ON jobs(kind, client_request_id)
    WHERE client_request_id IS NOT NULL;
"""


def new_row(**fields) -> dict:
    """A complete job row with defaults for every column."""
    row = {name: None for name in _PLAIN}
    row.update(deadline_extended=0, cleanup_pending=0, request={}, remote={}, cleanup=None,
               warnings=[], result=None)
    row.update(fields)
    return row


def _to_record(row: dict) -> tuple:
    values = [row.get(name) for name in _PLAIN]
    values += [None if row.get(name) is None else json.dumps(row[name]) for name in _JSON]
    return tuple(values)


def _from_record(record) -> dict:
    row = {name: record[name] for name in _PLAIN}
    for name, empty in _JSON.items():
        raw = record[f"{name}_json"]
        row[name] = json.loads(raw) if raw else (empty() if empty else None)
    return row


def _matches(row: dict, f: dict) -> bool:
    if f.get("states") and row["state"] not in f["states"]:
        return False
    for key in ("kind", "plan_id", "executor"):
        if f.get(key) is not None and row[key] != f[key]:
            return False
    if f.get("before") is not None and not row["created_at"] < f["before"]:
        return False
    if f.get("deadline_before") is not None and not (
            row["deadline_at"] is not None and row["deadline_at"] < f["deadline_before"]):
        return False
    if f.get("cleanup_pending") is not None and bool(row["cleanup_pending"]) != f["cleanup_pending"]:
        return False
    return True


class JobStore:
    """Interface shared by SqliteJobStore and MemoryJobStore."""

    def create(self, row: dict, *, max_active: int):
        """Atomically: idempotency lookup, active-job limit, insert.

        Returns ("created", row) | ("existing", row) | ("conflict", row) | ("full", count).
        """
        raise NotImplementedError

    def get(self, job_id: str) -> Optional[dict]:
        raise NotImplementedError

    def mutate(self, job_id: str, fn: Callable[[dict], bool]) -> Optional[dict]:
        """Apply fn to the current row under the write lock. fn edits the row in place and
        returns True to commit; returns the committed row, or None if nothing was written."""
        raise NotImplementedError

    def select(self, *, states=None, kind=None, plan_id=None, executor=None, before=None,
               deadline_before=None, cleanup_pending=None, limit=None, newest_first=True) -> list:
        raise NotImplementedError

    def count_active(self) -> int:
        raise NotImplementedError

    def purge(self, finished_before: float, created_before: float) -> int:
        """Delete finished jobs older than finished_before and any job created before
        created_before (a never-finished remote job nobody asked about)."""
        raise NotImplementedError


class MemoryJobStore(JobStore):
    """stdio mode: one dict per process, never written to disk."""

    def __init__(self):
        self._rows = {}
        self._lock = threading.RLock()

    def create(self, row, *, max_active):
        with self._lock:
            idem = row.get("client_request_id")
            if idem:
                existing = next((r for r in self._rows.values()
                                 if r["kind"] == row["kind"] and r["client_request_id"] == idem), None)
                if existing is not None:
                    same = existing["request_hash"] == row["request_hash"]
                    return ("existing" if same else "conflict"), copy.deepcopy(existing)
            active = sum(1 for r in self._rows.values() if r["state"] in ACTIVE_STATES)
            if active >= max_active:
                return "full", active
            self._rows[row["job_id"]] = copy.deepcopy(row)
            return "created", copy.deepcopy(row)

    def get(self, job_id):
        with self._lock:
            row = self._rows.get(job_id)
            return copy.deepcopy(row) if row is not None else None

    def mutate(self, job_id, fn):
        with self._lock:
            current = self._rows.get(job_id)
            if current is None:
                return None
            row = copy.deepcopy(current)
            if not fn(row):
                return None
            self._rows[job_id] = row
            return copy.deepcopy(row)

    def select(self, *, states=None, kind=None, plan_id=None, executor=None, before=None,
               deadline_before=None, cleanup_pending=None, limit=None, newest_first=True):
        filters = {"states": states, "kind": kind, "plan_id": plan_id, "executor": executor,
                   "before": before, "deadline_before": deadline_before,
                   "cleanup_pending": cleanup_pending}
        with self._lock:
            rows = [copy.deepcopy(r) for r in self._rows.values() if _matches(r, filters)]
        rows.sort(key=lambda r: r["created_at"], reverse=newest_first)
        return rows[:limit] if limit else rows

    def count_active(self):
        with self._lock:
            return sum(1 for r in self._rows.values() if r["state"] in ACTIVE_STATES)

    def purge(self, finished_before, created_before):
        with self._lock:
            doomed = [key for key, r in self._rows.items()
                      if (r["finished_at"] is not None and r["finished_at"] < finished_before)
                      or r["created_at"] < created_before]
            for key in doomed:
                del self._rows[key]
            return len(doomed)


class SqliteJobStore(JobStore):
    """Server mode. One shared write connection guarded by a lock that spans the whole
    transaction; one read connection per thread (WAL keeps readers off the writer)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._write = self._connect()
        self._migrate()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _reader(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _migrate(self):
        with self._lock:
            version = self._write.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                self._write.executescript(_SCHEMA)
                self._write.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self):
        with self._lock:
            self._write.close()

    def _tx(self, body):
        with self._lock:
            self._write.execute("BEGIN IMMEDIATE")
            try:
                result = body(self._write)
            except BaseException:
                self._write.execute("ROLLBACK")
                raise
            self._write.execute("COMMIT")
            return result

    def _insert(self, conn, row):
        placeholders = ", ".join("?" for _ in COLUMNS)
        conn.execute(f"INSERT INTO jobs ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                     _to_record(row))

    def create(self, row, *, max_active):
        def body(conn):
            idem = row.get("client_request_id")
            if idem:
                found = conn.execute("SELECT * FROM jobs WHERE kind=? AND client_request_id=?",
                                     (row["kind"], idem)).fetchone()
                if found is not None:
                    existing = _from_record(found)
                    same = existing["request_hash"] == row["request_hash"]
                    return ("existing" if same else "conflict"), existing
            active = conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')"
                                  ).fetchone()[0]
            if active >= max_active:
                return "full", active
            self._insert(conn, row)
            return "created", copy.deepcopy(row)
        return self._tx(body)

    def get(self, job_id):
        found = self._reader().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return _from_record(found) if found is not None else None

    def mutate(self, job_id, fn):
        def body(conn):
            found = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if found is None:
                return None
            row = _from_record(found)
            if not fn(row):
                return None
            assignments = ", ".join(f"{name}=?" for name in COLUMNS[1:])
            conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?",
                         _to_record(row)[1:] + (job_id,))
            return row
        return self._tx(body)

    def select(self, *, states=None, kind=None, plan_id=None, executor=None, before=None,
               deadline_before=None, cleanup_pending=None, limit=None, newest_first=True):
        clauses, args = [], []
        if states:
            clauses.append(f"state IN ({', '.join('?' for _ in states)})")
            args += list(states)
        for column, value in (("kind", kind), ("plan_id", plan_id), ("executor", executor)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if before is not None:
            clauses.append("created_at < ?")
            args.append(before)
        if deadline_before is not None:
            clauses.append("deadline_at IS NOT NULL AND deadline_at < ?")
            args.append(deadline_before)
        if cleanup_pending is not None:
            clauses.append("cleanup_pending=?")
            args.append(1 if cleanup_pending else 0)
        sql = "SELECT * FROM jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_from_record(r) for r in self._reader().execute(sql, args).fetchall()]

    def count_active(self):
        return self._reader().execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]

    def purge(self, finished_before, created_before):
        def body(conn):
            return conn.execute(
                "DELETE FROM jobs WHERE (finished_at IS NOT NULL AND finished_at < ?) OR created_at < ?",
                (finished_before, created_before)).rowcount
        return self._tx(body)
