"""Example client for the /query and /stitching REST endpoints.

Install requests, set IFWI_MCP_BASE_URL and IFWI_MCP_SERVER_TOKEN in your
environment, and edit QUERY_PAYLOAD / STITCH_PAYLOAD with real FIV data.
This client does not read server-side .env or deploy.env files. Then run:

    python scripts/example_rest.py query
    python scripts/example_rest.py stitching

The stitching action submits a REAL stitching job and polls it until `terminal` is true,
waiting `poll_after_seconds` between polls. Deliverables come back as Artifactory links.
"""

import argparse
import json
import os
import sys
import time
import uuid

import requests


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
    # A value may also be a direct http(s) link to an ingredient package (skips FIV).
    "ingredients": {"MMC1": "0.967.0"},
    "swimlane": "release/post-silicon",
    # Optional: stitch onto a customized IFWI instead of the release binary; version,
    # swimlane and binary_name may then be omitted (read from its OEM region).
    # "ifwi_url": "https://af01p-or.devtools.intel.com/artifactory/.../custom_ifwi.bin",
    "async": True,  # Real stitching returns a job to poll.
}


def call(method, path, headers, *, base_url, payload=None, timeout=30, expected_instance=None):
    """Send a request and print both successful and error JSON responses."""
    response = requests.request(method, base_url + path, headers=headers,
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
                    "Job was submitted to instance {} but {}{} is now served by instance {}, which "
                    "does not know it. Check that both use the same job database (jobs.db on the /data "
                    "volume) and inspect existing run artifacts before submitting another stitch."
                    .format(expected_instance, base_url, path, current_instance))
            raise RuntimeError(
                "Job status unavailable at {}{} (submitted instance: {}; current instance: {}). "
                "The job never existed or is past the retention period. Inspect existing run "
                "artifacts before retrying."
                .format(base_url, path, expected_instance or "unknown", current_instance or "unknown"))
        raise RuntimeError("Request failed; for HTTP 409, select a swimlane or binary_name from detail.candidates")
    return body["data"]


def wait_for_result(status_url, headers, *, base_url, expected_instance=None):
    """Poll a real stitch without blocking on one long POST."""
    deadline = time.monotonic() + 2 * 3600
    print("Job accepted. Polling {} ...".format(status_url), flush=True)
    while True:
        status = call("GET", status_url, headers, base_url=base_url,
                      expected_instance=expected_instance)
        # terminal is the stable end-of-job signal (state may also be "cancelled").
        if status.get("terminal"):
            if status["state"] == "succeeded":
                return status["result"]["data"]
            raise RuntimeError("Job {}: {}".format(
                status["state"], json.dumps(status["result"], ensure_ascii=False)))
        if time.monotonic() >= deadline:
            raise RuntimeError("Job still running; check {} later with the same bearer token".format(status_url))
        time.sleep(status.get("poll_after_seconds") or 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("query", "stitching"))
    args = parser.parse_args()

    base_url = os.getenv("IFWI_MCP_BASE_URL", "").rstrip("/")
    token = os.getenv("IFWI_MCP_SERVER_TOKEN")
    if not base_url.startswith(("http://", "https://")):
        parser.error("set IFWI_MCP_BASE_URL to the HTTP(S) address of the REST server (without /mcp)")
    if not token or token == "replace-with-a-strong-random-token":
        parser.error("set IFWI_MCP_SERVER_TOKEN in the client process environment")
    headers = {"Authorization": "Bearer " + token}

    try:
        if args.action == "query":
            call("POST", "/query", headers, base_url=base_url, payload=QUERY_PAYLOAD)
            return 0

        # A fresh idempotency key per run: resending this same POST after a network
        # error returns the job it already created instead of starting a second stitch.
        payload = {**STITCH_PAYLOAD, "async": True, "client_request_id": uuid.uuid4().hex}
        job = call("POST", "/stitching", headers, base_url=base_url, payload=payload)
        result = wait_for_result(job["status_url"], headers, base_url=base_url,
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