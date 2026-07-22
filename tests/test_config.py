from pathlib import Path
from ifwi_mcp import config
from ifwi_mcp.result import ErrorCode


def test_default_cache_dir(monkeypatch):
    monkeypatch.delenv("IFWI_MCP_CACHE_DIR", raising=False)
    assert config.get_cache_dir() == Path.home() / ".ifwi-stitching-mcp" / "cache"


def test_cache_dir_from_env(clean_env):
    assert config.get_cache_dir() == clean_env / "cache"


def test_cache_subdir_is_created(clean_env):
    sub = config.cache_subdir("ifwi")
    assert sub.is_dir()
    assert sub == clean_env / "cache" / "ifwi"


def test_base_url_strips_trailing_slash(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "https://fiv.example.com/")
    assert config.get_base_url() == "https://fiv.example.com"


def test_fiv_auth_missing_returns_missing_token(clean_env):
    result = config.get_fiv_auth_header()
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.MISSING_TOKEN
    assert result["detail"] == {"which": "fiv"}


def test_fiv_auth_present(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_TOKEN", "Basic abc123")
    result = config.get_fiv_auth_header()
    assert result == {"ok": True, "data": {"header_value": "Basic abc123"}}


def test_artifactory_token_missing(clean_env):
    result = config.get_artifactory_token()
    assert result["error_code"] == ErrorCode.MISSING_TOKEN
    assert result["detail"] == {"which": "artifactory"}


def test_validate_startup_rejects_missing_base_url(clean_env):
    result = config.validate_startup()
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert result["detail"]["param"] == "FIV_BASE_URL"


def test_validate_startup_rejects_non_http_url(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "ftp://nope")
    assert config.validate_startup()["ok"] is False


def test_validate_startup_ok(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "https://fiv.example.com")
    assert config.validate_startup() == {"ok": True, "data": {}}
