# IFWI Stitching MCP

English | [简体中文](README.zh-CN.md)

An [MCP](https://modelcontextprotocol.io/) server (Python + [FastMCP](https://github.com/jlowin/fastmcp)) that
drives the full IFWI stitching workflow from an MCP host: it queries the **FIV Portal** for projects and
releases, downloads IFWI / ingredient / stitch-tool artifacts from Artifactory, parses the OEM region of a
local IFWI image, and runs the stitch tool inside an isolated per-tool virtualenv.

Every function returns a **unified result shape** — `{"ok": True, "data": {...}}` or
`{"ok": False, "error_code": <ENUM>, "message": <str>, "detail": {...}}`. No bare exception ever crosses a
tool boundary.

## What you can do with it

Talk to your MCP host in plain language; it picks the right tools for you. Typical scenarios:

- **"What MMC version does the latest Orange release on DMR B0 PowerOn ship?"** — the host looks up the
  release, lists its binaries (asking you which one if there's more than one), and reads off the ingredient
  versions baked into it.
- **"Swap MMC1 and MMC2 to version 0.967.0 and stitch me a new image."** — the host builds a plan from the
  base IFWI plus the two ingredient versions, shows you the resulting command for confirmation, then
  downloads everything, runs the stitch tool, and hands you back the stitched `.bin` plus a manifest.
- **"I have this local `.bin` file and don't know which release it came from."** — the host parses its OEM
  region and reverse-matches it to a FIV build.
- **"What ingredient versions have ever been released for MMC1?"** — a straight FIV Portal lookup.

If you just want to use the server from an MCP host, [For users](#for-users) below is all you need.
Tool-by-tool details and the mechanics behind these scenarios live in [For developers](#for-developers).

## For users

### Requirements

- **Python 3.9+**

### Installation

```bash
# from the project root
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# runtime install (editable)
pip install -e .

# with dev/test extras
pip install -e ".[dev]"
```

### Configuration

The server needs `FIV_TOKEN` and `ARTIFACTORY_TOKEN` (validated lazily, only when a tool that needs them is
called). `FIV_BASE_URL` defaults to `https://fiv-ifwi.intel.com` and only needs to be set if you use a
different FIV Portal.

Copy [`.env.example`](.env.example) to `.env` in the project root and fill in your values:

```bash
cp .env.example .env
```

The server auto-loads `.env` (via `python-dotenv`) from its working directory or one of its parents on
startup — no host-specific `envFile` setting required. Values already present in the process environment
(e.g. set inline under a host's `env` block) always take priority over `.env`.

| Variable | Required | Purpose |
|----------|----------|---------|
| `FIV_BASE_URL` | no | Base URL of the FIV Portal. Defaults to `https://fiv-ifwi.intel.com`. |
| `FIV_TOKEN` | for FIV calls | The FIV access token, sent as `Authorization: Bearer <token>`. |
| `ARTIFACTORY_TOKEN` | for downloads | The Artifactory access token, sent the same way when downloading artifacts. |
| `IFWI_MCP_CACHE_DIR` | no | Cache root. Defaults to `~/.ifwi-stitching-mcp/cache`. |

Everything else — where stitch jobs run, how deliverables are packaged, dependency fallbacks for legacy stitch
tools — has sane defaults and only matters if you need to change it; see
[Advanced configuration](#advanced-configuration).

### Registering with an MCP host

**Claude Code / Claude Desktop** (`.mcp.json`, top-level key `mcpServers`):

```json
{
  "mcpServers": {
    "ifwi-stitching": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ifwi_mcp.server"],
      "cwd": "/path/to/ifwi-stitching-mcp"
    }
  }
}
```

`cwd` makes sure the server finds your `.env` regardless of where Claude Code's own process happens to run
from. If you'd rather not rely on `.env` at all, you can still set values inline under an `env` block instead
— see [Configuring from the MCP host instead of a file](#configuring-from-the-mcp-host-instead-of-a-file) for
the full list of overrides.

**VS Code Copilot** (`.vscode/mcp.json`, top-level key `servers` — *not* `mcpServers`):

```json
{
  "servers": {
    "ifwi-stitching": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/Scripts/python.exe",
      "args": ["-m", "ifwi_mcp.server"],
      "cwd": "${workspaceFolder}",
      "dev": { "watch": "ifwi_mcp/**/*.py" }
    }
  }
}
```

`dev.watch` restarts the server whenever the source changes, which together with the editable install means
an edit takes effect on save. VS Code's own `envFile` key still works if you prefer it over the server's
built-in `.env` loading — both end up setting the same environment variables.

The `command` path is platform-dependent: `.venv/Scripts/python.exe` on Windows, `.venv/bin/python` on
Linux/macOS. Both host configs are per-machine, so pick the one that matches where the server runs — note
that a config written inside WSL needs the Linux path even when VS Code itself is on Windows.

### Serving from a shared host

`IFWI_MCP_SERVER_MODE` controls **how clients connect** to this MCP server; it does not
control where stitching runs. The default `local` keeps the existing stdio behavior.
Set it to `server` to listen for authenticated Streamable HTTP requests at `/mcp`:

```bash
IFWI_MCP_SERVER_MODE=server
IFWI_MCP_SERVER_HOST=127.0.0.1
IFWI_MCP_SERVER_PORT=8000
IFWI_MCP_SERVER_TOKEN=<strong-random-token>
IFWI_MCP_EXEC_MODE=remote
IFWI_MCP_EXEC_ENDPOINT=http://infra-api-fiv-linux.bios-dev-sh-cloud.intel.com/v1/fivportal/job
IFWI_MCP_ARTIFACTORY_REPO=https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/FIV_Portal/RemoteStitch
IFWI_MCP_REPORT_BASE_URL=https://your-host.example   # where stitch pods call back
python -m ifwi_mcp.server
```

These settings can also live in the `serve` section of the JSON config file (keys
`mode`, `host`, `port`, `token`); environment variables take precedence. A token is
mandatory in server mode and is sent by clients as a Bearer token. For example, a
remote VS Code MCP client can register a server with `type: "http"`,
`url: "https://your-host.example/mcp"` and an `Authorization: Bearer <token>` header.
Use a TLS-terminating reverse proxy for shared access; the default loopback bind is
only reachable from the server itself. Do not place tokens in a committed MCP host config.

#### Docker deployment

The [Dockerfile](Dockerfile) starts the authenticated HTTP server on container port
`8000` with remote-only stitch execution. With [compose.yaml](compose.yaml), copy
[deploy.env.example](deploy.env.example) to an ignored `deploy.env` on the Docker host,
replace the placeholders, then run:

```bash
docker compose up -d --build
docker compose logs -f ifwi-stitching-mcp
```

Compose reads `deploy.env` at **container startup** via `env_file`; it is not copied
into the image. The repository's `.env` is not used by this service (Compose may use
it for variable interpolation). Keep `deploy.env` on the deployment host and do not
commit it. To use another path, change `env_file` to that path before starting.
Alternatively, build and run without Compose (supplying credentials at runtime,
never as Docker build arguments):

```bash
docker build -t ifwi-stitching-mcp .
docker run --name ifwi-stitching-mcp --restart unless-stopped \
  --env-file /secure/path/ifwi-mcp.env \
  -p 127.0.0.1:8000:8000 \
  -v ifwi-mcp-data:/data \
  ifwi-stitching-mcp
```

The runtime env file must define `IFWI_MCP_SERVER_TOKEN` (a strong random value),
`IFWI_MCP_EXEC_ENDPOINT` (the Linux FIV Job API URL) and `IFWI_MCP_ARTIFACTORY_REPO`
(where stitch pods publish their results), plus `FIV_TOKEN`
for queries and `ARTIFACTORY_TOKEN` for artifact downloads. Set `IFWI_MCP_REPORT_BASE_URL`
to an address the stitch pods can reach (normally the HTTPS reverse proxy) and let the proxy
forward `/internal/jobs/*/result`: each pod calls that route once with its result. The image sets
`IFWI_MCP_SERVER_MODE=server`, `IFWI_MCP_SERVER_HOST=0.0.0.0`,
`IFWI_MCP_EXEC_MODE=remote` and `IFWI_MCP_CACHE_DIR=/data/cache`. The named volume
persists plans, cached files, deliverables **and job records** (`/data/cache/jobs.db`), so
`GET /jobs/{id}` keeps working across restarts and image rebuilds. **Do not use
`docker compose down -v` (or `docker volume rm`) for routine redeployments** — it deletes the
job database. Run a single server process (SQLite allows one writer process).
`fiv_project_id` belongs in each stitch request, not the env file.

The example publishes to host loopback only. For external clients, front it with an
HTTPS reverse proxy; if the proxy runs on another machine, deliberately change the
host-side bind and restrict access with a firewall. The container is not a trusted
multi-tenant sandbox: MCP tools can read or write paths on the container filesystem,
so only grant tokens to trusted callers and do not mount sensitive host directories.

#### Windows standalone deployment (local execution)

Without a pod runner, a Windows host can serve HTTP **and** run the stitches itself.
Put the settings in a `.env` next to the checkout (never commit it) and start the server:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# .env:
#   IFWI_MCP_SERVER_MODE=server
#   IFWI_MCP_SERVER_HOST=127.0.0.1
#   IFWI_MCP_SERVER_TOKEN=<strong-random-token>
#   IFWI_MCP_EXEC_MODE=local
#   IFWI_MCP_ARTIFACTORY_REPO=https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/FIV_Portal/RemoteStitch
#   IFWI_MCP_CACHE_DIR=D:\ifwi-cache
#   FIV_TOKEN=...
#   ARTIFACTORY_TOKEN=...
.\.venv\Scripts\python.exe -m ifwi_mcp.server
```

- **Results are links.** Startup fails without `IFWI_MCP_ARTIFACTORY_REPO`. Each run uploads its
  deliverables to `<repo>/<run_id>/deliverables/` and returns only `plan_id`, `run_id`,
  `artifactory_url`, `stitched_bin` (`url`, `sha256`, `bytes`), `log_url` and `warnings`, never
  server paths. The command line, source URLs and full artifact list go to
  `<artifactory_url>/run.json` for debugging. Failures return the uploaded log link.
  Local deliverable copies are deleted after a successful upload and kept if an upload fails.
- **Runs are physically isolated, with no concurrency limit.** Every asynchronous job gets its own
  thread, and every run its own `work/<run_id>/` directory: `tool/` (a full copy of the stitch tool,
  venv included), `inputs/` (copies of the IFWI and ingredients), `tmp/` (its private `TEMP`) and
  `<run_id>.log`. Only the download/extraction cache is shared, and runs never execute inside it.
  Each copy costs disk space and time.
- **Debug retention.** `IFWI_MCP_RUN_RETENTION_HOURS` (default `0`) keeps each finished run
  directory for that many hours instead of deleting it right after upload; older ones are pruned
  when a later run finishes, never while a run is in flight. Each kept run can take hundreds of MB.
- **Keep paths short.** Tool files run from `<cache>/work/<run_id>/tool/...` (about 50 characters
  after the cache root); use a short `IFWI_MCP_CACHE_DIR` or enable Windows long paths.
- The low-level `run_stitch` MCP tool stays disabled in server mode; use `stitch_execute_plan` or
  `POST /stitching`.
- Keep the loopback bind and publish through an HTTPS reverse proxy. Run one server process. Job
  records survive a restart (`<cache>/jobs.db`), but a local run cannot be resumed: a job that was
  running when the server stopped ends as `JOB_INTERRUPTED`.

`IFWI_MCP_EXEC_MODE` is separate from the serve mode. Both stdio and server mode support
local or remote execution; remote needs `IFWI_MCP_EXEC_ENDPOINT`, and server mode with local
execution needs `IFWI_MCP_ARTIFACTORY_REPO`. Startup fails otherwise. The FIV project ID is supplied
with each stitch request rather than hard-coded in the server configuration.
The existing authenticated MCP endpoint `/mcp` remains available. The same server also
exposes independent authenticated REST JSON interfaces:

- `POST /query`: specify `fiv_project_id`, `phase`, and one or two release `versions`.
  One version returns `data.release.ingredients`; two versions return only the
  changed ingredient versions in `data.differences` as
  `{name, from_version, to_version}` (a missing ingredient is `null`).
- `POST /stitching`: specify `fiv_project_id`, `phase`, `version` (optional with `ifwi_url`), and a non-empty
  `ingredients` name-to-version map. The server resolves FIV metadata into pinned
  IFWI, ingredient and stitch-tool URLs, saves a plan and executes that same plan
  wherever `IFWI_MCP_EXEC_MODE` points. Downloading, extraction, config validation
  and the stitch itself happen only during execution. The result includes `plan_id`
  and Artifactory links to the deliverables. An `ingredients` value may also be a
  direct `http(s)://` download link instead of a version; it is used as-is and the
  FIV lookup is skipped (the file is not validated against FIV).
- An ingredient named `BIOS` may point to a BIOS **build package** (e.g. a
  `..._BuildPkg.7z` link). It is resolved during execution, once the target config is known:
  if the package holds `IFWIStitch20.bin` (a zip; an already extracted `IFWIStitch20/BiosPkg`
  folder works too), its `BiosPkg/BiosFile/MAIN.fd` and `SECPEI.fd` (plus
  `ConfigInfo/BuildConfig.ini` for the VAB state and `SourceFile/BinaryFd.fdf`) are laid out as a
  BIOSID version folder (named after the `$IBIOSI$` version, at the paths of the target config's
  `[BIOSID]` `regex_mandatory_dict`) and stitched as `BIOSID`. Otherwise one 64 MB IFWI `.bin` is
  stitched as `BIOS` and the tool slices MAIN+SECPEI out of it: `*_PO_SAFE*` images are never used,
  images that are neither `Simics` nor `jc_2s_b` are preferred, then the first by name; the chosen
  file is reported in `warnings`. A package with only PO_SAFE images, a config without `[BIOSID]`
  or an unreadable `IFWIStitch20.bin` fails with `BIOS_PACKAGE_INVALID` (HTTP 400). A plain image
  or folder that is not a build package is passed through unchanged. BIOS replacement needs a
  stitch tool release that has `Source/StitchStep/BiosRegion` (older `stitch2.py` releases cannot
  stitch BIOS).
- Pass an optional `ifwi_url` (`http(s)://` link) on `/stitching` to stitch onto a
  customized IFWI instead of the release binary. `version` then becomes optional: the
  server reads only the 256-byte OEM region at `0xF00` (HTTP Range; without Range
  support it stops after that window) and derives `version` and `swimlane` from its
  `ifwi_version` (e.g. `ap.post-silicon.2026.41.3.01`), plus `binary_name` = product +
  flavor for official builds. The release still selects the stitch tool and config;
  only the base binary download is replaced. An explicit `version`, `swimlane` or
  `binary_name` that contradicts the OEM region is HTTP 400; an OEM region without a
  release version and no `version` is HTTP 400 (no fallback to another release's stitch
  tool); a customized flavor with several candidate binaries is HTTP 409. The dry-run
  result includes the parsed `oem` fields.
- Files from caller-supplied links (`ifwi_url` and link-valued `ingredients`) are
  downloaded and extracted into per-URL cache directories, so a same-named release
  file or a different link with the same file name is never reused.
- Set `"dry_run": true` on `/stitching` to preview `source_urls`, the inferred
  `config_ini` and placeholder-based `command_template` without downloading or
  executing anything. The config hint cannot be verified until execution extracts
  the tool. FIV metadata is still queried. The preview returns its final
  result directly with HTTP 200, even if `"async": true` was provided; it does not
  create a job.
- Both operations accept optional `swimlane` and `binary_name`. When several
  swimlanes or binaries match, choose one from the returned candidates. For
  stitching, multiple stitch configs without a unique match also return HTTP 409
  with config candidates; no config is guessed.
- Query and real stitching default to synchronous JSON results. Set `"async": true` to get HTTP 202
  (headers `Location` and `Retry-After`) with the job: `data.job_id`, `status_url`, `cancel_url`,
  `state`, `stage`, `terminal`, `poll_after_seconds` and `instance_id`. A synchronous call is a job
  too: its id is in the `X-Job-Id` header and in `data.task_job_id` (success) or `detail.job_id`
  (failure). Stitching dry runs always return directly and create no job.
- Optional `client_request_id` (1–128 characters of `[A-Za-z0-9_.:-]`) makes a submission
  idempotent: resending the same request with the same id returns the existing job (HTTP 200)
  instead of starting another stitch; reusing the id for a different request is HTTP 409
  `IDEMPOTENCY_CONFLICT`. Generate a fresh id per logical request and reuse it only on retries.
- `GET /jobs/{job_id}` always answers HTTP 200 for a known job. Stop polling when `data.terminal`
  is true; `data.state` is `queued`, `running`, `succeeded`, `failed` or `cancelled`.
  `data.stage` is informational (`resolving`, `queued`, `submitting`, `submit_unknown`, `pending`, `running`,
  `finalizing`, `done`; remote runs only expose the pod's state). `data.result` holds the unified
  envelope once terminal, and `data.result_source` says where it came from (`callback`,
  `result_json`, `inferred` or `server`). While a remote job runs, each query checks the pod live
  (at most every 5 s per job). Wait `poll_after_seconds` between polls. Add `?events=true` to get
  `data.events`, the job's event log (state changes, callbacks, Job API trouble, pod deletion).
- In server mode jobs are stored in `<cache>/jobs.db` and kept for `IFWI_MCP_JOB_RETENTION_HOURS`
  (7 days) after they finish; HTTP 404 `JOB_NOT_FOUND` means the job never existed or is past
  retention. At most `IFWI_MCP_MAX_ACTIVE_JOBS` (64) jobs run at once; beyond that a submission
  gets HTTP 503 `JOB_QUEUE_FULL` with `Retry-After`. Remote jobs are also limited to
  `IFWI_MCP_MAX_RUNNING_PODS` (5) live pods: a job beyond that waits in state `queued` (no pod,
  no `run_id` yet) and is submitted first-come first-served as soon as a pod finishes. Its
  pending and run timeouts start only when it is submitted. Cancelling a queued job never
  creates a pod; a server restart fails queued jobs with retryable `JOB_INTERRUPTED`.
- `GET /jobs?state=running,failed&kind=stitch&plan_id=...&limit=20&before=<created_at>` lists jobs
  (newest first, without `result`); `data.next_before` pages further.
- `POST /jobs/{job_id}/cancel` ends an unfinished job at once as `cancelled` and deletes its pod
  (`data.cleanup.pod_delete`). A finished job answers HTTP 409 `JOB_ALREADY_FINISHED`; a local
  stitch that is already running cannot be interrupted (HTTP 409 `JOB_NOT_CANCELLABLE`).

Example JSON bodies (one request per example; omit optional fields when not needed):

Query one version (`POST /query`):
```json
{ "fiv_project_id": "232", "phase": "Orange", "versions": ["2026.35.3.01"] }
```

Compare two versions (`POST /query`):
```json
{ "fiv_project_id": "232", "phase": "Orange", "versions": ["2026.35.3.01", "2026.36.3.01"], "binary_name": "chosen-binary" }
```

Submit an asynchronous stitch (`POST /stitching`):
```json
{ "fiv_project_id": "232", "phase": "Orange", "version": "2026.35.3.01", "ingredients": {"PowerOn_DMRAP_MMC1": "0.967.0"}, "binary_name": "chosen-binary", "async": true }
```

`binary_name` must be a real release binary name;
if omitted and several are available, retry with a name from the returned candidates.

All REST calls require `Authorization: Bearer <IFWI_MCP_SERVER_TOKEN>`. Request errors
use the same `ok`/`error_code`/`message`/`detail` envelope as MCP tools; invalid input
returns HTTP 400, ambiguous binary/swimlane HTTP 409, unauthorized HTTP 401.
File paths passed to tools refer to the **server** filesystem, not the client's.
The current plan/deliverable cache is shared and does not enforce per-user ownership;
do not offer this endpoint as a multi-tenant service without authorization, path
confinement, and per-user storage isolation. Prefer FIV-hosted inputs until a safe
upload flow is implemented.

### Command-line usage

Everything is also available as a standalone `ifwi-cli` command — no MCP host needed, useful from scripts,
CI, or an ad-hoc terminal. `pip install -e .` registers it on `PATH`; if the entry point isn't picked up
(e.g. a stale editable install), run it as a module instead: `python -m ifwi_mcp.cli`.

Commands are grouped the same way the tools are: `fiv ...` for FIV Portal lookups, `stitch ...` for the
plan/build/run workflow, `oem parse` for OEM decoding, plus a few top-level utilities (`download`,
`list-local-files`, `extract-archive`). Run `ifwi-cli --help`, `ifwi-cli fiv --help`, or
`ifwi-cli stitch --help` to see every subcommand.

```bash
ifwi-cli fiv list-releases --project DMR --phase PowerOn

ifwi-cli stitch build-plan --project DMR --phase PowerOn \
  --ingredients '[{"name": "PowerOn_DMRAP_MMC1", "version": "0.967.0"}]' \
  --config-ini Config_Stitch_OKSDCRB1_IPCleanDFXEnable_Trace_DebugSigned
# -> prints the saved plan, including its plan_id

ifwi-cli stitch execute-plan --plan-id <plan_id>

ifwi-cli download https://artifactory.example.com/some/ifwi.7z --category ifwi
```

Every command prints the same unified result envelope the MCP tools return (`{"ok": true, "data": ...}` or
`{"ok": false, "error_code": ..., "message": ..., "detail": ...}`) as JSON on stdout, and exits `0` when
`ok` is `true`, `1` otherwise — pipe it through `jq` for scripting.

### Troubleshooting

- **Code changes have no effect** — the MCP server is a long-lived process and must be restarted. With
  `dev.watch` configured VS Code does it automatically; otherwise use `MCP: List Servers` → Restart.
- **The tool list or descriptions are stale** — run `MCP: Reset Cached Tools`.
- **The server won't start** — check `MCP: List Servers` → Show Output; a failed `validate_startup()` prints a
  JSON error naming the offending setting.

## For developers

### Architecture

Focused, single-responsibility modules sit behind a thin FastMCP tool layer.

| Module | Responsibility | Constraints |
|--------|----------------|-------------|
| `result.py` | `ok()` / `err()` helpers + `ErrorCode` constants | — |
| `config.py` | env vars, `.env` loading, config file, cache layout, startup validation, token access | the **only** module reading `os.environ` / the config file |
| `fiv_portal.py` | FIV Portal REST client (projects, releases, swimlanes, finders) | HTTP + JSON only — no file I/O, no subprocess |
| `artifactory.py` | Artifactory download/caching + deliverables upload | transfers only — no FIV knowledge |
| `oem_parser.py` | zero-dependency OEM region decoder | stdlib only — no network, no subprocess |
| `archive.py` | `.zip` / `.tar*` / `.7z` extraction | pure file I/O |
| `up_config.py` | generate/parse UP (Unified Patch) Muppet config ini files | config file manipulation |
| `stitch_runner.py` | extract archive + build venv + render and run `cli.py` / `muppet.py` | the **only** module using subprocess / venv |
| `prepare.py` | find → download → extract, for IFWI / ingredient / stitch tool | orchestration of the layers above |
| `plan.py` | confirmed answers → validated plan + rendered command line | no network, no subprocess |
| `executor.py` | prepare the environment, run the plan locally, and the remote primitives (create/poll/delete the pod, read `result.json`, build the wrapper spec) | reads the execution switch |
| `job_store.py` | job records and their event log: `SqliteJobStore` (server mode) and `MemoryJobStore` (stdio), conditional writes | SQLite only |
| `jobs.py` | `JobManager`: submit, callback, refresh-on-query, finalize, cancel, restart recovery, retention | no background polling thread |
| `interfaces.py` | validate Query and Stitching REST payloads and call existing workflow | request-level project ID for remote dispatch |
| `http_api.py` | authenticated REST routes (`/query`, `/stitching`, `/jobs`) and the pod callback route | mounted alongside FastMCP `/mcp` |
| `deliverables.py` | collect results + artifacts into one dir with a manifest | pure I/O + downloads |
| `cli.py` | standalone CLI (`ifwi-cli`) with subcommands for all MCP capabilities | CLI entry point |
| `server.py` | FastMCP app: registers tools as thin pass-through wrappers | orchestration only |

### Advanced configuration

#### Config file — where stitch jobs run

A JSON config file holds the **execution switch** and optional `serve` settings. It lives at `IFWI_MCP_CONFIG`, or next to the cache dir
(`~/.ifwi-stitching-mcp/config.json`) by default. A missing file means "all defaults", i.e. local execution.

`execution.mode = "remote"` creates a Linux pod through the FIV Job API. The pod runs the image's
`/app/stitch_wrapper.py`, which installs this repo, runs `python -m ifwi_mcp.worker` (the exact
local-execution codepath) and calls the server back once with the result; `result.json` on
Artifactory is the fallback channel. See [Remote runner contract](#remote-runner-contract) below.

```json
{
  "execution": {
    "mode": "remote",
    "endpoint": "http://infra-api-fiv-linux.bios-dev-sh-cloud.intel.com/v1/fivportal/job",
    "artifactory_repo": "https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/FIV_Portal/RemoteStitch",
    "report_base_url": "https://ifwi-mcp.example.intel.com",
    "timeout_seconds": 3600,
    "poll_interval_seconds": 5
  },
  "deliverables": {
    "archive": true
  },
  "stitch": {
    "fallback_deps": ["colorama", "crcmod", "cryptography", "lxml", "packaging", "cbor2"]
  }
}
```

| Key | Default | Purpose |
|-----|---------|---------|
| `execution.mode` | `local` | `local` runs the stitch command as a subprocess here, in a private per-run copy of the tool and inputs; `remote` runs it in a pod created through the Job API. |
| `execution.endpoint` | — | **Required when `mode` is `remote`.** Linux Job API URL (`http://infra-api-fiv-linux.bios-dev-sh-cloud.intel.com/v1/fivportal/job`). |
| `execution.artifactory_repo` | — | **Required when `mode` is `remote`, or `local` in server mode.** Each run publishes under `<artifactory_repo>/<plan_id>-<random>/`. |
| `execution.image` | `amr-registry.caas.intel.com/fia-cloud/fiv-ubuntu24.04-python3.13:v1.1.1` | Linux pod image; must ship `/app/stitch_wrapper.py` (v1.1.1 or later, whose wrapper honours `deadline_epoch` — startup rejects older `vX.Y.Z` tags) and the Artifactory token. |
| `execution.repo_url` | `https://github.com/i-BinSun/ifwi-stitching-mcp.git` | Repo the wrapper installs `ifwi_mcp.worker` from. |
| `execution.repo_ref` | `main` | Branch, tag or 40-hex commit the wrapper checks out (pin a commit in production). |
| `execution.wrapper_path` | `/app/stitch_wrapper.py` | Wrapper path inside the image. |
| `execution.report_base_url` | — | Server mode: address the pods can reach; each pod POSTs its result to `<report_base_url>/internal/jobs/<job_id>/result`. Unset → no callback; jobs end when queried (Job API + `result.json`). |
| `execution.token` | — | Optional Bearer token sent to the Job API (it takes none today). |
| `execution.timeout_seconds` | `3600` | Kills a local run; for remote it is the pod's `activedeadlineseconds` and the wrapper's budget. Must exceed 465 s in remote mode. |
| `execution.poll_interval_seconds` | `5` | Synchronous waits (`async: false`, `stitch_execute_plan`) re-check the job this often; there is no background polling. |
| `execution.pending_timeout_seconds` | `900` | A pod the Job API still reports as pending after this long fails with `REMOTE_SCHEDULE_TIMEOUT` (must be below `timeout_seconds`). |
| `execution.finalize_grace_seconds` | `120` | After a pod ends without a callback, how long to wait for a late callback or `result.json`. |
| `execution.refresh_min_interval_seconds` | `5` | Minimum gap between two Job API checks of one job. |
| `execution.refresh_workers` | `16` | Threads for query-time refreshes, separate from the synchronous-request pool. |
| `execution.max_inline_spec_bytes` | `4096` | Larger wrapper specs are uploaded to `<run>/spec.json` and passed with `--spec-url`. |
| `execution.max_active_jobs` | `64` | Active-job limit (`JOB_QUEUE_FULL` beyond it). |
| `execution.max_running_pods` | `5` | Live remote pods at once; further remote jobs wait in state `queued`. `0` = no limit. |
| `execution.job_retention_hours` | `168` | How long finished job records are kept. |
| `execution.pod_cleanup_delay_seconds` | `30` | After a remote job ends on its own, delete its pod this many seconds later (gives the wrapper time for its last uploads). `0` deletes at once. Cancel and timeouts delete immediately. |
| `execution.run_retention_hours` | `0` | Server mode with local execution: hours to keep each finished `work/<run_id>/` for debugging; `0` deletes it right after upload. |
| `deliverables.archive` | `true` | Also zip the collected deliverables. |
| `stitch.fallback_deps` | `[]` | pip packages installed into the tool's venv when neither a `requirements.txt` nor a platform-specific `requirements_windows.txt`/`requirements_linux.txt` is found anywhere in it (see below). Empty by default — nothing installs unless set. |

`IFWI_MCP_EXEC_MODE`, `IFWI_MCP_EXEC_ENDPOINT`, `IFWI_MCP_EXEC_IMAGE`, `IFWI_MCP_EXEC_REPO`,
`IFWI_MCP_EXEC_REF`, `IFWI_MCP_EXEC_TOKEN`, the job settings listed below and `IFWI_MCP_STITCH_FALLBACK_DEPS`
(comma-separated) override the file. An invalid execution section fails startup, so a misconfigured
remote endpoint is caught before any work is done.

#### Dependency install when the tool ships no `requirements.txt`

`extract_stitch_tool` builds a venv per tool and installs its dependencies, in order:

1. `requirements.txt` next to `cli.py`/`stitch2.py`, or at the tool root.
2. A nested `requirements_windows.txt` / `requirements_linux.txt` (picked by host platform) anywhere under
   the tool root — legacy packages ship the FIT tool's own deps this way, several directories away from the
   entry script (e.g. under `FITm_Py/<version>/`). This is real, not a fallback: the FIT tool is invoked with
   `sys.executable` from that same venv, so its deps have to land there too.
3. Only if neither is found: `stitch.fallback_deps` / `IFWI_MCP_STITCH_FALLBACK_DEPS`, if configured.
   Nothing installs by default — set this only as a stopgap for a tool package that is missing its
   requirements file outright, until its owner ships one.

#### Configuring from the MCP host instead of a file

Every key above has an env-var equivalent, so the whole configuration can live in the host's `env` block
(e.g. `.mcp.json`) and no config file is needed at all:

| Env var | Config key |
|---------|------------|
| `IFWI_MCP_EXEC_MODE` | `execution.mode` |
| `IFWI_MCP_EXEC_ENDPOINT` | `execution.endpoint` |
| `IFWI_MCP_ARTIFACTORY_REPO` | `execution.artifactory_repo` |
| `IFWI_MCP_EXEC_IMAGE` | `execution.image` |
| `IFWI_MCP_EXEC_REPO` | `execution.repo_url` |
| `IFWI_MCP_EXEC_REF` | `execution.repo_ref` |
| `IFWI_MCP_EXEC_TOKEN` | `execution.token` |
| `IFWI_MCP_EXEC_TIMEOUT` | `execution.timeout_seconds` |
| `IFWI_MCP_EXEC_POLL_INTERVAL` | `execution.poll_interval_seconds` |
| `IFWI_MCP_REPORT_BASE_URL` | `execution.report_base_url` |
| `IFWI_MCP_WRAPPER_PATH` | `execution.wrapper_path` |
| `IFWI_MCP_EXEC_PENDING_TIMEOUT` | `execution.pending_timeout_seconds` |
| `IFWI_MCP_EXEC_FINALIZE_GRACE` | `execution.finalize_grace_seconds` |
| `IFWI_MCP_REFRESH_MIN_INTERVAL` | `execution.refresh_min_interval_seconds` |
| `IFWI_MCP_REFRESH_WORKERS` | `execution.refresh_workers` |
| `IFWI_MCP_MAX_INLINE_SPEC` | `execution.max_inline_spec_bytes` |
| `IFWI_MCP_MAX_ACTIVE_JOBS` | `execution.max_active_jobs` |
| `IFWI_MCP_MAX_RUNNING_PODS` | `execution.max_running_pods` |
| `IFWI_MCP_JOB_RETENTION_HOURS` | `execution.job_retention_hours` |
| `IFWI_MCP_POD_CLEANUP_DELAY` | `execution.pod_cleanup_delay_seconds` |
| `IFWI_MCP_JOBS_DB` | server-mode job database path (default `<cache>/jobs.db`; never on a network share) |
| `IFWI_MCP_RUN_RETENTION_HOURS` | `execution.run_retention_hours` |
| `IFWI_MCP_DELIVERABLES_ARCHIVE` | `deliverables.archive` (`0`/`false`/`no`/`off` → off) |
| `IFWI_MCP_STITCH_FALLBACK_DEPS` | `stitch.fallback_deps` (comma-separated) |
| `IFWI_MCP_CONFIG` | path of the config file itself |

Env vars always win over the file, so a shared config file can hold the defaults while one host overrides
just the mode.

#### Remote runner contract

When `mode` is `remote`, the server talks to the Linux FIV Job API directly. The pod side is
`stitch_wrapper.py` in the pod image (contract: cloud-dockerfile `docs/stitch_wrapper_contract.md`,
protocol 1); the full design is `docs/superpowers/specs/2026-10-28-remote-execution-status-design.md`.

1. The job is recorded first: `run_id = <plan_id>-<8 hex>` and `run_url = <artifactory_repo>/<run_id>`
   are stored before the pod exists, so a result can always be found under `run_url`.
2. `POST <endpoint>` with `{"image", "command", "os": "linux", "activedeadlineseconds",
   "ttlsecondsafterfinished"}` → `{"Code": 0, "Data": {"jobid": "fivportal-xxxxx"}}`. `command` is
   `exec python3 /app/stitch_wrapper.py --job-id <job_id> --spec <spec> --report-token <token>`. The Job
   API runs it as `sh -c "<command>"` with that `sh` as PID 1, which ignores SIGTERM; `exec` makes the
   wrapper PID 1 so a pod delete or deadline reaches it and it can still report. The spec (`zlib+base64url` JSON, or
   `--spec-url <run_url>/spec.json` when long) holds `run_id`, `run_url`, `report_url`, the worker repo,
   `timeouts.budget_seconds` and `timeouts.deadline_epoch`, and the plan — never a credential. The
   one-job report token is stored only as a sha256 hash. A create request that certainly never reached
   the Job API is retried with the same `run_id`; an uncertain one is never retried (`submit_unknown`).
3. The wrapper installs `repo_url@repo_ref`, runs `python -m ifwi_mcp.worker --plan-b64 ... --upload-url
   <run_url>`, uploads `pod.log`, writes `result.json` if the worker did not, and POSTs the final
   envelope once to `<report_base_url>/internal/jobs/<job_id>/result` (`Authorization: Bearer
   <report token>`). The worker uploads every deliverable to `<run_url>/deliverables/<name>` and writes
   its envelope (stamped with `schema_version`, `run_id`, `finished_at`) to `result.json`, to
   `IFWI_STITCH_RESULT_FILE` and to stdout — on success, failure, crash and SIGTERM alike.
4. The server ends the job on the callback. Without one, a status query checks the pod; once the
   pod has ended (or is gone, or the deadline passed) the job enters `finalizing` and reads
   `result.json`, inferring `REMOTE_RESULT_MISSING` / `REMOTE_TIMEOUT` only after
   `finalize_grace_seconds`. An inferred failure is replaced if the real result shows up later.
5. In server mode the result has the same shape as a local run: `plan_id`, `run_id`,
   `artifactory_url`, `stitched_bin` (`url`, `sha256`, `bytes`), `log_url` and `warnings`. Pod
   details (`pod_job_id`, `pod_log_url`, ...) are in the job's `remote` block; the full artifact list,
   command line and source URLs are in `<artifactory_url>/result.json`. In stdio mode the result keeps
   the full v1 shape and the deliverables are also downloaded into the local deliverables dir.
6. The pod is deleted once the job is over: `pod_cleanup_delay_seconds` (30 s) after it ends on its own, at
   once on cancel or timeout. A failed delete is retried on later queries and at startup; the Job API's
   `ttlsecondsafterfinished` (12 h) is the last resort. Progress shows in `data.cleanup.pod_delete`.

The pod image provides `IFWI_ARTIFACTORY_TOKEN` (and optionally `IFWI_FIVPORTAL_TOKEN`, needed only
for plans whose sources are not pinned to URLs); the wrapper passes them to the worker.

### Tools

All tools return the unified result shape described above.

| Tool | Signature | Purpose |
|------|-----------|---------|
| `fiv_list_projects` | `()` | List FIV projects (`id`, `name`, `project_name`). |
| `fiv_get_project` | `(project)` | Project detail + coarse type from `ifwi_type`/`ifwi_sub_type` (IFWI server/client/graphic, or UP/BIOS). |
| `fiv_list_swimlanes` | `(project, phase=None)` | All configured build types / swimlane branches (from build metadata, visible only); optional phase filter. |
| `fiv_list_releases` | `(project, phase, swimlane=None)` | Releases for a phase, newest first (no version needed). Name a `swimlane` to list a non-default lane. |
| `fiv_find_ifwi` | `(project, phase, version, swimlane=None)` | Resolve the IFWI package (`ifwi_url` = downloadable `.7z`) + its `.bin` list. If `swimlane` is omitted and more than one exists → `MULTIPLE_SWIMLANES`. |
| `fiv_list_release_binaries` | `(project, phase, version, swimlane=None)` | **Preferred way to pick an IFWI.** The curated, validated binaries of the release report; each entry links straight to a `.bin`. |
| `fiv_list_ifwi_binaries` | `(project, phase, version, swimlane=None)` | *Fallback* listing: all build targets of a release, each with its `.7z` `package_url` and contained `.bin` files. Use only when the user rejects every report binary, or no report exists. |
| `fiv_get_binary_ingredients` | `(project, phase, version, binary_name=None, swimlane=None, package_name=None)` | List the ingredients (name, version, ...) baked into one release binary — e.g. to read off which MMC version shipped in a given `.bin`. `binary_name` must match a `fiv_list_release_binaries`/`fiv_list_ifwi_binaries` entry; if omitted and the release has more than one binary, returns `IFWI_BINARY_AMBIGUOUS` with every candidate instead of guessing. |
| `fiv_find_ingredient` | `(project, name, version)` | Resolve an ingredient's download URL by exact name and version. |
| `fiv_list_ingredient_versions` | `(project, name, status="ALL", include_stitch_ingredient=0)` | List every version FIV has ever recorded for a named ingredient — authoritative, hits FIV's dedicated endpoint rather than scanning historical release binaries. |
| `fiv_find_stitch_tool` | `(project, phase, version, swimlane=None)` | Resolve the stitch-tool package (`stitch_url` = downloadable `.7z`) + its file list. For UP projects, automatically delegates to `fiv_find_env_package`. If no `build_target` reports a stitch package, falls back to browsing `release_root` on Artifactory directly (`<release_root>/<target-name>/<package>`) before giving up. |
| `fiv_find_env_package` | `(project, phase, version, swimlane=None)` | Resolve a UP (Unified Patch) release's env package URL from `get_ifwi_release_detail` (`env_atf_link` or download links). |
| `fiv_match_build_by_oem` | `(project, product, ifwi_version, flavor, phase, swimlane=None)` | **Path B** — reverse-match a build from parsed OEM info. |
| `parse_ifwi_oem` | `(local_ifwi_path)` | Decode the OEM region (offset `0xF00`, 256 bytes) of a local IFWI. Returns `product`, `ifwi_version`, `version`, `swimlane_hint`, `flavor_value`, `flavor_type`, `hash`. |
| `download` | `(url_or_path, category="ifwi", dest_name=None)` | Download an Artifactory URL or copy a local file into the cache. `category` ∈ `ifwi` / `ingredients` / `stitch`. |
| `list_local_files` | `()` | List files already cached across the categories. |
| `extract_archive` | `(archive_path, dest_name=None)` | Extract any `.zip`/`.tar*`/`.7z` into the cache; returns `extract_dir`, `file_count`, and the contained `.bin` files. Use for IFWI `.7z` packages. |
| `extract_stitch_tool` | `(archive_path)` | Extract a `.zip`/`.tar*`/`.7z` stitch tool, build its venv, list `Config_Stitch_*.ini` targets. |
| `run_stitch` | `(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None)` | Run the stitch tool's `cli.py` in its venv; returns the newest stitched `.bin`. `ingredients` is a JSON-array string, e.g. `[{"name": "MMC1", "path": "/abs/dir"}, {"name": "MMC2", "path": "/abs/dir2"}]` — all entries are stitched together in one invocation (pipe-joined, matching the vendor tool's own syntax). |
| `fiv_prepare_ifwi` | `(project, phase, version, swimlane=None, binary_name=None, from_build_targets=False)` | Fetch the base `.bin`. Reads the release report by default (direct download); `from_build_targets=True` downloads and extracts the build target's `.7z` instead. |
| `fiv_prepare_ingredient` | `(project, name, version)` | Download + extract an ingredient (exact version, no fallback). |
| `fiv_prepare_stitch` | `(project, phase, version=None, swimlane=None, search_neighbors=False)` | Download + extract the stitch tool and build its venv. A specific version with no stitch tool fails fast; re-call with `search_neighbors=True` to check adjacent versions and get candidates. |
| `get_execution_config` | `()` | Report the execution switch: `mode`, `endpoint`, timeouts, whether a token is set. |
| `stitch_build_plan` | `(project, phase, ingredients, config_ini, version=None, swimlane=None, ifwi_binary=None, ifwi_path=None, ifwi_from_build_targets=False, stitch_version=None, stitch_path=None, soft_strap=None, fiv_project_id=None)` | Turn confirmed answers into a saved plan + command line; `ingredients` is a JSON-array string of `{"name": str, "version": str}` or `{"name": str, "path": str}`. Remote execution requires the caller to provide `fiv_project_id` when building the plan. |
| `stitch_get_plan` / `stitch_list_plans` | `(plan_id)` / `()` | Re-read a saved plan, or list them. |
| `stitch_prepare_environment` | `(plan_id)` | Fetch and unpack everything the plan needs, without running it. |
| `stitch_execute_plan` | `(plan_id)` | Prepare, run (local or remote), and collect the deliverables; blocks until the run ends. Prefer `stitch_submit_plan`. |
| `stitch_submit_plan` | `(plan_id, client_request_id=None)` | Start a run and return at once with its `job_id` (same view as `GET /jobs/{id}`). |
| `stitch_get_job` | `(job_id, include_events=False)` | Job status; checks a running remote pod live. Poll every `poll_after_seconds` until `terminal`. `include_events=True` adds the job's event log. |
| `stitch_cancel_job` | `(job_id)` | Cancel an unfinished job (deletes its pod). |
| `stitch_list_jobs` | `(state=None, limit=20)` | Recent jobs, newest first. |
| `stitch_get_deliverables` | `(plan_id)` | stdio: re-read the local deliverables manifest. Server mode: the links of the plan's latest succeeded job. |
| `create_up_config` | `(output_path, default_params=None, ingredients=None, template_path=None, fit1_params=None, soc_params=None)` | Generate or update a UP (Unified Patch) Muppet config ini file. |
| `cache_cleanup` | `(max_age_days=14, categories=None, dry_run=True)` | Delete cached downloads, extractions, workspaces and deliverables older than N days. Defaults to a dry run. |

`phase` ∈ `Blue` / `Orange` / `Purple` / `Daily`. `version` matches `YYYY.WW.D.NN` (a leading dot is tolerated).

#### Error codes

Every failure carries a stable `error_code`, one of:

`INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `PROJECT_AMBIGUOUS`, `RELEASE_NOT_FOUND`,
`MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `IFWI_BINARY_NOT_FOUND`,
`IFWI_BINARY_AMBIGUOUS`, `OEM_REGION_NOT_FOUND`, `OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`,
`OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`, `VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`,
`CONFIG_INVALID`, `PLAN_INVALID`, `PLAN_NOT_FOUND`, `ENV_PREPARE_FAILED`, `REMOTE_EXEC_FAILED`,
`REMOTE_TIMEOUT`, `DELIVERABLE_MISSING`, `INTERNAL_ERROR`, and for jobs and remote runs
`REMOTE_SUBMIT_FAILED`, `REMOTE_SUBMIT_UNKNOWN`, `REMOTE_SCHEDULE_TIMEOUT`, `REMOTE_TERMINATED`,
`REMOTE_BOOTSTRAP_FAILED`, `REMOTE_WORKER_CRASHED`, `REMOTE_RESULT_MISSING`, `REMOTE_RESULT_INVALID`,
`REMOTE_RESULT_UNREADABLE`, `WRAPPER_SPEC_INVALID`, `WRAPPER_INTERNAL_ERROR`, `JOB_NOT_FOUND`,
`JOB_CANCELLED`, `JOB_INTERRUPTED`, `JOB_ALREADY_FINISHED`, `JOB_NOT_CANCELLABLE`, `JOB_QUEUE_FULL`,
`IDEMPOTENCY_CONFLICT`. Job errors carry `detail.job_id`, `run_id`, `artifactory_url` (remote:
`pod_job_id`, `pod_status`) and `detail.retryable`. In a job view (`GET /jobs/{id}`) `job_id`,
`plan_id`, `run_id` and `artifactory_url` appear only at the top level, not again inside `result`;
once a remote job ends, `remote` drops the polling fields (`pod_status`, `last_checked_at`,
`degraded`, `last_error`).

### Tool call sequences

These are the mechanics an MCP host (or a human calling tools directly) follows to turn a user's request
into a stitched image.

#### Choosing the IFWI binary

Once a version is settled, pick the binary in two steps — do **not** start from the build targets:

1. `fiv_list_release_binaries(project, phase, version)` → the release report's IFWI binaries. Show them to
   the user. Each entry is a direct `.bin` URL, so nothing has to be downloaded and extracted.
2. Only if the user says none of them is the one they want, call
   `fiv_list_ifwi_binaries(project, phase, version)`, which enumerates every build target of the release,
   and record the choice with `ifwi_from_build_targets=True`.

A release with no report answers `RELEASE_NOT_FOUND`; `fiv_prepare_ifwi` then falls back to the build
targets on its own. Other errors (`MULTIPLE_SWIMLANES`, `AUTH_FAILED`, …) are never swallowed by the
fallback — resolve them first, otherwise you risk picking a binary from the wrong swimlane.

#### Plan-driven flow (recommended)

The host asks the user questions, then hands the confirmed answers over:

1. Query tools (`fiv_list_projects`, `fiv_list_releases`, `fiv_list_release_binaries`, …) to fill in the
   answers. Ambiguity is reported as an error with `detail.candidates` so the host can ask one more question.
2. `stitch_build_plan(...)` → saved plan + `command_line`, e.g.
   `{python} cli.py --binary_file {binary_file} --ingredient_name MMC1|MMC2 --ingredient_path {ingredient_path} --config_ini {config_ini}`
   (multiple ingredients are pipe-joined into a single invocation, one entry each in the `ingredients` list).
   The braces are placeholders for the paths that only exist once the environment is prepared — show this to
   the user for a final confirmation.
3. `stitch_execute_plan(plan_id)` → prepares the environment (downloads the IFWI package, the ingredient and
   the stitch tool, builds the venv), runs the resolved command line locally or on the remote runner, and
   returns the exit code, the command that actually ran, and a deliverables manifest.

Deliverables land in `$CACHE_DIR/deliverables/<plan_id>/` — the stitched `.bin` (`kind: stitched_bin`), the
run log (`kind: log`), anything else the tool wrote to `output/` (`kind: output`), a `manifest.json` with
size + sha256 per item, and (unless disabled) a zip of the set. When a run fails, the log is still collected
and referenced from `detail.deliverables`.

Use `stitch_prepare_environment(plan_id)` first if you want to fetch everything and review it before running.

#### Path A — low-level, one call per step

1. `fiv_list_projects` → pick a project
2. `fiv_list_release_binaries(project, phase, version)` → pick a `.bin` and `download` it directly; if the
   user rejects them all, fall back to `fiv_find_ifwi` / `fiv_list_ifwi_binaries` (resolve the swimlane first
   if `MULTIPLE_SWIMLANES` comes back), then `download(ifwi_url)` + `extract_archive(local_path)`
3. `fiv_find_ingredient(...)` + `download(...)`
4. `fiv_find_stitch_tool(...)` + `download(..., category="stitch")`
5. `extract_stitch_tool(archive_path)` → pick a `config_ini` target
6. `run_stitch(stitch_dir, binary_file, ingredients, config_ini)` → stitched `.bin`

#### Path B — start from a local IFWI image

1. `parse_ifwi_oem(local.bin)` → `product`, `ifwi_version`, `flavor_value`
2. `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase)` → pinned swimlane / package
3. continue with `stitch_build_plan` / `stitch_execute_plan` (pass the local image as `ifwi_path`)

#### Path C — Unified Patch (UP) build flow

For UP projects (e.g. `category: "UP"` in `fiv_get_project`), the build tool is Muppet inside the release's env package:

1. `fiv_get_project(project)` → verifies project is a UP project (`category: "UP"`).
2. `fiv_find_env_package(project, phase, version, swimlane)` (or `fiv_find_stitch_tool`, which auto-delegates) → downloads the release's `env_atf_link` env package.
3. `create_up_config(output_path, default_params=..., ingredients=...)` → generates the UP config ini file (with `[default]`, `[FIT1]`, `[SOC]`, `[ucode_section]`).
4. `stitch_build_plan(project, phase, ingredients, config_ini)` → creates a UP plan running `python muppet.py -c <config_ini>` inside `MUPPET/` or `MUPPT_<project>/`.
5. `stitch_execute_plan(plan_id)` → executes locally or on remote worker, collecting output deliverables from `<build_path>/<version>/release/`.

### Development

```bash
# run the full test suite (uses responses to mock HTTP; stitch_runner tests build real venvs)
.venv/bin/python -m pytest -v
```

The test suite covers every module and the server layer. HTTP is mocked with `responses`; the
`stitch_runner` tests create real virtualenvs and run subprocesses, so they take a little longer.
