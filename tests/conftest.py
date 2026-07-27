import pytest


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate every config env var and point the cache at a tmp dir."""
    for var in ("FIV_BASE_URL", "FIV_TOKEN", "ARTIFACTORY_TOKEN", "IFWI_MCP_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("IFWI_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path
