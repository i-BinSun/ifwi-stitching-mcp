# Stitch Flow Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce stitch-workflow round-trips with two high-level "prepare" tools, fix `find_stitch_tool`'s false-negative on collateral packages, and make `run_stitch` auto-assemble its `ingredient_path` from a directory (regex as advisory, warnings never fail).

**Architecture:** A new `ifwi_mcp/prepare.py` orchestration module composes the existing pure-HTTP `fiv_portal` with the I/O modules (`downloader`, `archive`, `stitch_runner`) — keeping `fiv_portal`'s "no file I/O" contract intact. `find_stitch_tool` and `run_stitch` receive targeted fixes. Everything returns the project's standard `ok()/err()` result shape.

**Tech Stack:** Python ≥3.9, `requests`, `py7zr`, `fastmcp`; tests use `pytest` + `responses`.

## Global Constraints

- `requires-python = ">=3.9"` — no 3.10+-only syntax.
- Runtime dependencies limited to `fastmcp`, `requests`, `py7zr` (stdlib otherwise: `re`, `ast`, `configparser`, `pathlib`).
- Every public function returns the unified result dict: `ok(data)` → `{"ok": True, "data": {...}}`; `err(code, msg, detail)` → `{"ok": False, "error_code", "message", "detail"}` (from `ifwi_mcp/result.py`).
- `ErrorCode` values already exist: `RELEASE_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `INGREDIENT_NOT_FOUND`, `INVALID_ARGUMENT`, `STITCH_RUN_FAILED`.
- Diagnosability rule: regex/ingredient mismatch produces **warnings only — never a hard failure** in `run_stitch`.
- Ingredient rule: `prepare_ingredient` locates by **exact** `project+name+version` and **never** auto-falls-back to another version.
- Tests: HTTP mocked with `responses`; use the `clean_env` / `fiv_env` fixtures from `tests/conftest.py` and `tests/test_fiv_portal.py`. Run the full suite with `.venv/bin/python -m pytest -q`.

---

### Task 1: Fix `find_stitch_tool` detection (Fix A-1)

The current guard `if "stitch" in name.lower() and binaries:` (`ifwi_mcp/fiv_portal.py:412`) drops `IFWI_Stitch_Tool_Release` because that collateral package has an empty `binary_list` (no `.bin` payload). Stop requiring binaries; keep populating them when present.

**Files:**
- Modify: `ifwi_mcp/fiv_portal.py:405-420` (the loop inside `find_stitch_tool`)
- Test: `tests/test_fiv_portal.py`

**Interfaces:**
- Consumes: existing `find_stitch_tool(project, phase, version, swimlane=None)` and its `_get`/`resolve_project_id`/`_resolve_swimlane_or_multi` helpers (unchanged).
- Produces: `find_stitch_tool` returns `ok({"stitch_url": <release_root+package_path>, "package_name": <str>, "binaries": [<str>...]})` — `binaries` may now be `[]`. On no match, unchanged `err(STITCH_TOOL_NOT_FOUND, ..., {"available_packages": [...]})`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fiv_portal.py`:

```python
@responses.activate
def test_find_stitch_tool_collateral_empty_binaries(fiv_env):
    # The stitch-tool package is collateral: it carries NO .bin (empty binary_list).
    # It must still be found.
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [
            {"package_name": "OakStreamRp_DMR_FSP_Glue_Debug", "package_path": "g/glue.7z",
             "binary_list": [{"full_binary_name": "a.bin"}]},
            {"package_name": "IFWI_Stitch_Tool_Release", "package_path": "s/stitch.7z",
             "binary_list": []},
        ],
    }, status=200)
    r = fiv_portal.find_stitch_tool("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["ok"] is True
    assert r["data"]["stitch_url"] == "https://art/root/s/stitch.7z"
    assert r["data"]["package_name"] == "IFWI_Stitch_Tool_Release"
    assert r["data"]["binaries"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fiv_portal.py::test_find_stitch_tool_collateral_empty_binaries -v`
Expected: FAIL — currently returns `STITCH_TOOL_NOT_FOUND` (the `and binaries` gate skips the empty-binary_list package).

- [ ] **Step 3: Write minimal implementation**

Replace the loop body in `ifwi_mcp/fiv_portal.py` (currently lines ~407-418):

```python
    available = []
    for target in data.get("build_target", []):
        name = target.get("package_name") or ""
        available.append(name)
        # Stitch-tool packages are collateral and carry no .bin, so do NOT require a
        # non-empty binary_list. Match by package name; list any binaries if present.
        if "stitch" in name.lower():
            files = []
            for entry in (target.get("binary_list") or []):
                for fn in (entry.get("full_binary_name") or "").split(","):
                    fn = fn.strip()
                    if fn:
                        files.append(fn)
            return ok({"stitch_url": release_root + (target.get("package_path") or ""),
                       "package_name": name, "binaries": files})
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
               {"available_packages": available})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fiv_portal.py -k stitch_tool -v`
Expected: PASS — new test plus existing `test_find_stitch_tool_success` and `test_find_stitch_tool_none` all green (the success fixture has a non-empty binary_list → `binaries == ["stitch_tool.zip"]` still holds; the none fixture has no "stitch" package → still `STITCH_TOOL_NOT_FOUND`).

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/fiv_portal.py tests/test_fiv_portal.py
git commit -m "fix: find_stitch_tool detects collateral stitch packages with no .bin

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `run_stitch` auto-assembles `ingredient_path` (Fix A-2 + B)

The stitch `cli.py` expects `--ingredient_path` to be a `{regex_key: absolute_path}` dict-string. Teach `run_stitch` to build that from a **directory** by matching each `regex_mandatory_dict` key from the resolved config against the directory's files. Regex is advisory: 0 matches → best-effort token pick + warning; multiple → deterministic pick + warning; regex errors are swallowed. A dict-string passed in is used verbatim (manual override). When the config has no section for the ingredient (e.g. `"BIOS"`), the path is passed through unchanged (back-compat).

**Files:**
- Modify: `ifwi_mcp/stitch_runner.py` (add helpers; rework `run_stitch` ingredient handling + result payload)
- Test: `tests/test_stitch_runner.py`

**Interfaces:**
- Consumes: existing `run_stitch(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini, soft_strap=None)` and its config-resolution block (`resolved_ini`).
- Produces:
  - `_read_regex_mandatory(config_ini: Path, ingredient_name: str) -> Optional[dict]` — the ingredient's `{key: regex}` dict, or `None` if absent/unparseable.
  - `_pick_file(regex: str, files: list[Path], warnings: list, key: str) -> Optional[Path]` — advisory match; appends to `warnings`.
  - `_resolve_ingredient_arg(config_ini: Path, ingredient_name: str, ingredient_path: str) -> tuple[str, list]` — returns `(cli_ingredient_arg, warnings)`.
  - `run_stitch` success payload gains `"warnings": [<str>...]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_stitch_runner.py` (top-level imports `re`, `ast`, `configparser` are not needed in the test file itself):

```python
def test_read_regex_mandatory_reads_section(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n"
        "[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*_imh1-a0\\\\.bin$\"}\n"
    )
    d = stitch_runner._read_regex_mandatory(ini, "MMC1")
    assert d == {"MMC1_file": ".*mmc_pkg_.*_imh1-a0\\.bin$"}
    assert stitch_runner._read_regex_mandatory(ini, "NoSuchIngredient") is None


def test_resolve_ingredient_arg_exact_regex_match(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*imh1-a0\\\\.bin$\"}\n"
    )
    d = tmp_path / "ing"; d.mkdir()
    good = d / "mmc_pkg_0.915.0_00_10_sign-prod-debug_encrypt-prod_imh1-a0.bin"
    good.write_bytes(b"x")
    (d / "readme.txt").write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", str(d))
    assert ast.literal_eval(arg) == {"MMC1_file": str(good)}
    assert warnings == []


def test_resolve_ingredient_arg_best_effort_when_no_regex_match(clean_env, tmp_path):
    # 0.907.0 package lacks the _imh1-a0 suffix the regex requires -> best-effort + warning.
    ini = tmp_path / "cfg.ini"
    ini.write_text(
        "[Configuration]\n[MMC1]\n"
        "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*_sign-prod-debug_encrypt-prod_imh1-a0\\\\.bin$\"}\n"
    )
    d = tmp_path / "ing"; d.mkdir()
    close = d / "mmc_pkg_0.907.0_00_10_sign-prod-debug_encrypt-prod.bin"
    close.write_bytes(b"x")
    (d / "mmc_pkg_0.907.0_00_00_unsigned_unencrypted.bin").write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", str(d))
    assert ast.literal_eval(arg)["MMC1_file"] == str(close)  # most token overlap
    assert any("best-effort" in w for w in warnings)


def test_resolve_ingredient_arg_passthrough_dict_string(clean_env, tmp_path):
    ini = tmp_path / "cfg.ini"; ini.write_text("[Configuration]\n")
    dict_str = '{"MMC1_file": "/abs/path/to/file.bin"}'
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "MMC1", dict_str)
    assert arg == dict_str
    assert warnings == []


def test_resolve_ingredient_arg_passthrough_when_no_config_section(clean_env, tmp_path):
    # Back-compat: ingredient not in config -> path forwarded unchanged.
    ini = tmp_path / "cfg.ini"; ini.write_text("[Configuration]\n")
    f = tmp_path / "ing.bin"; f.write_bytes(b"x")
    arg, warnings = stitch_runner._resolve_ingredient_arg(ini, "BIOS", str(f))
    assert arg == str(f)
    assert warnings == []
```

Also add `import ast` to the top of `tests/test_stitch_runner.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_stitch_runner.py -k "regex_mandatory or resolve_ingredient" -v`
Expected: FAIL with `AttributeError: module 'ifwi_mcp.stitch_runner' has no attribute '_read_regex_mandatory'`.

- [ ] **Step 3: Write minimal implementation**

In `ifwi_mcp/stitch_runner.py`, add these imports near the top (with existing imports):

```python
import ast
import configparser
```

Add the helpers (place them above `run_stitch`):

```python
def _read_regex_mandatory(config_ini: Path, ingredient_name: str) -> Optional[dict]:
    """Return the ingredient's {file_key: regex} dict from the config, or None."""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(config_ini)
    except configparser.Error:
        return None
    if ingredient_name not in cp:
        return None
    raw = cp[ingredient_name].get("regex_mandatory_dict")
    if not raw:
        return None
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _pick_file(regex: str, files: list, warnings: list, key: str) -> Optional[Path]:
    """Advisory match of one regex against candidate files. Never raises."""
    strict = []
    for f in files:
        for cand in (str(f), str(f).replace("/", "\\")):
            try:
                if re.search(regex, cand):
                    strict.append(f)
                    break
            except re.error:
                break  # unparseable regex -> treat as no strict match
    if len(strict) == 1:
        return strict[0]
    if len(strict) > 1:
        pick = sorted(strict)[0]
        warnings.append(f"{key}: {len(strict)} files matched regex; picked {pick.name}")
        return pick
    # best-effort: score by literal token overlap with the regex
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", regex) if len(t) >= 3]
    if not files:
        warnings.append(f"{key}: no files available to match")
        return None
    best = max(files, key=lambda f: sum(1 for t in tokens if t.lower() in f.name.lower()))
    best_score = sum(1 for t in tokens if t.lower() in best.name.lower())
    if best_score == 0:
        warnings.append(f"{key}: no regex match and no token overlap; "
                        f"candidates={[f.name for f in files]}")
        return None
    warnings.append(f"{key}: no exact regex match; best-effort picked {best.name}")
    return best


def _resolve_ingredient_arg(config_ini: Path, ingredient_name: str,
                            ingredient_path: str) -> tuple:
    """Turn a directory (or dict-string) into the {key: path} dict-string cli.py wants.

    Regex matching is advisory: it never fails, only warns. If the ingredient has no
    config section, or an explicit dict-string is given, the input is passed through.
    """
    warnings: list = []
    stripped = ingredient_path.strip()
    if stripped.startswith("{"):
        try:
            if isinstance(ast.literal_eval(stripped), dict):
                return ingredient_path, warnings  # explicit override, verbatim
        except (ValueError, SyntaxError):
            pass
    regex_dict = _read_regex_mandatory(config_ini, ingredient_name)
    if not regex_dict:
        return ingredient_path, warnings  # back-compat passthrough
    p = Path(ingredient_path)
    if p.is_dir():
        files = [q for q in p.rglob("*") if q.is_file()]
    elif p.is_file():
        files = [p]
    else:
        files = []
    chosen = {}
    for key, regex in regex_dict.items():
        picked = _pick_file(regex, files, warnings, key)
        if picked is not None:
            chosen[key] = str(picked)
    if len(chosen) == len(regex_dict) and chosen:
        return str(chosen), warnings
    warnings.append(f"{ingredient_name}: could not resolve all mandatory files "
                    f"({len(chosen)}/{len(regex_dict)}); passing path through")
    return ingredient_path, warnings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_stitch_runner.py -k "regex_mandatory or resolve_ingredient" -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Wire the helper into `run_stitch` + add warnings to the payload**

In `ifwi_mcp/stitch_runner.py`, in `run_stitch`, replace the early ingredient-existence check so a dict-string is allowed. Change:

```python
    if not Path(ingredient_path).exists():
        return err(ErrorCode.INVALID_ARGUMENT, "ingredient_path not found",
                   {"param": "ingredient_path", "expected": "existing path"})
```

to:

```python
    if not ingredient_path.strip().startswith("{") and not Path(ingredient_path).exists():
        return err(ErrorCode.INVALID_ARGUMENT, "ingredient_path not found",
                   {"param": "ingredient_path", "expected": "existing path or dict-string"})
```

Then, after `resolved_ini` is validated and before building `cmd`, resolve the ingredient arg:

```python
    ingredient_arg, ingredient_warnings = _resolve_ingredient_arg(
        resolved_ini, ingredient_name, ingredient_path)
```

Change the `cmd` to use `ingredient_arg`:

```python
           "--ingredient_path", ingredient_arg,
```

Finally, include warnings in the success result:

```python
    return ok({"stitched_bin": str(bins[-1]), "log_path": str(log_path),
               "exit_code": 0, "warnings": ingredient_warnings})
```

- [ ] **Step 6: Add an integration test that the chosen dict-string reaches cli.py**

Add to `tests/test_stitch_runner.py`:

```python
def _make_stitch_zip_echo_ingredient(zip_path):
    """cli.py that echoes --ingredient_path into output/ingredient_arg.txt and a .bin."""
    cli_src = (
        "import argparse, os\n"
        "p = argparse.ArgumentParser()\n"
        "for a in ['binary_file','ingredient_name','ingredient_path','config_ini','soft_strap']:\n"
        "    p.add_argument(f'--{a}', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open('output/ingredient_arg.txt','w').write(a.ingredient_path)\n"
        "open('output/out.bin','wb').write(b'STITCHED')\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/cli.py", cli_src)
        z.writestr("Output/config/Config_Stitch_MMC.ini",
                   "[Configuration]\n[MMC1]\n"
                   "regex_mandatory_dict = {\"MMC1_file\": \".*mmc_pkg_.*imh1-a0\\\\.bin$\"}\n")


def test_run_stitch_auto_assembles_ingredient_dict(clean_env, tmp_path):
    zpath = tmp_path / "tool.zip"
    _make_stitch_zip_echo_ingredient(zpath)
    stitch_dir = stitch_runner.extract_stitch_tool(str(zpath))["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing"; ing.mkdir()
    mmc = ing / "mmc_pkg_0.915.0_00_10_sign-prod-debug_encrypt-prod_imh1-a0.bin"
    mmc.write_bytes(b"m")
    r = stitch_runner.run_stitch(stitch_dir, str(binary), "MMC1", str(ing), "Config_Stitch_MMC")
    assert r["ok"] is True, r
    echoed = Path(stitch_dir, "output", "ingredient_arg.txt").read_text()
    assert ast.literal_eval(echoed) == {"MMC1_file": str(mmc)}
    assert r["data"]["warnings"] == []
```

- [ ] **Step 7: Run the full stitch_runner suite**

Run: `.venv/bin/python -m pytest tests/test_stitch_runner.py -v`
Expected: PASS — new tests plus all pre-existing ones (`test_run_stitch_success` still passes because `"BIOS"` has no config section → passthrough).

- [ ] **Step 8: Commit**

```bash
git add ifwi_mcp/stitch_runner.py tests/test_stitch_runner.py
git commit -m "feat: run_stitch auto-assembles ingredient_path from a directory

Reads regex_mandatory_dict from the resolved config and matches files in
the ingredient directory; regex is advisory (best-effort pick + warning,
never a hard failure). Dict-strings and no-config-section ingredients pass
through unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `prepare.prepare_stitch` orchestration (C)

New module `ifwi_mcp/prepare.py`. `prepare_stitch` merges find → download → extract. Version strategy: if `version` given and it has a stitch tool → prepare it; if given but absent → return ±3 adjacent candidates for the caller to choose (no download); if omitted → scan latest→oldest (cap 30), prepare the first with a stitch tool.

**Files:**
- Create: `ifwi_mcp/prepare.py`
- Test: `tests/test_prepare.py` (new)

**Interfaces:**
- Consumes: `fiv_portal.find_stitch_tool(project, phase, version, swimlane)`, `fiv_portal.list_releases(project, phase, swimlane)` → `data["releases"]` (newest-first, each `{"version", ...}`); `downloader.download(url, category="stitch")` → `data["local_path"]`; `stitch_runner.extract_stitch_tool(local_path)` → `data` with `stitch_dir/venv_python/config_targets`.
- Produces:
  - Prepared: `ok({"prepared": True, "selected_version": <str>, "stitch_dir", "venv_python", "config_targets", "warnings": [...]})`.
  - Needs selection: `ok({"prepared": False, "selected_version": None, "candidates": [{"version", "stitch_url"}...], "warnings": [...]})`.
  - Errors propagate the underlying `err(...)`; exhausted scan → `err(STITCH_TOOL_NOT_FOUND, ...)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prepare.py`:

```python
# tests/test_prepare.py
import pytest
from ifwi_mcp import prepare
from ifwi_mcp.result import ok, err, ErrorCode


@pytest.fixture
def fakes(monkeypatch):
    """Replace prepare's collaborators with in-memory fakes."""
    state = {
        "stitch_by_version": {},   # version -> stitch_url (present == has stitch tool)
        "releases": [],            # newest-first list of version strings
        "downloaded": [],
        "extracted": [],
    }

    def fake_find_stitch_tool(project, phase, version, swimlane=None):
        url = state["stitch_by_version"].get(version)
        if url:
            return ok({"stitch_url": url, "package_name": "IFWI_Stitch_Tool_Release",
                       "binaries": []})
        return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
                   {"available_packages": []})

    def fake_list_releases(project, phase, swimlane=None):
        if not state["releases"]:
            return err(ErrorCode.RELEASE_NOT_FOUND, "no releases", {})
        rel = [{"version": v} for v in state["releases"]]
        return ok({"releases": rel, "latest": rel[0], "count": len(rel)})

    def fake_download(url, category="ifwi", dest_name=None):
        state["downloaded"].append((url, category))
        return ok({"local_path": f"/cache/{category}/{url.split('/')[-1]}",
                   "source": "download", "bytes": 1})

    def fake_extract_stitch_tool(local_path):
        state["extracted"].append(local_path)
        return ok({"stitch_dir": f"/x/{local_path.split('/')[-1]}",
                   "venv_python": "/x/venv/bin/python",
                   "config_targets": ["Config_Stitch_A"]})

    monkeypatch.setattr(prepare.fiv_portal, "find_stitch_tool", fake_find_stitch_tool)
    monkeypatch.setattr(prepare.fiv_portal, "list_releases", fake_list_releases)
    monkeypatch.setattr(prepare.downloader, "download", fake_download)
    monkeypatch.setattr(prepare.stitch_runner, "extract_stitch_tool", fake_extract_stitch_tool)
    return state


def test_prepare_stitch_version_present(fakes):
    fakes["stitch_by_version"] = {"2026.30.2.01": "https://art/s/stitch.7z"}
    r = prepare.prepare_stitch("P", "Orange", "2026.30.2.01")
    assert r["ok"] is True
    assert r["data"]["prepared"] is True
    assert r["data"]["selected_version"] == "2026.30.2.01"
    assert r["data"]["config_targets"] == ["Config_Stitch_A"]
    assert fakes["downloaded"] == [("https://art/s/stitch.7z", "stitch")]


def test_prepare_stitch_version_absent_offers_adjacent(fakes):
    # center 2026.30.2.01 has no stitch tool; a newer and an older neighbor do.
    fakes["releases"] = ["2026.31.1.01", "2026.30.4.01", "2026.30.2.01",
                         "2026.29.6.02", "2026.28.3.05"]
    fakes["stitch_by_version"] = {"2026.30.4.01": "https://art/a.7z",
                                  "2026.29.6.02": "https://art/b.7z"}
    r = prepare.prepare_stitch("P", "Orange", "2026.30.2.01")
    assert r["ok"] is True
    assert r["data"]["prepared"] is False
    vers = [c["version"] for c in r["data"]["candidates"]]
    assert vers == ["2026.30.4.01", "2026.29.6.02"]  # nearest-first
    assert fakes["downloaded"] == []                  # nothing downloaded on selection
    assert r["data"]["warnings"]


def test_prepare_stitch_no_version_scans_backward(fakes):
    fakes["releases"] = ["2026.30.2.01", "2026.30.4.01", "2026.29.6.02"]
    # latest has none; second one does
    fakes["stitch_by_version"] = {"2026.30.4.01": "https://art/x.7z"}
    r = prepare.prepare_stitch("P", "Orange")
    assert r["ok"] is True
    assert r["data"]["prepared"] is True
    assert r["data"]["selected_version"] == "2026.30.4.01"
    assert any("2026.30.2.01" in w for w in r["data"]["warnings"])


def test_prepare_stitch_no_version_none_found(fakes):
    fakes["releases"] = ["2026.30.2.01", "2026.30.4.01"]
    fakes["stitch_by_version"] = {}
    r = prepare.prepare_stitch("P", "Orange")
    assert r["error_code"] == ErrorCode.STITCH_TOOL_NOT_FOUND
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prepare.py -k stitch -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ifwi_mcp.prepare'`.

- [ ] **Step 3: Write minimal implementation**

Create `ifwi_mcp/prepare.py`:

```python
"""High-level orchestration: prepare a stitch tool or an ingredient end-to-end.

Composes the pure-HTTP fiv_portal with the I/O modules (downloader, archive,
stitch_runner). This is the only place those layers are chained together.
"""
from pathlib import Path

from . import archive, downloader, fiv_portal, stitch_runner
from .result import ok, err, ErrorCode

_SCAN_CAP = 30
_NEIGHBORS = 3


def _download_and_extract_stitch(stitch_url: str, version: str) -> dict:
    dl = downloader.download(stitch_url, category="stitch")
    if not dl["ok"]:
        return dl
    ext = stitch_runner.extract_stitch_tool(dl["data"]["local_path"])
    if not ext["ok"]:
        return ext
    data = dict(ext["data"])
    data["prepared"] = True
    data["selected_version"] = version
    data.setdefault("warnings", [])
    return ok(data)


def prepare_stitch(project: str, phase: str, version: str = None,
                   swimlane: str = None) -> dict:
    if version:
        found = fiv_portal.find_stitch_tool(project, phase, version, swimlane)
        if found["ok"]:
            return _download_and_extract_stitch(found["data"]["stitch_url"], version)
        # No stitch tool at the requested version -> offer adjacent candidates.
        rels = fiv_portal.list_releases(project, phase, swimlane)
        if not rels["ok"]:
            return rels
        versions = [r["version"] for r in rels["data"]["releases"]]
        if version not in versions:
            return err(ErrorCode.RELEASE_NOT_FOUND, "version not found in release list",
                       {"project": project, "phase": phase, "version": version})
        i = versions.index(version)
        neighbors = []
        for j in range(1, _NEIGHBORS + 1):
            for k in (i - j, i + j):          # nearest-first, newer then older
                if 0 <= k < len(versions):
                    neighbors.append(versions[k])
        candidates = []
        for v in neighbors:
            f = fiv_portal.find_stitch_tool(project, phase, v, swimlane)
            if f["ok"]:
                candidates.append({"version": v, "stitch_url": f["data"]["stitch_url"]})
        return ok({"prepared": False, "selected_version": None, "candidates": candidates,
                   "warnings": [f"version {version} has no stitch tool; "
                                f"{len(candidates)} adjacent candidate(s) found"]})

    # No version: scan latest -> oldest, prepare the first with a stitch tool.
    rels = fiv_portal.list_releases(project, phase, swimlane)
    if not rels["ok"]:
        return rels
    versions = [r["version"] for r in rels["data"]["releases"]]
    for v in versions[:_SCAN_CAP]:
        f = fiv_portal.find_stitch_tool(project, phase, v, swimlane)
        if f["ok"]:
            res = _download_and_extract_stitch(f["data"]["stitch_url"], v)
            if res["ok"] and v != versions[0]:
                res["data"]["warnings"].append(
                    f"latest release {versions[0]} had no stitch tool; used {v}")
            return res
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND,
               "no stitch tool in the latest releases scanned",
               {"project": project, "phase": phase, "scanned": len(versions[:_SCAN_CAP])})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prepare.py -k stitch -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/prepare.py tests/test_prepare.py
git commit -m "feat: prepare_stitch orchestrates find/download/extract with version fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `prepare.prepare_ingredient` orchestration (C)

Add `prepare_ingredient` to `ifwi_mcp/prepare.py`: exact `find_ingredient` → download → extract → return the extract dir and its file inventory. No version fallback.

**Files:**
- Modify: `ifwi_mcp/prepare.py` (add `prepare_ingredient`)
- Test: `tests/test_prepare.py`

**Interfaces:**
- Consumes: `fiv_portal.find_ingredient(project, name, version)` → `data["ingredient_url"]`; `downloader.download(url, category="ingredients")` → `data["local_path"]`; `archive.extract_archive(local_path)` → `data["extract_dir"]`.
- Produces: `ok({"ingredient_dir": <str>, "extracted_files": [<str>...], "ingredient_url", "ingredient_name", "ingredient_version", "warnings": []})`; on miss, propagates `err(INGREDIENT_NOT_FOUND, ...)` unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_prepare.py`:

```python
@pytest.fixture
def ing_fakes(monkeypatch, tmp_path):
    state = {"url_by_key": {}, "extract_dir": None}

    def fake_find_ingredient(project, name, version):
        url = state["url_by_key"].get((name, version))
        if url:
            return ok({"ingredient_url": url, "ingredient_name": name,
                       "ingredient_version": version})
        return err(ErrorCode.INGREDIENT_NOT_FOUND, "no ingredient link",
                   {"candidates": [], "ingredient_name": name,
                    "ingredient_version": version})

    def fake_download(url, category="ifwi", dest_name=None):
        return ok({"local_path": f"/cache/{category}/pkg.7z", "source": "download",
                   "bytes": 1})

    def fake_extract_archive(local_path, dest_name=None):
        d = tmp_path / "extracted"; (d / "binaries").mkdir(parents=True)
        (d / "binaries" / "mmc_pkg_0.907.0.bin").write_bytes(b"m")
        (d / "readme.txt").write_bytes(b"r")
        state["extract_dir"] = str(d)
        return ok({"extract_dir": str(d), "file_count": 2, "bins": []})

    monkeypatch.setattr(prepare.fiv_portal, "find_ingredient", fake_find_ingredient)
    monkeypatch.setattr(prepare.downloader, "download", fake_download)
    monkeypatch.setattr(prepare.archive, "extract_archive", fake_extract_archive)
    return state


def test_prepare_ingredient_success_lists_files(ing_fakes):
    ing_fakes["url_by_key"] = {("MMC1", "0.907.0"): "https://art/ing/z.7z"}
    r = prepare.prepare_ingredient("P", "MMC1", "0.907.0")
    assert r["ok"] is True
    assert r["data"]["ingredient_dir"] == ing_fakes["extract_dir"]
    names = [f.rsplit("/", 1)[-1] for f in r["data"]["extracted_files"]]
    assert "mmc_pkg_0.907.0.bin" in names and "readme.txt" in names
    assert r["data"]["ingredient_version"] == "0.907.0"


def test_prepare_ingredient_exact_miss_no_fallback(ing_fakes):
    ing_fakes["url_by_key"] = {}      # nothing registered
    r = prepare.prepare_ingredient("P", "MMC1", "0.999.0")
    assert r["error_code"] == ErrorCode.INGREDIENT_NOT_FOUND
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prepare.py -k ingredient -v`
Expected: FAIL with `AttributeError: module 'ifwi_mcp.prepare' has no attribute 'prepare_ingredient'`.

- [ ] **Step 3: Write minimal implementation**

Append to `ifwi_mcp/prepare.py`:

```python
def prepare_ingredient(project: str, name: str, version: str) -> dict:
    found = fiv_portal.find_ingredient(project, name, version)
    if not found["ok"]:
        return found  # INGREDIENT_NOT_FOUND passes through; never auto-fall-back
    dl = downloader.download(found["data"]["ingredient_url"], category="ingredients")
    if not dl["ok"]:
        return dl
    ext = archive.extract_archive(dl["data"]["local_path"])
    if not ext["ok"]:
        return ext
    extract_dir = ext["data"]["extract_dir"]
    files = sorted(str(p) for p in Path(extract_dir).rglob("*") if p.is_file())
    return ok({"ingredient_dir": extract_dir, "extracted_files": files,
               "ingredient_url": found["data"]["ingredient_url"],
               "ingredient_name": name, "ingredient_version": version, "warnings": []})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prepare.py -v`
Expected: PASS (all prepare tests, stitch + ingredient).

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/prepare.py tests/test_prepare.py
git commit -m "feat: prepare_ingredient (exact version, no fallback) with file inventory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Register the two new MCP tools (C)

Expose `fiv_prepare_stitch` and `fiv_prepare_ingredient` as MCP tools in `server.py`, guarded and registered like the rest.

**Files:**
- Modify: `ifwi_mcp/server.py` (import `prepare`; add two `@_guard` wrappers; add both to the registration tuple)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `prepare.prepare_stitch(project, phase, version=None, swimlane=None)`, `prepare.prepare_ingredient(project, name, version)`.
- Produces: `server.fiv_prepare_stitch(...)` and `server.fiv_prepare_ingredient(...)` importable callables, registered as MCP tools.

- [ ] **Step 1: Write the failing test**

In `tests/test_server.py`, extend the tool-existence list in `test_all_tools_exist` to include the two new names:

```python
def test_all_tools_exist():
    for name in ["fiv_list_projects", "fiv_get_project", "fiv_list_swimlanes",
                 "fiv_list_releases", "fiv_find_ifwi", "fiv_list_ifwi_binaries",
                 "fiv_find_ingredient", "fiv_find_stitch_tool", "fiv_match_build_by_oem",
                 "fiv_prepare_stitch", "fiv_prepare_ingredient",
                 "parse_ifwi_oem", "download", "list_local_files",
                 "extract_archive", "extract_stitch_tool", "run_stitch", "main"]:
        assert hasattr(server, name), name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_server.py::test_all_tools_exist -v`
Expected: FAIL — `assert hasattr(server, "fiv_prepare_stitch")` is False.

- [ ] **Step 3: Write minimal implementation**

In `ifwi_mcp/server.py`, add `prepare` to the import line:

```python
from . import archive, config, downloader, fiv_portal, oem_parser, prepare, stitch_runner
```

Add two wrappers (next to the other `@_guard` functions):

```python
@_guard
def fiv_prepare_stitch(project: str, phase: str, version: Optional[str] = None,
                       swimlane: Optional[str] = None) -> dict:
    return prepare.prepare_stitch(project, phase, version, swimlane)


@_guard
def fiv_prepare_ingredient(project: str, name: str, version: str) -> dict:
    return prepare.prepare_ingredient(project, name, version)
```

Add both to the registration tuple:

```python
for _fn in (fiv_list_projects, fiv_get_project, fiv_list_swimlanes, fiv_list_releases,
            fiv_find_ifwi, fiv_list_ifwi_binaries, fiv_find_ingredient,
            fiv_find_stitch_tool, fiv_match_build_by_oem,
            fiv_prepare_stitch, fiv_prepare_ingredient,
            parse_ifwi_oem, download,
            list_local_files, extract_archive, extract_stitch_tool, run_stitch):
    mcp.tool(_fn)
```

- [ ] **Step 4: Add a wrapper passthrough test**

Add to `tests/test_server.py`:

```python
def test_fiv_prepare_stitch_wrapper_passes_through(fiv_env, monkeypatch):
    from ifwi_mcp.result import ok
    monkeypatch.setattr(server.prepare, "prepare_stitch",
                        lambda *a, **k: ok({"prepared": False, "candidates": []}))
    r = server.fiv_prepare_stitch("P", "Orange")
    assert r["ok"] is True
    assert r["data"]["prepared"] is False
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — all pre-existing tests plus the new ones.

- [ ] **Step 6: Commit**

```bash
git add ifwi_mcp/server.py tests/test_server.py
git commit -m "feat: register fiv_prepare_stitch and fiv_prepare_ingredient MCP tools

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- New Tool 1 `fiv_prepare_stitch` (version-present / absent-±3 / omitted-backward-scan) → Task 3 + registration Task 5. ✓
- New Tool 2 `fiv_prepare_ingredient` (exact, no fallback, file inventory) → Task 4 + Task 5. ✓
- Fix A-1 `find_stitch_tool` collateral detection → Task 1. ✓
- Fix A-2 + B `run_stitch` auto-assemble, regex advisory, warnings-never-fail → Task 2. ✓
- Non-goals (no black-box `fiv_stitch`, no ingredient auto-fallback, no auth/OEM/archive changes) → respected; no task touches them. ✓

**Placeholder scan:** No TBD/TODO; every code and test step is complete and concrete. ✓

**Type consistency:**
- `find_stitch_tool` returns `{stitch_url, package_name, binaries}` — consumed by `prepare._download_and_extract_stitch` via `data["stitch_url"]`. ✓
- `list_releases` returns `data["releases"]` as list of dicts with `"version"` — `prepare_stitch` reads `r["version"]`. ✓
- `extract_stitch_tool` returns `stitch_dir/venv_python/config_targets` — `prepare` merges and adds `prepared/selected_version/warnings`. ✓
- `_resolve_ingredient_arg` returns `(str, list)`; `run_stitch` unpacks into `ingredient_arg, ingredient_warnings`. ✓
- `download` category `"ingredients"` matches `downloader._CATEGORIES` so `list_local_files` sees prepared ingredients. ✓

## Testing (full suite)

Final gate for the whole plan: `.venv/bin/python -m pytest -q` — expect the original 83 tests plus the new ones all passing.
