# IFWI Stitching MCP

English | [简体中文](README.zh-CN.md)

An [MCP](https://modelcontextprotocol.io/) server (Python + [FastMCP](https://github.com/jlowin/fastmcp)) that
drives the full IFWI stitching workflow from an MCP host: it queries the **FIV Portal** for projects and
releases, downloads IFWI / ingredient / stitch-tool artifacts from Artifactory, parses the OEM region of a
local IFWI image, and runs the stitch tool inside an isolated per-tool virtualenv.

## Architecture

Focused, single-responsibility modules sit behind a thin FastMCP tool layer. Every function returns a
**unified result shape** — `{"ok": True, "data": {...}}` or
`{"ok": False, "error_code": <ENUM>, "message": <str>, "detail": {...}}`.
No bare exception ever crosses a tool boundary.

| Module | Responsibility | Constraints |
|--------|----------------|-------------|
| `result.py` | `ok()` / `err()` helpers + `ErrorCode` constants | — |
| `config.py` | env vars, config file, cache layout, startup validation, token access | the **only** module reading `os.environ` / the config file |
| `fiv_portal.py` | FIV Portal REST client (projects, releases, swimlanes, finders) | HTTP + JSON only — no file I/O, no subprocess |
| `downloader.py` | Artifactory download + local copy → cache | files only — no FIV knowledge |
| `oem_parser.py` | zero-dependency OEM region decoder | stdlib only — no network, no subprocess |
| `archive.py` | `.zip` / `.tar*` / `.7z` extraction | pure file I/O |
| `stitch_runner.py` | extract archive + build venv + render and run the `cli.py` command | the **only** module using subprocess / venv |
| `prepare.py` | find → download → extract, for IFWI / ingredient / stitch tool | orchestration of the layers above |
| `plan.py` | confirmed answers → validated plan + rendered command line | no network, no subprocess |
| `executor.py` | prepare the environment, then run the plan locally or on a remote runner | reads the execution switch |
| `deliverables.py` | collect results + artifacts into one dir with a manifest | pure I/O + downloads |
| `server.py` | FastMCP app: registers tools as thin pass-through wrappers | orchestration only |

## Requirements

- **Python 3.9+**
- Dependencies: `fastmcp`, `requests`, `py7zr` (runtime); `pytest`, `responses` (dev/test)

## Installation

```bash
# from the project root
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# runtime install (editable)
pip install -e .

# with dev/test extras
pip install -e ".[dev]"
```

## Configuration

The server reads three environment variables. `FIV_BASE_URL` is required at startup; the two tokens are
validated lazily, only when a tool that needs them is called.

| Variable | Required | Purpose |
|----------|----------|---------|
| `FIV_BASE_URL` | **yes** | Base URL of the FIV Portal, e.g. `https://fiv.example.com`. REST paths are formed as `<base>/app/rest/<endpoint>`. |
| `FIV_TOKEN` | for FIV calls | The FIV access token. Sent as `Authorization: Bearer <token>` to FIV. |
| `ARTIFACTORY_TOKEN` | for downloads | The Artifactory access token. Sent as `Authorization: Bearer <token>` when downloading artifacts. |
| `IFWI_MCP_CACHE_DIR` | no | Cache root. Defaults to `~/.ifwi-stitching-mcp/cache`. Subdirs: `ifwi/`, `ingredients/`, `stitch/`, `extracted/`, `work/`, `plans/`, `deliverables/`. |

```bash
export FIV_BASE_URL="https://fiv.example.com"
export FIV_TOKEN="<fiv-access-token>"
export ARTIFACTORY_TOKEN="<artifactory-access-token>"
```

### Config file — where stitch jobs run

A JSON config file holds the **execution switch**. It lives at `IFWI_MCP_CONFIG`, or next to the cache dir
(`~/.ifwi-stitching-mcp/config.json`) by default. A missing file means "all defaults", i.e. local execution.

```json
{
  "execution": {
    "mode": "remote",
    "endpoint": "https://stitch-runner.example.com/api/v1",
    "token": "<runner-access-token>",
    "timeout_seconds": 3600,
    "poll_interval_seconds": 5
  },
  "deliverables": {
    "archive": true
  }
}
```

| Key | Default | Purpose |
|-----|---------|---------|
| `execution.mode` | `local` | `local` runs the stitch command as a subprocess here; `remote` hands the plan to a runner. |
| `execution.endpoint` | — | **Required when `mode` is `remote`.** Base URL of the runner's job API. |
| `execution.token` | — | Bearer token for the runner. Never echoed back by any tool. |
| `execution.timeout_seconds` | `3600` | Kills a local run / gives up polling a remote job after this long. |
| `execution.poll_interval_seconds` | `5` | How often a remote job's status is polled. |
| `deliverables.archive` | `true` | Also zip the collected deliverables. |

`IFWI_MCP_EXEC_MODE`, `IFWI_MCP_EXEC_ENDPOINT` and `IFWI_MCP_EXEC_TOKEN` override the file. An invalid
execution section fails startup, so a misconfigured remote endpoint is caught before any work is done.

#### Configuring from the MCP host instead of a file

Every key above has an env-var equivalent, so the whole configuration can live in the host's `env` block
(e.g. `.mcp.json`) and no config file is needed at all:

| Env var | Config key |
|---------|------------|
| `IFWI_MCP_EXEC_MODE` | `execution.mode` |
| `IFWI_MCP_EXEC_ENDPOINT` | `execution.endpoint` |
| `IFWI_MCP_EXEC_TOKEN` | `execution.token` |
| `IFWI_MCP_EXEC_TIMEOUT` | `execution.timeout_seconds` |
| `IFWI_MCP_EXEC_POLL_INTERVAL` | `execution.poll_interval_seconds` |
| `IFWI_MCP_DELIVERABLES_ARCHIVE` | `deliverables.archive` (`0`/`false`/`no`/`off` → off) |
| `IFWI_MCP_CONFIG` | path of the config file itself |

Env vars always win over the file, so a shared config file can hold the defaults while one host overrides
just the mode.

### Remote runner contract

When `mode` is `remote`, the server:

1. `POST <endpoint>/jobs` with `{"plan": {...}}` → expects `{"job_id": "..."}`
2. polls `GET <endpoint>/jobs/<job_id>` → expects `{"status": "running|succeeded|failed", "exit_code": 0,
   "command_line": "...", "log_tail": "...", "deliverables": [{"name", "url", "kind"}]}`
3. downloads every deliverable URL into the local deliverables dir

The runner is expected to perform the same environment preparation the local mode does — the plan carries
everything needed for that. `Authorization: Bearer <execution.token>` is sent on all three steps.

## Running

```bash
python -m ifwi_mcp.server
```

On startup the server validates `FIV_BASE_URL` and that the cache dir is creatable; on failure it prints an
error JSON to stderr and exits non-zero. Otherwise it starts the FastMCP server over stdio.

### Registering with an MCP host

**Claude Code / Claude Desktop** (`.mcp.json`, top-level key `mcpServers`):

```json
{
  "mcpServers": {
    "ifwi-stitching": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ifwi_mcp.server"],
      "env": {
        "FIV_BASE_URL": "https://fiv.example.com",
        "FIV_TOKEN": "<fiv-access-token>",
        "ARTIFACTORY_TOKEN": "<artifactory-access-token>",

        "IFWI_MCP_EXEC_MODE": "remote",
        "IFWI_MCP_EXEC_ENDPOINT": "https://stitch-runner.example.com/api/v1",
        "IFWI_MCP_EXEC_TOKEN": "<runner-access-token>"
      }
    }
  }
}
```

**VS Code Copilot** (`.vscode/mcp.json`, top-level key `servers` — *not* `mcpServers`):

```json
{
  "servers": {
    "ifwi-stitching": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/Scripts/python.exe",
      "args": ["-m", "ifwi_mcp.server"],
      "cwd": "${workspaceFolder}",
      "envFile": "${workspaceFolder}/.env",
      "dev": { "watch": "ifwi_mcp/**/*.py" }
    }
  }
}
```

`envFile` keeps the tokens in a gitignored `.env` instead of the config file; `dev.watch` restarts the server
whenever the source changes, which together with the editable install means an edit takes effect on save.

The `command` path is platform-dependent: `.venv/Scripts/python.exe` on Windows, `.venv/bin/python` on
Linux/macOS. Both host configs are per-machine, so pick the one that matches where the server runs — note
that a config written inside WSL needs the Linux path even when VS Code itself is on Windows.

Drop the three `IFWI_MCP_EXEC_*` entries to run locally, which is the default.

## Tools

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
| `fiv_find_ingredient` | `(project, name, version)` | Resolve an ingredient's download URL. |
| `fiv_find_stitch_tool` | `(project, phase, version, swimlane=None)` | Resolve the stitch-tool package (`stitch_url` = downloadable `.7z`) + its file list. If no `build_target` reports a stitch package, falls back to browsing `release_root` on Artifactory directly (`<release_root>/<target-name>/<package>`) before giving up. |
| `fiv_match_build_by_oem` | `(project, product, ifwi_version, flavor, phase, swimlane=None)` | **Path B** — reverse-match a build from parsed OEM info. |
| `parse_ifwi_oem` | `(local_ifwi_path)` | Decode the OEM region (offset `0xF00`, 256 bytes) of a local IFWI. Returns `product`, `ifwi_version`, `flavor_value`, `flavor_type`, `hash`. |
| `download` | `(url_or_path, category="ifwi", dest_name=None)` | Download an Artifactory URL or copy a local file into the cache. `category` ∈ `ifwi` / `ingredients` / `stitch`. |
| `list_local_files` | `()` | List files already cached across the categories. |
| `extract_archive` | `(archive_path, dest_name=None)` | Extract any `.zip`/`.tar*`/`.7z` into the cache; returns `extract_dir`, `file_count`, and the contained `.bin` files. Use for IFWI `.7z` packages. |
| `extract_stitch_tool` | `(archive_path)` | Extract a `.zip`/`.tar*`/`.7z` stitch tool, build its venv, list `Config_Stitch_*.ini` targets. |
| `run_stitch` | `(stitch_dir, binary_file, ingredients, config_ini, soft_strap=None)` | Run the stitch tool's `cli.py` in its venv; returns the newest stitched `.bin`. `ingredients` is a JSON-array string, e.g. `[{"name": "MMC1", "path": "/abs/dir"}, {"name": "MMC2", "path": "/abs/dir2"}]` — all entries are stitched together in one invocation (pipe-joined, matching the vendor tool's own syntax). |
| `fiv_prepare_ifwi` | `(project, phase, version, swimlane=None, binary_name=None, from_build_targets=False)` | Fetch the base `.bin`. Reads the release report by default (direct download); `from_build_targets=True` downloads and extracts the build target's `.7z` instead. |
| `fiv_prepare_ingredient` | `(project, name, version)` | Download + extract an ingredient (exact version, no fallback). |
| `fiv_prepare_stitch` | `(project, phase, version=None, swimlane=None, search_neighbors=False)` | Download + extract the stitch tool and build its venv. A specific version with no stitch tool fails fast; re-call with `search_neighbors=True` to check adjacent versions and get candidates. |
| `get_execution_config` | `()` | Report the execution switch: `mode`, `endpoint`, timeouts, whether a token is set. |
| `stitch_build_plan` | `(project, phase, ingredients, config_ini, version=None, swimlane=None, ifwi_binary=None, ifwi_path=None, ifwi_from_build_targets=False, stitch_version=None, stitch_path=None, soft_strap=None)` | Turn the answers confirmed with the user into a saved plan + its command line. Downloads nothing. `ingredients` is a JSON-array string, one entry per ingredient: `{"name": str, "version": str}` (fetched from FIV) or `{"name": str, "path": str}` (existing local dir/file). |
| `stitch_get_plan` / `stitch_list_plans` | `(plan_id)` / `()` | Re-read a saved plan, or list them. |
| `stitch_prepare_environment` | `(plan_id)` | Fetch and unpack everything the plan needs, without running it. |
| `stitch_execute_plan` | `(plan_id)` | Prepare, run (local or remote), and collect the deliverables. |
| `stitch_get_deliverables` | `(plan_id)` | Re-read the deliverables manifest of a finished plan. |

`phase` ∈ `Blue` / `Orange` / `Purple` / `Daily`. `version` matches `YYYY.WW.D.NN` (a leading dot is tolerated).

### Error codes

Every failure carries a stable `error_code`, one of:

`INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `RELEASE_NOT_FOUND`,
`MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `IFWI_BINARY_NOT_FOUND`,
`IFWI_BINARY_AMBIGUOUS`, `OEM_REGION_NOT_FOUND`, `OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`,
`OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`, `VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`,
`CONFIG_INVALID`, `PLAN_INVALID`, `PLAN_NOT_FOUND`, `ENV_PREPARE_FAILED`, `REMOTE_EXEC_FAILED`,
`REMOTE_TIMEOUT`, `DELIVERABLE_MISSING`, `INTERNAL_ERROR`.

## Usage flows

### Choosing the IFWI binary

Once a version is settled, pick the binary in two steps — do **not** start from the build targets:

1. `fiv_list_release_binaries(project, phase, version)` → the release report's IFWI binaries. Show them to
   the user. Each entry is a direct `.bin` URL, so nothing has to be downloaded and extracted.
2. Only if the user says none of them is the one they want, call
   `fiv_list_ifwi_binaries(project, phase, version)`, which enumerates every build target of the release,
   and record the choice with `ifwi_from_build_targets=True`.

A release with no report answers `RELEASE_NOT_FOUND`; `fiv_prepare_ifwi` then falls back to the build
targets on its own. Other errors (`MULTIPLE_SWIMLANES`, `AUTH_FAILED`, …) are never swallowed by the
fallback — resolve them first, otherwise you risk picking a binary from the wrong swimlane.

**Plan-driven flow (recommended)** — the host asks the user questions, then hands the confirmed answers over:

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

**Path A — low-level, one call per step:**

1. `fiv_list_projects` → pick a project
2. `fiv_list_release_binaries(project, phase, version)` → pick a `.bin` and `download` it directly; if the
   user rejects them all, fall back to `fiv_find_ifwi` / `fiv_list_ifwi_binaries` (resolve the swimlane first
   if `MULTIPLE_SWIMLANES` comes back), then `download(ifwi_url)` + `extract_archive(local_path)`
3. `fiv_find_ingredient(...)` + `download(...)`
4. `fiv_find_stitch_tool(...)` + `download(..., category="stitch")`
5. `extract_stitch_tool(archive_path)` → pick a `config_ini` target
6. `run_stitch(stitch_dir, binary_file, ingredients, config_ini)` → stitched `.bin`

**Path B — start from a local IFWI image:**

1. `parse_ifwi_oem(local.bin)` → `product`, `ifwi_version`, `flavor_value`
2. `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase)` → pinned swimlane / package
3. continue with `stitch_build_plan` / `stitch_execute_plan` (pass the local image as `ifwi_path`)

## Development

```bash
# run the full test suite (uses responses to mock HTTP; stitch_runner tests build real venvs)
.venv/bin/python -m pytest -v
```

The test suite covers every module and the server layer. HTTP is mocked with `responses`; the
`stitch_runner` tests create real virtualenvs and run subprocesses, so they take a little longer.

### Troubleshooting the server itself

- **Code changes have no effect** — the MCP server is a long-lived process and must be restarted. With
  `dev.watch` configured VS Code does it automatically; otherwise use `MCP: List Servers` → Restart.
- **The tool list or descriptions are stale** — run `MCP: Reset Cached Tools`.
- **The server won't start** — check `MCP: List Servers` → Show Output; a failed `validate_startup()` prints a
  JSON error naming the offending setting.
