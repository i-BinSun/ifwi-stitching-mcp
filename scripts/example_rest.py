"""Example client for the Docker-hosted /query and /stitching REST endpoints.

Edit QUERY_PAYLOAD / STITCH_PAYLOAD below with your real FIV data, then run:

    python scripts/example_rest.py query
    python scripts/example_rest.py stitching
    python scripts/example_rest.py stitching --confirm-stitch

Without --confirm-stitch, stitching requests a direct dry-run preview:
the IFWI/ingredient/tool URLs and command template, without downloading files
or submitting a remote stitch. The config name is a hint, verified by the
worker only during execution. The flag submits a REAL remote stitching job.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


# Docker Compose reads deploy.env. Load the same file for this example client.
load_dotenv(Path(__file__).resolve().parents[1] / "deploy.env", override=False)
BASE_URL = os.getenv("IFWI_MCP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# One version -> data.release.ingredients; two versions -> data.differences.
QUERY_PAYLOAD = {
    "fiv_project_id": "232",  # FIV Portal project ID.
    "phase": "Orange",
    "versions": ["2026.41.3.01"],  # Or ["2026.41.3.01", "2026.40.5.01"].
    "swimlane": "release/post-silicon",
    "binary_name": "OKSDCRB1_NonIPClean_Trace_DebugSigned",
}

# Replace ALL example values before running the stitching command.
# Server resolves URLs only; worker downloads and validates the stitch config.
STITCH_PAYLOAD = {
    "fiv_project_id": "232",  # FIV Portal project ID for the remote runner.
    "phase": "Orange",
    "version": "2026.41.3.01",
    "binary_name": "OKSDCRB1_NonIPClean_Trace_DebugSigned",
    "ingredients": {"PowerOn_DMRAP_MMC1": "0.967.0"},
    "swimlane": "release/post-silicon",
    "async": True,  # Real stitching returns a job to poll.
}


def call(method, path, headers, *, payload=None, timeout=30):
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
        raise RuntimeError("Request failed; for HTTP 409, select a swimlane or binary_name from detail.candidates")
    return body["data"]


def wait_for_result(status_url, headers):
    """Poll a real stitch without blocking on one long POST."""
    deadline = time.monotonic() + 3600
    print("Job accepted. Polling {} (up to 1 hour)...".format(status_url), flush=True)
    while True:
        status = call("GET", status_url, headers)
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
                        help="submit a real remote job instead of previewing it")
    args = parser.parse_args()

    token = os.getenv("IFWI_MCP_SERVE_TOKEN")
    if not token or token == "replace-with-a-strong-random-token":
        parser.error("set IFWI_MCP_SERVE_TOKEN in the environment or deploy.env")
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
            print("Preview ready; no files downloaded or remote stitch submitted. Check source_urls"
                " and command_template; the worker will verify config_ini after extracting the tool.")
            return 0

        payload["async"] = True
        job = call("POST", "/stitching", headers, payload=payload)
        wait_for_result(job["status_url"], headers)
        print("Stitching succeeded; see result.data for plan_id and deliverables.")
        return 0
    except (requests.RequestException, RuntimeError, KeyError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())