"""Authenticated REST facade mounted beside FastMCP's /mcp route."""
import asyncio
import hmac
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from starlette.responses import JSONResponse

from . import config, interfaces
from .result import ErrorCode, err

_LOG = logging.getLogger(__name__)
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ifwi-api")
_JOBS = {}
_LOCK = threading.Lock()
_TTL = 3600
_MAX_JOBS = 256
# New on every process start; helps distinguish a restarted server from a lost job.
_INSTANCE_ID = uuid.uuid4().hex


def _status(result: dict) -> int:
    if result["ok"]:
        return 200
    code = result["error_code"]
    if code in (ErrorCode.INVALID_ARGUMENT, ErrorCode.PLAN_INVALID):
        return 400
    if code in (ErrorCode.MULTIPLE_SWIMLANES, ErrorCode.IFWI_BINARY_AMBIGUOUS,
                ErrorCode.CONFIG_AMBIGUOUS):
        return 409
    return 502 if code != ErrorCode.INTERNAL_ERROR else 500


def _response(result: dict) -> JSONResponse:
    return JSONResponse(result, status_code=_status(result))


def _authorized(request) -> bool:
    header = request.headers.get("Authorization", "")
    token = config.get_serve_token()
    return bool(token) and hmac.compare_digest(header, "Bearer " + token)


def _run(operation, payload):
    try:
        return operation(payload)
    except Exception:
        _LOG.exception("REST operation failed")
        return err(ErrorCode.INTERNAL_ERROR, "operation failed unexpectedly")


def _complete(job_id, operation, payload):
    result = _run(operation, payload)
    with _LOCK:
        _JOBS[job_id] = {"state": "succeeded" if result["ok"] else "failed",
                         "result": result, "updated": time.monotonic()}


async def _post(request, operation, *, direct_dry_run=False):
    if not _authorized(request):
        return JSONResponse(err(ErrorCode.AUTH_FAILED, "bearer token required"), status_code=401)
    try:
        payload = await request.json()
    except ValueError:
        return _response(err(ErrorCode.INVALID_ARGUMENT, "request body must be valid JSON"))
    if not isinstance(payload, dict) or type(payload.get("async", False)) is not bool:
        return _response(err(ErrorCode.INVALID_ARGUMENT, "payload must be an object with optional boolean async"))
    # A stitch preview is a final response, never a queued job. Preserve the
    # normal async behavior for queries and real stitching requests.
    if payload.get("async") and not (direct_dry_run and payload.get("dry_run") is True):
        with _LOCK:
            now = time.monotonic()
            for key, job in list(_JOBS.items()):
                if job["state"] != "running" and now - job["updated"] > _TTL:
                    del _JOBS[key]
            if len(_JOBS) >= _MAX_JOBS:
                return JSONResponse(err(ErrorCode.INTERNAL_ERROR, "job queue is full"), status_code=503)
            job_id = uuid.uuid4().hex
            _JOBS[job_id] = {"state": "running", "result": None, "updated": now}
        try:
            # One thread per job: long stitches must never queue behind each other.
            threading.Thread(target=_complete, args=(job_id, operation, payload),
                             name=f"ifwi-job-{job_id[:8]}", daemon=True).start()
        except RuntimeError:
            with _LOCK:
                del _JOBS[job_id]
            return JSONResponse(err(ErrorCode.INTERNAL_ERROR, "job queue is unavailable"), status_code=503)
        return JSONResponse({"ok": True, "data": {"job_id": job_id, "state": "running",
                               "status_url": f"/jobs/{job_id}",
                               "instance_id": _INSTANCE_ID}}, status_code=202)
    result = await asyncio.get_running_loop().run_in_executor(_POOL, _run, operation, payload)
    return _response(result)


async def query(request):
    return await _post(request, interfaces.query)


async def stitching(request):
    return await _post(request, interfaces.stitching, direct_dry_run=True)


async def job_status(request):
    if not _authorized(request):
        return JSONResponse(err(ErrorCode.AUTH_FAILED, "bearer token required"), status_code=401)
    job_id = request.path_params["job_id"]
    with _LOCK:
        job = _JOBS.get(job_id)
        if job and job["state"] != "running" and time.monotonic() - job["updated"] > _TTL:
            del _JOBS[job_id]
            job = None
        snapshot = dict(job) if job else None
    if snapshot is None:
        return JSONResponse(err(ErrorCode.JOB_NOT_FOUND,
                                "job not found; job status is process-local and is lost after a server restart or when requests reach another instance",
                                {"instance_id": _INSTANCE_ID}), status_code=404)
    return JSONResponse({"ok": True, "data": {"job_id": job_id, "state": snapshot["state"],
                                              "result": snapshot["result"],
                                              "instance_id": _INSTANCE_ID}})


def create_app(mcp):
    """Build the single ASGI app hosting MCP and the three authenticated REST routes."""
    app = mcp.http_app()
    app.add_route("/query", query, methods=["POST"])
    app.add_route("/stitching", stitching, methods=["POST"])
    app.add_route("/jobs/{job_id}", job_status, methods=["GET"])
    return app