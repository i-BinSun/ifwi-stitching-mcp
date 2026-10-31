"""Turn a BIOS build package into the stitch tool's BIOS replacement input.

A BIOS build package (e.g. OakStreamRp_..._BuildPkg.7z) is not something the stitch tool's
BIOS ingredient takes as-is: it holds many IFWI images and the raw BIOS FDs. This module picks
what the tool needs, at execution time, once the target config is known:

- IFWIStitch20.bin (a zip; or its extracted BiosPkg folder) holds BiosPkg/BiosFile/MAIN.fd and
  SECPEI.fd. Raw FDs have no FIT, so the tool's BIOS route cannot tell their VAB state; its
  BIOSID route can (from BuildConfig.ini). They are laid out as a BIOSID version folder at the
  paths the target config's [BIOSID] regex_mandatory_dict expects, and stitched as BIOSID.
- Otherwise one 64 MB IFWI .bin is picked and stitched as BIOS; the tool slices MAIN+SECPEI
  out of its BIOS region and redoes FitGen/BpmGen.

Pure file I/O, no network.
"""
import ast
import configparser
import json
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Optional

from .result import ok, err, ErrorCode

IFWISTITCH_ZIP = "ifwistitch20.bin"
IFWI_SIZE = 64 * 1024 * 1024
FD_SIGNATURE = 0x0FF0A55A
FD_SIG_OFFSET = 0x10

# BIOSID regex_mandatory_dict key -> file under BiosPkg/ (lower case)
PKG_FILES = {
    "main_fd": "biosfile/main.fd",
    "secpei_bin": "biosfile/secpei.fd",
    "BuildConfig": "configinfo/buildconfig.ini",
    "BinaryFd_fdf": "sourcefile/binaryfd.fdf",
}
REQUIRED_KEYS = ("main_fd", "secpei_bin", "BuildConfig")

BIOS_ID_MARKER = b"$IBIOSI$"
BIOS_VERSION = r"\d{4}\.[A-Z]\d{2}"
BIOS_ID_PATTERN = re.compile(r"^[A-Z0-9]+\.[A-Z0-9]+\.(" + BIOS_VERSION + r")\.\d{10}$")
_REGEX_META = re.compile(r"[*+?\[\](){}|^$\\]")


def _invalid(message: str, detail: dict) -> dict:
    return err(ErrorCode.BIOS_PACKAGE_INVALID, message, detail)


def _pkg_key(rel: str) -> Optional[str]:
    """PKG_FILES key for a path that ends in BiosPkg/<file>, else None."""
    norm = "/" + rel.replace("\\", "/").lower()
    for key, suffix in PKG_FILES.items():
        if norm.endswith("/biospkg/" + suffix):
            return key
    return None


def _is_ifwi_image(path: Path) -> bool:
    try:
        if path.stat().st_size != IFWI_SIZE:
            return False
        with path.open("rb") as f:
            f.seek(FD_SIG_OFFSET)
            head = f.read(4)
    except OSError:
        return False
    return len(head) == 4 and struct.unpack("<I", head)[0] == FD_SIGNATURE


def detect(package_dir: str) -> Optional[dict]:
    """What a BIOS package folder offers, or None when it is not a BIOS build package.

    {"kind": "zip", "zip": path} - IFWIStitch20.bin
    {"kind": "dir", "files": {key: path}} - an extracted IFWIStitch20 BiosPkg
    {"kind": "bin", "bins": [paths]} - 64 MB IFWI images only
    """
    root = Path(package_dir)
    if not root.is_dir():
        return None
    zips, bins, extracted = [], [], {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.name.lower() == IFWISTITCH_ZIP:
            zips.append(path)
        elif path.suffix.lower() == ".bin" and _is_ifwi_image(path):
            bins.append(path)
        else:
            key = _pkg_key(rel)
            if key:
                extracted.setdefault(key, []).append(path)
    if zips:
        # Shallowest first: the package's own IFWIStitch20.bin, not one nested deeper.
        zips.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
        return {"kind": "zip", "zip": str(zips[0])}
    if extracted.get("main_fd") or extracted.get("secpei_bin"):
        dupes = {k: [str(p) for p in v] for k, v in extracted.items() if len(v) > 1}
        if dupes:
            return {"kind": "dir", "files": {}, "duplicates": dupes}
        return {"kind": "dir", "files": {k: str(v[0]) for k, v in extracted.items()}}
    if bins:
        return {"kind": "bin", "bins": [str(p) for p in bins]}
    return None


def extract_ifwistitch(zip_path: str, dest: str) -> dict:
    """Extract only the BiosPkg files PKG_FILES names from IFWIStitch20.bin into dest."""
    found = {}
    try:
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                key = _pkg_key(info.filename)
                if key:
                    found.setdefault(key, []).append(info)
            dupes = {k: [i.filename for i in v] for k, v in found.items() if len(v) > 1}
            if dupes:
                return _invalid("IFWIStitch20.bin holds several copies of a BIOS file",
                                {"zip": zip_path, "duplicates": dupes})
            out_dir = Path(dest)
            out_dir.mkdir(parents=True, exist_ok=True)
            files = {}
            for key, (info,) in found.items():
                # Written under fixed names: member paths never reach the file system.
                target = out_dir / Path(PKG_FILES[key]).name
                with z.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                files[key] = str(target)
    except (zipfile.BadZipFile, OSError) as exc:
        return _invalid("IFWIStitch20.bin is not a readable zip",
                        {"zip": zip_path, "reason": str(exc)})
    return ok({"files": files})


def parse_bios_version(block: bytes) -> dict:
    """BIOS version (e.g. 0040.D62) from the $IBIOSI$ string, by the stitch tool's rule."""
    texts = set()
    for match in re.finditer(re.escape(BIOS_ID_MARKER), block):
        raw = block[match.end():match.end() + 128]
        end = next((i for i in range(0, len(raw) - 1, 2) if raw[i:i + 2] == b"\x00\x00"), len(raw))
        texts.add(raw[:end].decode("utf-16-le", "replace"))
    if len(texts) != 1:
        return _invalid("MAIN.fd/SECPEI.fd need exactly one $IBIOSI$ string",
                        {"bios_ids": sorted(texts)})
    text = texts.pop()
    match = BIOS_ID_PATTERN.match(text)
    if not match:
        return _invalid("unrecognized $IBIOSI$ string", {"bios_id": text})
    return ok({"bios_id": text, "version": match.group(1)})


def pick_ifwi_bin(bins: list) -> dict:
    """One 64 MB image: never *_PO_SAFE*, then prefer neither Simics nor jc_2s_b, then by name."""
    usable = [b for b in bins if "po_safe" not in Path(b).name.lower()]
    if not usable:
        return _invalid("the BIOS package has only PO_SAFE 64MB images",
                        {"candidates": [Path(b).name for b in bins]})

    def rank(path):
        name = Path(path).name.lower()
        return ("simics" in name, "jc_2s_b" in name, name)

    chosen = sorted(usable, key=rank)[0]
    return ok({"bin": chosen, "candidates": [Path(b).name for b in bins]})


def _biosid_regexes(config_ini: str) -> dict:
    config = configparser.RawConfigParser()
    config.optionxform = str
    try:
        config.read(config_ini, encoding="utf-8")
    except configparser.Error as exc:
        return _invalid("target config is not readable", {"config_ini": config_ini,
                                                          "reason": str(exc)})
    if not config.has_section("BIOSID"):
        return _invalid("target config has no [BIOSID] section: raw MAIN.fd/SECPEI.fd "
                        "cannot be stitched with it", {"config_ini": config_ini})
    raw = config.get("BIOSID", "regex_mandatory_dict", fallback="")
    try:
        regexes = json.loads(raw)
    except ValueError:
        try:
            regexes = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            regexes = None
    if not isinstance(regexes, dict):
        return _invalid("[BIOSID] regex_mandatory_dict is not a JSON object",
                        {"config_ini": config_ini})
    return ok({"regexes": regexes})


def _literal_path(pattern: str) -> Optional[str]:
    """The one relative path a [BIOSID] pattern names, or None if it is not a plain path."""
    # Same normalization as the stitch tool: a pattern's backslashes are path separators.
    norm = pattern.replace("\\", "/")
    rel = norm[1:] if norm.startswith("^") else norm
    rel = rel[:-1] if rel.endswith("$") else rel
    parts = rel.split("/")
    if (not rel or _REGEX_META.search(rel) or rel.startswith("/") or ":" in rel
            or any(p in ("", ".", "..") for p in parts)):
        return None
    if not re.match(norm, rel, re.I):
        return None
    return rel


def build_biosid_dir(files: dict, version: str, config_ini: str, dest_root: str) -> dict:
    """Lay files out as <dest_root>/<version>/<path from the [BIOSID] regex> for the tool's BIOSID route."""
    parsed = _biosid_regexes(config_ini)
    if not parsed["ok"]:
        return parsed
    regexes = parsed["data"]["regexes"]
    version_dir = Path(dest_root) / version
    if version_dir.exists():
        shutil.rmtree(version_dir)
    placed = {}
    for key in PKG_FILES:
        pattern = regexes.get(key)
        if not pattern:
            if key in REQUIRED_KEYS:
                return _invalid(f"[BIOSID] regex_mandatory_dict has no {key}",
                                {"config_ini": config_ini, "key": key})
            continue
        if key not in files:
            if key in REQUIRED_KEYS:
                return _invalid(f"the BIOS package has no BiosPkg/{PKG_FILES[key]}", {"key": key})
            continue
        rel = _literal_path(str(pattern))
        if rel is None:
            return _invalid(f"[BIOSID] {key} pattern is not a plain file path",
                            {"config_ini": config_ini, "key": key, "pattern": pattern})
        target = version_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files[key], target)
        placed[key] = rel
    return ok({"biosid_dir": str(version_dir), "placed": placed})


def _resolve_fd(found: dict, config_ini: Optional[str], work_dir: Path) -> dict:
    if found["kind"] == "zip":
        extracted = extract_ifwistitch(found["zip"], str(work_dir / "IFWIStitch20"))
        if not extracted["ok"]:
            return extracted
        files, source = extracted["data"]["files"], found["zip"]
    else:
        if found.get("duplicates"):
            return _invalid("the BIOS package holds several BiosPkg copies of a BIOS file",
                            {"duplicates": found["duplicates"]})
        files = found["files"]
        source = str(Path(files.get("main_fd") or files["secpei_bin"]).parent)
    missing = [PKG_FILES[k] for k in REQUIRED_KEYS if k not in files]
    if missing:
        return _invalid("the BIOS package's BiosPkg lacks required files",
                        {"source": source, "missing": missing})
    if not config_ini:
        return _invalid("the target config is needed to stitch MAIN.fd/SECPEI.fd", {"source": source})
    block = Path(files["main_fd"]).read_bytes() + Path(files["secpei_bin"]).read_bytes()
    version = parse_bios_version(block)
    if not version["ok"]:
        version["detail"]["source"] = source
        return version
    ver = version["data"]["version"]
    built = build_biosid_dir(files, ver, config_ini, str(work_dir / "BIOSID"))
    if not built["ok"]:
        return built
    return ok({"name": "BIOSID", "path": built["data"]["biosid_dir"],
               "warning": f"BIOS: stitching MAIN.fd/SECPEI.fd of {Path(source).name} as BIOSID {ver} "
                          f"({version['data']['bios_id']})"})


def resolve_bios_ingredients(ingredients: list, config_ini: Optional[str], work_dir: str) -> dict:
    """Replace each BIOS build-package ingredient with what the stitch tool takes.

    Ingredients that are not named BIOS, or BIOS paths that are not a build package
    (a single image, a folder without IFWI images), are returned unchanged.
    """
    result, warnings = [], []
    for index, item in enumerate(ingredients):
        if str(item.get("name", "")).strip().lower() != "bios":
            result.append(item)
            continue
        found = detect(item["path"])
        if found is None:
            result.append(item)
            continue
        if found["kind"] == "bin":
            picked = pick_ifwi_bin(found["bins"])
            if not picked["ok"]:
                picked["detail"]["package"] = item["path"]
                return picked
            chosen = picked["data"]["bin"]
            result.append({**item, "path": chosen, "verbatim": True})
            warnings.append(f"BIOS: no IFWIStitch20 in the package; slicing MAIN+SECPEI from "
                            f"{Path(chosen).name} (64MB candidates: {picked['data']['candidates']})")
            continue
        resolved = _resolve_fd(found, config_ini, Path(work_dir) / f"{index}_bios")
        if not resolved["ok"]:
            resolved["detail"]["package"] = item["path"]
            return resolved
        result.append({**item, "name": resolved["data"]["name"], "path": resolved["data"]["path"],
                       "verbatim": True})
        warnings.append(resolved["data"]["warning"])
    return ok({"ingredients": result, "warnings": warnings})
