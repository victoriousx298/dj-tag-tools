"""Delete old backup folders when the tool starts.

Every write run creates its own folder under backups/ (e.g. temp_20261114-163739/ or convert_<time>/).
Folders whose newest file is older than MAX_AGE_DAYS are removed. Only direct sub-folders of the
backup root are ever touched.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

MAX_AGE_DAYS = 30


def _newest(d: Path) -> float:
    latest = d.stat().st_mtime
    for p in d.rglob("*"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    return latest


def prune(root: Path, days: int = MAX_AGE_DAYS, now: float | None = None) -> list[str]:
    """Remove backup folders older than `days`. Returns the names removed."""
    now = now if now is not None else time.time()
    removed = []
    if not root.is_dir():
        return removed
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        try:
            if now - _newest(d) > days * 86400:
                shutil.rmtree(d)
                removed.append(d.name)
        except OSError as e:
            print(f"Could not remove old backup {d.name}: {e}")
    return removed
