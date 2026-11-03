### Remote stitch execution: job status, callbacks and persistence (2026-10-29)

**Pull requests**
- ifwi-stitching-mcp: https://github.com/i-BinSun/ifwi-stitching-mcp/pull/3 (branch `agents/remote-execution-status-flow-design`)
- cloud-dockerfile: https://github.com/intel-innersource/firmware.boot.uefi.iafw.devops.fivportal.cloud-dockerfile/pull/13 (stitch_wrapper 1.1.0, `timeouts.deadline_epoch`), on top of the merged #12 (wrapper 1.0.0, image v1.1.0)

**What changed (ifwi-stitching-mcp)**
- Remote stitching runs in Linux pods through `/app/stitch_wrapper.py`. Before creating a pod, the server records the run's `run_id` and `run_url`. The pod gets a spec without credentials (time budget plus an absolute `deadline_epoch`) and a callback token valid for this job only.
- No background polling: the wrapper calls the server back once when the pod ends (`POST /internal/jobs/<id>/result`). If no callback arrives, a status query checks the Job API, and once the pod has ended it reads `result.json`.
- Safeguards: "not found" must be seen twice, 30 s apart; a pod only times out as stuck in pending when the Job API explicitly says pending; results get a 120 s grace period; an inferred failure is replaced if the real result arrives later.
- Server mode stores jobs in SQLite (`<cache>/jobs.db`), so job status survives restarts and redeploys. stdio mode keeps them in memory.
- After a restart, unfinished jobs are recovered or marked interrupted. Jobs are kept for 7 days.
- Each job has an append-only `job_events` log (`GET /jobs/<id>?events=true`).
- New `GET /jobs` and `POST /jobs/<id>/cancel`.
- Submissions can be made idempotent with `client_request_id`.
- Job status adds `terminal`, `stage`, `poll_after_seconds` and `result_source`; existing fields are unchanged.
- New MCP tools: `stitch_submit_plan`, `stitch_get_job`, `stitch_cancel_job`, `stitch_list_jobs`.
- New error codes for submit, schedule, timeout, termination, bootstrap, crash and result failures. Each carries `job_id`, `run_id`, `pod_job_id` and `artifactory_url`.
- In server mode, results contain only Artifactory links; the v1 fields are kept as aliases.
- Linux fix: the stitch output folder is found under both `output/` and `Output/` (older tools write `Output/`).
- Worker: writes a result file for the wrapper, handles SIGTERM, and adds `schema_version`, `run_id` and `finished_at` to `result.json`.
- Defaults are now the Linux Job API endpoint and the image `fiv-ubuntu24.04-python3.13:v1.1.0`.
- New settings include `report_base_url`, `pending_timeout_seconds`, `finalize_grace_seconds`, `max_active_jobs` and `job_retention_hours`.
- README (English and Chinese) updated.

**Validation**
- Tests: 427 passed and 2 skipped on Windows. The 2 skipped need a case-sensitive file system (`output/` and `Output/` both present); they pass in the Linux pod image. Test files are kept local, not in the PR.
- End-to-end on Docker: a real server, a mock Job API that starts pods with `docker run`, a mock Artifactory, and wrapper 1.1.0. The callback, no-callback (`result.json` fallback) and cancel scenarios all pass.
- An independent code review found 3 issues; all are fixed.

**Next steps**
- Review and merge PR #3 and cloud-dockerfile PR #13.
- Publish an image tag with wrapper 1.1.0, then raise `execution.image` and the minimum tag check.
- Before production, confirm that stitch pods can reach `report_base_url`; that the reverse proxy forwards `/internal/jobs/*/result`; that the Job API does not restart failed pods; the maximum length of the pod `command`; and whether `activedeadlineseconds` applies to the whole Job or to each pod.
- Run a first stitch against the real FIV Job API.


---

### Live run on the real Linux Job API (2026-10-29)

The remote stitch flow ran end to end on the real cluster: this branch's server, the real Linux Job API, Artifactory and FIV, and image `fiv-ubuntu24.04-python3.13:v1.1.1`.

**Results**
- Real stitch (OKSDCRB1 2026.41.3.01 + MMC1 0.967.0): succeeded in 82–117 s and ended through the pod's callback. The 64 MB stitched bin on Artifactory matches the sha256 reported in the callback.
- `deadline_epoch` works: the pod logged `pod_deadline_in=3539s` for a 3600 s budget.
- Cancelling a running job deletes the pod. The wrapper reports `REMOTE_TERMINATED` within 1 s and writes `pod.log` and `result.json`.

**Two Job API behaviours found and fixed** (PR #3, commit `49b4a8e`)
- The Job API runs the command as `sh -c "<command>"`, and that `sh` is PID 1 and ignores SIGTERM. Before the fix, a cancelled pod was killed without any callback, `pod.log` or `result.json`. The command now starts with `exec`, so the wrapper itself is PID 1.
- A failed pod is never reported as `failed`. The job shows `active` with the pod `Failed`, then stays `pending` with no pods. The server now reads the pod list, so a failed pod ends the job instead of waiting 15 minutes for the schedule timeout.

**Other confirmations**
- The Linux Job API needs no token.
- Failed pods are not retried.
- Pod creation returns in about 0.3 s.
- Pods can reach GitHub, FIV, Artifactory and the internal server address.

**Configuration**: the server now defaults to image v1.1.1 and rejects older `vX.Y.Z` tags at startup.

**Still open for the Rancher deployment**: the `report_base_url` the pods will use, and the reverse proxy forwarding `/internal/jobs/*/result`.


---

### Pod cleanup after a job ends (2026-10-29)

Previously the server deleted a pod only on cancel or timeout, so a job that ended normally left its K8s job behind until `ttlsecondsafterfinished` (12 h). PR #3 now deletes the pod once the job is over (commit `6e90075`):
- After a normal end (callback, `result.json` or an inferred failure), the server deletes the pod after `pod_cleanup_delay_seconds` (default 30 s). The delay gives the wrapper time to finish its last uploads.
- Cancel and timeouts still delete the pod immediately.
- If the delete fails, or the server restarts before it runs, it is retried on later status queries and at startup.

Verified on the real Linux Job API: a real stitch succeeded through the callback, the delete ran 30 s later, and the Job API then returned `jobs.batch "fivportal-vwwjuf" not found`.
