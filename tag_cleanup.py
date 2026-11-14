#!/usr/bin/env python3
"""Optional tag cleanup for MP3s in a playlist (artist, title, album). Only runs when you ask for it.

Rules (every change is shown in the preview and can be unticked):
  1. junk    - remove download-site leftovers from artist/title/album, e.g.
               "SpotiDownloader.com - ", "(SPOTISAVER)", "(Official Video)".
               Bitrate info such as "(128kbps)" is deliberately LEFT as it is.
               DJ-relevant words like (Clean), (Dirty), (Extended), (Intro) are NEVER removed.
  2. spaces  - trim and collapse repeated spaces.
  3. fill    - if the artist tag is empty or "Unknown Artist", take the artist from a file name of the
               form "Artist - Title.mp3"; if the title tag is empty, take the title from it. Existing
               artist/title text is never replaced by file-name text.
Dry run by default; --write backs up each file first.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import rekordbox_bpm_sync as bpm_tool
import tagio

FIELDS = {"artist": "TPE1", "title": "TIT2", "album": "TALB"}

_PREFIX = re.compile(r"^\s*(?:spotidownloader\.com|spotisaver(?:\.net)?|spotifydown\.com|spotmate\.online|"
                     r"yt1s\.[a-z]+|ytmp3\.[a-z]+|y2mate[\w.]*|savefrom\.net)\s*[-–_:]\s*", re.I)
_BRACKET = re.compile(r"\s*[\(\[]\s*(?:spotisaver|spotidownloader[^)\]]*|"
                      r"official\s+(?:music\s+)?(?:video|audio)|lyric\s+video|official\s+lyric\s+video)\s*[\)\]]", re.I)
_UNKNOWN_ARTIST = {"", "unknown artist", "unknown"}


def clean_text(s: str) -> str:
    """Rules 1 and 2."""
    prev, t = None, s or ""
    while prev != t:
        prev = t
        t = _BRACKET.sub("", _PREFIX.sub("", t))
    return re.sub(r"\s{2,}", " ", t).strip()


def split_filename(stem: str) -> tuple[Optional[str], Optional[str]]:
    stem = clean_text(stem)
    m = re.match(r"^(.+?)\s+-\s+(.+)$", stem)
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, None)


def propose(current: dict[str, str], filename_stem: str) -> dict[str, tuple[str, str]]:
    """Return {field: (new value, reason)} for fields that would change."""
    out: dict[str, tuple[str, str]] = {}
    fa, ft = split_filename(filename_stem)
    for f in ("artist", "title", "album"):
        old = current.get(f, "")
        new = clean_text(old)
        reason = ""
        if new != old:
            reason = "remove download junk" if clean_text(re.sub(r"\s+", " ", old)) != re.sub(r"\s+", " ", old).strip() \
                else "tidy spaces"
        if f == "artist" and new.casefold() in _UNKNOWN_ARTIST and fa:
            new, reason = fa, "artist from file name"
        if f == "title" and not new and ft:
            new, reason = ft, "title from file name"
        if new != old and new:
            out[f] = (new, reason)
    return out


@dataclass
class CleanRow:
    index: int
    path: Optional[str]
    current: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)   # field -> (new, reason)
    selected: set = field(default_factory=set)    # fields the user wants applied
    action: str = ""
    detail: str = ""
    result: str = ""


def read_fields(path: Path) -> tuple[dict[str, str], Optional[str]]:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return {f: "" for f in FIELDS}, None
    except Exception as e:
        return {}, f"cannot read tags: {e}"
    out = {}
    for f, frame in FIELDS.items():
        v = tags.get(frame)
        out[f] = str(v.text[0]).strip() if v and v.text else ""
    return out, None


def write_fields(path: Path, values: dict[str, str]) -> None:
    from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TIT2, TPE1
    cls = {"artist": TPE1, "title": TIT2, "album": TALB}
    try:
        tags = ID3(str(path))
        version = tags.version[1] if tags.version[1] in (3, 4) else 3
    except ID3NoHeaderError:
        tags, version = ID3(), 3
    for f, v in values.items():
        tags.setall(FIELDS[f], [cls[f](encoding=3 if version == 4 else 1, text=[v])])
    tags.save(str(path), v2_version=version)


def plan(entries: list[dict], progress=None) -> list[CleanRow]:
    rows, seen = [], set()
    for i, e in enumerate(entries, 1):
        if progress:
            progress(i - 1, len(entries), f"{e.get('Artist', '')} - {e.get('Name', '')}")
        r = CleanRow(i, bpm_tool.resolve_location(e.get("Location", "")))
        rows.append(r)
        if not r.path:
            r.action, r.detail = "SKIP", "location cannot be resolved"
            continue
        key = bpm_tool.path_key(r.path)
        if key in seen:
            r.action, r.detail = "SKIP", "duplicate playlist row"
            continue
        seen.add(key)
        p = Path(r.path)
        if p.suffix.lower() != ".mp3":
            r.action, r.detail = "SKIP", "not an MP3 file"
        elif not p.is_file():
            r.action, r.detail = "SKIP", "file missing on disk"
        else:
            r.current, err = read_fields(p)
            if err:
                r.action, r.detail = "SKIP", err
                continue
            r.changes = propose(r.current, p.stem)
            r.selected = set(r.changes)
            r.action, r.detail = ("UPDATE", "") if r.changes else ("SKIP", "nothing to clean")
    return rows


def apply_writes(rows: list[CleanRow], backup_dir: Path) -> tuple[int, int]:
    def writer(r):
        write_fields(Path(r.path), {f: r.changes[f][0] for f in r.selected})

    def verify(r):
        back, err = read_fields(Path(r.path))
        if err:
            return err
        bad = [f for f in r.selected if back.get(f) != r.changes[f][0]]
        return f"verification failed for {', '.join(bad)}" if bad else None
    return tagio.apply_with_backup(rows, backup_dir, lambda r: r.action == "UPDATE" and r.selected, writer, verify,
                                   lambda r: ", ".join(sorted(r.selected)))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Optional artist/title/album cleanup (dry run unless --write).")
    ap.add_argument("playlist", type=Path)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--backup-dir", type=Path)
    a = ap.parse_args(argv)
    if not a.playlist.is_file():
        sys.exit(f"Playlist not found: {a.playlist}")
    _, entries = bpm_tool.parse_playlist(a.playlist)
    rows = plan(entries)
    n = 0
    for r in rows:
        for f, (new, why) in r.changes.items():
            n += 1
            print(f"{r.index:>3} {f:<6} {r.current[f]!r} -> {new!r}   [{why}]")
    print(f"\n{n} change(s) in {sum(1 for r in rows if r.changes)} file(s).")
    if a.write:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ok, failed = apply_writes(rows, a.backup_dir or a.playlist.with_name(f"{a.playlist.stem}_cleanup_backup_{stamp}"))
        print(f"Done. Updated: {ok}  Failed: {failed}")
        return 1 if failed else 0
    print("DRY RUN: nothing modified. Re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
