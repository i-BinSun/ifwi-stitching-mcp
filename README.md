# IFWI Stitching MCP

An [MCP](https://modelcontextprotocol.io/) server (Python + [FastMCP](https://github.com/jlowin/fastmcp)) that
drives the full IFWI stitching workflow from an MCP host: it queries the **FIV Portal** for projects and
releases, downloads IFWI / ingredient / stitch-tool artifacts from Artifactory, parses the OEM region of a
local IFWI image, and runs the stitch tool inside an isolated per-tool virtualenv.

## Architecture

Six focused modules sit behind a thin FastMCP tool layer. Every function returns a **unified result shape** —
`{"ok": True, "data": {...}}` or `{"ok": False, "error_code": <ENUM>, "message": <str>, "detail": {...}}`.
No bare exception ever crosses a tool boundary.

| Module | Responsibility | Constraints |
|--------|----------------|-------------|
| `result.py` | `ok()` / `err()` helpers + `ErrorCode` constants | — |
| `config.py` | env vars, cache layout, startup validation, token access | the **only** module reading `os.environ` |
| `fiv_portal.py` | FIV Portal REST client (projects, releases, swimlanes, finders) | HTTP + JSON only — no file I/O, no subprocess |
| `downloader.py` | Artifactory download + local copy → cache | files only — no FIV knowledge |
| `oem_parser.py` | zero-dependency OEM region decoder | stdlib only — no network, no subprocess |
| `stitch_runner.py` | extract archive + build venv + run `cli.py` | the **only** module using subprocess / venv |
| `server.py` | FastMCP app: registers tools as thin pass-through wrappers | orchestration only |

## Requirements

- **Python 3.9+**
- Dependencies: `fastmcp`, `requests` (runtime); `pytest`, `responses` (dev/test)

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
| `IFWI_MCP_CACHE_DIR` | no | Cache root. Defaults to `~/.ifwi-stitching-mcp/cache`. Subdirs: `ifwi/`, `ingredients/`, `stitch/`, `work/`. |

```bash
export FIV_BASE_URL="https://fiv.example.com"
export FIV_TOKEN="<fiv-access-token>"
export ARTIFACTORY_TOKEN="<artifactory-access-token>"
```

## Running

```bash
python -m ifwi_mcp.server
```

On startup the server validates `FIV_BASE_URL` and that the cache dir is creatable; on failure it prints an
error JSON to stderr and exits non-zero. Otherwise it starts the FastMCP server over stdio.

### Registering with an MCP host

Add an entry like this to your MCP host's server config (e.g. Claude Desktop's `mcpServers`):

```json
{
  "mcpServers": {
    "ifwi-stitching": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "ifwi_mcp.server"],
      "env": {
        "FIV_BASE_URL": "https://fiv.example.com",
        "FIV_TOKEN": "<fiv-access-token>",
        "ARTIFACTORY_TOKEN": "<artifactory-access-token>"
      }
    }
  }
}
```

## Tools

All tools return the unified result shape described above.

| Tool | Signature | Purpose |
|------|-----------|---------|
| `fiv_list_projects` | `()` | List FIV projects (`id`, `name`, `project_name`). |
| `fiv_get_project` | `(project)` | Project detail + coarse type from `ifwi_type`/`ifwi_sub_type` (IFWI server/client/graphic, or UP/BIOS). |
| `fiv_list_swimlanes` | `(project, phase=None)` | All configured build types / swimlane branches (from build metadata, visible only); optional phase filter. |
| `fiv_list_releases` | `(project, phase, swimlane=None)` | Releases for a phase, newest first (no version needed). Name a `swimlane` to list a non-default lane. |
| `fiv_find_ifwi` | `(project, phase, version, swimlane=None)` | Resolve the IFWI package (`ifwi_url` = downloadable `.7z`) + its `.bin` list. If `swimlane` is omitted and more than one exists → `MULTIPLE_SWIMLANES`. |
| `fiv_list_ifwi_binaries` | `(project, phase, version, swimlane=None)` | All build targets of a release, each with its `.7z` `package_url` and contained `.bin` files. |
| `fiv_find_ingredient` | `(project, name, version)` | Resolve an ingredient's download URL. |
| `fiv_find_stitch_tool` | `(project, phase, version, swimlane=None)` | Resolve the stitch-tool package (`stitch_url` = downloadable `.7z`) + its file list. |
| `fiv_match_build_by_oem` | `(project, product, ifwi_version, flavor, phase, swimlane=None)` | **Path B** — reverse-match a build from parsed OEM info. |
| `parse_ifwi_oem` | `(local_ifwi_path)` | Decode the OEM region (offset `0xF00`, 256 bytes) of a local IFWI. Returns `product`, `ifwi_version`, `flavor_value`, `flavor_type`, `hash`. |
| `download` | `(url_or_path, category="ifwi", dest_name=None)` | Download an Artifactory URL or copy a local file into the cache. `category` ∈ `ifwi` / `ingredients` / `stitch`. |
| `list_local_files` | `()` | List files already cached across the categories. |
| `extract_stitch_tool` | `(archive_path)` | Extract a `.zip`/`.tar*`/`.7z` stitch tool, build its venv, list `Config_Stitch_*.ini` targets. |
| `run_stitch` | `(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini, soft_strap=None)` | Run the stitch tool's `cli.py` in its venv; returns the newest stitched `.bin`. |

`phase` ∈ `Blue` / `Orange` / `Purple` / `Daily`. `version` matches `YYYY.WW.D.NN` (a leading dot is tolerated).

### Error codes

Every failure carries a stable `error_code`, one of:

`INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `RELEASE_NOT_FOUND`,
`MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `OEM_REGION_NOT_FOUND`,
`OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`, `OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`,
`VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`, `INTERNAL_ERROR`.

## Usage flows

**Path A — known project / phase / version:**

1. `fiv_list_projects` → pick a project
2. `fiv_find_ifwi(project, phase, version)` → resolve swimlane if `MULTIPLE_SWIMLANES` is returned (returns the `.7z` `ifwi_url` + the `.bin` names it contains; use `fiv_list_ifwi_binaries` to see every build target)
3. `download(ifwi_url)` → fetches the `.7z` build package
4. `fiv_find_ingredient(...)` + `download(...)`
5. `fiv_find_stitch_tool(...)` + `download(..., category="stitch")`
6. `extract_stitch_tool(archive_path)` → pick a `config_ini` target
7. `run_stitch(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini)` → stitched `.bin`

**Path B — start from a local IFWI image:**

1. `parse_ifwi_oem(local.bin)` → `product`, `ifwi_version`, `flavor_value`
2. `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase)` → pinned swimlane / package
3. continue with the download + stitch chain from Path A

## Development

```bash
# run the full test suite (uses responses to mock HTTP; stitch_runner tests build real venvs)
.venv/bin/python -m pytest -v
```

The test suite covers all six modules and the server layer. HTTP is mocked with `responses`; the
`stitch_runner` tests create real virtualenvs and run subprocesses, so they take a little longer.
