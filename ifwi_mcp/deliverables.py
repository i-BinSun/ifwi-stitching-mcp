"""Collect what a finished stitch job produced into one place with a manifest.

Deliverables land in $CACHE_DIR/deliverables/<plan_id>/ regardless of whether the
job ran locally or on a remote runner, so the caller always gets the same shape:
a list of items with size + sha256, plus an optional zip of the whole set.
"""
import hashlib
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from . import config, plan as plan_mod
from .result import ok, err, ErrorCode

MANIFEST_NAME = "manifest.json"
_DOWNLOAD_TIMEOUT = 300


def deliverables_dir(plan_id: str) -> Path:
    return config.cache_subdir("deliverables") / plan_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(path: Path, kind: str) -> dict:
    return {"name": path.name, "kind": kind, "path": str(path),
            "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _target(dest: Path, name: str) -> Path:
    """Non-clobbering destination path inside the deliverables dir."""
    candidate = dest / name
    stem, suffix, n = candidate.stem, candidate.suffix, 1
    while candidate.exists():
        candidate = dest / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def _write_manifest(plan_id: str, dest: Path, items: list, extra: dict) -> dict:
    manifest = {"plan_id": plan_id,
                "collected_at": datetime.now().isoformat(timespec="seconds"),
                "item_count": len(items), "items": items}
    manifest.update(extra)
    path = dest / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(path)
    return manifest


def _archive_items(plan_id: str, dest: Path, items: list) -> Optional[str]:
    archive_path = dest / f"{plan_id}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            zf.write(item["path"], arcname=item["name"])
    return str(archive_path)


def collect(plan_id: str, entries: list) -> dict:
    """Copy locally produced artifacts into the deliverables dir.

    entries: [{"path": str, "kind": str, "required": bool}]. A missing required
    entry fails the collection; missing optional ones are simply skipped.
    """
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.INVALID_ARGUMENT, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    dest = deliverables_dir(plan_id)
    dest.mkdir(parents=True, exist_ok=True)

    items = []
    for entry in entries:
        src = Path(entry["path"])
        if not src.is_file():
            if entry.get("required"):
                return err(ErrorCode.DELIVERABLE_MISSING, "expected deliverable not found",
                           {"kind": entry.get("kind"), "path": entry["path"]})
            continue
        try:
            copied = shutil.copy2(src, _target(dest, src.name))
        except OSError as exc:
            return err(ErrorCode.DELIVERABLE_MISSING, "deliverable could not be copied",
                       {"kind": entry.get("kind"), "path": entry["path"], "reason": str(exc)})
        items.append(_describe(Path(copied), entry.get("kind", "artifact")))

    return _finish(plan_id, dest, items)


def fetch(plan_id: str, entries: list, token: str = "") -> dict:
    """Download deliverables published by a remote runner.

    entries: [{"url": str, "name": str, "kind": str}].
    """
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.INVALID_ARGUMENT, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    dest = deliverables_dir(plan_id)
    dest.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    items = []
    for entry in entries:
        url = entry.get("url")
        if not url:
            return err(ErrorCode.REMOTE_EXEC_FAILED, "remote deliverable has no url",
                       {"entry": entry})
        name = Path(str(entry.get("name") or url.rsplit("/", 1)[-1])).name
        if not name:
            return err(ErrorCode.REMOTE_EXEC_FAILED, "remote deliverable has no usable name",
                       {"entry": entry})
        target = _target(dest, name)
        try:
            resp = requests.get(url, headers=headers, stream=True,
                                timeout=_DOWNLOAD_TIMEOUT, verify=False)
            resp.raise_for_status()
            with target.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
        except (requests.RequestException, OSError) as exc:
            return err(ErrorCode.DOWNLOAD_FAILED, "deliverable download failed",
                       {"url": url, "reason": str(exc)})
        items.append(_describe(target, entry.get("kind", "artifact")))

    return _finish(plan_id, dest, items)


def _finish(plan_id: str, dest: Path, items: list) -> dict:
    cfg = config.get_deliverables_config()
    if not cfg["ok"]:
        return cfg
    archive_path = _archive_items(plan_id, dest, items) if (cfg["data"]["archive"] and items) else None
    manifest = _write_manifest(plan_id, dest, items, {"archive": archive_path})
    return ok({"deliverables_dir": str(dest), "items": items, "item_count": len(items),
               "archive": archive_path, "manifest_path": manifest["manifest_path"]})


def load_manifest(plan_id: str) -> dict:
    if not plan_id or not plan_mod.PLAN_ID_RE.match(plan_id):
        return err(ErrorCode.INVALID_ARGUMENT, "plan_id is missing or unsafe",
                   {"param": "plan_id", "expected": "[A-Za-z0-9_.-]+"})
    path = deliverables_dir(plan_id) / MANIFEST_NAME
    if not path.is_file():
        return err(ErrorCode.DELIVERABLE_MISSING, "no deliverables collected for this plan",
                   {"plan_id": plan_id, "expected": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return err(ErrorCode.DELIVERABLE_MISSING, "manifest is not readable JSON",
                   {"plan_id": plan_id, "reason": str(exc)})
    return ok(data)
