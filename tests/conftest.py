import pytest


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate every config env var and point the cache at a tmp dir."""
    for var in ("FIV_BASE_URL", "FIV_TOKEN", "ARTIFACTORY_TOKEN", "IFWI_MCP_CACHE_DIR",
                "IFWI_MCP_CONFIG", "IFWI_MCP_EXEC_MODE", "IFWI_MCP_EXEC_ENDPOINT",
                "IFWI_MCP_EXEC_TOKEN", "IFWI_MCP_EXEC_TIMEOUT",
                "IFWI_MCP_EXEC_POLL_INTERVAL", "IFWI_MCP_DELIVERABLES_ARCHIVE",
                "IFWI_MCP_STITCH_FALLBACK_DEPS"):
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
