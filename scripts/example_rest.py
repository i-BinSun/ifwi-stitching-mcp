"""Example client for the /query and /stitching REST endpoints.

Edit QUERY_PAYLOAD / STITCH_PAYLOAD below with your real FIV data, then run:

    python scripts/example_rest.py query
    python scripts/example_rest.py stitching
    python scripts/example_rest.py stitching --confirm-stitch

Without --confirm-stitch, stitching requests a direct dry-run preview:
the IFWI/ingredient/tool URLs and command template, without downloading files
or running a stitch. The config name is a hint, verified only during execution.
The flag submits a REAL stitching job; its deliverables come back as Artifactory links.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


# Docker Compose reads deploy.env; a Windows standalone server reads .env.
load_dotenv(Path(__file__).resolve().parents[1] / "deploy.env", override=False)
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
BASE_URL = os.getenv("IFWI_MCP_BASE_URL", "http://127.0.0.1:8767").rstrip("/")

# One version -> data.release.ingredients; two versions -> data.differences.
QUERY_PAYLOAD = {
    "fiv_project_id": "232",  # FIV Portal project ID.
    "phase": "Orange",
    "versions": ["2026.41.3.01"],  # Or ["2026.41.3.01", "2026.40.5.01"].
    "swimlane": "release/post-silicon",
    "binary_name": "OKSDCRB1_NonIPClean_Trace_DebugSigned",
}

# Replace ALL example values before running the stitching command.
# Server resolves URLs only; execution downloads and validates the stitch config.
STITCH_PAYLOAD = {
    "fiv_project_id": "232",  # FIV Portal project ID.
    "phase": "Orange",
    "version": "2026.41.3.01",
    "binary_name": "OKSDCRB1_NonIPClean_Trace_DebugSigned",
    "ingredients": {"MMC1": "0.967.0"},
    "swimlane": "release/post-silicon",
    "async": True,  # Real stitching returns a job to poll.
}


def call(method, path, headers, *, payload=None, timeout=30, expected_instance=None):
    """Send a request and print both successful and error JSON responses."""
    response = requests.request(method, BASE_URL + path, headers=headers,
                                json=payload, timeout=timeout)
    print("{} {} -> HTTP {}".format(method, path, response.status_code))
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError("Server returned non-JSON: {}".format(response.text[:300])) from None
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if not response.ok or not body.get("ok"):
        if method == "GET" and path.startswith("/jobs/") and response.status_code == 404:
            current_instance = body.get("detail", {}).get("instance_id")
            if expected_instance and current_instance and current_instance != expected_instance:
                raise RuntimeError(
                    "Job was submitted to instance {} but {}{} is now served by instance {}. "
                    "The server restarted or the request reached another process; the job's in-memory status "
                    "cannot be recovered. Check existing plan/run artifacts before submitting another stitch."
                    .format(expected_instance, BASE_URL, path, current_instance))
            raise RuntimeError(
                "Job status unavailable at {}{} (submitted instance: {}; current instance: {}). "
                "If these match, check whether the job expired; otherwise check for a restart or another "
                "server process. Inspect existing plan/run artifacts before retrying."
                .format(BASE_URL, path, expected_instance or "unknown", current_instance or "unknown"))
        raise RuntimeError("Request failed; for HTTP 409, select a swimlane or binary_name from detail.candidates")
    return body["data"]


def wait_for_result(status_url, headers, *, expected_instance=None):
    """Poll a real stitch without blocking on one long POST."""
    deadline = time.monotonic() + 3600
    print("Job accepted. Polling {} (up to 1 hour)...".format(status_url), flush=True)
    while True:
        status = call("GET", status_url, headers, expected_instance=expected_instance)
        if status["state"] == "succeeded":
            return status["result"]["data"]
        if status["state"] == "failed":
            raise RuntimeError("Job failed: {}".format(
                json.dumps(status["result"], ensure_ascii=False)))
        if time.monotonic() >= deadline:
            raise RuntimeError("Job still running; check {} later with the same bearer token".format(status_url))
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("query", "stitching"))
    parser.add_argument("--confirm-stitch", action="store_true",
                        help="submit a real stitching job instead of previewing it")
    args = parser.parse_args()

    token = os.getenv("IFWI_MCP_SERVER_TOKEN")
    if not token or token == "replace-with-a-strong-random-token":
        parser.error("set IFWI_MCP_SERVER_TOKEN in the environment, .env or deploy.env")
    headers = {"Authorization": "Bearer " + token}

    try:
        if args.action == "query":
            call("POST", "/query", headers, payload=QUERY_PAYLOAD)
            return 0

        payload = dict(STITCH_PAYLOAD)
        payload["dry_run"] = not args.confirm_stitch
        if payload["dry_run"]:
            payload["async"] = False
            # Preview queries several FIV endpoints; each server-side lookup
            # may take longer than the ordinary 30-second client timeout.
            call("POST", "/stitching", headers, payload=payload, timeout=180)
            print("Preview ready; nothing downloaded or executed. Check source_urls and"
                " command_template; config_ini is verified after the tool is extracted.")
            return 0

        payload["async"] = True
        job = call("POST", "/stitching", headers, payload=payload)
        result = wait_for_result(job["status_url"], headers,
                     expected_instance=job.get("instance_id"))
        # Remote (pod) execution still reports stitched_bin_url.
        url = (result.get("stitched_bin") or {}).get("url") or result.get("stitched_bin_url")
        print("Stitching succeeded; stitched binary: {}".format(url))
        return 0
    except (requests.RequestException, RuntimeError, KeyError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())