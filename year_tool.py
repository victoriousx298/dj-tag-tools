#!/usr/bin/env python3
"""Fill the year tag (TDRC / TYER) of MP3s in a playlist.

Rule: a year is written ONLY if the file has no year, or its year is 1970 (the
epoch placeholder; cleared if MusicBrainz has no year). Any other existing year is never changed.

Year source: always MusicBrainz (never Rekordbox): the earliest first-release date among
recordings with the same artist and title (exact title only; same-name artists are ignored
as ambiguous).
Dry run by default; --write backs up each file first.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import genre_suggest as genre_tool
import rekordbox_bpm_sync as bpm_tool
import tagio


@dataclass
class YearRow:
    index: int
    path: Optional[str]
    artist: str
    title: str
    existing: Optional[str] = None   # year text as found in the tag
    proposed: Optional[int] = None
    source: str = ""                 # Rekordbox | MusicBrainz
    action: str = ""                 # WRITE | SKIP
    detail: str = ""
    result: str = ""


def parse_existing_year(text: Optional[str]) -> Optional[int]:
    try:
        y = int(str(text).strip()[:4])
    except (TypeError, ValueError):
        return None
    return y if y > 0 else None


def year_replaceable(existing: Optional[str]) -> bool:
    """Empty/invalid or 1970 -> may be filled. Anything else is protected."""
    y = parse_existing_year(existing)
    return y is None or y == 1970


def read_year(path: Path) -> tuple[Optional[str], Optional[str]]:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return None, None
    except Exception as e:
        return None, f"cannot read tags: {e}"
    f = tags.get("TDRC")
    text = str(f.text[0]).strip() if f and f.text else ""
    return (text or None), None


def write_year(path: Path, year: Optional[int]) -> None:
    """Write `year`; None removes the year tag (only allowed for empty/1970)."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TDRC
    try:
        tags = ID3(str(path))
        version = tags.version[1] if tags.version[1] in (3, 4) else 3
    except ID3NoHeaderError:
        tags, version = ID3(), 3
    f = tags.get("TDRC")
    if year is None:
        if not f:
            return
        tags.delall("TDRC")
    else:
        tags.setall("TDRC", [TDRC(encoding=3 if version == 4 else 1, text=[str(year)])])
    tags.save(str(path), v2_version=version)


def musicbrainz_year(mb, artist: str, title: str) -> tuple[Optional[int], str]:
    """(year, detail). Exact title and artist match only."""
    a, t = genre_tool.primary_artist(artist), genre_tool.clean_title(title)
    if not a or not t:
        return None, "no artist/title"
    years, ids = [], set()
    for rec in mb.search_recordings(a, t):
        if rec.get("score", 0) < 90 or genre_tool.norm(rec.get("title", "")) != genre_tool.norm(t) \
                or not genre_tool._artist_matches(rec, a):
            continue
        for c in rec.get("artist-credit", []):
            if isinstance(c, dict) and genre_tool.norm(a) in genre_tool.norm(c.get("name", "")) and c.get("artist", {}).get("id"):
                ids.add(c["artist"]["id"])
        y = bpm_tool.parse_year((rec.get("first-release-date") or "")[:4])
        if y:
            years.append(y)
    if len(ids) > 1:
        return None, "several artists share this name"
    if not years:
        return None, "no exact MusicBrainz match with a date"
    return min(years), "MusicBrainz"  # earliest = original release; later dates are re-releases


def plan(entries: list[dict], bpm_rows: Optional[list], mb, progress=None) -> list[YearRow]:
    rows, seen = [], set()
    for i, e in enumerate(entries, 1):
        if progress:
            progress(i - 1, len(entries), f"{e.get('Artist', '')} - {e.get('Name', '')}")
        loc = e.get("Location", "")
        b = bpm_rows[i - 1] if bpm_rows else None
        r = YearRow(i, bpm_tool.resolve_location(loc), (b.artist if b and b.match == "matched" else e.get("Artist", "")),
                    (b.title if b and b.match == "matched" else e.get("Name", "")))
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
            r.existing, err = read_year(p)
            if err:
                r.action, r.detail = "SKIP", err
            elif not year_replaceable(r.existing):
                r.action, r.detail = "SKIP", "year already set (only empty or 1970 is replaced)"
                try:  # shown for comparison only; never written because action stays SKIP
                    r.proposed, _ = musicbrainz_year(mb, r.artist, r.title)
                    r.source = "MusicBrainz" if r.proposed else ""
                except Exception:
                    r.proposed = None
            else:
                try:
                    r.proposed, d = musicbrainz_year(mb, r.artist, r.title)
                except Exception as ex:
                    r.proposed, d = None, f"lookup failed: {ex}"
                r.source, r.detail = ("MusicBrainz", "") if r.proposed else ("", d)
                if r.proposed:
                    r.action = "WRITE"
                    r.detail = "replace 1970" if parse_existing_year(r.existing) == 1970 else "fill empty year"
                elif parse_existing_year(r.existing) == 1970 and not d.startswith("lookup failed"):
                    r.action, r.detail = "WRITE", "clear 1970 (no MusicBrainz year)"
                else:
                    r.action = r.action or "SKIP"
                    r.detail = r.detail or "no year found"
    return rows


def apply_writes(rows: list[YearRow], backup_dir: Path) -> tuple[int, int]:
    def verify(r):
        back, err = read_year(Path(r.path))
        ok = (back is None) if r.proposed is None else (back and parse_existing_year(back) == r.proposed)
        return err or (None if ok else f"verification failed (read back {back!r})")
    return tagio.apply_with_backup(rows, backup_dir, lambda r: r.action == "WRITE",
                                   lambda r: write_year(Path(r.path), r.proposed), verify, lambda r: str(r.proposed) if r.proposed else "")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fill empty or 1970 year tags from MusicBrainz.")
    ap.add_argument("playlist", type=Path)
    ap.add_argument("--write", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--backup-dir", type=Path)
    a = ap.parse_args(argv)
    if not a.playlist.is_file():
        sys.exit(f"Playlist not found: {a.playlist}")
    _, entries = bpm_tool.parse_playlist(a.playlist)
    mb = genre_tool.MusicBrainz(Path(__file__).resolve().parent / ".genre_cache.sqlite")
    rows = plan(entries, None, mb)
    print(f"{'#':>3}  {'Artist':<22} {'Title':<32} {'Existing':<10} {'Year':<6} {'Source':<12} Action")
    for r in rows:
        if r.action == "WRITE" or a.write is False and r.detail.startswith(("year already", "no ")):
            print(f"{r.index:>3}  {r.artist[:22]:<22} {r.title[:32]:<32} {(r.existing or '-'):<10} {r.proposed or '-':<6} "
                  f"{r.source or '-':<12} {r.action} {r.detail}")
    print(f"\nWould write {sum(1 for r in rows if r.action == 'WRITE')} year tag(s).")
    if a.write:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ok, failed = apply_writes(rows, a.backup_dir or a.playlist.with_name(f"{a.playlist.stem}_year_backup_{stamp}"))
        print(f"Done. Updated: {ok}  Failed: {failed}")
        return 1 if failed else 0
    print("DRY RUN: nothing modified. Re-run with --write to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
