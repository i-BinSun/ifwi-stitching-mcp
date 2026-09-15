"""Entry point AUTO_INTEGRATION_SERVICE runs on a worker for script_name=mcp_stitch.

Registered in fiv-portal-common-handler's task_dispatcher.json, which clones this repo,
installs requirements.txt, and invokes this script with --plan-json (per that entry's
`params` alias mapping). Reuses executor._execute_local unmodified -- the same codepath
`ifwi-cli stitch execute-plan` uses locally -- then uploads the produced artifacts to
Artifactory so the caller's deliverables.fetch can pull them back without any
remote-specific code path.

Prints exactly one JSON line (the ok()/err() envelope) to stdout and exits 0/1 to match,
mirroring ifwi_mcp/cli.py's convention -- this becomes task_data.task_result for the
poller in executor.py to read.
"""
import argparse
import json
import sys

from ifwi_mcp import artifactory_upload, config, executor
from ifwi_mcp.result import ok, err, ErrorCode


def run(plan: dict) -> dict:
    exec_cfg = config.get_execution_config()
    if not exec_cfg["ok"]:
        return exec_cfg
    local_result = executor._execute_local(plan, exec_cfg["data"])
    if not local_result["ok"]:
        return local_result

    uploaded = []
    for item in local_result["data"]["deliverables"]["items"]:
        res = artifactory_upload.upload_file(item["path"], item["name"])
        if not res["ok"]:
            return res
        uploaded.append({"url": res["data"]["url"], "name": item["name"], "kind": item["kind"]})

    data = dict(local_result["data"])
    data["deliverables"] = uploaded
    return ok(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-json", required=True)
    args = parser.parse_args()

    try:
        plan = json.loads(args.plan_json)
    except ValueError as exc:
        result = err(ErrorCode.PLAN_INVALID, "plan_json is not valid JSON", {"reason": str(exc)})
        print(json.dumps(result))
        sys.exit(1)

    result = run(plan)
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
