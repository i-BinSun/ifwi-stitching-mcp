"""Entry point of a remote stitch pod: `python -m ifwi_mcp.worker --plan-b64 ... --upload-url ...`.

Runs the plan through executor._execute_local (the same codepath local mode uses), uploads
every deliverable to <upload-url>/deliverables/, then writes the ok()/err() envelope -- with
those Artifactory URLs -- to <upload-url>/result.json. That file is the only channel back to
the MCP server, so it is written on every outcome, including crashes.

Credentials come from the pod environment (ARTIFACTORY_TOKEN or IFWI_ARTIFACTORY_TOKEN).
"""
import argparse
import json
import sys
import tempfile
import zlib
from pathlib import Path

from . import artifactory, config, executor, plan as plan_mod
from .result import ok, err, ErrorCode


def run(plan: dict, upload_url: str) -> dict:
    exec_cfg = config.get_execution_config()
    if not exec_cfg["ok"]:
        return exec_cfg
    result = executor._execute_local(plan, exec_cfg["data"])
    if not result["ok"]:
        # Publish whatever was kept (usually the stitch log) so the failure is debuggable.
        items = ((result["detail"].get("deliverables") or {}).get("items")) or []
        if items:
            published = executor.publish_deliverables(items, upload_url)
            if published["ok"]:
                result["detail"]["deliverables"] = published["data"]["items"]
        return result

    published = executor.publish_deliverables(result["data"]["deliverables"]["items"], upload_url)
    if not published["ok"]:
        return published
    data = dict(result["data"])
    data["deliverables"] = published["data"]["items"]
    return ok(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ifwi_mcp.worker")
    parser.add_argument("--plan-b64", required=True)
    parser.add_argument("--upload-url", required=True)
    args = parser.parse_args(argv)
    upload_url = args.upload_url.rstrip("/")

    try:
        plan = plan_mod.decode_plan(args.plan_b64)
    except (ValueError, zlib.error) as exc:
        result = err(ErrorCode.PLAN_INVALID, "plan_b64 is not a valid encoded plan",
                     {"reason": str(exc)})
    else:
        try:
            result = run(plan, upload_url)
        except Exception as exc:  # result.json must still be written
            result = err(ErrorCode.INTERNAL_ERROR, "stitch worker crashed",
                         {"type": type(exc).__name__, "reason": str(exc)})

    print(json.dumps(result))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / executor.REMOTE_RESULT_NAME
        path.write_text(json.dumps(result), encoding="utf-8")
        published = artifactory.upload_file(str(path), f"{upload_url}/{executor.REMOTE_RESULT_NAME}")
    if not published["ok"]:
        print(json.dumps(published), file=sys.stderr)
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
