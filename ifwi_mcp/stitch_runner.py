"""Extract a stitch-tool archive, build its venv, and run cli.py.

The only module that shells out (subprocess) or creates venvs.
"""
import ast
import configparser
import re
import subprocess
import sys
import venv
from pathlib import Path
from typing import Optional

from . import archive, config
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


def extract_stitch_tool(archive_path: str) -> dict:
    src = Path(archive_path)
    if not src.is_file():
        return err(ErrorCode.EXTRACT_FAILED, "archive not found",
                   {"archive": archive_path, "reason": "not a file"})
    tool_root = config.cache_subdir("stitch") / src.name.split(".")[0]
    tool_root.mkdir(parents=True, exist_ok=True)
    extract_err = archive.extract_into(src, tool_root)
    if extract_err:
        return extract_err

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
    if not ingredient_path.strip().startswith("{") and not Path(ingredient_path).exists():
        return err(ErrorCode.INVALID_ARGUMENT, "ingredient_path not found",
                   {"param": "ingredient_path", "expected": "existing path or dict-string"})

    config_dir = (sdir / "config").resolve()
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

    ingredient_arg, ingredient_warnings = _resolve_ingredient_arg(
        resolved_ini, ingredient_name, ingredient_path)

    vpython = _find_venv_python(sdir)
    cmd = [str(vpython), "cli.py",
           "--binary_file", binary_file,
           "--ingredient_name", ingredient_name,
           "--ingredient_path", ingredient_arg,
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
    return ok({"stitched_bin": str(bins[-1]), "log_path": str(log_path),
               "exit_code": 0, "warnings": ingredient_warnings})
