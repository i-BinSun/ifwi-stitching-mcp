"""Reclaim space from the on-disk cache.

Nothing here ever descends through a symlink/junction: copied run directories under
work/ can contain links (e.g. a venv interpreter), and older workspaces were link farms
pointing back at the shared stitch tool; following one would delete its target.
"""
import os
import stat as stat_mod
import time
from pathlib import Path
from typing import Optional

from . import archive, config
from .result import ok, err, ErrorCode

# Everything under these is reproducible: re-downloaded or re-extracted on demand.
CATEGORIES = ("work", "extracted", "stitch", "ifwi", "ingredients", "deliverables", "plans")
_EXTRACTION_CATEGORIES = ("extracted", "stitch")
_DEFAULT_MAX_AGE_DAYS = 14


def is_link(path: Path) -> bool:
    """Symlink, or a Windows directory junction - Path.is_symlink() reports False for those."""
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def remove_tree(path: Path) -> None:
    """Delete path, never descending through a link into whatever it points at."""
    if is_link(path):
        try:
            path.unlink()
        except OSError:
            os.rmdir(path)  # Windows directory junction
        return
    if path.is_dir():
        for child in path.iterdir():
            remove_tree(child)
        path.rmdir()
        return
    path.unlink(missing_ok=True)


def _entry_bytes(path: Path) -> int:
    if is_link(path):
        return 0
    if path.is_dir():
        total = 0
        for child in path.iterdir():
            total += _entry_bytes(child)
        return total
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _age_days(path: Path, now: float) -> float:
    try:
        return (now - path.lstat().st_mtime) / 86400.0
    except OSError:
        return 0.0


def cleanup(max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
            categories: Optional[list] = None, dry_run: bool = False) -> dict:
    """Delete cache entries whose last modification is older than max_age_days.

    max_age_days=0 clears the selected categories outright.
    """
    try:
        max_age = float(max_age_days)
    except (TypeError, ValueError):
        return err(ErrorCode.INVALID_ARGUMENT, "max_age_days must be a number",
                   {"param": "max_age_days", "got": repr(max_age_days)})
    if max_age < 0:
        return err(ErrorCode.INVALID_ARGUMENT, "max_age_days must not be negative",
                   {"param": "max_age_days", "got": max_age_days})

    selected = list(categories) if categories else list(CATEGORIES)
    unknown = [c for c in selected if c not in CATEGORIES]
    if unknown:
        return err(ErrorCode.INVALID_ARGUMENT, "unknown cache category",
                   {"param": "categories", "expected": list(CATEGORIES), "got": unknown})

    now = time.time()
    removed, kept, freed = [], 0, 0
    for category in selected:
        base = config.cache_subdir(category)
        for entry in sorted(base.iterdir()):
            if entry.name.endswith(archive.COMPLETE_SUFFIX):
                continue  # removed together with the directory it belongs to
            age = _age_days(entry, now)
            if age < max_age:
                kept += 1
                continue
            size = _entry_bytes(entry)
            if not dry_run:
                try:
                    if category in _EXTRACTION_CATEGORIES:
                        archive.discard_extraction(entry)
                    else:
                        remove_tree(entry)
                except OSError as exc:
                    return err(ErrorCode.INTERNAL_ERROR, "cache entry could not be removed",
                               {"path": str(entry), "reason": str(exc),
                                "removed_so_far": len(removed)})
            removed.append({"category": category, "path": str(entry),
                            "bytes": size, "age_days": round(age, 2)})
            freed += size

    return ok({"dry_run": dry_run, "max_age_days": max_age, "categories": selected,
               "removed_count": len(removed), "kept_count": kept,
               "freed_bytes": freed, "removed": removed})
