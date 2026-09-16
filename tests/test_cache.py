import os
import time

from ifwi_mcp import cache, config, stitch_runner
from ifwi_mcp.result import ErrorCode


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_rejects_unknown_category(clean_env):
    r = cache.cleanup(categories=["nope"])
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert r["detail"]["got"] == ["nope"]


def test_rejects_negative_age(clean_env):
    assert cache.cleanup(max_age_days=-1)["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_keeps_fresh_entries_and_removes_old_ones(clean_env):
    ifwi = config.cache_subdir("ifwi")
    fresh, stale = ifwi / "fresh.bin", ifwi / "stale.bin"
    fresh.write_bytes(b"0123456789")
    stale.write_bytes(b"0123456789")
    _age(stale, 30)

    r = cache.cleanup(max_age_days=14, categories=["ifwi"])
    assert r["ok"] is True
    assert r["data"]["removed_count"] == 1 and r["data"]["kept_count"] == 1
    assert r["data"]["freed_bytes"] == 10
    assert fresh.exists() and not stale.exists()


def test_dry_run_deletes_nothing(clean_env):
    stale = config.cache_subdir("ifwi") / "stale.bin"
    stale.write_bytes(b"x")
    _age(stale, 30)

    r = cache.cleanup(max_age_days=14, categories=["ifwi"], dry_run=True)
    assert r["data"]["removed_count"] == 1
    assert stale.exists()


def test_cleaning_a_workspace_never_deletes_through_its_links(clean_env, tmp_path):
    tool_root = tmp_path / "tool"
    (tool_root / "config").mkdir(parents=True)
    (tool_root / "cli.py").write_text("print('hi')\n")
    (tool_root / "config" / "Config_Stitch_A.ini").write_text("[Configuration]\n")
    workspace = stitch_runner.materialize_workspace(tool_root, "key")
    assert workspace["ok"] is True

    r = cache.cleanup(max_age_days=0, categories=["work"])
    assert r["ok"] is True
    assert not (config.cache_subdir("work") / "key").exists()
    assert (tool_root / "cli.py").is_file()
    assert (tool_root / "config" / "Config_Stitch_A.ini").is_file()
