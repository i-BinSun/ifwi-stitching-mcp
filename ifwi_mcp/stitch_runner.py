"""Extract a stitch-tool archive, build its venv, and run its entry script.

Modern stitch tools expose cli.py at the tool root. Older ones ship no cli.py at
all; instead their entry point is Source/stitch2.py. Both are supported.

The only module that shells out (subprocess) or creates venvs.
"""
import ast
import configparser
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import Optional

from . import archive, config, lock
from .result import ok, err, ErrorCode

_SOFT_STRAP_RE = re.compile(r"^(\w+:\w+=[^,\s]+)([ ,]\w+:\w+=[^,\s]+)*$")
_run_counter = [0]
_WORKSPACE_SKIP_NAMES = {"stitching", "output"}

_ENTRY_SCRIPT = "cli.py"
_LEGACY_ENTRY_DIR = "Source"
_LEGACY_ENTRY_SCRIPT = "stitch2.py"


def _quote_argv(argv: list) -> str:
    """Shell-quoted rendering of argv, for display and for remote runners."""
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def find_entry_script(stitch_dir: Path) -> Optional[str]:
    """cli.py if present, else the legacy stitch2.py, else None.

    Returns the name exactly as it appears on disk: Path.is_file() is
    case-insensitive on Windows, but `python -m Pkg.module` is not, so a
    lowercased guess would fail to import a file actually named Stitch2.py.
    """
    try:
        entries = {p.name.lower(): p.name for p in stitch_dir.iterdir() if p.is_file()}
    except OSError:
        return None
    if _ENTRY_SCRIPT in entries:
        return entries[_ENTRY_SCRIPT]
    if _LEGACY_ENTRY_SCRIPT in entries:
        return entries[_LEGACY_ENTRY_SCRIPT]
    return None


def find_config_dir(stitch_dir: Path) -> Optional[Path]:
    """Modern tools keep config/ under stitch_dir. Legacy tools ship Source/stitch2.py
    with a Config/ directory that is a *sibling* of Source, not a child of it."""
    for candidate in (stitch_dir / "config", stitch_dir / "Config",
                     stitch_dir.parent / "config", stitch_dir.parent / "Config"):
        if candidate.is_dir() and any(candidate.glob("Config_Stitch_*.ini")):
            return candidate
    return None


def find_cli_dir(extract_root: Path) -> Optional[Path]:
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
    # legacy layout: no cli.py anywhere, but a Source/stitch2.py under one of the
    # same candidate dirs
    candidates = [extract_root]
    candidates += [c for c in sorted(extract_root.iterdir()) if c.is_dir()]
    for child in sorted(extract_root.iterdir()):
        if child.is_dir():
            candidates += [c for c in sorted(child.iterdir()) if c.is_dir()]
    for candidate in candidates:
        legacy_dir = candidate / _LEGACY_ENTRY_DIR
        if (legacy_dir / _LEGACY_ENTRY_SCRIPT).is_file():
            return legacy_dir
    return None


def _find_requirements(tool_root: Path, cli_dir: Path) -> Optional[Path]:
    """Locate the tool's own requirements file, if it ships one.

    Modern layout: requirements.txt next to cli.py. Legacy tools ship a nested
    FIT-tool subdirectory (e.g. FITm_Py/<version>/) with platform-specific
    requirements_windows.txt / requirements_linux.txt instead — that FIT tool is
    invoked via sys.executable from the *same* venv, so its deps have to land here.
    """
    for candidate in (cli_dir / "requirements.txt", tool_root / "requirements.txt"):
        if candidate.is_file():
            return candidate
    variant = "requirements_windows.txt" if sys.platform.startswith("win") else "requirements_linux.txt"
    matches = sorted(tool_root.rglob(variant))
    return matches[0] if matches else None


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def find_venv_python(stitch_dir: Path) -> Path:
    for ancestor in [stitch_dir, *stitch_dir.parents]:
        candidate = ancestor / "venv"
        if candidate.is_dir():
            return _venv_python(candidate)
    return Path(sys.executable)


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


def _extract_stitch_tool_locked(archive_path: str, tool_root: Path) -> dict:
    src = Path(archive_path)
    cli_dir = find_cli_dir(tool_root) if tool_root.is_dir() else None
    if cli_dir is None:
        tool_root.mkdir(parents=True, exist_ok=True)
        extract_err = archive.extract_into(src, tool_root)
        if extract_err:
            return extract_err
        cli_dir = find_cli_dir(tool_root)
        if cli_dir is None:
            return err(ErrorCode.EXTRACT_FAILED, "cli.py or stitch2.py not found after extraction",
                       {"archive": archive_path, "reason": "no entry script found"})

    venv_dir = tool_root / "venv"
    if not venv_dir.is_dir():
        try:
            venv.create(venv_dir, with_pip=True)
        except Exception as exc:  # noqa: BLE001
            return err(ErrorCode.VENV_SETUP_FAILED, "venv creation failed",
                       {"pip_output": str(exc)})
    vpython = _venv_python(venv_dir)
    deps_marker = venv_dir / ".deps_installed"
    deps_source = "none"
    if not deps_marker.is_file():
        reqs = _find_requirements(tool_root, cli_dir)
        pip_argv = ["-r", str(reqs)] if reqs is not None else None
        deps_source = "requirements_file" if reqs is not None else "none"
        if pip_argv is None:
            fallback = config.get_stitch_config()
            if not fallback["ok"]:
                return fallback
            fallback_deps = fallback["data"]["fallback_deps"]
            if fallback_deps:
                pip_argv = list(fallback_deps)
                deps_source = "fallback"
        if pip_argv is not None:
            proc = subprocess.run([str(vpython), "-m", "pip", "install", *pip_argv],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                return err(ErrorCode.VENV_SETUP_FAILED, "pip install failed",
                           {"pip_output": (proc.stdout + proc.stderr)[-2000:]})
            deps_marker.write_text("")

    config_dir = find_config_dir(cli_dir)
    targets = sorted(p.stem for p in config_dir.glob("Config_Stitch_*.ini")) if config_dir else []
    return ok({"stitch_dir": str(cli_dir), "venv_python": str(vpython), "config_targets": targets,
               "tool_root": str(tool_root), "deps_source": deps_source})


def extract_stitch_tool(archive_path: str) -> dict:
    src = Path(archive_path)
    if not src.is_file():
        return err(ErrorCode.EXTRACT_FAILED, "archive not found",
                   {"archive": archive_path, "reason": "not a file"})
    tool_root = config.cache_subdir("stitch") / src.name.split(".")[0]
    try:
        with lock.acquire(str(tool_root)):
            return _extract_stitch_tool_locked(archive_path, tool_root)
    except TimeoutError as exc:
        return err(ErrorCode.LOCK_TIMEOUT,
                   "timed out waiting for another extraction of the same stitch tool",
                   {"tool_root": str(tool_root), "reason": str(exc)})


def _link_entry(src: Path, dst: Path) -> None:
    """Materialize dst as a same-name mirror of src: a junction/symlink for a
    directory (no admin privilege needed for a Windows junction), a hardlink for a
    file (falling back to a copy if hardlinking isn't possible, e.g. cross-volume)."""
    if src.is_dir():
        if sys.platform.startswith("win"):
            proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise OSError(f"mklink /J failed: {(proc.stdout + proc.stderr).strip()}")
        else:
            dst.symlink_to(src, target_is_directory=True)
    else:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)


def materialize_workspace(tool_root: Path, key: str) -> dict:
    """A private, per-`key` working copy of a shared stitch-tool install.

    Every top-level entry of tool_root is linked (not copied) into the new
    workspace, except any mutable output directories the vendor tool itself
    creates and wipes on every run (Stitching/, Output/, output/) -- those are
    left absent so each caller gets its own, and the vendor tool's cleanup never
    touches another run's files.
    """
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    workspace_root = config.cache_subdir("work") / digest / tool_root.name
    if workspace_root.is_dir():
        return ok({"workspace_root": str(workspace_root)})
    try:
        workspace_root.mkdir(parents=True)
        for entry in tool_root.iterdir():
            if entry.name.lower() in _WORKSPACE_SKIP_NAMES:
                continue
            _link_entry(entry, workspace_root / entry.name)
    except OSError as exc:
        shutil.rmtree(workspace_root, ignore_errors=True)
        return err(ErrorCode.WORKSPACE_SETUP_FAILED, "could not materialize isolated workspace",
                   {"tool_root": str(tool_root), "reason": str(exc)})
    return ok({"workspace_root": str(workspace_root)})


def _entry_invocation(stitch_dir: Path, entry_script: str) -> tuple:
    """argv prefix and cwd needed to invoke the entry script.

    cli.py runs as a plain script from stitch_dir. Some legacy stitch2.py tools
    ship as a real package (stitch_dir/__init__.py exists) that does internal
    `from <PkgName>.x import y` imports -- those must run as `python -m
    <PkgName>.stitch2` with cwd one level up, so the package is importable.
    """
    if entry_script.lower() == _ENTRY_SCRIPT:
        return [entry_script], stitch_dir
    if (stitch_dir / "__init__.py").is_file():
        module = f"{stitch_dir.name}.{Path(entry_script).stem}"
        return ["-m", module], stitch_dir.parent
    return [entry_script], stitch_dir


def build_stitch_command(stitch_dir: str, binary_file: str, ingredients: list,
                         config_ini: str, soft_strap: Optional[str] = None) -> dict:
    """Validate every input and render the cli.py command line without running it.

    ingredients is a non-empty list of {"name": str, "path": str}, one entry per
    ingredient. The vendor tool natively stitches several ingredients in a single
    invocation via pipe-separated --ingredient_name/--ingredient_path lists
    (Stitch2.py's __parse_inputs does `.split("|")` on both) — so a single ingredient
    is just the one-element-list case of the same code path.

    Returns argv (for subprocess) plus a copy-pasteable command_line string, so the
    caller can show the user exactly what will run — locally or on a remote runner.
    """
    sdir = Path(stitch_dir)
    entry_script = find_entry_script(sdir)
    if entry_script is None:
        return err(ErrorCode.INVALID_ARGUMENT, "stitch_dir has no cli.py or stitch2.py",
                   {"param": "stitch_dir", "expected": "dir containing cli.py or stitch2.py"})
    if not Path(binary_file).is_file():
        return err(ErrorCode.INVALID_ARGUMENT, "binary_file not found",
                   {"param": "binary_file", "expected": "existing file"})
    if not ingredients:
        return err(ErrorCode.INVALID_ARGUMENT, "ingredients is required",
                   {"param": "ingredients", "expected": "non-empty list of {name, path}"})
    for i, item in enumerate(ingredients):
        name = (item.get("name") or "").strip()
        path = (item.get("path") or "").strip()
        if not name:
            return err(ErrorCode.INVALID_ARGUMENT, "each ingredient needs a name",
                       {"param": f"ingredients[{i}].name", "expected": "non-empty name"})
        if not path.startswith("{") and not Path(path).exists():
            return err(ErrorCode.INVALID_ARGUMENT, "ingredient path not found",
                       {"param": f"ingredients[{i}].path",
                        "expected": "existing path or dict-string"})

    found_config_dir = find_config_dir(sdir)
    if found_config_dir is None:
        return err(ErrorCode.INVALID_ARGUMENT, "no config directory found near stitch_dir",
                   {"param": "stitch_dir",
                    "expected": "config/ or Config/ under stitch_dir or its parent"})
    config_dir = found_config_dir.resolve()
    if "/" not in config_ini and "\\" not in config_ini:
        # Bare name: append .ini if needed
        name = config_ini if config_ini.endswith(".ini") else f"{config_ini}.ini"
        resolved_ini = config_dir / name
    else:
        resolved_ini = Path(config_ini).resolve()
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

    names, args, warnings = [], [], []
    for item in ingredients:
        name = item["name"].strip()
        arg, item_warnings = _resolve_ingredient_arg(resolved_ini, name, item["path"].strip())
        names.append(name)
        args.append(arg)
        warnings += item_warnings

    vpython = find_venv_python(sdir)
    entry_argv, run_cwd = _entry_invocation(sdir, entry_script)
    argv = [str(vpython)] + entry_argv + [
            "--binary_file", binary_file,
            "--ingredient_name", "|".join(names),
            "--ingredient_path", "|".join(args),
            "--config_ini", str(resolved_ini)]
    if soft_strap:
        argv += ["--soft_strap", soft_strap]
    return ok({"argv": argv, "command_line": _quote_argv(argv), "cwd": str(run_cwd),
               "config_ini": str(resolved_ini), "ingredient_args": args,
               "warnings": warnings})


def _augmented_env(cwd: Path) -> dict:
    """Some stitch tools shell out to bundled EDK2 BaseTools (GenFw, GenFfs, ...) by
    bare name, expecting them on PATH. Prepend any Tools/ dir found next to cwd or
    its parent so those resolve, without touching the caller's real PATH."""
    env = os.environ.copy()
    extra = [p for p in (cwd / "Tools", cwd.parent / "Tools") if p.is_dir()]
    if extra:
        env["PATH"] = os.pathsep.join(str(p) for p in extra) + os.pathsep + env.get("PATH", "")
    return env


def execute_command(argv: list, cwd: str, log_name: Optional[str] = None,
                    timeout: Optional[int] = None) -> dict:
    """Run a prepared stitch command, capture its log, and locate the stitched .bin."""
    sdir = Path(cwd)
    if log_name:
        log_path = config.cache_subdir("work") / f"{log_name}.log"
    else:
        _run_counter[0] += 1
        log_path = config.cache_subdir("work") / f"stitch_{_run_counter[0]}.log"
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env=_augmented_env(sdir))
    except subprocess.TimeoutExpired:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch command timed out",
                   {"command_line": _quote_argv(argv), "timeout_seconds": timeout})
    except OSError as exc:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch command could not be started",
                   {"command_line": _quote_argv(argv), "reason": str(exc)})
    combined = (proc.stdout or "") + (proc.stderr or "")
    log_path.write_text(combined)

    if proc.returncode != 0:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch entry script exited non-zero",
                   {"exit_code": proc.returncode, "log_tail": combined[-2000:],
                    "full_log_path": str(log_path), "command_line": _quote_argv(argv)})

    output_dir = sdir / "output"
    bins = sorted(output_dir.glob("*.bin"), key=lambda p: p.stat().st_mtime) \
        if output_dir.is_dir() else []
    if not bins:
        return err(ErrorCode.STITCH_RUN_FAILED, "stitch succeeded but produced no .bin",
                   {"exit_code": 0, "log_tail": combined[-2000:],
                    "full_log_path": str(log_path), "command_line": _quote_argv(argv)})
    return ok({"stitched_bin": str(bins[-1]), "log_path": str(log_path),
               "output_dir": str(output_dir), "exit_code": 0,
               "command_line": _quote_argv(argv)})


def run_stitch(stitch_dir: str, binary_file: str, ingredients: list,
               config_ini: str, soft_strap: Optional[str] = None) -> dict:
    built = build_stitch_command(stitch_dir, binary_file, ingredients, config_ini, soft_strap)
    if not built["ok"]:
        return built
    run = execute_command(built["data"]["argv"], built["data"]["cwd"])
    if not run["ok"]:
        return run
    data = dict(run["data"])
    data["warnings"] = built["data"]["warnings"]
    return ok(data)
