"""Environment configuration, cache layout, and deferred token access.

This is the only module that reads os.environ.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from .result import ok, err, ErrorCode

_DEFAULT_CACHE = Path.home() / ".ifwi-stitching-mcp" / "cache"


class ConfigError(Exception):
    pass


def get_base_url() -> str:
    return os.environ.get("FIV_BASE_URL", "").rstrip("/")


def get_cache_dir() -> Path:
    raw = os.environ.get("IFWI_MCP_CACHE_DIR")
    return Path(raw).expanduser() if raw else _DEFAULT_CACHE


def cache_subdir(name: str) -> Path:
    sub = get_cache_dir() / name
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def get_ca_bundle():
    """CA bundle for TLS verification of FIV/Artifactory requests.

    Honors FIV_CA_BUNDLE / REQUESTS_CA_BUNDLE if set; otherwise falls back to the
    system trust store (holds the Intel intranet CAs). Returns a path str, or True
    to use requests' default certifi bundle if no system store is found.
    """
    for var in ("FIV_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(var)
        if val:
            return val
    for candidate in ("/etc/ssl/certs/ca-certificates.crt",
                      "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(candidate):
            return candidate
    return True


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
    return ok({})
