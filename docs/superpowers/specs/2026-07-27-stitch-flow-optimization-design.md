# Stitch Flow Optimization — Design

**Date:** 2026-07-27
**Status:** Approved (design)
**Scope:** `ifwi_mcp/fiv_portal.py`, `ifwi_mcp/stitch_runner.py`, `ifwi_mcp/server.py`

## Motivation

A real end-to-end session (locate DMR IMH2 pre-silicon Orange release → download & extract
stitch tool → find/download/extract an MMC1 ingredient → attempt stitch) exposed several
friction points:

1. **Too many round-trips.** The happy path required ~12 sequential tool calls
   (get_project → swimlanes → releases → find_stitch_tool → *(fails)* → list_ifwi_binaries →
   download → extract_stitch_tool → find_ingredient → download → extract → run_stitch).
2. **`find_stitch_tool` false-negative.** The latest release *did* contain an
   `IFWI_Stitch_Tool_Release` package, but the tool returned `STITCH_TOOL_NOT_FOUND`.
3. **`ingredient_path` semantics undocumented.** The stitch `cli.py` expects a
   `{regex_key: absolute_path}` dict-string, but the MCP layer passed the argument
   through verbatim, causing file→directory trial-and-error.
4. **No pre-flight feedback.** Whether an ingredient file matches the config's
   `regex_mandatory_dict` was only discoverable by running the entire stitch.

## Priorities

1. **C — Orchestration.** Merge deterministic multi-step chains into high-level tools.
2. **A — Correctness.** Fix `find_stitch_tool` detection and `run_stitch` `ingredient_path`.
3. **B — Diagnosability.** Regex mismatch / multi-candidate situations produce **warnings
   only — never a hard failure.**

## Root causes (verified in code)

- `fiv_portal.py:412` gates stitch-package detection on `"stitch" in name.lower() and binaries`.
  `IFWI_Stitch_Tool_Release` is a *collateral* package with an empty `full_binary_name`
  (no `.bin` payload), so `and binaries` filters it out. Other releases only ship
  `FSP_Glue` targets, hence `NOT_FOUND`.
- `stitch_runner.run_stitch` forwards `ingredient_path` unchanged. The stitch tool's
  `_resolve_mandatory_files` (`steps/bios_stitch.py:379`) `eval()`s the argument; a bare
  file path raises → falls back to `os.listdir()` → for a file that raises
  `NotADirectoryError`, for a directory it returns **bare filenames** that later fail
  `get_file_path()` (`infra/util.py:135`), yielding `GenFfs ... -i None`.
- The stitch GUI (`gui.py`) reads `regex_mandatory_dict` only to *display* the regex as a
  hint; the actual file is chosen by the user via a Browse dialog, then serialized as
  `str({file_name: absolute_path})`. Regex is advisory, not an auto-matcher.

## New Tool 1 — `fiv_prepare_stitch`

Merges `find_stitch_tool → download → extract_stitch_tool` into one call.

**Inputs:** `project` (required), `phase` (required), `version` (optional),
`swimlane` (optional).

**Version-selection strategy:**

- **`version` given:** look for a stitch-tool package in that release first. If absent,
  scan **±3 adjacent releases** (3 newer + 3 older, centered on the given version) and
  return the candidates that *do* contain a stitch tool as a **list for the caller to
  choose** — do **not** auto-select. Attach a warning that the central version had no
  stitch package. Candidates ordered by distance from center (nearest first).
- **`version` omitted:** scan from the latest release **backward** (cap 30 releases) and
  return the first release that contains a stitch tool, noting the actually-selected
  version.

**Returns:** `stitch_dir`, `venv_python`, `config_targets`, `selected_version`,
plus any `warnings`.

When adjacent-release candidates are returned for the caller to choose, the response
carries the candidate list (each with version + package URL) and does not download;
the caller re-invokes with an explicit `version`.

## New Tool 2 — `fiv_prepare_ingredient`

Merges `find_ingredient → download → extract` into one call.

**Inputs:** `project` (required), `name` (required), `version` (required, exact).

**Behavior:** locate strictly by `project + name + version`, download, extract.
On no exact match: return `INGREDIENT_NOT_FOUND` (a warning **may** list adjacent
available versions for reference), but **never auto-fall-back** to a different version —
the ingredient is the unit under test and must be exactly what the caller selected.

**Returns:** `ingredient_dir`, `extracted_files` (file inventory to help the next
`run_stitch` pick a variant), `ingredient_url`, plus any `warnings`.

## Fix A-1 — `find_stitch_tool` detection

Stitch-tool packages carry no `.bin`, so stop requiring `binaries` to be non-empty.
Match the package by name (`"stitch" in name.lower()`, recognizing `*Stitch_Tool*`),
and populate `binaries` when present, else return an empty list.

## Fix A-2 + B — `run_stitch` `ingredient_path` (GUI-informed, auto-assemble)

`run_stitch` accepts either:

- **A directory** (as produced by `fiv_prepare_ingredient`), or
- **A dict-string** `{file_key: absolute_path}` (explicit override / manual variant choice).

When given a directory, `run_stitch`:

1. Reads `regex_mandatory_dict` for `ingredient_name` from the resolved `config_ini`.
2. For each `file_key: regex`, matches files in the directory:
   - **exactly 1 match** → use it;
   - **0 matches** → **warning** (list all directory files as candidates) and proceed
     with a nearest/best-effort choice, rather than failing; if no reasonable choice can
     be made, report candidates back for the caller to specify explicitly next round;
   - **multiple matches** → **warning**, pick one (deterministic ordering) and list the rest.
3. Assembles `{file_key: absolute_path}` and serializes to the dict-string cli.py expects.

Multi-file ingredients (e.g. TDXModule `.so` + `.sigstruct`) resolve each `file_key`
independently and assemble the full dict.

**Invariant:** regex matching is advisory. A regex mismatch **never** fails `run_stitch`;
it degrades to warning + best-effort selection. This directly covers the observed
`_imh1-a0` case: the 0.907.0 MMC1 package lacks the `_imh1-a0` suffix the regex requires,
so it would be selected with a warning, letting the validation proceed.

## Non-goals (YAGNI)

- No fully-automated `fiv_stitch` (project-name → stitched bin in one black-box call).
  Config-target selection, ingredient-variant selection, and `run_stitch` stay as
  human-judgment decision points.
- No ingredient version auto-fallback.
- No changes to authentication, OEM parsing, or archive internals.

## Testing

- **`find_stitch_tool`:** unit test with a fixture release whose stitch target has an
  empty `full_binary_name` → asserts the package is now found.
- **`fiv_prepare_stitch`:** version-given-present, version-given-absent (adjacent
  candidates listed, none downloaded), version-omitted (backward scan picks first with
  stitch tool). Mock the portal + download + extract layers.
- **`fiv_prepare_ingredient`:** exact hit; exact miss → `INGREDIENT_NOT_FOUND` with
  adjacent-versions warning and no fallback.
- **`run_stitch` ingredient resolution:** directory with 1 regex match; directory with 0
  matches (warning + best-effort, no failure); directory with multiple matches (warning +
  deterministic pick); explicit dict-string passthrough; multi-file ingredient.
- Existing tests continue to pass.
