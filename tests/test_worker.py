import json

from ifwi_mcp import plan, worker
from ifwi_mcp.result import ok, err, ErrorCode


def _capture_uploads(monkeypatch):
    uploads = {}

    def fake_upload(local_path, url):
        with open(local_path, "rb") as fh:
            uploads[url] = fh.read()
        return ok({"url": url})

    monkeypatch.setattr(worker.artifactory, "upload_file", fake_upload)
    return uploads


def test_worker_uploads_deliverables_then_result(clean_env, monkeypatch, tmp_path):
    stitched = tmp_path / "out.bin"; stitched.write_bytes(b"bin")
    log = tmp_path / "p.log"; log.write_text("log")
    uploads = _capture_uploads(monkeypatch)
    seen = {}

    def fake_local(p, exec_cfg):
        seen["plan_id"] = p["plan_id"]
        return ok({"plan_id": p["plan_id"], "exit_code": 0, "command_line": "py cli.py",
                   "deliverables": {"items": [
                       {"name": "out.bin", "kind": "stitched_bin", "path": str(stitched)},
                       {"name": "p.log", "kind": "log", "path": str(log)}]}})

    monkeypatch.setattr(worker.executor, "_execute_local", fake_local)
    code = worker.main(["--plan-b64", plan.encode_plan({"plan_id": "p"}),
                        "--upload-url", "https://af/repo/p-1/"])
    assert code == 0 and seen["plan_id"] == "p"
    assert uploads["https://af/repo/p-1/deliverables/out.bin"] == b"bin"
    result = json.loads(uploads["https://af/repo/p-1/result.json"])
    assert result["ok"] is True
    assert result["data"]["deliverables"] == [
        {"url": "https://af/repo/p-1/deliverables/out.bin", "name": "out.bin", "kind": "stitched_bin"},
        {"url": "https://af/repo/p-1/deliverables/p.log", "name": "p.log", "kind": "log"}]


def test_worker_failure_publishes_log_and_result(clean_env, monkeypatch, tmp_path):
    log = tmp_path / "p.log"; log.write_text("log")
    uploads = _capture_uploads(monkeypatch)
    monkeypatch.setattr(worker.executor, "_execute_local", lambda p, c: err(
        ErrorCode.STITCH_RUN_FAILED, "boom",
        {"exit_code": 2, "deliverables": {"items": [
            {"name": "p.log", "kind": "log", "path": str(log)}]}}))
    code = worker.main(["--plan-b64", plan.encode_plan({"plan_id": "p"}),
                        "--upload-url", "https://af/run"])
    assert code == 1
    result = json.loads(uploads["https://af/run/result.json"])
    assert result["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert result["detail"]["deliverables"][0]["url"] == "https://af/run/deliverables/p.log"


def test_worker_crash_still_writes_result(clean_env, monkeypatch):
    uploads = _capture_uploads(monkeypatch)

    def boom(p, c):
        raise RuntimeError("kaput")

    monkeypatch.setattr(worker.executor, "_execute_local", boom)
    assert worker.main(["--plan-b64", plan.encode_plan({"plan_id": "p"}),
                        "--upload-url", "https://af/run"]) == 1
    assert json.loads(uploads["https://af/run/result.json"])["error_code"] == ErrorCode.INTERNAL_ERROR


def test_worker_bad_plan_is_reported(clean_env, monkeypatch):
    uploads = _capture_uploads(monkeypatch)
    assert worker.main(["--plan-b64", "not-a-plan", "--upload-url", "https://af/run"]) == 1
    assert json.loads(uploads["https://af/run/result.json"])["error_code"] == ErrorCode.PLAN_INVALID


def test_worker_exits_2_when_result_cannot_be_published(clean_env, monkeypatch):
    monkeypatch.setattr(worker.executor, "_execute_local", lambda p, c: err("X", "x"))
    assert worker.main(["--plan-b64", plan.encode_plan({"plan_id": "p"}),
                        "--upload-url", "https://af/run"]) == 2   # no ARTIFACTORY_TOKEN


def test_plan_encoding_round_trips_and_is_shell_safe():
    p = {"plan_id": "p", "sources": {"ifwi": {"url": "https://a/b c.bin"}}, "x": "'\";&|"}
    token = plan.encode_plan(p)
    assert all(ch.isalnum() or ch in "-_" for ch in token)
    assert plan.decode_plan(token) == p
