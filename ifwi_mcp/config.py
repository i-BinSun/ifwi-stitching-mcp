"""Environment configuration, cache layout, and deferred token access.

This is the only module that reads os.environ or the on-disk config file.
"""
import json
import os
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
    value = os.environ.get("FIV_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "FIV_TOKEN is not set", {"which": "fiv"})
    return ok({"header_value": f"Bearer {value}"})


def get_artifactory_token() -> dict:
    value = os.environ.get("ARTIFACTORY_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "ARTIFACTORY_TOKEN is not set", {"which": "artifactory"})
    return ok({"token": value})


def get_artifactory_upload_base() -> dict:
    """Base Artifactory URL that remote-stitch deliverables get PUT under."""
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
    return ok({"base_url": str(raw).rstrip("/")})


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


def _positive_int(value, default: int, param: str, path: str):
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return err(ErrorCode.CONFIG_INVALID, "value must be an integer",
                   {"path": path, "param": param, "expected": "positive integer"})
    if number <= 0:
        return err(ErrorCode.CONFIG_INVALID, "value must be positive",
                   {"path": path, "param": param, "expected": "positive integer"})
    return number


def _as_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def get_execution_config() -> dict:
    """Where a stitch job runs: local subprocess or a remote runner endpoint.

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
    if serving["data"]["mode"] == "server" and mode != "remote":
        return err(ErrorCode.CONFIG_INVALID, "server mode requires remote execution",
                   {"path": path, "param": "execution.mode", "expected": "remote", "got": mode})

    endpoint = str(os.environ.get("IFWI_MCP_EXEC_ENDPOINT") or section.get("endpoint") or "").rstrip("/")
    if mode == "remote":
        if not endpoint:
            return err(ErrorCode.CONFIG_INVALID, "remote execution requires execution.endpoint",
                       {"path": path, "param": "execution.endpoint",
                        "expected": "http(s) URL of the stitch runner"})
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

    script_name = str(os.environ.get("IFWI_MCP_EXEC_SCRIPT_NAME") or
                      section.get("script_name") or "mcp_stitch").strip()

    return ok({"mode": mode, "endpoint": endpoint, "config_path": path,
               "has_token": bool(_token_from(section)),
               "timeout_seconds": timeout, "poll_interval_seconds": poll,
               "script_name": script_name})


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
    mode = str(os.environ.get("IFWI_MCP_SERVE_MODE") or section.get("mode") or "local").strip().lower()
    if mode not in _SERVE_MODES:
        return err(ErrorCode.CONFIG_INVALID, "serve.mode must be 'local' or 'server'",
                   {"path": path, "param": "serve.mode", "expected": list(_SERVE_MODES), "got": mode})
    host = str(os.environ.get("IFWI_MCP_SERVE_HOST") or section.get("host") or "127.0.0.1").strip()
    if not host:
        return err(ErrorCode.CONFIG_INVALID, "serve.host cannot be empty",
                   {"path": path, "param": "serve.host"})
    port = _positive_int(os.environ.get("IFWI_MCP_SERVE_PORT") or section.get("port"),
                         8000, "serve.port", path)
    if isinstance(port, dict):
        return port
    if port > 65535:
        return err(ErrorCode.CONFIG_INVALID, "serve.port must be at most 65535",
                   {"path": path, "param": "serve.port"})
    token = os.environ.get("IFWI_MCP_SERVE_TOKEN") or str(section.get("token") or "")
    if mode == "server" and not token:
        return err(ErrorCode.CONFIG_INVALID, "server mode requires a bearer token",
                   {"path": path, "param": "serve.token", "env": "IFWI_MCP_SERVE_TOKEN"})
    return ok({"mode": mode, "host": host, "port": port, "has_token": bool(token)})


def get_serve_token() -> str:
    """Server-mode bearer token; never include it in the public configuration result."""
    env = os.environ.get("IFWI_MCP_SERVE_TOKEN")
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
    return ok({})
