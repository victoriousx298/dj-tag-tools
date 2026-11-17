"""Shared helper: back up each file, write one kind of change, read it back."""
from __future__ import annotations

import contextlib
import csv
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

import rekordbox_bpm_sync as bpm_tool


def apply_with_backup(rows: Iterable, backup_dir: Optional[Path], is_todo: Callable, writer: Callable,
                      verifier: Callable, describe: Callable) -> tuple[int, int]:
    """For each row where is_todo(row): copy the file to a NEW backup dir, call writer(row),
    then verifier(row) must return None (ok) or an error string. Sets row.result.

    A failure on one file is reported and never stops the others.
    """
    todo = [r for r in rows if is_todo(r)]
    if not todo:
        return 0, 0
    if backup_dir is not None:
        backup_dir = bpm_tool.make_backup_dir(backup_dir)
        print(f"\nBackups go to: {backup_dir}")
        mf = (backup_dir / "manifest.csv").open("w", newline="", encoding="utf-8")
    else:
        print("\nNo backup (disabled).")
        mf = contextlib.nullcontext()
    ok = failed = 0
    with mf:
        mw = csv.writer(mf) if backup_dir is not None else None
        if mw:
            mw.writerow(["backup_file", "original_path"])
        for n, r in enumerate(todo, 1):
            src = Path(r.path)
            try:
                if mw:
                    dst = backup_dir / f"{n:04d}_{src.name}"
                    shutil.copy2(src, dst)
                    if dst.stat().st_size != src.stat().st_size:
                        raise OSError("backup size mismatch; file not modified")
                    mw.writerow([str(dst), str(src)])
                    mf.flush()
                writer(r)
                err = verifier(r)
                if err:
                    raise OSError(err)
                r.result = "ok"
                ok += 1
                print(f"  ok     {src.name}  ->  {describe(r)}")
            except Exception as e:
                r.result = f"FAILED: {e}"
                failed += 1
                print(f"  FAILED {src.name}: {e}", file=sys.stderr)
    return ok, failed
