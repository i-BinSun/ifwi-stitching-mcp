"""Entry point of a remote stitch pod: `python -m ifwi_mcp.worker --plan-b64 ... --upload-url ...`.

stitch_wrapper.py (in the pod image) runs this module. It runs the plan through
executor._execute_local (the same codepath local mode uses), uploads every deliverable to
<upload-url>/deliverables/, then writes the ok()/err() envelope -- with those Artifactory
URLs -- to <upload-url>/result.json, to IFWI_STITCH_RESULT_FILE for the wrapper, and as the
last stdout line. It does so on every outcome, including crashes and SIGTERM.

Credentials come from the pod environment (ARTIFACTORY_TOKEN or IFWI_ARTIFACTORY_TOKEN).
"""
import argparse
import contextlib
import datetime
import json
import os
import signal
import sys
import tempfile
import time
import zlib
from pathlib import Path

from . import artifactory, config, executor, plan as plan_mod
from .result import ok, err, ErrorCode

RESULT_SCHEMA_VERSION = 1
# SIGTERM this close to the wrapper deadline means the time budget ran out.
_DEADLINE_SLACK_SECONDS = 5


def run(plan: dict, upload_url: str) -> dict:
    exec_cfg = config.get_execution_config()
    if not exec_cfg["ok"]:
        return exec_cfg
    # Name the local run after the server's run_id so log and run names match the job.
    run_id = os.environ.get("IFWI_STITCH_RUN_ID", "")
    result = executor._execute_local(plan, exec_cfg["data"], **({"run_id": run_id} if run_id else {}))
    if not result["ok"]:
        # Publish whatever was kept (usually the stitch log) so the failure is debuggable.
        items = ((result["detail"].get("deliverables") or {}).get("items")) or []
        if items:
            published = executor.publish_deliverables(items, upload_url)
            if published["ok"]:
                result["detail"]["deliverables"] = published["data"]["items"]
        return result

    published = executor.publish_deliverables(result["data"]["deliverables"]["items"], upload_url)
    if not published["ok"]:
        return published
    data = dict(result["data"])
    data["deliverables"] = published["data"]["items"]
    return ok(data)


def _deadline_reached() -> bool:
    try:
        deadline = float(os.environ.get("IFWI_STITCH_DEADLINE_EPOCH") or 0)
    except ValueError:
        return False
    return bool(deadline) and time.time() >= deadline - _DEADLINE_SLACK_SECONDS


class _Terminated(BaseException):
    """Raised from the SIGTERM handler; subprocess.run kills the stitch child on the way out."""


def _on_sigterm(signum, _frame):
    raise _Terminated(signum)


def _terminated_result(step: str) -> dict:
    if _deadline_reached():
        return err(ErrorCode.REMOTE_TIMEOUT, "stitch worker stopped at the pod time budget",
                   {"step": step})
    return err(ErrorCode.REMOTE_TERMINATED, "stitch worker was terminated", {"step": step})


def _stamp(result: dict) -> dict:
    """result.json fields that let the server check the file belongs to this run."""
    stamped = {"schema_version": RESULT_SCHEMA_VERSION, **result,
               "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    run_id = os.environ.get("IFWI_STITCH_RUN_ID")
    if run_id:
        stamped["run_id"] = run_id
    return stamped


def _write_result_file(result: dict) -> None:
    """Atomic copy of the final envelope for stitch_wrapper (IFWI_STITCH_RESULT_FILE)."""
    target = os.environ.get("IFWI_STITCH_RESULT_FILE")
    if not target:
        return
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(result), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(json.dumps(err(ErrorCode.INTERNAL_ERROR, "could not write the result file",
                             {"path": target, "reason": str(exc)})), file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ifwi_mcp.worker")
    parser.add_argument("--plan-b64", required=True)
    parser.add_argument("--upload-url", required=True)
    args = parser.parse_args(argv)
    upload_url = args.upload_url.rstrip("/")

    previous = None
    with contextlib.suppress(ValueError):          # not the main thread (tests)
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        try:
            plan = plan_mod.decode_plan(args.plan_b64)
        except (ValueError, zlib.error) as exc:
            result = err(ErrorCode.PLAN_INVALID, "plan_b64 is not a valid encoded plan",
                         {"reason": str(exc)})
        else:
            try:
                result = run(plan, upload_url)
            except _Terminated:
                result = _terminated_result("stitch")
            except Exception as exc:  # result.json must still be written
                result = err(ErrorCode.INTERNAL_ERROR, "stitch worker crashed",
                             {"type": type(exc).__name__, "reason": str(exc)})
        # Publishing the result must not be cut short by a second SIGTERM.
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except _Terminated:
        result = _terminated_result("startup")

    result = _stamp(result)
    _write_result_file(result)
    print(json.dumps(result))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / executor.REMOTE_RESULT_NAME
        path.write_text(json.dumps(result), encoding="utf-8")
        published = artifactory.upload_file(str(path), f"{upload_url}/{executor.REMOTE_RESULT_NAME}")
    if previous is not None:
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, previous)
    if not published["ok"]:
        print(json.dumps(published), file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
