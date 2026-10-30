import pytest

from ifwi_mcp import config, fiv_portal, jobs


@pytest.fixture(autouse=True)
def _reset_caches():
    """Module-level memoization must not leak between tests."""
    fiv_portal.reset_cache()
    config.reset_cache()
    jobs.reset_manager()
    yield
    jobs.reset_manager()
    fiv_portal.reset_cache()
    config.reset_cache()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate every config env var and point the cache at a tmp dir."""
    for var in ("FIV_BASE_URL", "FIV_TOKEN", "ARTIFACTORY_TOKEN", "IFWI_MCP_CACHE_DIR",
                "IFWI_MCP_CONFIG", "IFWI_MCP_EXEC_MODE", "IFWI_MCP_EXEC_ENDPOINT",
                "IFWI_MCP_EXEC_TOKEN", "IFWI_MCP_EXEC_TIMEOUT",
                "IFWI_MCP_EXEC_POLL_INTERVAL", "IFWI_MCP_DELIVERABLES_ARCHIVE",
                "IFWI_MCP_STITCH_FALLBACK_DEPS", "IFWI_MCP_SERVER_MODE",
                "IFWI_MCP_SERVER_HOST", "IFWI_MCP_SERVER_PORT", "IFWI_MCP_SERVER_TOKEN",
                "IFWI_MCP_RUN_RETENTION_HOURS",
                "IFWI_MCP_ARTIFACTORY_REPO", "IFWI_MCP_EXEC_IMAGE", "IFWI_MCP_EXEC_REPO",
                "IFWI_MCP_EXEC_REF", "IFWI_ARTIFACTORY_TOKEN", "IFWI_FIVPORTAL_TOKEN",
                "IFWI_MCP_REPORT_BASE_URL", "IFWI_MCP_WRAPPER_PATH",
                "IFWI_MCP_EXEC_PENDING_TIMEOUT", "IFWI_MCP_EXEC_FINALIZE_GRACE",
                "IFWI_MCP_REFRESH_MIN_INTERVAL", "IFWI_MCP_REFRESH_WORKERS",
                "IFWI_MCP_MAX_INLINE_SPEC", "IFWI_MCP_MAX_ACTIVE_JOBS", "IFWI_MCP_MAX_RUNNING_PODS",
                "IFWI_MCP_JOB_RETENTION_HOURS", "IFWI_MCP_JOBS_DB",
                "IFWI_STITCH_RESULT_FILE", "IFWI_STITCH_RUN_ID", "IFWI_STITCH_DEADLINE_EPOCH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("IFWI_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("IFWI_MCP_CONFIG", str(tmp_path / "config.json"))
    return tmp_path


@pytest.fixture
def write_config(tmp_path):
    """Write the JSON config file that clean_env points IFWI_MCP_CONFIG at."""
    import json

    def _write(data):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path
    return _write
