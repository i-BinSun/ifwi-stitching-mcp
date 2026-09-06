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
    monkeypatch.setenv("FIV_TOKEN", "abc123")
    result = config.get_fiv_auth_header()
    assert result == {"ok": True, "data": {"header_value": "Bearer abc123"}}


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


def test_execution_defaults_to_local_without_config_file(clean_env):
    result = config.get_execution_config()
    assert result["ok"] is True
    assert result["data"]["mode"] == "local"
    assert result["data"]["endpoint"] == ""
    assert result["data"]["has_token"] is False


def test_execution_remote_from_config_file(clean_env, write_config):
    write_config({"execution": {"mode": "remote", "token": "t",
                                "endpoint": "https://runner.example.com/api/",
                                "timeout_seconds": 60, "poll_interval_seconds": 2}})
    result = config.get_execution_config()
    assert result["ok"] is True
    assert result["data"]["mode"] == "remote"
    assert result["data"]["endpoint"] == "https://runner.example.com/api"
    assert result["data"]["timeout_seconds"] == 60
    assert result["data"]["poll_interval_seconds"] == 2
    assert result["data"]["has_token"] is True
    assert "token" not in result["data"]          # secret never echoed back
    assert config.get_execution_token() == "t"


def test_execution_remote_without_endpoint_is_rejected(clean_env, write_config):
    write_config({"execution": {"mode": "remote"}})
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.endpoint"


def test_execution_rejects_unknown_mode(clean_env, write_config):
    write_config({"execution": {"mode": "cloud"}})
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.mode"


def test_execution_rejects_non_http_endpoint(clean_env, write_config):
    write_config({"execution": {"mode": "remote", "endpoint": "ftp://runner"}})
    assert config.get_execution_config()["error_code"] == ErrorCode.CONFIG_INVALID


def test_execution_env_overrides_config_file(clean_env, write_config, monkeypatch):
    write_config({"execution": {"mode": "local", "timeout_seconds": 60,
                                "poll_interval_seconds": 5}})
    monkeypatch.setenv("IFWI_MCP_EXEC_MODE", "remote")
    monkeypatch.setenv("IFWI_MCP_EXEC_ENDPOINT", "https://other.example.com")
    monkeypatch.setenv("IFWI_MCP_EXEC_TOKEN", "env-token")
    monkeypatch.setenv("IFWI_MCP_EXEC_TIMEOUT", "120")
    monkeypatch.setenv("IFWI_MCP_EXEC_POLL_INTERVAL", "3")
    result = config.get_execution_config()
    assert result["data"]["mode"] == "remote"
    assert result["data"]["endpoint"] == "https://other.example.com"
    assert result["data"]["timeout_seconds"] == 120
    assert result["data"]["poll_interval_seconds"] == 3
    assert config.get_execution_token() == "env-token"


def test_execution_fully_configurable_without_a_config_file(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_EXEC_MODE", "remote")
    monkeypatch.setenv("IFWI_MCP_EXEC_ENDPOINT", "https://runner.example.com/api/")
    monkeypatch.setenv("IFWI_MCP_EXEC_TOKEN", "t")
    result = config.get_execution_config()
    assert result["ok"] is True
    assert result["data"]["endpoint"] == "https://runner.example.com/api"
    assert result["data"]["has_token"] is True


def test_execution_rejects_non_numeric_timeout_env(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_EXEC_TIMEOUT", "soon")
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.timeout_seconds"


def test_bad_config_file_is_reported(clean_env, tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    assert config.load_config_file()["error_code"] == ErrorCode.CONFIG_INVALID


def test_validate_startup_rejects_bad_execution_config(clean_env, write_config, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "https://fiv.example.com")
    write_config({"execution": {"mode": "remote"}})
    assert config.validate_startup()["error_code"] == ErrorCode.CONFIG_INVALID


def test_deliverables_archive_defaults_true(clean_env):
    assert config.get_deliverables_config()["data"]["archive"] is True


def test_deliverables_archive_can_be_disabled(clean_env, write_config):
    write_config({"deliverables": {"archive": False}})
    assert config.get_deliverables_config()["data"]["archive"] is False


def test_deliverables_archive_env_override(clean_env, write_config, monkeypatch):
    write_config({"deliverables": {"archive": True}})
    monkeypatch.setenv("IFWI_MCP_DELIVERABLES_ARCHIVE", "false")
    assert config.get_deliverables_config()["data"]["archive"] is False
