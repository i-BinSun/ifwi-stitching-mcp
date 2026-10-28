"""FastMCP server: thin tool wrappers over the ifwi_mcp modules."""
import functools
import json
import sys
import traceback
from typing import Optional

from fastmcp import FastMCP

from . import (archive, artifactory, cache, config, deliverables, executor, fiv_portal, jobs,
               oem_parser, plan as plan_mod, prepare, stitch_runner, up_config)
from .result import err, ok, ErrorCode

mcp = FastMCP("ifwi-stitching-mcp")


def _guard(fn):
    """Map any escaped exception to a unified INTERNAL_ERROR result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            # stdout is the MCP transport; diagnostics only ever go to stderr.
            traceback.print_exc(file=sys.stderr)
            return err(ErrorCode.INTERNAL_ERROR,
                       f"unexpected {type(exc).__name__} in {fn.__name__}",
                       {"tool": fn.__name__, "exception_type": type(exc).__name__,
                        "exception": str(exc),
                        "traceback": traceback.format_exc()[-2000:]})
    return wrapper


@_guard
def fiv_list_projects() -> dict:
    """List every FIV Portal project with its id, name and project_name."""
    return fiv_portal.list_projects()


@_guard
def fiv_get_project(project: str) -> dict:
    """Get one project's detail and its category (IFWI / UP / BIOS) from its FIV type fields."""
    return fiv_portal.get_project(project)


@_guard
def fiv_list_swimlanes(project: str, phase: Optional[str] = None) -> dict:
    """List a project's configured swimlane branches, optionally filtered to one phase.

    Use this to resolve a MULTIPLE_SWIMLANES error by asking the user which lane to use.
    """
    return fiv_portal.list_swimlanes(project, phase)


@_guard
def fiv_list_releases(project: str, phase: str, swimlane: Optional[str] = None) -> dict:
    """List releases for a phase, newest first. No version needed - use it to discover one."""
    return fiv_portal.list_releases(project, phase, swimlane)


@_guard
def fiv_find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    """Resolve one release's IFWI package: the downloadable .7z URL and the .bin names inside it.

    Don't just report the version and go: always show the returned `binaries` names to
    the user and let them pick which one they want before downloading anything.
    Returns MULTIPLE_SWIMLANES with candidates when swimlane is omitted and several exist.
    """
    return fiv_portal.find_ifwi(project, phase, version, swimlane)


@_guard
def fiv_list_release_binaries(project: str, phase: str, version: str,
                              swimlane: Optional[str] = None) -> dict:
    """List the IFWI binaries from a release's report. Try this FIRST to pick an IFWI.

    These are the curated, validated binaries of the release, each linking straight to a
    .bin. Show them to the user; only if they say none is the right one, fall back to
    fiv_list_ifwi_binaries, which enumerates every build target of the release.

    IMPORTANT: when `count` > 1, do not pick one yourself for ANY purpose - not just
    downloading. That includes answering data questions ("what MMC version does the latest
    release use?", "what's the download URL?"). Always list every `binary_name` and ask the
    user which one they mean before calling fiv_get_binary_ingredients or fiv_prepare_ifwi.
    """
    return fiv_portal.list_release_binaries(project, phase, version, swimlane)


@_guard
def fiv_list_ifwi_binaries(project: str, phase: str, version: str,
                           swimlane: Optional[str] = None) -> dict:
    """List every build target of a release with its package URL and contained .bin files.

    This is the fallback listing - prefer fiv_list_release_binaries, and come here only
    when the user rejects every binary in the release report, or no report exists.
    """
    return fiv_portal.list_ifwi_binaries(project, phase, version, swimlane)


@_guard
def fiv_get_binary_ingredients(project: str, phase: str, version: str,
                               binary_name: Optional[str] = None,
                               swimlane: Optional[str] = None,
                               package_name: Optional[str] = None) -> dict:
    """List the ingredients (name, version, ...) baked into one specific release binary.

    binary_name must match exactly (the `binary_name` field from fiv_list_release_binaries /
    fiv_list_ifwi_binaries), e.g. to read off which MMC version shipped in a given .bin.

    Omit binary_name only when you haven't checked the release yet: if the release has more
    than one binary, this refuses to guess and returns IFWI_BINARY_AMBIGUOUS with every
    candidate - show that list to the user and ask which one they mean before answering any
    question about ingredient/MMC versions. Never call this with an arbitrarily-picked
    binary_name when fiv_list_release_binaries showed several candidates.
    """
    return fiv_portal.get_binary_ingredients(project, phase, version, binary_name,
                                             swimlane, package_name)


@_guard
def fiv_find_ingredient(project: str, name: str, version: str) -> dict:
    """Resolve an ingredient's download URL by exact name and version. No version fallback."""
    return fiv_portal.find_ingredient(project, name, version)


@_guard
def fiv_list_ingredient_versions(project: str, name: str, status: str = "ALL",
                                 include_stitch_ingredient: int = 0) -> dict:
    """List every version FIV has ever recorded for a named ingredient.

    Authoritative - hits FIV's dedicated get_ingredient_version/ endpoint instead of
    inferring versions by scanning historical release binaries. status defaults to ALL
    (every VCS check-in status); pass e.g. 'Accept' to narrow to just checked-in versions.
    """
    return fiv_portal.list_ingredient_versions(project, name, status, include_stitch_ingredient)


@_guard
def fiv_find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    """Resolve a release's stitch-tool package URL and its file list."""
    return fiv_portal.find_stitch_tool(project, phase, version, swimlane)


@_guard
def fiv_find_env_package(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    """Resolve a UP (Unified Patch) release's env-package URL."""
    return fiv_portal.find_env_package(project, phase, version, swimlane)


@_guard
def fiv_match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                           phase: str, swimlane: Optional[str] = None) -> dict:
    """Reverse-match a FIV build from OEM fields parsed out of a local IFWI image.

    Feed it the output of parse_ifwi_oem to identify which release an unknown .bin came from.
    """
    return fiv_portal.match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane)


@_guard
def fiv_prepare_stitch(project: str, phase: str, version: Optional[str] = None,
                       swimlane: Optional[str] = None,
                       search_neighbors: bool = False) -> dict:
    """Download and extract the stitch tool and build its venv, in one call.

    Omit version to auto-pick the newest release that ships one. If a specific version has
    no stitch tool, this fails fast (no candidates) so the caller can tell the user first;
    re-call with search_neighbors=True to check adjacent versions and get candidates back.

    Returns `config_targets`: the Config_Stitch_*.ini names (without extension) found under
    the tool's config/ dir. Pick the one matching the chosen IFWI binary's flavor and pass
    its bare name as `config_ini` to stitch_build_plan/run_stitch — never ask the user for a
    local config path, it already lives inside this stitch tool.
    """
    return prepare.prepare_stitch(project, phase, version, swimlane, search_neighbors)


@_guard
def fiv_prepare_ingredient(project: str, name: str, version: str) -> dict:
    """Download and extract an ingredient, in one call. Exact version only, never falls back."""
    return prepare.prepare_ingredient(project, name, version)


@_guard
def fiv_prepare_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None,
                     binary_name: Optional[str] = None,
                     from_build_targets: bool = False) -> dict:
    """Fetch the base IFWI .bin, in one call.

    Reads the release report by default, which downloads the .bin directly. Pass
    from_build_targets=True when the user rejected every binary in that report; it then
    downloads and extracts the build target's .7z package instead. binary_name may be
    omitted only when there is exactly one candidate; otherwise the result lists them.
    """
    return prepare.prepare_ifwi(project, phase, version, swimlane, binary_name,
                                from_build_targets)


@_guard
def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    """Decode a local IFWI image's OEM region into product, ifwi_version, flavor and hash.

    ifwi_version may carry a swimlane prefix (ap.post-silicon.2026.41.3.01); it is split
    into `version` (the FIV release version) and `swimlane_hint`.
    """
    return oem_parser.parse_ifwi_oem(local_ifwi_path)


@_guard
def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    """Download an Artifactory URL, or copy a local file, into the cache.

    category is one of ifwi, ingredients or stitch.
    """
    return artifactory.download(url_or_path, category, dest_name)


@_guard
def list_local_files() -> dict:
    """List the files already cached, across all categories."""
    return artifactory.list_local_files()


@_guard
def extract_archive(archive_path: str, dest_name: Optional[str] = None) -> dict:
    """Extract a .zip / .tar* / .7z into the cache and report the .bin files it contained."""
    return archive.extract_archive(archive_path, dest_name)


@_guard
def extract_stitch_tool(archive_path: str) -> dict:
    """Extract a stitch-tool archive, build its venv, and list its Config_Stitch_*.ini targets."""
    return stitch_runner.extract_stitch_tool(archive_path)


def _parse_ingredients(ingredients: str):
    """Parse the ingredients JSON-array-string param. Returns (list, None) or (None, err_dict)."""
    try:
        parsed = json.loads(ingredients)
    except (ValueError, TypeError) as exc:
        return None, err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a JSON array string",
                         {"param": "ingredients", "reason": str(exc)})
    if not isinstance(parsed, list):
        return None, err(ErrorCode.INVALID_ARGUMENT, "ingredients must be a JSON array",
                         {"param": "ingredients", "got": type(parsed).__name__})
    return parsed, None


@_guard
def run_stitch(stitch_dir: str, binary_file: str, ingredients: str, config_ini: str,
               soft_strap: Optional[str] = None) -> dict:
    """Run an already-prepared stitch tool's cli.py and return the stitched .bin.

    Low-level: everything must already be on disk. Prefer stitch_execute_plan, which
    prepares the environment and collects deliverables for you.

    ingredients is a JSON array string, one entry per ingredient to stitch together in a
    single run (pipe-joined internally, as cli.py's own --ingredient_name/--ingredient_path
    expect): [{"name": "PowerOn_DMRAP_MMC1", "path": "/abs/dir"}, {"name": "...", "path": "..."}].
    """
    serving = config.get_serve_config()
    if not serving["ok"]:
        return serving
    if serving["data"]["mode"] == "server":
        return err(ErrorCode.CONFIG_INVALID,
                   "run_stitch runs locally; use stitch_execute_plan or POST /stitching in server mode",
                   {"param": "serve.mode", "expected": "local"})
    parsed, error = _parse_ingredients(ingredients)
    if error:
        return error
    return stitch_runner.run_stitch(stitch_dir, binary_file, parsed, config_ini, soft_strap)


@_guard
def get_execution_config() -> dict:
    """Report where stitch jobs run (local or remote) and which endpoint is used."""
    return config.get_execution_config()


@_guard
def stitch_build_plan(project: str, phase: str, ingredients: str, config_ini: str,
                      version: Optional[str] = None, swimlane: Optional[str] = None,
                      ifwi_binary: Optional[str] = None, ifwi_path: Optional[str] = None,
                      ifwi_from_build_targets: bool = False,
                      stitch_version: Optional[str] = None,
                      stitch_path: Optional[str] = None,
                      soft_strap: Optional[str] = None,
                      fiv_project_id: Optional[str] = None) -> dict:
    """Turn the answers confirmed with the user into a saved plan plus its command line.

    Call this once every parameter is settled. Nothing runs, so the returned
    command_line can be shown to the user for a final confirmation before
    stitch_execute_plan. Each input is taken either from FIV (version fields) or from a
    local path (the *_path arguments). Set ifwi_from_build_targets=True if the chosen
    ifwi_binary came from fiv_list_ifwi_binaries rather than the release report.

    If any ingredient is given as a local path, the stitch tool is prepared here (cheap
    if already fetched via fiv_prepare_stitch) and that ingredient's regex from the
    resolved config_ini is advisory-matched against the path. Check the response's
    ingredient_match_preview/warnings and show them to the user — never ask the user to
    pick which local file to use before this match has been attempted.

    ingredients is a JSON array string, one entry per ingredient to stitch together in a
    single run: [{"name": "PowerOn_DMRAP_MMC1", "version": "0.958.0"},
    {"name": "PowerOn_DMRAP_MMC2", "path": "/abs/dir"}]. Each entry needs a name and
    either version (fetched from FIV) or path (an existing local dir/file).

    config_ini is the bare name (with or without .ini) of one of the stitch tool's own
    Config_Stitch_*.ini files, e.g. "Config_Stitch_OKSDCRB1_IPCleanDFXEnable_Trace_DebugSigned"
    — get the list from fiv_prepare_stitch/extract_stitch_tool's `config_targets` and match it
    to the chosen IFWI binary's flavor. It is resolved against the prepared stitch tool's
    config/ dir at run time, so never ask the user for a local filesystem path here.

    fiv_project_id is optional and only recorded in the plan; remote execution runs the
    saved plan in a stitch pod and returns the Artifactory URLs of its deliverables.
    """
    parsed, error = _parse_ingredients(ingredients)
    if error:
        return error
    return plan_mod.build_plan(project, phase, parsed, config_ini, version,
                               swimlane, ifwi_binary, ifwi_path, ifwi_from_build_targets,
                               stitch_version, stitch_path, soft_strap, fiv_project_id)


@_guard
def stitch_get_plan(plan_id: str) -> dict:
    """Re-read a saved plan and its command line."""
    return plan_mod.load_plan(plan_id)


@_guard
def stitch_list_plans() -> dict:
    """List the saved plans, newest inputs first, with their project / phase / version."""
    return plan_mod.list_plans()


@_guard
def stitch_prepare_environment(plan_id: str) -> dict:
    """Download and unpack everything the plan needs, without running the command."""
    loaded = plan_mod.load_plan(plan_id)
    if not loaded["ok"]:
        return loaded
    return executor.prepare_environment(loaded["data"]["plan"])


@_guard
def stitch_execute_plan(plan_id: str) -> dict:
    """Prepare the environment, run the plan locally or remotely, return deliverables.

    Where it runs is decided by the execution.mode config switch - see get_execution_config.
    Blocks until the run ends (a remote run can take many minutes); prefer
    stitch_submit_plan + stitch_get_job. Returns the command line that actually ran, the
    exit code and the deliverables; data.task_job_id (or detail.job_id on failure) names
    the tracked job.
    """
    manager = jobs.get_manager()
    status, job = manager.submit_plan(plan_id)
    if status == "error":
        return job
    return manager.sync_result(manager.wait(job["job_id"]))


@_guard
def stitch_submit_plan(plan_id: str, client_request_id: Optional[str] = None) -> dict:
    """Start running a saved plan and return at once with its job_id.

    Preferred over stitch_execute_plan. Tell the user the job_id, then call stitch_get_job
    every poll_after_seconds until terminal is true. client_request_id is optional: pass
    the same value again only to retry a submission whose answer was lost.
    """
    bad = jobs.check_client_request_id(client_request_id)
    if bad:
        return bad
    manager = jobs.get_manager()
    status, job = manager.submit_plan(plan_id, client_request_id)
    if status == "error":
        return job
    return ok(manager.view(job, include_result=status == "existing"))


@_guard
def stitch_get_job(job_id: str) -> dict:
    """Status of a job: state, stage, terminal, poll_after_seconds and, once terminal, result.

    While a remote job runs this checks the pod live. Stop polling when terminal is true.
    If the job failed, do not submit it again without the user's confirmation. On
    REMOTE_SUBMIT_UNKNOWN, warn the user that a pod may still be running.
    """
    manager = jobs.get_manager()
    row = manager.get(job_id)
    if row is None:
        return jobs.not_found(job_id)
    return ok(manager.view(row))


@_guard
def stitch_cancel_job(job_id: str) -> dict:
    """Cancel a job that has not finished; a remote job's pod is deleted."""
    _status, body = jobs.get_manager().cancel(job_id)
    return body


@_guard
def stitch_list_jobs(state: Optional[str] = None, limit: int = 20) -> dict:
    """Recent jobs, newest first. state may be a comma-separated list, e.g. 'running'."""
    if not 1 <= limit <= 100:
        return err(ErrorCode.INVALID_ARGUMENT, "limit must be between 1 and 100", {"param": "limit"})
    states = tuple(s for s in (state or "").split(",") if s) or None
    items = jobs.get_manager().list_jobs(states=states, limit=limit)
    return ok({"jobs": items, "count": len(items)})


@_guard
def create_up_config(output_path: str,
                     default_params: Optional[str] = None,
                     ingredients: Optional[str] = None,
                     template_path: Optional[str] = None,
                     fit1_params: Optional[str] = None,
                     soc_params: Optional[str] = None) -> dict:
    """Generate or update a UP (Unified Patch) Muppet config ini file.

    default_params, ingredients, fit1_params, soc_params can be JSON strings (e.g. '{"mcheck": "/path/..."}').
    """
    def _parse_dict(d):
        if not d:
            return {}
        if isinstance(d, dict):
            return d
        try:
            return json.loads(d)
        except (ValueError, TypeError):
            return {}

    return up_config.generate_up_config_ini(
        output_path,
        default_params=_parse_dict(default_params),
        ingredients=_parse_dict(ingredients),
        template_path=template_path,
        fit1_params=_parse_dict(fit1_params),
        soc_params=_parse_dict(soc_params),
    )


@_guard
def stitch_get_deliverables(plan_id: str) -> dict:
    """Re-read the deliverables of a plan's latest successful run.

    stdio mode: the local manifest (artifacts, sizes and sha256). Server mode: the
    Artifactory links of the most recent succeeded job for this plan.
    """
    if not jobs.get_manager().server_mode:
        return deliverables.load_manifest(plan_id)
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.INVALID_ARGUMENT, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    row = jobs.get_manager().latest_succeeded(plan_id)
    if row is None:
        return err(ErrorCode.DELIVERABLE_MISSING, "no successful job for this plan",
                   {"plan_id": plan_id, "hint": "check the job with stitch_get_job or stitch_list_jobs"})
    data = (row["result"] or {}).get("data") or {}
    return ok({"plan_id": plan_id, "job_id": row["job_id"], "run_id": row["run_id"],
               **{key: data.get(key) for key in ("stitched_bin", "log_url", "artifacts",
                                                 "artifactory_url")}})


@_guard
def cache_cleanup(max_age_days: int = 14, categories: Optional[str] = None,
                  dry_run: bool = True) -> dict:
    """Delete cached downloads, extractions, workspaces and deliverables older than N days.

    Nothing here is irreplaceable - everything is re-downloaded or re-extracted on demand.
    Defaults to dry_run=True: run it once to see what would go, then again with
    dry_run=False. categories is an optional JSON array string, e.g. '["work", "extracted"]';
    omit it to sweep all of work, extracted, stitch, ifwi, ingredients, deliverables, plans.
    max_age_days=0 clears the selected categories outright.
    """
    selected = None
    if categories:
        try:
            selected = json.loads(categories)
        except (ValueError, TypeError) as exc:
            return err(ErrorCode.INVALID_ARGUMENT, "categories must be a JSON array string",
                       {"param": "categories", "reason": str(exc)})
        if not isinstance(selected, list):
            return err(ErrorCode.INVALID_ARGUMENT, "categories must be a JSON array",
                       {"param": "categories", "got": type(selected).__name__})
    return cache.cleanup(max_age_days, selected, dry_run)


# Register each plain function as an MCP tool (keeps the plain callable importable for tests).
for _fn in (fiv_list_projects, fiv_get_project, fiv_list_swimlanes, fiv_list_releases,
            fiv_find_ifwi, fiv_list_release_binaries, fiv_list_ifwi_binaries,
            fiv_get_binary_ingredients, fiv_find_ingredient, fiv_list_ingredient_versions,
            fiv_find_stitch_tool, fiv_find_env_package, fiv_match_build_by_oem,
            fiv_prepare_stitch, fiv_prepare_ingredient, fiv_prepare_ifwi,
            parse_ifwi_oem, download,
            list_local_files, extract_archive, extract_stitch_tool, run_stitch,
            get_execution_config, stitch_build_plan, stitch_get_plan, stitch_list_plans,
            stitch_prepare_environment, stitch_execute_plan, stitch_submit_plan, stitch_get_job,
            stitch_cancel_job, stitch_list_jobs, stitch_get_deliverables,
            create_up_config, cache_cleanup):
    mcp.tool(_fn)


def main() -> None:
    startup = config.validate_startup()
    if not startup["ok"]:
        print(json.dumps(startup), file=sys.stderr)
        sys.exit(1)
    serving = config.get_serve_config()["data"]
    execution = config.get_execution_config()["data"]
    if execution["mode"] == "remote" and config.image_tag_version(execution["image"]) is None:
        print(f"WARNING: cannot tell whether execution.image {execution['image']} ships "
              "stitch_wrapper.py (v1.1.0 or later is required)", file=sys.stderr)
    if serving["mode"] == "server":
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        # Auth is attached to the network-facing instance before it starts serving.
        mcp.auth = StaticTokenVerifier({config.get_serve_token(): {"client_id": "ifwi-mcp-client",
                                                           "scopes": []}})
        import threading

        import uvicorn
        from .http_api import create_app

        # Settle jobs the previous process left behind before serving, then catch up
        # on callbacks lost while it was down without delaying startup.
        manager = jobs.get_manager()
        recovered = manager.recover_on_startup()
        print(f"job store {config.get_jobs_db_path()}: {json.dumps(recovered)}", file=sys.stderr)
        threading.Thread(target=manager.startup_sweep, name="ifwi-startup-sweep",
                         daemon=True).start()
        uvicorn.run(create_app(mcp), host=serving["host"], port=serving["port"])
    else:
        mcp.run()


if __name__ == "__main__":
    main()
