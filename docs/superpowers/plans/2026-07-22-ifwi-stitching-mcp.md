# IFWI Stitching MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure MCP server (Python + FastMCP) that queries FIV Portal, downloads IFWI/ingredient/stitch-tool artifacts, parses local IFWI OEM regions, and runs the stitch tool in a per-tool venv.

**Architecture:** Six focused modules behind a thin FastMCP tool layer. `config` reads env/validates; `fiv_portal` is a pure HTTP+JSON client; `downloader` turns URLs/paths into cached files; `oem_parser` is a zero-dependency byte→struct decoder copied from the stitch tool; `stitch_runner` handles zip/venv/subprocess; `server` wires them into MCP tools. Every function returns a unified `{ok, error_code, message, detail}` result — no bare exceptions cross a tool boundary.

**Tech Stack:** Python 3.9+, `fastmcp`, `requests`; stdlib `zipfile`/`tarfile`/`venv`/`subprocess`/`struct`/`dataclasses`. Tests with `pytest` + `responses` (HTTP mocking).

## Global Constraints

- **Python floor: 3.9+.** Use `typing.Optional`/`typing.Union`, NOT `X | Y` union syntax. Dataclasses OK.
- **Unified result shape.** Every tool and every internal module entry point returns either `{"ok": True, "data": {...}}` or `{"ok": False, "error_code": <ENUM>, "message": <str>, "detail": {...}}`. Never raise across a tool boundary; catch and map to `INTERNAL_ERROR`.
- **Stable error_code enum** (exact strings): `INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `RELEASE_NOT_FOUND`, `MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `OEM_REGION_NOT_FOUND`, `OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`, `OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`, `VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`, `INTERNAL_ERROR`.
- **Fail fast.** Every tool validates its args at entry; validation failure returns `INVALID_ARGUMENT` with `detail={"param": ..., "expected": ...}`.
- **FIV auth:** send env `FIV_TOKEN` value verbatim as the `Authorization` header. Missing → `MISSING_TOKEN` `detail={"which":"fiv"}`.
- **Artifactory auth:** send `Authorization: Bearer <ARTIFACTORY_TOKEN>`. Missing → `MISSING_TOKEN` `detail={"which":"artifactory"}`.
- **FIV REST**: base = `FIV_BASE_URL`, path prefix `/app/rest/`, all GET. Phase ∈ {`Blue`,`Orange`,`Purple`,`Daily`}.
- **OEM region**: fixed offset `0xF00`, length `256` bytes, struct `<16s 48s 128s 32s 32s`. No magic bytes. Copied from stitch tool `defs/structure.py` — keep a provenance comment at module top.
- **No network / no subprocess in `oem_parser`.** No network in `stitch_runner` except pip (which needs it). No subprocess anywhere except `stitch_runner`.
- **DRY/YAGNI/TDD.** Write the failing test first, minimal impl, commit per task.

## File Structure

```
ifwi-stitching-mcp/
  pyproject.toml            # package metadata + deps + pytest config
  ifwi_mcp/
    __init__.py
    result.py               # ok()/err() helpers + ErrorCode constants
    config.py               # env vars, cache dir layout, startup validation, token accessors
    fiv_portal.py           # FIV Portal REST client (pure HTTP+JSON)
    downloader.py           # Artifactory download + local copy → cache
    oem_parser.py           # zero-dependency OEM region decoder
    stitch_runner.py        # extract archive + venv + run cli.py
    server.py               # FastMCP app: registers tools, thin wrappers
  tests/
    conftest.py
    test_result.py
    test_config.py
    test_fiv_portal.py
    test_downloader.py
    test_oem_parser.py
    test_stitch_runner.py
    test_server.py
    fixtures/
      oem_official.bin       # synthesized 256-byte OEM sample (built in test)
```

Responsibilities: `result` = shared shape; `config` = the only module reading `os.environ`; `fiv_portal` = HTTP only, no file I/O; `downloader` = files only, no FIV knowledge; `oem_parser` = bytes only; `stitch_runner` = zip/venv/subprocess only; `server` = orchestration wrappers that call the above and pass results straight through.

---

### Task 1: Project scaffold + unified result helpers

**Files:**
- Create: `pyproject.toml`
- Create: `ifwi_mcp/__init__.py`
- Create: `ifwi_mcp/result.py`
- Test: `tests/test_result.py`
- Create: `tests/__init__.py` (empty)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ErrorCode` — class with string constants: `INVALID_ARGUMENT`, `MISSING_TOKEN`, `AUTH_FAILED`, `PROJECT_NOT_FOUND`, `RELEASE_NOT_FOUND`, `MULTIPLE_SWIMLANES`, `INGREDIENT_NOT_FOUND`, `STITCH_TOOL_NOT_FOUND`, `OEM_REGION_NOT_FOUND`, `OEM_PARSE_FAILED`, `OEM_MATCH_AMBIGUOUS`, `OEM_MATCH_NONE`, `DOWNLOAD_FAILED`, `EXTRACT_FAILED`, `VENV_SETUP_FAILED`, `STITCH_RUN_FAILED`, `INTERNAL_ERROR`.
  - `ok(data: dict) -> dict` → `{"ok": True, "data": data}`
  - `err(error_code: str, message: str, detail: Optional[dict] = None) -> dict` → `{"ok": False, "error_code": ..., "message": ..., "detail": detail or {}}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_result.py
from ifwi_mcp.result import ok, err, ErrorCode


def test_ok_wraps_data():
    assert ok({"x": 1}) == {"ok": True, "data": {"x": 1}}


def test_err_defaults_detail_to_empty_dict():
    result = err(ErrorCode.INVALID_ARGUMENT, "bad param")
    assert result == {
        "ok": False,
        "error_code": "INVALID_ARGUMENT",
        "message": "bad param",
        "detail": {},
    }


def test_err_keeps_detail():
    result = err(ErrorCode.MISSING_TOKEN, "need token", {"which": "fiv"})
    assert result["detail"] == {"which": "fiv"}
    assert result["error_code"] == "MISSING_TOKEN"


def test_error_code_constants_are_stable_strings():
    assert ErrorCode.MULTIPLE_SWIMLANES == "MULTIPLE_SWIMLANES"
    assert ErrorCode.STITCH_RUN_FAILED == "STITCH_RUN_FAILED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_result.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ifwi_mcp'`

- [ ] **Step 3: Create the package scaffold**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "ifwi-stitching-mcp"
version = "0.1.0"
description = "MCP server for IFWI stitching via FIV Portal"
requires-python = ">=3.9"
dependencies = ["fastmcp", "requests"]

[project.optional-dependencies]
dev = ["pytest", "responses"]

[tool.setuptools.packages.find]
include = ["ifwi_mcp*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```python
# ifwi_mcp/__init__.py
"""IFWI Stitching MCP server package."""
```

```python
# tests/__init__.py
```

- [ ] **Step 4: Write minimal implementation**

```python
# ifwi_mcp/result.py
"""Unified result shape shared by every module and MCP tool."""
from typing import Optional


class ErrorCode:
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    MISSING_TOKEN = "MISSING_TOKEN"
    AUTH_FAILED = "AUTH_FAILED"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    MULTIPLE_SWIMLANES = "MULTIPLE_SWIMLANES"
    INGREDIENT_NOT_FOUND = "INGREDIENT_NOT_FOUND"
    STITCH_TOOL_NOT_FOUND = "STITCH_TOOL_NOT_FOUND"
    OEM_REGION_NOT_FOUND = "OEM_REGION_NOT_FOUND"
    OEM_PARSE_FAILED = "OEM_PARSE_FAILED"
    OEM_MATCH_AMBIGUOUS = "OEM_MATCH_AMBIGUOUS"
    OEM_MATCH_NONE = "OEM_MATCH_NONE"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    EXTRACT_FAILED = "EXTRACT_FAILED"
    VENV_SETUP_FAILED = "VENV_SETUP_FAILED"
    STITCH_RUN_FAILED = "STITCH_RUN_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def err(error_code: str, message: str, detail: Optional[dict] = None) -> dict:
    return {"ok": False, "error_code": error_code, "message": message, "detail": detail or {}}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_result.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml ifwi_mcp/__init__.py ifwi_mcp/result.py tests/__init__.py tests/test_result.py
git commit -m "feat: project scaffold + unified result helpers"
```

---

### Task 2: config module — env vars, cache layout, tokens

**Files:**
- Create: `ifwi_mcp/config.py`
- Test: `tests/test_config.py`
- Create: `tests/conftest.py`

**Interfaces:**
- Consumes: `result.ok`, `result.err`, `result.ErrorCode`.
- Produces:
  - `class ConfigError(Exception)` — raised only by `validate_startup()`.
  - `get_base_url() -> str` — value of `FIV_BASE_URL` (no trailing slash), or `""` if unset.
  - `get_cache_dir() -> pathlib.Path` — `IFWI_MCP_CACHE_DIR` or default `~/.ifwi-stitching-mcp/cache`, expanduser-ed.
  - `cache_subdir(name: str) -> pathlib.Path` — ensures and returns `get_cache_dir()/name` (e.g. `"ifwi"`, `"ingredients"`, `"stitch"`, `"work"`).
  - `get_fiv_auth_header() -> dict` — returns `{}` result-style: on success `ok({"header_value": <FIV_TOKEN>})`; if unset → `err(MISSING_TOKEN, ..., {"which": "fiv"})`.
  - `get_artifactory_token() -> dict` — `ok({"token": <ARTIFACTORY_TOKEN>})` or `err(MISSING_TOKEN, ..., {"which": "artifactory"})`.
  - `validate_startup() -> dict` — `ok({})` if `FIV_BASE_URL` is a non-empty valid http(s) URL and cache dir is creatable/writable; else `err(INVALID_ARGUMENT, ...)`. Does NOT check tokens (deferred to use time).

- [ ] **Step 1: Write conftest fixture for clean env**

```python
# tests/conftest.py
import pytest


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate every config env var and point the cache at a tmp dir."""
    for var in ("FIV_BASE_URL", "FIV_TOKEN", "ARTIFACTORY_TOKEN", "IFWI_MCP_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("IFWI_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path
from ifwi_mcp import config
from ifwi_mcp.result import ErrorCode


def test_default_cache_dir(monkeypatch):
    monkeypatch.delenv("IFWI_MCP_CACHE_DIR", raising=False)
    assert config.get_cache_dir() == Path.home() / ".ifwi-stitching-mcp" / "cache"


def test_cache_dir_from_env(clean_env):
    assert config.get_cache_dir() == clean_env / "cache"


def test_cache_subdir_is_created(clean_env):
    sub = config.cache_subdir("ifwi")
    assert sub.is_dir()
    assert sub == clean_env / "cache" / "ifwi"


def test_base_url_strips_trailing_slash(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "https://fiv.example.com/")
    assert config.get_base_url() == "https://fiv.example.com"


def test_fiv_auth_missing_returns_missing_token(clean_env):
    result = config.get_fiv_auth_header()
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.MISSING_TOKEN
    assert result["detail"] == {"which": "fiv"}


def test_fiv_auth_present(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_TOKEN", "Basic abc123")
    result = config.get_fiv_auth_header()
    assert result == {"ok": True, "data": {"header_value": "Basic abc123"}}


def test_artifactory_token_missing(clean_env):
    result = config.get_artifactory_token()
    assert result["error_code"] == ErrorCode.MISSING_TOKEN
    assert result["detail"] == {"which": "artifactory"}


def test_validate_startup_rejects_missing_base_url(clean_env):
    result = config.validate_startup()
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert result["detail"]["param"] == "FIV_BASE_URL"


def test_validate_startup_rejects_non_http_url(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "ftp://nope")
    assert config.validate_startup()["ok"] is False


def test_validate_startup_ok(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", "https://fiv.example.com")
    assert config.validate_startup() == {"ok": True, "data": {}}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.config' has no attribute ...`

- [ ] **Step 4: Write minimal implementation**

```python
# ifwi_mcp/config.py
"""Environment configuration, cache layout, and deferred token access.

This is the only module that reads os.environ.
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from .result import ok, err, ErrorCode

_DEFAULT_CACHE = Path.home() / ".ifwi-stitching-mcp" / "cache"


class ConfigError(Exception):
    pass


def get_base_url() -> str:
    return os.environ.get("FIV_BASE_URL", "").rstrip("/")


def get_cache_dir() -> Path:
    raw = os.environ.get("IFWI_MCP_CACHE_DIR")
    return Path(raw).expanduser() if raw else _DEFAULT_CACHE


def cache_subdir(name: str) -> Path:
    sub = get_cache_dir() / name
    sub.mkdir(parents=True, exist_ok=True)
    return sub


def get_fiv_auth_header() -> dict:
    value = os.environ.get("FIV_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "FIV_TOKEN is not set", {"which": "fiv"})
    return ok({"header_value": value})


def get_artifactory_token() -> dict:
    value = os.environ.get("ARTIFACTORY_TOKEN")
    if not value:
        return err(ErrorCode.MISSING_TOKEN, "ARTIFACTORY_TOKEN is not set", {"which": "artifactory"})
    return ok({"token": value})


def validate_startup() -> dict:
    base = get_base_url()
    if not base:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL must be set",
                   {"param": "FIV_BASE_URL", "expected": "non-empty http(s) URL"})
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL must be a valid http(s) URL",
                   {"param": "FIV_BASE_URL", "expected": "http(s) URL"})
    try:
        get_cache_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return err(ErrorCode.INVALID_ARGUMENT, "cache dir not creatable",
                   {"param": "IFWI_MCP_CACHE_DIR", "expected": f"writable path ({exc})"})
    return ok({})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (10 passed)

- [ ] **Step 6: Commit**

```bash
git add ifwi_mcp/config.py tests/test_config.py tests/conftest.py
git commit -m "feat: config module with env vars, cache layout, deferred tokens"
```

---

### Task 3: oem_parser — zero-dependency OEM region decoder

**Files:**
- Create: `ifwi_mcp/oem_parser.py`
- Test: `tests/test_oem_parser.py`

**Interfaces:**
- Consumes: `result.ok`, `result.err`, `result.ErrorCode`.
- Produces:
  - `OEM_INFO_START = 0xF00`, `OEM_INFO_LEN = 256`.
  - `decode_entry(oem_bytes: bytes) -> dict` — pure: takes exactly 256 bytes, returns `{"product", "ifwi_version", "flavor_value", "flavor_type", "hash"}`. Raises `ValueError` on wrong length. `flavor_type` ∈ {`official`,`customized`,`invalid`} per the classification rules below.
  - `parse_ifwi_oem(local_ifwi_path: str) -> dict` — result-style. Reads the file, extracts the OEM region, calls `decode_entry`, returns `ok({...five fields...})`. Error mapping: file missing/unreadable → `INVALID_ARGUMENT`; file smaller than `OEM_INFO_START + OEM_INFO_LEN` → `OEM_REGION_NOT_FOUND`; decode raises / product empty → `OEM_PARSE_FAILED` with `detail={"raw_snippet": <hex of first 64 OEM bytes>}`.

**Provenance note (put at top of module):** copied/adapted from `OakStreamAPIfwi/.../Output/defs/structure.py` (`IFWIEntry.from_binary`, `<16s 48s 128s 32s 32s`) and `diagnostics/binary_hash.py` (`parse_oem_info`, offsets `OEM_INFO_START_ADDR=0xF00`). Zero third-party deps.

Classification rules (from `__parsing_ifwi_entry`): split `ifwi_version` on `.`; if part-count ∉ {4,5} → `flavor_type="invalid"`. Else split `flavor_value` on `.`; if ≥3 parts and last part matches `^[0-9]{6}$` (time) and second-last matches `^[0-9]{8}$` (date) → `customized`, else `official`. (We do NOT verify the binary hash — that needs the whole 60MB image and is out of scope; `flavor_type` from hash-mismatch is not produced here.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oem_parser.py
import struct
from pathlib import Path
import pytest
from ifwi_mcp import oem_parser
from ifwi_mcp.result import ErrorCode

STRUCT_FMT = "<16s 48s 128s 32s 32s"


def make_oem_bytes(product="OKSDCRB1", version=".2026.28.3.01",
                   flavor="_1P0_NonIPClean_Trace_DebugSigned",
                   hash_hex="90" * 32):
    return struct.pack(
        STRUCT_FMT,
        product.encode().ljust(16, b"\x00"),
        version.encode().ljust(48, b"\x00"),
        flavor.encode().ljust(128, b"\x00"),
        bytes.fromhex(hash_hex).ljust(32, b"\x00"),
        b"\x00" * 32,
    )


def make_ifwi_file(path: Path, oem_bytes: bytes):
    data = bytearray(b"\xff" * (oem_parser.OEM_INFO_START + oem_parser.OEM_INFO_LEN))
    data[oem_parser.OEM_INFO_START:oem_parser.OEM_INFO_START + 256] = oem_bytes
    path.write_bytes(data)
    return path


def test_decode_entry_official():
    entry = oem_parser.decode_entry(make_oem_bytes())
    assert entry["product"] == "OKSDCRB1"
    assert entry["ifwi_version"] == ".2026.28.3.01"
    assert entry["flavor_value"] == "_1P0_NonIPClean_Trace_DebugSigned"
    assert entry["flavor_type"] == "official"
    assert entry["hash"] == "90" * 32


def test_decode_entry_customized():
    entry = oem_parser.decode_entry(make_oem_bytes(flavor="yyao7.20250507.130527"))
    assert entry["flavor_type"] == "customized"


def test_decode_entry_invalid_version():
    entry = oem_parser.decode_entry(make_oem_bytes(version="2026.28"))
    assert entry["flavor_type"] == "invalid"


def test_decode_entry_wrong_length_raises():
    with pytest.raises(ValueError):
        oem_parser.decode_entry(b"\x00" * 100)


def test_parse_ifwi_oem_success(tmp_path):
    f = make_ifwi_file(tmp_path / "ifwi.bin", make_oem_bytes())
    result = oem_parser.parse_ifwi_oem(str(f))
    assert result["ok"] is True
    assert result["data"]["product"] == "OKSDCRB1"


def test_parse_ifwi_oem_missing_file(tmp_path):
    result = oem_parser.parse_ifwi_oem(str(tmp_path / "nope.bin"))
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_parse_ifwi_oem_too_small(tmp_path):
    small = tmp_path / "small.bin"
    small.write_bytes(b"\x00" * 100)
    result = oem_parser.parse_ifwi_oem(str(small))
    assert result["error_code"] == ErrorCode.OEM_REGION_NOT_FOUND


def test_parse_ifwi_oem_empty_product_is_parse_failed(tmp_path):
    f = make_ifwi_file(tmp_path / "ifwi.bin", make_oem_bytes(product=""))
    result = oem_parser.parse_ifwi_oem(str(f))
    assert result["error_code"] == ErrorCode.OEM_PARSE_FAILED
    assert "raw_snippet" in result["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_oem_parser.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.oem_parser' has no attribute 'OEM_INFO_START'`

- [ ] **Step 3: Write minimal implementation**

```python
# ifwi_mcp/oem_parser.py
"""Zero-dependency decoder for the IFWI OEM information region.

Copied/adapted from the stitch tool (do NOT import from it):
  - OakStreamAPIfwi/.../Output/defs/structure.py : IFWIEntry.from_binary,
    struct "<16s 48s 128s 32s 32s" (product/ifwi_version/flavor/hash/padding).
  - OakStreamAPIfwi/.../Output/diagnostics/binary_hash.py : parse_oem_info,
    OEM_INFO_START_ADDR = 0xF00, region length 256.
When the upstream OEM structure changes, re-sync this module by hand.
Stdlib only — no network, no subprocess.
"""
import re
import struct

from .result import ok, err, ErrorCode

OEM_INFO_START = 0xF00
OEM_INFO_LEN = 256
_STRUCT_FMT = "<16s 48s 128s 32s 32s"
_DATE_RE = re.compile(r"^[0-9]{8}$")
_TIME_RE = re.compile(r"^[0-9]{6}$")


def _decode_str(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("utf-8", errors="ignore")


def _classify_flavor(ifwi_version: str, flavor_value: str) -> str:
    version_parts = ifwi_version.split(".")
    if len(version_parts) not in (4, 5):
        return "invalid"
    flavor_parts = flavor_value.split(".")
    if len(flavor_parts) >= 3 and _TIME_RE.match(flavor_parts[-1]) and _DATE_RE.match(flavor_parts[-2]):
        return "customized"
    return "official"


def decode_entry(oem_bytes: bytes) -> dict:
    if len(oem_bytes) != OEM_INFO_LEN:
        raise ValueError(f"OEM entry must be {OEM_INFO_LEN} bytes, got {len(oem_bytes)}")
    product_b, version_b, flavor_b, hash_b, _padding = struct.unpack(_STRUCT_FMT, oem_bytes)
    product = _decode_str(product_b)
    ifwi_version = _decode_str(version_b)
    flavor_value = _decode_str(flavor_b)
    return {
        "product": product,
        "ifwi_version": ifwi_version,
        "flavor_value": flavor_value,
        "flavor_type": _classify_flavor(ifwi_version, flavor_value),
        "hash": hash_b.hex(),
    }


def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    try:
        with open(local_ifwi_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size < OEM_INFO_START + OEM_INFO_LEN:
                return err(ErrorCode.OEM_REGION_NOT_FOUND,
                           "file too small to contain the OEM region",
                           {"file": local_ifwi_path, "reason": f"size {size} < {OEM_INFO_START + OEM_INFO_LEN}"})
            fh.seek(OEM_INFO_START)
            oem_bytes = fh.read(OEM_INFO_LEN)
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        return err(ErrorCode.INVALID_ARGUMENT, "cannot read IFWI file",
                   {"param": "local_ifwi_path", "expected": f"readable file ({exc})"})
    try:
        entry = decode_entry(oem_bytes)
    except Exception as exc:  # noqa: BLE001 - map any decode failure
        return err(ErrorCode.OEM_PARSE_FAILED, "failed to decode OEM entry",
                   {"raw_snippet": oem_bytes[:64].hex(), "reason": str(exc)})
    if not entry["product"]:
        return err(ErrorCode.OEM_PARSE_FAILED, "OEM product field is empty",
                   {"raw_snippet": oem_bytes[:64].hex()})
    return ok(entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_oem_parser.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/oem_parser.py tests/test_oem_parser.py
git commit -m "feat: zero-dependency OEM region parser"
```

---

### Task 4: downloader — Artifactory download + local copy

**Files:**
- Create: `ifwi_mcp/downloader.py`
- Test: `tests/test_downloader.py`

**Interfaces:**
- Consumes: `result`, `config.cache_subdir`, `config.get_artifactory_token`; `requests`.
- Produces:
  - `download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict` — if `url_or_path` is http(s): stream-download with `Authorization: Bearer <token>` into `config.cache_subdir(category)/<name>`; if it is an existing local path: copy into the same place. Returns `ok({"local_path": str, "source": "download"|"copy", "bytes": int})`.
  - `list_local_files() -> dict` — returns `ok({"files": [{"path", "category", "bytes"}...]})` scanning the `ifwi`/`ingredients`/`stitch` cache subdirs.
  - Validation: empty `url_or_path` → `INVALID_ARGUMENT`; a non-http string that does not exist → `INVALID_ARGUMENT`; `dest_name` containing `/`, `\`, or `..` → `INVALID_ARGUMENT` (`detail={"param":"dest_name","expected":"no path separators or .."}`).
  - Errors: URL that is http(s) but token missing → propagate `MISSING_TOKEN` from config; HTTP non-2xx → `AUTH_FAILED` for 401/403 else `DOWNLOAD_FAILED` (`detail={"url","http_status"}`); network exception → `DOWNLOAD_FAILED` (`detail={"url","reason"}`).
  - `dest_name` default: last path segment of the URL/path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_downloader.py
import responses
from ifwi_mcp import downloader
from ifwi_mcp.result import ErrorCode


def test_rejects_empty(clean_env):
    assert downloader.download("")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_rejects_traversal_dest_name(clean_env):
    r = downloader.download("https://a/b.bin", dest_name="../evil")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT
    assert r["detail"]["param"] == "dest_name"


def test_local_missing_path(clean_env):
    assert downloader.download("/no/such/file.bin")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_local_copy(clean_env, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello")
    r = downloader.download(str(src), category="ifwi")
    assert r["ok"] is True
    assert r["data"]["source"] == "copy"
    assert r["data"]["bytes"] == 5
    assert (clean_env / "cache" / "ifwi" / "src.bin").read_bytes() == b"hello"


def test_url_missing_token(clean_env, monkeypatch):
    monkeypatch.delenv("ARTIFACTORY_TOKEN", raising=False)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.MISSING_TOKEN


@responses.activate
def test_url_download_success(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", body=b"binary", status=200)
    r = downloader.download("https://artifactory/x.bin", category="ingredients")
    assert r["ok"] is True
    assert r["data"]["source"] == "download"
    assert (clean_env / "cache" / "ingredients" / "x.bin").read_bytes() == b"binary"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_url_403_is_auth_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=403)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.AUTH_FAILED
    assert r["detail"]["http_status"] == 403


@responses.activate
def test_url_500_is_download_failed(clean_env, monkeypatch):
    monkeypatch.setenv("ARTIFACTORY_TOKEN", "tok")
    responses.add(responses.GET, "https://artifactory/x.bin", status=500)
    r = downloader.download("https://artifactory/x.bin")
    assert r["error_code"] == ErrorCode.DOWNLOAD_FAILED


def test_list_local_files(clean_env, tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"xy")
    downloader.download(str(src), category="ifwi")
    listing = downloader.list_local_files()
    assert listing["ok"] is True
    names = [f["path"].split("/")[-1] for f in listing["data"]["files"]]
    assert "a.bin" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.downloader' has no attribute 'download'`

- [ ] **Step 3: Write minimal implementation**

```python
# ifwi_mcp/downloader.py
"""Turn Artifactory URLs or local paths into files in the local cache."""
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

from . import config
from .result import ok, err, ErrorCode

_CATEGORIES = ("ifwi", "ingredients", "stitch")


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in ("http", "https")


def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    if not url_or_path:
        return err(ErrorCode.INVALID_ARGUMENT, "url_or_path is required",
                   {"param": "url_or_path", "expected": "non-empty URL or path"})
    if dest_name and (("/" in dest_name) or ("\\" in dest_name) or (".." in dest_name)):
        return err(ErrorCode.INVALID_ARGUMENT, "dest_name must be a bare filename",
                   {"param": "dest_name", "expected": "no path separators or .."})

    name = dest_name or Path(urlparse(url_or_path).path if _is_url(url_or_path) else url_or_path).name
    target = config.cache_subdir(category) / name

    if _is_url(url_or_path):
        token_result = config.get_artifactory_token()
        if not token_result["ok"]:
            return token_result
        headers = {"Authorization": f"Bearer {token_result['data']['token']}"}
        try:
            resp = requests.get(url_or_path, headers=headers, stream=True, timeout=120)
        except requests.RequestException as exc:
            return err(ErrorCode.DOWNLOAD_FAILED, "network error during download",
                       {"url": url_or_path, "reason": str(exc)})
        if resp.status_code in (401, 403):
            return err(ErrorCode.AUTH_FAILED, "artifactory rejected credentials",
                       {"service": "artifactory", "http_status": resp.status_code})
        if not (200 <= resp.status_code < 300):
            return err(ErrorCode.DOWNLOAD_FAILED, "download returned error status",
                       {"url": url_or_path, "http_status": resp.status_code})
        written = 0
        with open(target, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        return ok({"local_path": str(target), "source": "download", "bytes": written})

    src = Path(url_or_path)
    if not src.is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "local path does not exist",
                   {"param": "url_or_path", "expected": "existing file or http(s) URL"})
    shutil.copyfile(src, target)
    return ok({"local_path": str(target), "source": "copy", "bytes": target.stat().st_size})


def list_local_files() -> dict:
    files = []
    for category in _CATEGORIES:
        base = config.cache_subdir(category)
        for path in sorted(base.glob("*")):
            if path.is_file():
                files.append({"path": str(path), "category": category, "bytes": path.stat().st_size})
    return ok({"files": files})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_downloader.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/downloader.py tests/test_downloader.py
git commit -m "feat: downloader for artifactory URLs and local copies"
```

---

### Task 5: fiv_portal core — HTTP, projects, release resolution, swimlanes, find_ifwi

**Files:**
- Create: `ifwi_mcp/fiv_portal.py`
- Test: `tests/test_fiv_portal.py`

**Interfaces:**
- Consumes: `result`, `config.get_base_url`, `config.get_fiv_auth_header`; `requests`.
- Produces (used by Task 6 and server):
  - `PHASES = ("Blue", "Orange", "Purple", "Daily")`.
  - `_get(endpoint: str, params: dict) -> dict` — internal. Builds `<base>/app/rest/<endpoint>`, sends GET with `Authorization: <fiv header value>`, returns `ok({"json": <parsed>})` or an error result. 401/403 → `AUTH_FAILED`; missing base url → `INVALID_ARGUMENT`; missing token → propagate `MISSING_TOKEN`; network error → `err(INTERNAL_ERROR)` with reason; non-2xx → `err(RELEASE_NOT_FOUND)` is WRONG — use a generic `err(INTERNAL_ERROR, "fiv http error", {"http_status"})` so callers decide.
  - `_validate_common(project, phase, version) -> Optional[dict]` — returns an `INVALID_ARGUMENT` error dict if any check fails, else `None`. Checks: `project` non-empty; `phase` in `PHASES`; `version` matches `^\.?\d{4}\.\d+\.\d+\.\d+$` (tolerant leading dot).
  - `list_projects() -> dict` — GET `get_project_info/`; returns `ok({"projects": [{"id","name","project_name"}...]})`.
  - `resolve_project_id(project: str) -> dict` — calls `list_projects`, matches `project` case-insensitively against `name` or `project_name`; `ok({"project_id": int, "name": str})`, or `PROJECT_NOT_FOUND` with `detail={"candidates":[names...]}`.
  - `list_swimlanes(project, phase, version) -> dict` — resolve project, GET `get_ifwi_release/` WITHOUT swimlane, collect distinct non-empty `swimlane_branch`; `ok({"swimlanes": [...]})`. Empty release list → `RELEASE_NOT_FOUND`.
  - `find_ifwi(project, phase, version, swimlane=None) -> dict` — resolve project; call `get_ifwi_release_package_info/` with params (include `swimlane` only if given). If `swimlane` omitted, first call `list_swimlanes`; if >1 distinct swimlane → `MULTIPLE_SWIMLANES` `detail={"candidates":[...]}`. On success build IFWI url = `release_root + package_path + full_binary_name` for the first `build_target` whose `binary_list` is non-empty; return `ok({"project_id","swimlane_branch","release_root","ifwi_url","full_binary_name","build_target":<package_name>})`. No matching binary → `RELEASE_NOT_FOUND`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fiv_portal.py
import responses
import pytest
from ifwi_mcp import fiv_portal
from ifwi_mcp.result import ErrorCode

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "Basic xyz")
    return clean_env


def _url(ep):
    return f"{BASE}/app/rest/{ep}"


PROJECTS = [
    {"id": 232, "name": "OakStreamAP", "project_name": "SiEn-OakStream-DiamonRapids-AP"},
    {"id": 99, "name": "OtherProj", "project_name": "Other"},
]


def test_validate_rejects_bad_phase(fiv_env):
    r = fiv_portal.find_ifwi("OakStreamAP", "Green", "2026.28.3.01")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_validate_rejects_bad_version(fiv_env):
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "not-a-version")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_validate_accepts_leading_dot_version(fiv_env):
    # leading-dot version passes validation (will fail later on network, that's fine)
    assert fiv_portal._validate_common("p", "Orange", ".2026.28.3.01") is None


@responses.activate
def test_list_projects(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.list_projects()
    assert r["ok"] is True
    assert r["data"]["projects"][0]["id"] == 232
    assert responses.calls[0].request.headers["Authorization"] == "Basic xyz"


@responses.activate
def test_resolve_project_id_case_insensitive(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.resolve_project_id("oakstreamap")
    assert r["data"]["project_id"] == 232


@responses.activate
def test_resolve_project_not_found(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    r = fiv_portal.resolve_project_id("nope")
    assert r["error_code"] == ErrorCode.PROJECT_NOT_FOUND
    assert "OakStreamAP" in r["detail"]["candidates"]


@responses.activate
def test_projects_401_is_auth_failed(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), status=401)
    r = fiv_portal.list_projects()
    assert r["error_code"] == ErrorCode.AUTH_FAILED


@responses.activate
def test_list_swimlanes_distinct(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
        {"version": "2026.28.3.01", "swimlane_branch": "release-ww28"},
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
    ], status=200)
    r = fiv_portal.list_swimlanes("OakStreamAP", "Orange", "2026.28.3.01")
    assert r["ok"] is True
    assert sorted(r["data"]["swimlanes"]) == ["main", "release-ww28"]


@responses.activate
def test_find_ifwi_multiple_swimlanes(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"), json=[
        {"version": "2026.28.3.01", "swimlane_branch": "main"},
        {"version": "2026.28.3.01", "swimlane_branch": "release-ww28"},
    ], status=200)
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "2026.28.3.01")
    assert r["error_code"] == ErrorCode.MULTIPLE_SWIMLANES
    assert sorted(r["detail"]["candidates"]) == ["main", "release-ww28"]


@responses.activate
def test_find_ifwi_success_with_swimlane(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [
            {"package_name": "empty", "package_path": "p0/", "binary_list": []},
            {"package_name": "ifwi_pkg", "package_path": "pkg/",
             "binary_list": [{"binary_name": "ifwi", "full_binary_name": "OakStreamAP.bin", "target_id": "t1"}]},
        ],
    }, status=200)
    r = fiv_portal.find_ifwi("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["ok"] is True
    assert r["data"]["ifwi_url"] == "https://art/root/pkg/OakStreamAP.bin"
    assert r["data"]["full_binary_name"] == "OakStreamAP.bin"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fiv_portal.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.fiv_portal' has no attribute 'find_ifwi'`

- [ ] **Step 3: Write minimal implementation**

```python
# ifwi_mcp/fiv_portal.py
"""FIV Portal REST client. Pure HTTP + JSON — no file I/O, no subprocess."""
import re
from typing import Optional

import requests

from . import config
from .result import ok, err, ErrorCode

PHASES = ("Blue", "Orange", "Purple", "Daily")
_VERSION_RE = re.compile(r"^\.?\d{4}\.\d+\.\d+\.\d+$")


def _get(endpoint: str, params: dict) -> dict:
    base = config.get_base_url()
    if not base:
        return err(ErrorCode.INVALID_ARGUMENT, "FIV_BASE_URL not set",
                   {"param": "FIV_BASE_URL", "expected": "http(s) URL"})
    auth = config.get_fiv_auth_header()
    if not auth["ok"]:
        return auth
    url = f"{base}/app/rest/{endpoint}"
    try:
        resp = requests.get(url, params=params,
                            headers={"Authorization": auth["data"]["header_value"]}, timeout=60)
    except requests.RequestException as exc:
        return err(ErrorCode.INTERNAL_ERROR, "fiv network error", {"url": url, "reason": str(exc)})
    if resp.status_code in (401, 403):
        return err(ErrorCode.AUTH_FAILED, "FIV Portal rejected credentials",
                   {"service": "fiv", "http_status": resp.status_code})
    if not (200 <= resp.status_code < 300):
        return err(ErrorCode.INTERNAL_ERROR, "fiv http error",
                   {"url": url, "http_status": resp.status_code})
    try:
        return ok({"json": resp.json()})
    except ValueError as exc:
        return err(ErrorCode.INTERNAL_ERROR, "fiv returned non-JSON", {"reason": str(exc)})


def _validate_common(project: str, phase: str, version: str) -> Optional[dict]:
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    if phase not in PHASES:
        return err(ErrorCode.INVALID_ARGUMENT, "invalid phase",
                   {"param": "phase", "expected": f"one of {PHASES}"})
    if not _VERSION_RE.match(version or ""):
        return err(ErrorCode.INVALID_ARGUMENT, "invalid version",
                   {"param": "version", "expected": "YYYY.WW.D.NN (leading dot tolerated)"})
    return None


def list_projects() -> dict:
    result = _get("get_project_info/", {})
    if not result["ok"]:
        return result
    projects = [{"id": p.get("id"), "name": p.get("name"), "project_name": p.get("project_name")}
                for p in result["data"]["json"]]
    return ok({"projects": projects})


def resolve_project_id(project: str) -> dict:
    if not project:
        return err(ErrorCode.INVALID_ARGUMENT, "project is required",
                   {"param": "project", "expected": "non-empty"})
    listing = list_projects()
    if not listing["ok"]:
        return listing
    projects = listing["data"]["projects"]
    needle = project.strip().lower()
    for p in projects:
        if needle in (str(p.get("name", "")).lower(), str(p.get("project_name", "")).lower()):
            return ok({"project_id": p["id"], "name": p["name"]})
    return err(ErrorCode.PROJECT_NOT_FOUND, "no project matched",
               {"candidates": [p["name"] for p in projects]})


def _release_rows(project_id: int, phase: str, version: str, swimlane: Optional[str]) -> dict:
    params = {"project_id": project_id, "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    return _get("get_ifwi_release/", params)


def list_swimlanes(project: str, phase: str, version: str) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    rows_result = _release_rows(pid["data"]["project_id"], phase, version, None)
    if not rows_result["ok"]:
        return rows_result
    rows = rows_result["data"]["json"]
    if not rows:
        return err(ErrorCode.RELEASE_NOT_FOUND, "no release rows",
                   {"project": project, "phase": phase, "version": version})
    swimlanes = sorted({r.get("swimlane_branch") for r in rows if r.get("swimlane_branch")})
    return ok({"swimlanes": swimlanes})


def find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    if not swimlane:
        lanes = list_swimlanes(project, phase, version)
        if not lanes["ok"]:
            return lanes
        candidates = lanes["data"]["swimlanes"]
        if len(candidates) > 1:
            return err(ErrorCode.MULTIPLE_SWIMLANES, "multiple swimlanes; pick one",
                       {"candidates": candidates})
        swimlane = candidates[0] if candidates else None

    params = {"project_id": project_id, "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    pkg = _get("get_ifwi_release_package_info/", params)
    if not pkg["ok"]:
        return pkg
    data = pkg["data"]["json"]
    release_root = data.get("release_root") or ""
    for target in data.get("build_target", []):
        binaries = target.get("binary_list") or []
        if binaries:
            full = binaries[0]["full_binary_name"]
            return ok({
                "project_id": project_id,
                "swimlane_branch": data.get("swimlane_branch"),
                "release_root": release_root,
                "ifwi_url": release_root + (target.get("package_path") or "") + full,
                "full_binary_name": full,
                "build_target": target.get("package_name"),
            })
    return err(ErrorCode.RELEASE_NOT_FOUND, "no IFWI binary in build targets",
               {"project": project, "phase": phase, "version": version})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fiv_portal.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/fiv_portal.py tests/test_fiv_portal.py
git commit -m "feat: fiv_portal core (projects, releases, swimlanes, find_ifwi)"
```

---

### Task 6: fiv_portal finders — ingredient, stitch tool, OEM reverse-match

**Files:**
- Modify: `ifwi_mcp/fiv_portal.py` (add three functions; reuse Task 5 helpers)
- Modify: `tests/test_fiv_portal.py` (append tests)

**Interfaces:**
- Consumes: everything from Task 5 (`_get`, `_validate_common`, `resolve_project_id`, `list_swimlanes`, `find_ifwi`).
- Produces:
  - `find_ingredient(project, name, version) -> dict` — validate `project`/`name`/`version` non-empty; resolve project; GET `get_ingredient_detail/` with `{project_id, ingredient_name, ingredient_version}`; return `ok({"ingredient_url": <ingredient_link>, "ingredient_name": name, "ingredient_version": version})`. Missing/empty `ingredient_link` → `INGREDIENT_NOT_FOUND` `detail={"candidates":[]}`.
  - `find_stitch_tool(project, phase, version, swimlane=None) -> dict` — resolve project + swimlane exactly as `find_ifwi` (reuse the same multi-swimlane guard by calling `list_swimlanes` when swimlane omitted); GET `get_ifwi_release_package_info/`; scan `build_target[]` for the first whose `package_name` contains `"stitch"` (case-insensitive) and has a non-empty `binary_list`; build url `release_root + package_path + full_binary_name`. Return `ok({"stitch_url","package_name","full_binary_name"})`. None found → `STITCH_TOOL_NOT_FOUND` `detail={"available_packages":[names...]}`.
  - `match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane=None) -> dict` — Path B. Validate all non-empty and phase valid. Resolve project. Enumerate swimlanes (if omitted); for each candidate swimlane call `get_ifwi_release_package_info/` and look for a `build_target` whose `package_name` contains `product` (case-insensitive) AND (loosely) whose name or a binary name contains the `flavor` token. Collect matches across swimlanes.
    - 0 matches → `OEM_MATCH_NONE` `detail={"parsed":{"product","ifwi_version","flavor"}}`.
    - matches spanning >1 swimlane and `swimlane` not pinned → `MULTIPLE_SWIMLANES` `detail={"candidates":[swimlanes...]}`.
    - >1 match within a single swimlane → `OEM_MATCH_AMBIGUOUS` `detail={"candidates":[{"package_name","swimlane"}...]}`.
    - exactly 1 → `ok({"project_id","phase","swimlane_branch","matched_package": name})` (caller then calls `find_ifwi`/`find_stitch_tool` with the pinned swimlane).

    Note: `ifwi_version` is validated for non-emptiness only (OEM versions like `.2026.28.3.01` are informational for matching; the FIV `version` query uses the user-supplied `version`/`find_ifwi` path). Here `ifwi_version` and `flavor` are matched against build_target/binary names, not sent as query params — the portal has no such fields.

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/test_fiv_portal.py

@responses.activate
def test_find_ingredient_success(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ingredient_detail/"),
                  json={"ingredient_link": "https://art/ing/B-1.0.bin",
                        "ingredient_name": "B", "ingredient_version": "1.0"}, status=200)
    r = fiv_portal.find_ingredient("OakStreamAP", "B", "1.0")
    assert r["ok"] is True
    assert r["data"]["ingredient_url"] == "https://art/ing/B-1.0.bin"


@responses.activate
def test_find_ingredient_not_found(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ingredient_detail/"),
                  json={"ingredient_link": None}, status=200)
    r = fiv_portal.find_ingredient("OakStreamAP", "B", "9.9")
    assert r["error_code"] == ErrorCode.INGREDIENT_NOT_FOUND


@responses.activate
def test_find_stitch_tool_success(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [
            {"package_name": "IFWI_Main", "package_path": "a/",
             "binary_list": [{"full_binary_name": "ifwi.bin"}]},
            {"package_name": "IFWI_Stitch_Tool", "package_path": "s/",
             "binary_list": [{"full_binary_name": "stitch_tool.zip"}]},
        ],
    }, status=200)
    r = fiv_portal.find_stitch_tool("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["ok"] is True
    assert r["data"]["stitch_url"] == "https://art/root/s/stitch_tool.zip"
    assert r["data"]["package_name"] == "IFWI_Stitch_Tool"


@responses.activate
def test_find_stitch_tool_none(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/",
        "swimlane_branch": "main",
        "build_target": [{"package_name": "IFWI_Main", "package_path": "a/",
                          "binary_list": [{"full_binary_name": "ifwi.bin"}]}],
    }, status=200)
    r = fiv_portal.find_stitch_tool("OakStreamAP", "Orange", "2026.28.3.01", swimlane="main")
    assert r["error_code"] == ErrorCode.STITCH_TOOL_NOT_FOUND
    assert r["detail"]["available_packages"] == ["IFWI_Main"]


@responses.activate
def test_match_build_by_oem_single(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    # list_swimlanes call (no swimlane) -> one lane
    responses.add(responses.GET, _url("get_ifwi_release/"),
                  json=[{"version": "2026.28.3.01", "swimlane_branch": "main"}], status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/", "swimlane_branch": "main",
        "build_target": [
            {"package_name": "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned", "package_path": "p/",
             "binary_list": [{"full_binary_name": "ifwi.bin"}]},
        ],
    }, status=200)
    r = fiv_portal.match_build_by_oem(
        "OakStreamAP", "OKSDCRB1", ".2026.28.3.01",
        "_1P0_NonIPClean_Trace_DebugSigned", "Orange")
    assert r["ok"] is True
    assert r["data"]["matched_package"] == "OKSDCRB1_1P0_NonIPClean_Trace_DebugSigned"
    assert r["data"]["swimlane_branch"] == "main"


@responses.activate
def test_match_build_by_oem_none(fiv_env):
    responses.add(responses.GET, _url("get_project_info/"), json=PROJECTS, status=200)
    responses.add(responses.GET, _url("get_ifwi_release/"),
                  json=[{"version": "2026.28.3.01", "swimlane_branch": "main"}], status=200)
    responses.add(responses.GET, _url("get_ifwi_release_package_info/"), json={
        "release_root": "https://art/root/", "swimlane_branch": "main",
        "build_target": [{"package_name": "SOMETHING_ELSE", "package_path": "p/",
                          "binary_list": [{"full_binary_name": "x.bin"}]}],
    }, status=200)
    r = fiv_portal.match_build_by_oem(
        "OakStreamAP", "OKSDCRB1", ".2026.28.3.01", "_1P0_NonIPClean", "Orange")
    assert r["error_code"] == ErrorCode.OEM_MATCH_NONE
    assert r["detail"]["parsed"]["product"] == "OKSDCRB1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fiv_portal.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.fiv_portal' has no attribute 'find_ingredient'`

- [ ] **Step 3: Write minimal implementation (append to fiv_portal.py)**

```python
# append to ifwi_mcp/fiv_portal.py

def _resolve_swimlane_or_multi(project, phase, version, swimlane):
    """Return ok({"swimlane": <str|None>}) or a MULTIPLE_SWIMLANES/error result."""
    if swimlane:
        return ok({"swimlane": swimlane})
    lanes = list_swimlanes(project, phase, version)
    if not lanes["ok"]:
        return lanes
    candidates = lanes["data"]["swimlanes"]
    if len(candidates) > 1:
        return err(ErrorCode.MULTIPLE_SWIMLANES, "multiple swimlanes; pick one",
                   {"candidates": candidates})
    return ok({"swimlane": candidates[0] if candidates else None})


def find_ingredient(project: str, name: str, version: str) -> dict:
    if not project or not name or not version:
        return err(ErrorCode.INVALID_ARGUMENT, "project, name, version are required",
                   {"param": "project|name|version", "expected": "non-empty"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    result = _get("get_ingredient_detail/", {
        "project_id": pid["data"]["project_id"],
        "ingredient_name": name,
        "ingredient_version": version,
    })
    if not result["ok"]:
        return result
    link = result["data"]["json"].get("ingredient_link")
    if not link:
        return err(ErrorCode.INGREDIENT_NOT_FOUND, "no ingredient link",
                   {"candidates": [], "ingredient_name": name, "ingredient_version": version})
    return ok({"ingredient_url": link, "ingredient_name": name, "ingredient_version": version})


def find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    bad = _validate_common(project, phase, version)
    if bad:
        return bad
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    lane = _resolve_swimlane_or_multi(project, phase, version, swimlane)
    if not lane["ok"]:
        return lane
    swimlane = lane["data"]["swimlane"]

    params = {"project_id": pid["data"]["project_id"], "phase": phase, "version": version}
    if swimlane:
        params["swimlane"] = swimlane
    pkg = _get("get_ifwi_release_package_info/", params)
    if not pkg["ok"]:
        return pkg
    data = pkg["data"]["json"]
    release_root = data.get("release_root") or ""
    available = []
    for target in data.get("build_target", []):
        name = target.get("package_name") or ""
        available.append(name)
        binaries = target.get("binary_list") or []
        if "stitch" in name.lower() and binaries:
            full = binaries[0]["full_binary_name"]
            return ok({"stitch_url": release_root + (target.get("package_path") or "") + full,
                       "package_name": name, "full_binary_name": full})
    return err(ErrorCode.STITCH_TOOL_NOT_FOUND, "no stitch package found",
               {"available_packages": available})


def match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                       phase: str, swimlane: Optional[str] = None) -> dict:
    if not project or not product or not ifwi_version or not flavor:
        return err(ErrorCode.INVALID_ARGUMENT, "project, product, ifwi_version, flavor are required",
                   {"param": "project|product|ifwi_version|flavor", "expected": "non-empty"})
    if phase not in PHASES:
        return err(ErrorCode.INVALID_ARGUMENT, "invalid phase",
                   {"param": "phase", "expected": f"one of {PHASES}"})
    pid = resolve_project_id(project)
    if not pid["ok"]:
        return pid
    project_id = pid["data"]["project_id"]

    # Determine which swimlanes to scan.
    if swimlane:
        lanes_to_scan = [swimlane]
    else:
        # Need a version to enumerate swimlanes; derive from OEM ifwi_version by
        # stripping the leading dot (e.g. ".2026.28.3.01" -> "2026.28.3.01").
        version = ifwi_version.lstrip(".")
        if not _VERSION_RE.match(version):
            return err(ErrorCode.INVALID_ARGUMENT, "OEM ifwi_version not usable as version",
                       {"param": "ifwi_version", "expected": "YYYY.WW.D.NN"})
        lanes = list_swimlanes(project, phase, version)
        if not lanes["ok"]:
            return lanes
        lanes_to_scan = lanes["data"]["swimlanes"] or [None]

    product_l = product.lower()
    flavor_l = flavor.strip("_").lower()
    matches = []  # list of {"package_name","swimlane"}
    version = ifwi_version.lstrip(".")
    for lane in lanes_to_scan:
        params = {"project_id": project_id, "phase": phase, "version": version}
        if lane:
            params["swimlane"] = lane
        pkg = _get("get_ifwi_release_package_info/", params)
        if not pkg["ok"]:
            return pkg
        data = pkg["data"]["json"]
        lane_branch = data.get("swimlane_branch") or lane
        for target in data.get("build_target", []):
            name = (target.get("package_name") or "")
            haystack = name.lower()
            for b in (target.get("binary_list") or []):
                haystack += " " + (b.get("full_binary_name") or "").lower()
            if product_l in haystack and flavor_l in haystack:
                matches.append({"package_name": name, "swimlane": lane_branch})

    if not matches:
        return err(ErrorCode.OEM_MATCH_NONE, "no build matched the OEM info",
                   {"parsed": {"product": product, "ifwi_version": ifwi_version, "flavor": flavor}})
    distinct_lanes = sorted({m["swimlane"] for m in matches if m["swimlane"]})
    if not swimlane and len(distinct_lanes) > 1:
        return err(ErrorCode.MULTIPLE_SWIMLANES, "OEM matched builds in multiple swimlanes",
                   {"candidates": distinct_lanes})
    if len(matches) > 1:
        return err(ErrorCode.OEM_MATCH_AMBIGUOUS, "OEM matched more than one build",
                   {"candidates": matches})
    return ok({"project_id": project_id, "phase": phase,
               "swimlane_branch": matches[0]["swimlane"], "matched_package": matches[0]["package_name"]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fiv_portal.py -v`
Expected: PASS (16 passed — 10 from Task 5 + 6 new)

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/fiv_portal.py tests/test_fiv_portal.py
git commit -m "feat: fiv_portal finders (ingredient, stitch tool, OEM reverse-match)"
```

---

### Task 7: stitch_runner — extract, venv, run cli.py

**Files:**
- Create: `ifwi_mcp/stitch_runner.py`
- Test: `tests/test_stitch_runner.py`

**Interfaces:**
- Consumes: `result`, `config.cache_subdir`; stdlib `zipfile`, `tarfile`, `venv`, `subprocess`, `sys`, `re`, `shutil`, `pathlib`.
- Produces:
  - `_find_cli_dir(extract_root: Path) -> Optional[Path]` — returns the directory containing `cli.py` (search extract_root then one level of subdirs), or `None`.
  - `_venv_python(venv_dir: Path) -> Path` — `venv_dir/bin/python` (POSIX) or `venv_dir/Scripts/python.exe` (Windows).
  - `extract_stitch_tool(archive_path: str) -> dict` — validate archive exists and is `.zip`/`.tar*`; extract into `config.cache_subdir("stitch")/<archive_stem>/`; locate `cli.py` dir; create a venv at `<toolname>/venv`; if a `requirements.txt` sits next to `cli.py`, `pip install -r`. Return `ok({"stitch_dir": <cli dir>, "venv_python": str, "config_targets": [names...]})` where `config_targets` are the `Config_Stitch_<X>.ini` stems under `<cli dir>/config/`. Errors: not found/bad format → `EXTRACT_FAILED`; no `cli.py` → `EXTRACT_FAILED` `detail={"reason":"cli.py not found"}`; venv/pip failure → `VENV_SETUP_FAILED` `detail={"pip_output": <tail>}`.
  - `run_stitch(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini, soft_strap=None) -> dict` — validate: `stitch_dir/cli.py` exists; `binary_file` exists; `ingredient_path` exists; `config_ini` exists under `stitch_dir/config/` (accept bare name or path, but the resolved file must live under `config/` — reject traversal); `soft_strap` (if given) matches `^(\w+:\w+=[^,\s]+)([ ,]\w+:\w+=[^,\s]+)*$`. Run `<venv_python> cli.py --binary_file .. --ingredient_name .. --ingredient_path .. --config_ini .. [--soft_strap ..]` with `cwd=stitch_dir`, capture stdout+stderr to `config.cache_subdir("work")/stitch_<n>.log`. Exit 0 → find newest `*.bin` under `stitch_dir/output/` → `ok({"stitched_bin": str, "log_path": str, "exit_code": 0})`. Non-zero → `STITCH_RUN_FAILED` `detail={"exit_code","log_tail","full_log_path"}`.
  - `_venv_python` is used by `run_stitch`; store it alongside stitch_dir by convention: venv is `<stitch_dir_parent>/venv` when stitch_dir is the toolname root, else the venv created in extract. To keep it simple: `run_stitch` recomputes venv as `Path(stitch_dir)`'s nearest ancestor containing a `venv/` dir; if none, fall back to `sys.executable`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stitch_runner.py
import os
import stat
import zipfile
from pathlib import Path
import pytest
from ifwi_mcp import stitch_runner
from ifwi_mcp.result import ErrorCode


def _make_stitch_zip(zip_path: Path, with_requirements=False):
    """A minimal stitch tool: cli.py that writes output/out_stitched.bin and exits 0."""
    cli_src = (
        "import argparse, os, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--binary_file', required=True)\n"
        "p.add_argument('--ingredient_name', required=True)\n"
        "p.add_argument('--ingredient_path', required=True)\n"
        "p.add_argument('--config_ini', required=True)\n"
        "p.add_argument('--soft_strap', default='')\n"
        "a = p.parse_args()\n"
        "os.makedirs('output', exist_ok=True)\n"
        "open(os.path.join('output', 'out_stitched.bin'), 'wb').write(b'STITCHED')\n"
        "print('done')\n"
        "sys.exit(0)\n"
    )
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Output/cli.py", cli_src)
        z.writestr("Output/config/Config_Stitch_TARGET_A.ini", "[Configuration]\n")
        z.writestr("Output/config/Config_Stitch_TARGET_B.ini", "[Configuration]\n")
        if with_requirements:
            z.writestr("Output/requirements.txt", "")  # empty: pip install is a no-op-ish


def test_extract_missing_archive(clean_env):
    assert stitch_runner.extract_stitch_tool("/no/such.zip")["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_bad_format(clean_env, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("nope")
    assert stitch_runner.extract_stitch_tool(str(bad))["error_code"] == ErrorCode.EXTRACT_FAILED


def test_extract_success_lists_config_targets(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    r = stitch_runner.extract_stitch_tool(str(zpath))
    assert r["ok"] is True
    assert Path(r["data"]["stitch_dir"], "cli.py").is_file()
    assert sorted(r["data"]["config_targets"]) == [
        "Config_Stitch_TARGET_A", "Config_Stitch_TARGET_B"]


def test_run_stitch_success(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00" * 16)
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_TARGET_A")
    assert r["ok"] is True, r
    assert Path(r["data"]["stitched_bin"]).read_bytes() == b"STITCHED"
    assert r["data"]["exit_code"] == 0


def test_run_stitch_rejects_config_traversal(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "../cli.py")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_run_stitch_bad_soft_strap(clean_env, tmp_path):
    zpath = tmp_path / "stitch_tool.zip"
    _make_stitch_zip(zpath)
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_TARGET_A",
        soft_strap="this is not valid")
    assert r["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_run_stitch_nonzero_exit(clean_env, tmp_path):
    # cli.py that exits 1
    zpath = tmp_path / "fail_tool.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Output/cli.py",
                   "import argparse,sys\n"
                   "p=argparse.ArgumentParser()\n"
                   "[p.add_argument(f'--{a}') for a in "
                   "['binary_file','ingredient_name','ingredient_path','config_ini','soft_strap']]\n"
                   "p.parse_args()\n"
                   "print('boom', file=sys.stderr)\n"
                   "sys.exit(1)\n")
        z.writestr("Output/config/Config_Stitch_T.ini", "[Configuration]\n")
    extracted = stitch_runner.extract_stitch_tool(str(zpath))
    stitch_dir = extracted["data"]["stitch_dir"]
    binary = tmp_path / "ifwi.bin"; binary.write_bytes(b"\x00")
    ing = tmp_path / "ing.bin"; ing.write_bytes(b"\x01")
    r = stitch_runner.run_stitch(
        stitch_dir, str(binary), "BIOS", str(ing), "Config_Stitch_T")
    assert r["error_code"] == ErrorCode.STITCH_RUN_FAILED
    assert r["detail"]["exit_code"] == 1
    assert "boom" in r["detail"]["log_tail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stitch_runner.py -v`
Expected: FAIL — `AttributeError: module 'ifwi_mcp.stitch_runner' has no attribute 'extract_stitch_tool'`

- [ ] **Step 3: Write minimal implementation**

```python
# ifwi_mcp/stitch_runner.py
"""Extract a stitch-tool archive, build its venv, and run cli.py.

The only module that shells out (subprocess) or creates venvs.
"""
import re
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path
from typing import Optional

from . import config
from .result import ok, err, ErrorCode

_SOFT_STRAP_RE = re.compile(r"^(\w+:\w+=[^,\s]+)([ ,]\w+:\w+=[^,\s]+)*$")
_run_counter = [0]


def _find_cli_dir(extract_root: Path) -> Optional[Path]:
    if (extract_root / "cli.py").is_file():
        return extract_root
    for child in sorted(extract_root.iterdir()):
        if child.is_dir() and (child / "cli.py").is_file():
            return child
    # one more level
    for child in sorted(extract_root.iterdir()):
        if child.is_dir():
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and (grandchild / "cli.py").is_file():
                    return grandchild
    return None


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _find_venv_python(stitch_dir: Path) -> Path:
    for ancestor in [stitch_dir, *stitch_dir.parents]:
        candidate = ancestor / "venv"
        if candidate.is_dir():
            return _venv_python(candidate)
    return Path(sys.executable)


def extract_stitch_tool(archive_path: str) -> dict:
    src = Path(archive_path)
    if not src.is_file():
        return err(ErrorCode.EXTRACT_FAILED, "archive not found",
                   {"archive": archive_path, "reason": "not a file"})
    tool_root = config.cache_subdir("stitch") / src.name.split(".")[0]
    tool_root.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as z:
                z.extractall(tool_root)
        elif tarfile.is_tarfile(src):
            with tarfile.open(src) as t:
                t.extractall(tool_root)
        else:
            return err(ErrorCode.EXTRACT_FAILED, "unsupported archive format",
                       {"archive": archive_path, "reason": "not zip or tar"})
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        return err(ErrorCode.EXTRACT_FAILED, "extraction failed",
                   {"archive": archive_path, "reason": str(exc)})

    cli_dir = _find_cli_dir(tool_root)
    if cli_dir is None:
        return err(ErrorCode.EXTRACT_FAILED, "cli.py not found after extraction",
                   {"archive": archive_path, "reason": "cli.py not found"})

    venv_dir = tool_root / "venv"
    if not venv_dir.is_dir():
        try:
            venv.create(venv_dir, with_pip=True)
        except Exception as exc:  # noqa: BLE001
            return err(ErrorCode.VENV_SETUP_FAILED, "venv creation failed",
                       {"pip_output": str(exc)})
    vpython = _venv_python(venv_dir)
    reqs = cli_dir / "requirements.txt"
    if reqs.is_file():
        proc = subprocess.run([str(vpython), "-m", "pip", "install", "-r", str(reqs)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return err(ErrorCode.VENV_SETUP_FAILED, "pip install failed",
                       {"pip_output": (proc.stdout + proc.stderr)[-2000:]})

    targets = sorted(p.stem for p in (cli_dir / "config").glob("Config_Stitch_*.ini")) \
        if (cli_dir / "config").is_dir() else []
    return ok({"stitch_dir": str(cli_dir), "venv_python": str(vpython), "config_targets": targets})


def run_stitch(stitch_dir: str, binary_file: str, ingredient_name: str,
               ingredient_path: str, config_ini: str, soft_strap: Optional[str] = None) -> dict:
    sdir = Path(stitch_dir)
    if not (sdir / "cli.py").is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "stitch_dir has no cli.py",
                   {"param": "stitch_dir", "expected": "dir containing cli.py"})
    if not Path(binary_file).is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "binary_file not found",
                   {"param": "binary_file", "expected": "existing file"})
    if not Path(ingredient_path).exists():
        return err(ErrorCode.INVALID_ARGUMENT, "ingredient_path not found",
                   {"param": "ingredient_path", "expected": "existing path"})

    config_dir = (sdir / "config").resolve()
    resolved_ini = (config_dir / Path(config_ini).name) if "/" not in config_ini and "\\" not in config_ini \
        else Path(config_ini).resolve()
    try:
        resolved_ini.relative_to(config_dir)
    except ValueError:
        return err(ErrorCode.INVALID_ARGUMENT, "config_ini must live under stitch_dir/config",
                   {"param": "config_ini", "expected": "name of a file under config/"})
    if not resolved_ini.is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "config_ini not found",
                   {"param": "config_ini", "expected": f"file under {config_dir}"})
    if soft_strap and not _SOFT_STRAP_RE.match(soft_strap):
        return err(ErrorCode.INVALID_ARGUMENT, "soft_strap has invalid syntax",
                   {"param": "soft_strap", "expected": "k:v=val[,k:v=val]"})

    vpython = _find_venv_python(sdir)
    cmd = [str(vpython), "cli.py",
           "--binary_file", binary_file,
           "--ingredient_name", ingredient_name,
           "--ingredient_path", ingredient_path,
           "--config_ini", str(resolved_ini)]
    if soft_strap:
        cmd += ["--soft_strap", soft_strap]

    _run_counter[0] += 1
    log_path = config.cache_subdir("work") / f"stitch_{_run_counter[0]}.log"
    proc = subprocess.run(cmd, cwd=str(sdir), capture_output=True, text=True)
    combined = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(combined)

    if proc.returncode != 0:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch cli.py exited non-zero",
                   {"exit_code": proc.returncode, "log_tail": combined[-2000:],
                    "full_log_path": str(log_path)})

    output_dir = sdir / "output"
    bins = sorted(output_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime) \
        if output_dir.is_dir() else []
    if not bins:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch succeeded but produced no .bin",
                   {"exit_code": 0, "log_tail": combined[-2000:], "full_log_path": str(log_path)})
    return ok({"stitched_bin": str(bins[-1]), "log_path": str(log_path), "exit_code": 0})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stitch_runner.py -v`
Expected: PASS (8 passed). Note: these tests create real venvs — they take a few seconds each.

- [ ] **Step 5: Commit**

```bash
git add ifwi_mcp/stitch_runner.py tests/test_stitch_runner.py
git commit -m "feat: stitch_runner (extract, venv, run cli.py)"
```

---

### Task 8: server — FastMCP tool wrappers

**Files:**
- Create: `ifwi_mcp/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: all modules above.
- Produces (MCP tools registered on a `FastMCP` instance named `mcp`; each is a thin wrapper returning the underlying result dict):
  - `fiv_list_projects()` → `fiv_portal.list_projects()`
  - `fiv_list_swimlanes(project, phase, version)` → `fiv_portal.list_swimlanes(...)`
  - `fiv_find_ifwi(project, phase, version, swimlane=None)` → `fiv_portal.find_ifwi(...)`
  - `fiv_find_ingredient(project, name, version)` → `fiv_portal.find_ingredient(...)`
  - `fiv_find_stitch_tool(project, phase, version, swimlane=None)` → `fiv_portal.find_stitch_tool(...)`
  - `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane=None)` → `fiv_portal.match_build_by_oem(...)`
  - `parse_ifwi_oem(local_ifwi_path)` → `oem_parser.parse_ifwi_oem(...)`
  - `download(url_or_path, category="ifwi", dest_name=None)` → `downloader.download(...)`
  - `list_local_files()` → `downloader.list_local_files()`
  - `extract_stitch_tool(archive_path)` → `stitch_runner.extract_stitch_tool(...)`
  - `run_stitch(stitch_dir, binary_file, ingredient_name, ingredient_path, config_ini, soft_strap=None)` → `stitch_runner.run_stitch(...)`
  - Each wrapper is decorated with `@mcp.tool` and wrapped in try/except mapping any escaped exception to `err(INTERNAL_ERROR, ..., {"exception": str(e)})`.
  - `main()` — calls `config.validate_startup()`; on failure prints the error JSON to stderr and exits non-zero; else `mcp.run()`.
  - Module guard: `if __name__ == "__main__": main()`.

The test verifies the underlying callables are wired correctly WITHOUT starting a server, by importing the module-level functions. Because FastMCP's `@mcp.tool` may wrap the function in a Tool object, define each tool as a plain module function first, then register it; the test calls the plain function.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py
import responses
import pytest
from ifwi_mcp import server
from ifwi_mcp.result import ErrorCode

BASE = "https://fiv.example.com"


@pytest.fixture
def fiv_env(clean_env, monkeypatch):
    monkeypatch.setenv("FIV_BASE_URL", BASE)
    monkeypatch.setenv("FIV_TOKEN", "Basic xyz")
    return clean_env


def test_all_tools_exist():
    for name in ["fiv_list_projects", "fiv_list_swimlanes", "fiv_find_ifwi",
                 "fiv_find_ingredient", "fiv_find_stitch_tool", "fiv_match_build_by_oem",
                 "parse_ifwi_oem", "download", "list_local_files",
                 "extract_stitch_tool", "run_stitch", "main"]:
        assert hasattr(server, name), name


def test_parse_ifwi_oem_wrapper_passes_through(fiv_env, tmp_path):
    result = server.parse_ifwi_oem(str(tmp_path / "missing.bin"))
    assert result["error_code"] == ErrorCode.INVALID_ARGUMENT


@responses.activate
def test_fiv_list_projects_wrapper(fiv_env):
    responses.add(responses.GET, f"{BASE}/app/rest/get_project_info/",
                  json=[{"id": 1, "name": "P", "project_name": "Proj"}], status=200)
    result = server.fiv_list_projects()
    assert result["ok"] is True
    assert result["data"]["projects"][0]["id"] == 1


def test_download_wrapper_validation(fiv_env):
    assert server.download("")["error_code"] == ErrorCode.INVALID_ARGUMENT


def test_wrapper_maps_unexpected_exception(fiv_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(server.oem_parser, "parse_ifwi_oem", boom)
    result = server.parse_ifwi_oem("/whatever")
    assert result["error_code"] == ErrorCode.INTERNAL_ERROR
    assert "kaboom" in result["detail"]["exception"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError` or `AttributeError: module 'ifwi_mcp.server' has no attribute ...`

- [ ] **Step 3: Write minimal implementation**

```python
# ifwi_mcp/server.py
"""FastMCP server: thin tool wrappers over the ifwi_mcp modules."""
import functools
import json
import sys
from typing import Optional

from fastmcp import FastMCP

from . import config, downloader, fiv_portal, oem_parser, stitch_runner
from .result import err, ErrorCode

mcp = FastMCP("ifwi-stitching-mcp")


def _guard(fn):
    """Map any escaped exception to a unified INTERNAL_ERROR result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return err(ErrorCode.INTERNAL_ERROR, "unexpected error", {"exception": str(exc)})
    return wrapper


@_guard
def fiv_list_projects() -> dict:
    return fiv_portal.list_projects()


@_guard
def fiv_list_swimlanes(project: str, phase: str, version: str) -> dict:
    return fiv_portal.list_swimlanes(project, phase, version)


@_guard
def fiv_find_ifwi(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.find_ifwi(project, phase, version, swimlane)


@_guard
def fiv_find_ingredient(project: str, name: str, version: str) -> dict:
    return fiv_portal.find_ingredient(project, name, version)


@_guard
def fiv_find_stitch_tool(project: str, phase: str, version: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.find_stitch_tool(project, phase, version, swimlane)


@_guard
def fiv_match_build_by_oem(project: str, product: str, ifwi_version: str, flavor: str,
                           phase: str, swimlane: Optional[str] = None) -> dict:
    return fiv_portal.match_build_by_oem(project, product, ifwi_version, flavor, phase, swimlane)


@_guard
def parse_ifwi_oem(local_ifwi_path: str) -> dict:
    return oem_parser.parse_ifwi_oem(local_ifwi_path)


@_guard
def download(url_or_path: str, category: str = "ifwi", dest_name: Optional[str] = None) -> dict:
    return downloader.download(url_or_path, category, dest_name)


@_guard
def list_local_files() -> dict:
    return downloader.list_local_files()


@_guard
def extract_stitch_tool(archive_path: str) -> dict:
    return stitch_runner.extract_stitch_tool(archive_path)


@_guard
def run_stitch(stitch_dir: str, binary_file: str, ingredient_name: str,
               ingredient_path: str, config_ini: str, soft_strap: Optional[str] = None) -> dict:
    return stitch_runner.run_stitch(stitch_dir, binary_file, ingredient_name,
                                    ingredient_path, config_ini, soft_strap)


# Register each plain function as an MCP tool (keeps the plain callable importable for tests).
for _fn in (fiv_list_projects, fiv_list_swimlanes, fiv_find_ifwi, fiv_find_ingredient,
            fiv_find_stitch_tool, fiv_match_build_by_oem, parse_ifwi_oem, download,
            list_local_files, extract_stitch_tool, run_stitch):
    mcp.tool(_fn)


def main() -> None:
    startup = config.validate_startup()
    if not startup["ok"]:
        print(json.dumps(startup), file=sys.stderr)
        sys.exit(1)
    mcp.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 6: Commit**

```bash
git add ifwi_mcp/server.py tests/test_server.py
git commit -m "feat: FastMCP server wiring all tools"
```

---

## Post-implementation: manual end-to-end validation

Not automated (needs live FIV Portal + real tokens). Perform once after Task 8, per spec §8:

1. Set `FIV_BASE_URL`, `FIV_TOKEN`, `ARTIFACTORY_TOKEN`.
2. **Path A:** `fiv_list_projects` → `fiv_find_ifwi(project, phase, version)` (resolve swimlane if `MULTIPLE_SWIMLANES`) → `download(ifwi_url)` → `fiv_find_ingredient` + `download` → `fiv_find_stitch_tool` + `download` → `extract_stitch_tool` → `run_stitch`. Confirm a stitched `.bin`.
3. **Path B:** `parse_ifwi_oem(local.bin)` → `fiv_match_build_by_oem(project, product, ifwi_version, flavor, phase)` → then the download + stitch chain as in Path A. Confirm a stitched `.bin`.
4. Register with an MCP host (`python -m ifwi_mcp.server`) and drive one path via natural language.

## README (optional follow-up, not a gated task)

Document env vars, install (`pip install -e .`), MCP host registration snippet, and the two orchestration flows. Fold into Task 8 commit or a follow-up.
