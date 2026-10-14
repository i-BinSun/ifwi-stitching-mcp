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


def test_base_url_defaults_when_unset(clean_env):
    assert config.get_base_url() == "https://fiv-ifwi.intel.com"


def test_validate_startup_rejects_empty_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "")
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
    assert "fiv_project_id" not in result["data"]
    assert "token" not in result["data"]          # secret never echoed back
    assert config.get_execution_token() == "t"


def test_execution_remote_without_endpoint_is_rejected(clean_env, write_config):
    write_config({"execution": {"mode": "remote"}})
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.endpoint"


def test_execution_remote_without_fiv_project_id_is_allowed(clean_env, write_config):
    write_config({"execution": {"mode": "remote", "endpoint": "https://runner.example.com/api"}})
    result = config.get_execution_config()
    assert result["ok"] is True
    assert "fiv_project_id" not in result["data"]


def test_execution_rejects_unknown_mode(clean_env, write_config):
    write_config({"execution": {"mode": "cloud"}})
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.mode"


def test_execution_rejects_non_http_endpoint(clean_env, write_config):
    write_config({"execution": {"mode": "remote", "endpoint": "ftp://runner"}})
    assert config.get_execution_config()["error_code"] == ErrorCode.CONFIG_INVALID


def test_execution_pod_defaults_and_overrides(clean_env, write_config, monkeypatch):
    write_config({"execution": {"repo_ref": "release-1"}})
    monkeypatch.setenv("IFWI_MCP_EXEC_IMAGE", "registry.example.com/img:v1")
    data = config.get_execution_config()["data"]
    assert data["image"] == "registry.example.com/img:v1"
    assert data["repo_url"].endswith("ifwi-stitching-mcp.git")
    assert data["repo_ref"] == "release-1"


def test_execution_rejects_shell_unsafe_pod_values(clean_env, write_config):
    write_config({"execution": {"repo_ref": "main; rm -rf /"}})
    result = config.get_execution_config()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.repo_ref"


def test_pod_token_names_are_accepted(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_ARTIFACTORY_TOKEN", "af")
    monkeypatch.setenv("IFWI_FIVPORTAL_TOKEN", "fiv")
    assert config.get_artifactory_token()["data"]["token"] == "af"
    assert config.get_fiv_auth_header()["data"]["header_value"] == "Bearer fiv"


def test_artifactory_upload_base_rejects_shell_unsafe_url(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_ARTIFACTORY_REPO", "https://af/repo dir")
    assert config.get_artifactory_upload_base()["error_code"] == ErrorCode.CONFIG_INVALID


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


def test_serve_defaults_to_local_stdio(clean_env):
    assert config.get_serve_config() == {"ok": True, "data": {
        "mode": "local", "host": "127.0.0.1", "port": 8000, "has_token": False}}


def test_serve_server_from_config_file(clean_env, write_config):
    write_config({"serve": {"mode": "server", "host": "0.0.0.0", "port": 8765,
                             "token": "secret"}})
    serving = config.get_serve_config()
    assert serving["data"] == {"mode": "server", "host": "0.0.0.0",
                               "port": 8765, "has_token": True}
    assert config.get_serve_token() == "secret"
    assert "secret" not in str(serving)


def test_serve_env_overrides_file_and_does_not_change_execution(clean_env, write_config,
                                                                  monkeypatch):
    write_config({"serve": {"mode": "local", "port": 9000, "token": "file-token"}})
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVE_HOST", "127.0.0.1")
    monkeypatch.setenv("IFWI_MCP_SERVE_PORT", "8123")
    monkeypatch.setenv("IFWI_MCP_SERVE_TOKEN", "env-token")
    assert config.get_serve_config()["data"]["port"] == 8123
    assert config.get_serve_token() == "env-token"
    assert config.get_execution_config()["detail"]["param"] == "execution.mode"


def test_serve_server_requires_token(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "server")
    assert config.get_serve_config()["detail"]["param"] == "serve.token"
    assert config.validate_startup()["error_code"] == ErrorCode.CONFIG_INVALID


def test_server_mode_rejects_local_execution_even_when_token_present(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVE_TOKEN", "secret")
    result = config.validate_startup()
    assert result["error_code"] == ErrorCode.CONFIG_INVALID
    assert result["detail"]["param"] == "execution.mode"
    assert config.get_execution_config()["ok"] is False


def test_server_mode_accepts_remote_execution(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "server")
    monkeypatch.setenv("IFWI_MCP_SERVE_TOKEN", "secret")
    monkeypatch.setenv("IFWI_MCP_EXEC_MODE", "remote")
    monkeypatch.setenv("IFWI_MCP_EXEC_ENDPOINT", "https://runner.example.com")
    assert config.validate_startup()["ok"] is True


def test_serve_rejects_unknown_mode_and_invalid_port(clean_env, monkeypatch):
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "remote")
    assert config.get_serve_config()["detail"]["param"] == "serve.mode"
    monkeypatch.setenv("IFWI_MCP_SERVE_MODE", "local")
    monkeypatch.setenv("IFWI_MCP_SERVE_PORT", "65536")
    assert config.get_serve_config()["detail"]["param"] == "serve.port"


def test_stitch_fallback_deps_defaults_empty(clean_env):
    assert config.get_stitch_config()["data"]["fallback_deps"] == []


def test_stitch_fallback_deps_from_config_file(clean_env, write_config):
    write_config({"stitch": {"fallback_deps": ["colorama", "cbor2"]}})
    assert config.get_stitch_config()["data"]["fallback_deps"] == ["colorama", "cbor2"]


def test_stitch_fallback_deps_env_override(clean_env, write_config, monkeypatch):
    write_config({"stitch": {"fallback_deps": ["colorama"]}})
    monkeypatch.setenv("IFWI_MCP_STITCH_FALLBACK_DEPS", "cryptography, lxml ,cbor2")
    assert config.get_stitch_config()["data"]["fallback_deps"] == ["cryptography", "lxml", "cbor2"]


def test_stitch_fallback_deps_rejects_non_list(clean_env, write_config):
    write_config({"stitch": {"fallback_deps": "colorama"}})
    r = config.get_stitch_config()
    assert r["error_code"] == ErrorCode.CONFIG_INVALID
    assert r["detail"]["param"] == "stitch.fallback_deps"


def test_deliverables_archive_defaults_true(clean_env):
    assert config.get_deliverables_config()["data"]["archive"] is True


def test_deliverables_archive_can_be_disabled(clean_env, write_config):
    write_config({"deliverables": {"archive": False}})
    assert config.get_deliverables_config()["data"]["archive"] is False


def test_deliverables_archive_env_override(clean_env, write_config, monkeypatch):
    write_config({"deliverables": {"archive": True}})
    monkeypatch.setenv("IFWI_MCP_DELIVERABLES_ARCHIVE", "false")
    assert config.get_deliverables_config()["data"]["archive"] is False
