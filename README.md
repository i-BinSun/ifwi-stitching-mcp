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

The server needs three values: `FIV_BASE_URL` (required at startup), `FIV_TOKEN` and `ARTIFACTORY_TOKEN`
(validated lazily, only when a tool that needs them is called).

Copy [`.env.example`](.env.example) to `.env` in the project root and fill in your values:

```bash
cp .env.example .env
```

The server auto-loads `.env` (via `python-dotenv`) from its working directory or one of its parents on
startup — no host-specific `envFile` setting required. Values already present in the process environment
(e.g. set inline under a host's `env` block) always take priority over `.env`.

| Variable | Required | Purpose |
|----------|----------|---------|
| `FIV_BASE_URL` | **yes** | Base URL of the FIV Portal, e.g. `https://fiv.example.com`. |
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
| `downloader.py` | Artifactory download + local copy → cache | files only — no FIV knowledge |
| `oem_parser.py` | zero-dependency OEM region decoder | stdlib only — no network, no subprocess |
| `archive.py` | `.zip` / `.tar*` / `.7z` extraction | pure file I/O |
| `stitch_runner.py` | extract archive + build venv + render and run the `cli.py` command | the **only** module using subprocess / venv |
| `prepare.py` | find → download → extract, for IFWI / ingredient / stitch tool | orchestration of the layers above |
| `plan.py` | confirmed answers → validated plan + rendered command line | no network, no subprocess |
| `executor.py` | prepare the environment, then run the plan locally or on a remote runner | reads the execution switch |
| `deliverables.py` | collect results + artifacts into one dir with a manifest | pure I/O + downloads |
| `server.py` | FastMCP app: registers tools as thin pass-through wrappers | orchestration only |

### Advanced configuration

#### Config file — where stitch jobs run

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
  },
  "stitch": {
    "fallback_deps": ["colorama", "crcmod", "cryptography", "lxml", "packaging", "cbor2"]
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
| `stitch.fallback_deps` | `[]` | pip packages installed into the tool's venv when neither a `requirements.txt` nor a platform-specific `requirements_windows.txt`/`requirements_linux.txt` is found anywhere in it (see below). Empty by default — nothing installs unless set. |

`IFWI_MCP_EXEC_MODE`, `IFWI_MCP_EXEC_ENDPOINT`, `IFWI_MCP_EXEC_TOKEN` and `IFWI_MCP_STITCH_FALLBACK_DEPS`
(comma-separated) override the file. An invalid execution section fails startup, so a misconfigured remote
endpoint is caught before any work is done.

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
| `IFWI_MCP_EXEC_TOKEN` | `execution.token` |
| `IFWI_MCP_EXEC_TIMEOUT` | `execution.timeout_seconds` |
| `IFWI_MCP_EXEC_POLL_INTERVAL` | `execution.poll_interval_seconds` |
| `IFWI_MCP_DELIVERABLES_ARCHIVE` | `deliverables.archive` (`0`/`false`/`no`/`off` → off) |
| `IFWI_MCP_STITCH_FALLBACK_DEPS` | `stitch.fallback_deps` (comma-separated) |
| `IFWI_MCP_CONFIG` | path of the config file itself |

Env vars always win over the file, so a shared config file can hold the defaults while one host overrides
just the mode.

#### Remote runner contract

When `mode` is `remote`, the server:

1. `POST <endpoint>/jobs` with `{"plan": {...}}` → expects `{"job_id": "..."}`
2. polls `GET <endpoint>/jobs/<job_id>` → expects `{"status": "running|succeeded|failed", "exit_code": 0,
   "command_line": "...", "log_tail": "...", "deliverables": [{"name", "url", "kind"}]}`
3. downloads every deliverable URL into the local deliverables dir

The runner is expected to perform the same environment preparation the local mode does — the plan carries
everything needed for that. `Authorization: Bearer <execution.token>` is sent on all three steps.

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

#### Error codes

Every failure carries a stable `error_code`, one of:

`INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `RELEASE_NOT_FOUND`,
`MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `IFWI_BINARY_NOT_FOUND`,
`IFWI_BINARY_AMBIGUOUS`, `OEM_REGION_NOT_FOUND`, `OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`,
`OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`, `VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`,
`CONFIG_INVALID`, `PLAN_INVALID`, `PLAN_NOT_FOUND`, `ENV_PREPARE_FAILED`, `REMOTE_EXEC_FAILED`,
`REMOTE_TIMEOUT`, `DELIVERABLE_MISSING`, `INTERNAL_ERROR`.

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

### Development

```bash
# run the full test suite (uses responses to mock HTTP; stitch_runner tests build real venvs)
.venv/bin/python -m pytest -v
```

The test suite covers every module and the server layer. HTTP is mocked with `responses`; the
`stitch_runner` tests create real virtualenvs and run subprocesses, so they take a little longer.
