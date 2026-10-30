# tests/test_deliverables.py
import json
import zipfile

from ifwi_mcp import deliverables
from ifwi_mcp.result import ErrorCode


def test_collect_copies_hashes_and_archives(clean_env, tmp_path):
    stitched = tmp_path / "out.bin"; stitched.write_bytes(b"binary")
    log = tmp_path / "run.log"; log.write_text("done")
    r = deliverables.collect("plan-1", [
        {"path": str(stitched), "kind": "stitched_bin", "required": True},
        {"path": str(log), "kind": "log", "required": False},
    ])
    assert r["ok"] is True
    kinds = {item["kind"]: item for item in r["data"]["items"]}
    assert kinds["stitched_bin"]["bytes"] == 6
    assert len(kinds["stitched_bin"]["sha256"]) == 64
    assert r["data"]["deliverables_dir"].endswith("plan-1")
    with zipfile.ZipFile(r["data"]["archive"]) as zf:
        assert sorted(zf.namelist()) == ["out.bin", "run.log"]
    manifest = json.loads(open(r["data"]["manifest_path"], encoding="utf-8").read())
    assert manifest["plan_id"] == "plan-1" and manifest["item_count"] == 2


def test_collect_skips_optional_but_fails_on_required(clean_env, tmp_path):
    missing = str(tmp_path / "nothing.bin")
    present = tmp_path / "a.log"; present.write_text("x")
    r = deliverables.collect("plan-2", [
        {"path": str(present), "kind": "log", "required": False},
        {"path": missing, "kind": "extra", "required": False},
    ])
    assert r["data"]["item_count"] == 1
    r = deliverables.collect("plan-3", [
        {"path": missing, "kind": "stitched_bin", "required": True}])
    assert r["error_code"] == ErrorCode.DELIVERABLE_MISSING


def test_collect_without_archive(clean_env, write_config, tmp_path):
    write_config({"deliverables": {"archive": False}})
    f = tmp_path / "a.bin"; f.write_bytes(b"z")
    r = deliverables.collect("plan-4", [{"path": str(f), "kind": "stitched_bin",
                                         "required": True}])
    assert r["data"]["archive"] is None


def test_collect_rejects_unsafe_plan_id(clean_env):
    assert deliverables.collect("../evil", [])["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_load_manifest_roundtrip(clean_env, tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"z")
    deliverables.collect("plan-5", [{"path": str(f), "kind": "stitched_bin",
                                     "required": True}])
    loaded = deliverables.load_manifest("plan-5")
    assert loaded["ok"] is True
    assert loaded["data"]["items"][0]["kind"] == "stitched_bin"


def test_load_manifest_missing(clean_env):
    assert deliverables.load_manifest("plan-6")["error_code"] == ErrorCode.DELIVERABLE_MISSING


def test_fetch_downloads_remote_deliverables(clean_env, monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=0):
            yield b"remote-bytes"

    monkeypatch.setattr(deliverables.requests, "get",
                        lambda url, **kw: FakeResponse())
    r = deliverables.fetch("plan-7", [{"url": "https://runner/f/out.bin",
                                       "name": "out.bin", "kind": "stitched_bin"}],
                           token="t")
    assert r["ok"] is True
    assert r["data"]["items"][0]["bytes"] == len(b"remote-bytes")


def test_fetch_rejects_entry_without_url(clean_env):
    r = deliverables.fetch("plan-8", [{"name": "x.bin"}])
    assert r["error_code"] == ErrorCode.REMOTE_EXEC_FAILED
