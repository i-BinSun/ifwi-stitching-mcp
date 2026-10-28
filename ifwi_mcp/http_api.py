"""Authenticated REST facade mounted beside FastMCP's /mcp route.

Tracked requests (async /query, every real /stitching) are jobs in jobs.JobManager; their
state survives a restart in server mode. /internal/jobs/{id}/result is the stitch pod's
one-shot callback and authenticates with the per-job report token, not serve.token.
"""
import asyncio
import hmac
import logging
from concurrent.futures import ThreadPoolExecutor

from starlette.responses import JSONResponse

from . import config, interfaces, jobs
from .result import ErrorCode, err, ok

_LOG = logging.getLogger(__name__)
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ifwi-api")
# Short control work (submissions, callbacks, cancels) never queues behind the
# hour-long synchronous waits that occupy _POOL.
_CONTROL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ifwi-control")
# Kept for diagnostics: lets a client tell a restarted server from a lost job.
_INSTANCE_ID = jobs.INSTANCE_ID
_CALLBACK_MAX_BYTES = 1024 * 1024
_LIST_MAX = 100

_STATUS = {
    ErrorCode.INVALID_ARGUMENT: 400, ErrorCode.PLAN_INVALID: 400,
    ErrorCode.MULTIPLE_SWIMLANES: 409, ErrorCode.IFWI_BINARY_AMBIGUOUS: 409,
    ErrorCode.CONFIG_AMBIGUOUS: 409, ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.JOB_CANCELLED: 409, ErrorCode.JOB_ALREADY_FINISHED: 409,
    ErrorCode.JOB_NOT_CANCELLABLE: 409, ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.JOB_QUEUE_FULL: 503, ErrorCode.JOB_INTERRUPTED: 503,
    ErrorCode.REMOTE_SCHEDULE_TIMEOUT: 504, ErrorCode.REMOTE_TIMEOUT: 504,
    ErrorCode.WRAPPER_SPEC_INVALID: 500, ErrorCode.INTERNAL_ERROR: 500,
}


def _status(result: dict) -> int:
    if result["ok"]:
        return 200
    return _STATUS.get(result["error_code"], 502)


def _response(result: dict, headers=None) -> JSONResponse:
    status = _status(result)
    if status == 503 and headers is None:
        headers = {"Retry-After": "30"}
    return JSONResponse(result, status_code=status, headers=headers)


def _authorized(request) -> bool:
    header = request.headers.get("Authorization", "")
    token = config.get_serve_token()
    return bool(token) and hmac.compare_digest(header, "Bearer " + token)


def _unauthorized() -> JSONResponse:
    return JSONResponse(err(ErrorCode.AUTH_FAILED, "bearer token required"), status_code=401)


def _run(operation, payload):
    try:
        return operation(payload)
    except Exception:
        _LOG.exception("REST operation failed")
        return err(ErrorCode.INTERNAL_ERROR, "operation failed unexpectedly")


async def _in_pool(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_POOL, fn, *args)


async def _in_control(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_CONTROL_POOL, fn, *args)


def _job_headers(view: dict) -> dict:
    headers = {"Location": view["status_url"], "X-Job-Id": view["job_id"]}
    if view["poll_after_seconds"]:
        headers["Retry-After"] = str(view["poll_after_seconds"])
    return headers


def _submitted(status, job) -> JSONResponse:
    if status == "error":
        return _response(job)
    view = jobs.get_manager().view(job, include_result=status == "existing")
    return JSONResponse(ok(view), status_code=202 if status == "created" else 200,
                        headers=_job_headers(view))


async def _payload(request):
    try:
        payload = await request.json()
    except ValueError:
        return None, _response(err(ErrorCode.INVALID_ARGUMENT, "request body must be valid JSON"))
    if not isinstance(payload, dict) or type(payload.get("async", False)) is not bool:
        return None, _response(err(ErrorCode.INVALID_ARGUMENT,
                                   "payload must be an object with optional boolean async"))
    bad = jobs.check_client_request_id(payload.get("client_request_id"))
    if bad:
        return None, _response(bad)
    return payload, None


async def query(request):
    if not _authorized(request):
        return _unauthorized()
    payload, problem = await _payload(request)
    if problem:
        return problem
    if not payload.get("async"):
        return _response(await _in_pool(_run, interfaces.query, payload))
    manager = jobs.get_manager()
    status, job = await _in_control(manager.submit_query, payload, payload.get("client_request_id"))
    return _submitted(status, job)


def _wait_sync(job_id):
    return jobs.get_manager().wait(job_id)


async def stitching(request):
    if not _authorized(request):
        return _unauthorized()
    payload, problem = await _payload(request)
    if problem:
        return problem
    # A stitch preview is a final response, never a job.
    if payload.get("dry_run") is True:
        return _response(await _in_pool(_run, interfaces.stitching, payload))
    manager = jobs.get_manager()
    status, job = await _in_control(manager.submit_stitching, payload, payload.get("client_request_id"))
    if status == "error" or payload.get("async"):
        return _submitted(status, job)
    row = await _in_pool(_wait_sync, job["job_id"])
    result = manager.sync_result(row)
    return _response(result, headers={"Location": f"/jobs/{job['job_id']}",
                                      "X-Job-Id": job["job_id"]})


def _refreshed_view(job_id):
    manager = jobs.get_manager()
    row = manager.get(job_id)
    return manager.view(row) if row is not None else None


async def job_status(request):
    if not _authorized(request):
        return _unauthorized()
    job_id = request.path_params["job_id"]
    manager = jobs.get_manager()
    pool, slots = manager.refresh_pool()
    if slots.acquire(blocking=False):
        try:
            view = await asyncio.get_running_loop().run_in_executor(pool, _refreshed_view, job_id)
        finally:
            slots.release()
    else:
        # Every refresh thread is busy (slow Job API): answer from the stored record.
        row = manager.get(job_id, refresh=False)
        view = manager.view(row) if row is not None else None
    if view is None:
        return JSONResponse(jobs.not_found(job_id), status_code=404)
    headers = {"Retry-After": str(view["poll_after_seconds"])} if not view["terminal"] else None
    return JSONResponse(ok(view), headers=headers)


async def list_jobs(request):
    if not _authorized(request):
        return _unauthorized()
    params = request.query_params
    states = tuple(s for s in (params.get("state") or "").split(",") if s) or None
    try:
        limit = int(params.get("limit") or 20)
        before = float(params["before"]) if params.get("before") else None
    except ValueError:
        return _response(err(ErrorCode.INVALID_ARGUMENT, "limit and before must be numbers",
                             {"param": "limit/before"}))
    if not 1 <= limit <= _LIST_MAX:
        return _response(err(ErrorCode.INVALID_ARGUMENT, f"limit must be between 1 and {_LIST_MAX}",
                             {"param": "limit"}))
    manager = jobs.get_manager()
    pool, _ = manager.refresh_pool()
    items = await asyncio.get_running_loop().run_in_executor(
        pool, lambda: manager.list_jobs(states=states, kind=params.get("kind"),
                                        plan_id=params.get("plan_id"), limit=limit, before=before))
    next_before = None
    if len(items) == limit:
        last = manager.get(items[-1]["job_id"], refresh=False)
        next_before = last["created_at"] if last else None
    return JSONResponse(ok({"jobs": items, "count": len(items), "next_before": next_before}))


async def cancel_job(request):
    if not _authorized(request):
        return _unauthorized()
    job_id = request.path_params["job_id"]
    manager = jobs.get_manager()
    status, body = await _in_control(manager.cancel, job_id)
    return JSONResponse(body, status_code=status)


async def job_callback(request):
    """The stitch pod's one result callback; never accepts serve.token."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > _CALLBACK_MAX_BYTES:
        return JSONResponse(err(ErrorCode.INVALID_ARGUMENT, "callback body too large"), status_code=413)
    body = await request.body()
    if len(body) > _CALLBACK_MAX_BYTES:
        return JSONResponse(err(ErrorCode.INVALID_ARGUMENT, "callback body too large"), status_code=413)
    manager = jobs.get_manager()
    try:
        status, result = await _in_control(manager.handle_callback, request.path_params["job_id"],
                                           request.headers.get("Authorization", ""), body)
    except Exception:  # noqa: BLE001 - e.g. the database is locked: the wrapper retries 5xx
        _LOG.exception("callback handling failed")
        return JSONResponse(err(ErrorCode.INTERNAL_ERROR, "callback could not be recorded"),
                            status_code=503)
    return JSONResponse(result, status_code=status)


def create_app(mcp):
    """Build the single ASGI app hosting MCP and the REST routes."""
    app = mcp.http_app()
    app.add_route("/query", query, methods=["POST"])
    app.add_route("/stitching", stitching, methods=["POST"])
    app.add_route("/jobs", list_jobs, methods=["GET"])
    app.add_route("/jobs/{job_id}", job_status, methods=["GET"])
    app.add_route("/jobs/{job_id}/cancel", cancel_job, methods=["POST"])
    app.add_route("/internal/jobs/{job_id}/result", job_callback, methods=["POST"])
    return app
