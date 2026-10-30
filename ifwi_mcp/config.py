"""Environment configuration, cache layout, and deferred token access.

This is the only module that reads os.environ or the on-disk config file.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import urllib3
from dotenv import load_dotenv

from .result import ok, err, ErrorCode

# Loads .env from the cwd or one of its parents; never overrides a var already set
# (e.g. inline in an MCP host's `env` block), so hosts always take priority.
load_dotenv()

# All outbound requests use verify=False (see fiv_portal/artifactory/executor/deliverables);
# suppress the resulting per-request InsecureRequestWarning noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_DEFAULT_CACHE = Path.home() / ".ifwi-stitching-mcp" / "cache"
_EXEC_MODES = ("local", "remote")
_SERVE_MODES = ("local", "server")
_DEFAULT_TIMEOUT = 3600
_DEFAULT_POLL = 5
# First image tag that ships /app/stitch_wrapper.py (Linux stitch pods).
_DEFAULT_POD_IMAGE = "amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.1.1"
# First tag whose stitch_wrapper honours spec.timeouts.deadline_epoch (wrapper 1.1.1).
_MIN_WRAPPER_IMAGE = (1, 1, 1)
_DEFAULT_WORKER_REPO = "https://github.com/i-BinSun/ifwi-stitching-mcp.git"
_DEFAULT_WORKER_REF = "main"
_DEFAULT_WRAPPER_PATH = "/app/stitch_wrapper.py"
# Sum of the wrapper's default reserves (upload 300 + min worker 120 + reserve 45).
_WRAPPER_RESERVES = 465
# (key, env var, default, minimum) of the integer job-tracking settings.
_JOB_SETTINGS = (
    ("pending_timeout_seconds", "IFWI_MCP_EXEC_PENDING_TIMEOUT", 900, 1),
    ("finalize_grace_seconds", "IFWI_MCP_EXEC_FINALIZE_GRACE", 120, 0),
    ("refresh_min_interval_seconds", "IFWI_MCP_REFRESH_MIN_INTERVAL", 5, 0),
    ("refresh_workers", "IFWI_MCP_REFRESH_WORKERS", 16, 1),
    ("max_inline_spec_bytes", "IFWI_MCP_MAX_INLINE_SPEC", 4096, 256),
    ("max_active_jobs", "IFWI_MCP_MAX_ACTIVE_JOBS", 64, 1),
    # Remote jobs beyond this many live pods wait in state "queued"; 0 means no limit.
    ("max_running_pods", "IFWI_MCP_MAX_RUNNING_PODS", 5, 0),
    ("job_retention_hours", "IFWI_MCP_JOB_RETENTION_HOURS", 168, 1),
    ("pod_cleanup_delay_seconds", "IFWI_MCP_POD_CLEANUP_DELAY", 30, 0),
)
_IMAGE_TAG_RE = re.compile(r":v(\d+)\.(\d+)\.(\d+)$")
# These values are spliced into the pod's shell command line.
_SHELL_SAFE_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")

# Parsed config file, keyed by (path, mtime_ns, size) so an edit is picked up at once.
_config_file_cache: dict = {}


def reset_cache() -> None:
    """Drop the memoized config-file contents."""
    _config_file_cache.clear()


class ConfigError(Exception):
    pass


def get_base_url() -> str:
    return os.environ.get("FIV_BASE_URL", "https://fiv-ifwi.intel.com").rstrip("/")


def get_cache_dir() -> Path:
    raw = os.environ.get("IFWI_MCP_CACHE_DIR")
    return Path(raw).expanduser() if raw else _DEFAULT_CACHE


def cache_subdir(name: str) -> Path:
    sub = get_cache_dir() / name
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def get_fiv_auth_header() -> dict:
    # IFWI_FIVPORTAL_TOKEN is the name stitch pods are provisioned with.
    value = os.environ.get("FIV_TOKEN") or os.environ.get("IFWI_FIVPORTAL_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "FIV_TOKEN is not set", {"which": "fiv"})
    return ok({"header_value": f"Bearer {value}"})


def get_artifactory_token() -> dict:
    # IFWI_ARTIFACTORY_TOKEN is the name stitch pods are provisioned with.
    value = os.environ.get("ARTIFACTORY_TOKEN") or os.environ.get("IFWI_ARTIFACTORY_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "ARTIFACTORY_TOKEN is not set", {"which": "artifactory"})
    return ok({"token": value})


def get_artifactory_upload_base() -> dict:
    """Base Artifactory URL under which each remote stitch pod publishes its run."""
    raw = os.environ.get("IFWI_MCP_ARTIFACTORY_REPO")
    if raw is None:
        loaded = _section("execution")
        if not loaded["ok"]:
            return loaded
        raw = loaded["data"]["section"].get("artifactory_repo")
    if not raw:
        return err(ErrorCode.CONFIG_INVALID, "execution.artifactory_repo is not set",
                   {"param": "execution.artifactory_repo",
                    "expected": "e.g. https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/FIV_Portal/RemoteStitch"})
    base_url = str(raw).strip().rstrip("/")
    if not _SHELL_SAFE_RE.match(base_url):
        return err(ErrorCode.CONFIG_INVALID, "execution.artifactory_repo has characters unsafe for the pod command",
                   {"param": "execution.artifactory_repo", "expected": _SHELL_SAFE_RE.pattern})
    return ok({"base_url": base_url})


def get_config_path() -> Path:
    """Path of the JSON config file (IFWI_MCP_CONFIG, else next to the cache dir)."""
    raw = os.environ.get("IFWI_MCP_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return get_cache_dir().parent / "config.json"


def load_config_file() -> dict:
    """Read the JSON config file. A missing file is not an error (all defaults apply)."""
    path = get_config_path()
    if not path.is_file():
        return ok({"config": {}, "path": str(path), "exists": False})
    try:
        stat = path.stat()
    except OSError:
        return ok({"config": {}, "path": str(path), "exists": False})
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _config_file_cache.get(key)
    if cached is not None:
        return ok({"config": cached, "path": str(path), "exists": True})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return err(ErrorCode.CONFIG_INVALID, "config file is not readable JSON",
                   {"path": str(path), "reason": str(exc)})
    if not isinstance(data, dict):
        return err(ErrorCode.CONFIG_INVALID, "config file must contain a JSON object",
                   {"path": str(path), "expected": "object"})
    _config_file_cache.clear()
    _config_file_cache[key] = data
    return ok({"config": data, "path": str(path), "exists": True})


def _section(name: str) -> dict:
    loaded = load_config_file()
    if not loaded["ok"]:
        return loaded
    raw = loaded["data"]["config"].get(name) or {}
    if not isinstance(raw, dict):
        return err(ErrorCode.CONFIG_INVALID, "config section must be an object",
                   {"path": loaded["data"]["path"], "param": name, "expected": "object"})
    return ok({"section": raw, "path": loaded["data"]["path"]})


def _positive_int(value, default: int, param: str, path: str, minimum: int = 1):
    expected = "positive integer" if minimum == 1 else f"integer >= {minimum}"
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return err(ErrorCode.CONFIG_INVALID, "value must be an integer",
                   {"path": path, "param": param, "expected": expected})
    if number < minimum:
        return err(ErrorCode.CONFIG_INVALID, "value must be positive" if minimum == 1
                   else f"value must be at least {minimum}",
                   {"path": path, "param": param, "expected": expected})
    return number


def _as_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _shell_safe(value: str, param: str, path: str):
    if not _SHELL_SAFE_RE.match(value):
        return err(ErrorCode.CONFIG_INVALID, "value contains characters unsafe for the pod command",
                   {"path": path, "param": param, "expected": _SHELL_SAFE_RE.pattern})
    return value


def get_execution_config() -> dict:
    """Where a stitch job runs: local subprocess or a pod created through the Job API.

    Config file (execution section) is the source of truth; the IFWI_MCP_EXEC_*
    env vars override it. The token is never echoed back — only has_token.
    """
    loaded = _section("execution")
    if not loaded["ok"]:
        return loaded
    section, path = loaded["data"]["section"], loaded["data"]["path"]

    mode = str(os.environ.get("IFWI_MCP_EXEC_MODE") or section.get("mode") or "local").strip().lower()
    if mode not in _EXEC_MODES:
        return err(ErrorCode.CONFIG_INVALID, "execution.mode must be 'local' or 'remote'",
                   {"path": path, "param": "execution.mode", "expected": list(_EXEC_MODES),
                    "got": mode})

    serving = get_serve_config()
    if not serving["ok"]:
        return serving

    endpoint = str(os.environ.get("IFWI_MCP_EXEC_ENDPOINT") or section.get("endpoint") or "").rstrip("/")
    if mode == "remote":
        if not endpoint:
            return err(ErrorCode.CONFIG_INVALID, "remote execution requires execution.endpoint",
                       {"path": path, "param": "execution.endpoint",
                        "expected": "http(s) URL of the Job API"})
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return err(ErrorCode.CONFIG_INVALID, "execution.endpoint must be a valid http(s) URL",
                       {"path": path, "param": "execution.endpoint", "got": endpoint})

    timeout = _positive_int(os.environ.get("IFWI_MCP_EXEC_TIMEOUT") or section.get("timeout_seconds"),
                            _DEFAULT_TIMEOUT, "execution.timeout_seconds", path)
    if isinstance(timeout, dict):
        return timeout
    poll = _positive_int(os.environ.get("IFWI_MCP_EXEC_POLL_INTERVAL") or section.get("poll_interval_seconds"),
                         _DEFAULT_POLL, "execution.poll_interval_seconds", path)
    if isinstance(poll, dict):
        return poll
    # 0 deletes a server-mode run directory right after its upload.
    retention = _positive_int(os.environ.get("IFWI_MCP_RUN_RETENTION_HOURS")
                              or section.get("run_retention_hours"),
                              0, "execution.run_retention_hours", path, minimum=0)
    if isinstance(retention, dict):
        return retention

    pod = {}
    for key, env, default in (("image", "IFWI_MCP_EXEC_IMAGE", _DEFAULT_POD_IMAGE),
                              ("repo_url", "IFWI_MCP_EXEC_REPO", _DEFAULT_WORKER_REPO),
                              ("repo_ref", "IFWI_MCP_EXEC_REF", _DEFAULT_WORKER_REF),
                              ("wrapper_path", "IFWI_MCP_WRAPPER_PATH", _DEFAULT_WRAPPER_PATH)):
        value = _shell_safe(str(os.environ.get(env) or section.get(key) or default).strip(),
                            f"execution.{key}", path)
        if isinstance(value, dict):
            return value
        pod[key] = value

    jobs = {}
    for key, env, default, minimum in _JOB_SETTINGS:
        value = _positive_int(os.environ.get(env) or section.get(key), default,
                              f"execution.{key}", path, minimum=minimum)
        if isinstance(value, dict):
            return value
        jobs[key] = value

    report_base_url = str(os.environ.get("IFWI_MCP_REPORT_BASE_URL")
                          or section.get("report_base_url") or "").strip().rstrip("/")
    if report_base_url:
        parsed = urlparse(report_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return err(ErrorCode.CONFIG_INVALID, "execution.report_base_url must be a valid http(s) URL",
                       {"path": path, "param": "execution.report_base_url", "got": report_base_url})

    return ok({"mode": mode, "endpoint": endpoint, "config_path": path,
               "has_token": bool(_token_from(section)),
               "timeout_seconds": timeout, "poll_interval_seconds": poll,
               "run_retention_hours": retention, "report_base_url": report_base_url,
               **pod, **jobs})


def _token_from(section: dict) -> str:
    return os.environ.get("IFWI_MCP_EXEC_TOKEN") or str(section.get("token") or "")


def get_execution_token() -> str:
    """Bearer token for the remote runner endpoint. Empty string when unauthenticated."""
    env = os.environ.get("IFWI_MCP_EXEC_TOKEN")
    if env:
        return env
    loaded = _section("execution")
    if not loaded["ok"]:
        return ""
    return _token_from(loaded["data"]["section"])


def get_serve_config() -> dict:
    """MCP transport, independent of where stitch jobs execute."""
    loaded = _section("serve")
    if not loaded["ok"]:
        return loaded
    section, path = loaded["data"]["section"], loaded["data"]["path"]
    mode = str(os.environ.get("IFWI_MCP_SERVER_MODE") or section.get("mode") or "local").strip().lower()
    if mode not in _SERVE_MODES:
        return err(ErrorCode.CONFIG_INVALID, "serve.mode must be 'local' or 'server'",
                   {"path": path, "param": "serve.mode", "expected": list(_SERVE_MODES), "got": mode})
    host = str(os.environ.get("IFWI_MCP_SERVER_HOST") or section.get("host") or "127.0.0.1").strip()
    if not host:
        return err(ErrorCode.CONFIG_INVALID, "serve.host cannot be empty",
                   {"path": path, "param": "serve.host"})
    port = _positive_int(os.environ.get("IFWI_MCP_SERVER_PORT") or section.get("port"),
                         8000, "serve.port", path)
    if isinstance(port, dict):
        return port
    if port > 65535:
        return err(ErrorCode.CONFIG_INVALID, "serve.port must be at most 65535",
                   {"path": path, "param": "serve.port"})
    token = os.environ.get("IFWI_MCP_SERVER_TOKEN") or str(section.get("token") or "")
    if mode == "server" and not token:
        return err(ErrorCode.CONFIG_INVALID, "server mode requires a bearer token",
                   {"path": path, "param": "serve.token", "env": "IFWI_MCP_SERVER_TOKEN"})
    return ok({"mode": mode, "host": host, "port": port, "has_token": bool(token)})


def get_serve_token() -> str:
    """Server-mode bearer token; never include it in the public configuration result."""
    env = os.environ.get("IFWI_MCP_SERVER_TOKEN")
    if env:
        return env
    loaded = _section("serve")
    return str(loaded["data"]["section"].get("token") or "") if loaded["ok"] else ""


def get_stitch_config() -> dict:
    """Extra pip packages for the stitch tool's venv when it ships no requirements file.

    Off by default: if neither a requirements.txt nor a platform-specific
    requirements_windows.txt/requirements_linux.txt is found anywhere in the extracted
    tool, nothing installs unless this is configured — so an unconfigured setup behaves
    exactly as before. IFWI_MCP_STITCH_FALLBACK_DEPS (comma-separated) overrides the
    stitch.fallback_deps config list.
    """
    loaded = _section("stitch")
    if not loaded["ok"]:
        return loaded
    section, path = loaded["data"]["section"], loaded["data"]["path"]
    env = os.environ.get("IFWI_MCP_STITCH_FALLBACK_DEPS")
    if env is not None:
        deps = [item.strip() for item in env.split(",") if item.strip()]
    else:
        raw = section.get("fallback_deps") or []
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            return err(ErrorCode.CONFIG_INVALID, "stitch.fallback_deps must be a list of strings",
                       {"path": path, "param": "stitch.fallback_deps", "expected": "list[str]"})
        deps = raw
    return ok({"fallback_deps": deps, "config_path": path})


def get_deliverables_config() -> dict:
    """How finished-job artifacts are packaged."""
    loaded = _section("deliverables")
    if not loaded["ok"]:
        return loaded
    section = loaded["data"]["section"]
    archive = _as_bool(os.environ.get("IFWI_MCP_DELIVERABLES_ARCHIVE"),
                       _as_bool(section.get("archive"), True))
    return ok({"archive": archive, "config_path": loaded["data"]["path"]})


def get_jobs_db_path() -> Path:
    """SQLite job store used in server mode (IFWI_MCP_JOBS_DB, else <cache>/jobs.db)."""
    raw = os.environ.get("IFWI_MCP_JOBS_DB")
    return Path(raw).expanduser() if raw else get_cache_dir() / "jobs.db"


def image_tag_version(image: str):
    """(major, minor, patch) of a vX.Y.Z image tag, or None for any other tag."""
    match = _IMAGE_TAG_RE.search(image)
    return tuple(int(part) for part in match.groups()) if match else None


def validate_startup() -> dict:
    base = get_base_url()
    if not base:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL must be set",
                   {"param": "FIV_BASE_URL", "expected": "non-empty http(s) URL"})
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL must be a valid http(s) URL",
                   {"param": "FIV_BASE_URL", "expected": "http(s) URL"})
    try:
        get_cache_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return err(ErrorCode.INVALID_ARGUMENT, "cache dir not creatable",
                   {"param": "IFWI_MCP_CACHE_DIR", "expected": f"writable path ({exc})"})
    execution = get_execution_config()
    if not execution["ok"]:
        return execution
    serving = get_serve_config()
    if not serving["ok"]:
        return serving
    if serving["data"]["mode"] == "server" and execution["data"]["mode"] == "local":
        # Server-mode clients only get Artifactory links, never server-local paths.
        upload = get_artifactory_upload_base()
        if not upload["ok"]:
            return upload
    if execution["data"]["mode"] == "remote":
        checked = _validate_remote(execution["data"])
        if not checked["ok"]:
            return checked
    if serving["data"]["mode"] == "server":
        db_path = get_jobs_db_path()
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with open(db_path, "ab"):
                pass
        except OSError as exc:
            return err(ErrorCode.CONFIG_INVALID, "job database is not writable",
                       {"param": "IFWI_MCP_JOBS_DB", "path": str(db_path), "reason": str(exc)})
    return ok({})


def _validate_remote(execution: dict) -> dict:
    path = execution["config_path"]
    timeout = execution["timeout_seconds"]
    if execution["pending_timeout_seconds"] >= timeout:
        return err(ErrorCode.CONFIG_INVALID,
                   "execution.pending_timeout_seconds must be smaller than timeout_seconds",
                   {"path": path, "param": "execution.pending_timeout_seconds",
                    "timeout_seconds": timeout})
    if timeout <= _WRAPPER_RESERVES:
        return err(ErrorCode.CONFIG_INVALID,
                   f"execution.timeout_seconds must exceed the stitch wrapper's reserves ({_WRAPPER_RESERVES}s)",
                   {"path": path, "param": "execution.timeout_seconds", "got": timeout})
    version = image_tag_version(execution["image"])
    if version is not None and version < _MIN_WRAPPER_IMAGE:
        return err(ErrorCode.CONFIG_INVALID,
                   "execution.image predates the stitch_wrapper this server needs (deadline_epoch); "
                   "use v{}.{}.{} or later".format(*_MIN_WRAPPER_IMAGE),
                   {"path": path, "param": "execution.image", "got": execution["image"]})
    return ok({})
