#!/usr/bin/env python3
"""Copy Rekordbox-analysed BPM values into the TBPM ID3 tag of MP3 files.

Input : an iTunes-exported playlist TXT (tab separated, has a "Location" column).
Source: Rekordbox's master.db (read from a private temporary COPY, never the
        original) or, optionally, a Rekordbox XML export (--xml).
Output: dry-run preview by default; MP3 tags are only touched with --write.

Files are matched by normalised absolute path only. No metadata guessing.
"""
from __future__ import annotations

import contextlib
import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

import settings
DEFAULT_DB = settings.rekordbox_db()  # from settings.json (setup page) or the usual Rekordbox location
BOOT_VOLUME_NAMES = {"Macintosh HD"}


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------

def path_key(path: str) -> str:
    """Comparison key: NFC-normalised, case-folded absolute path.

    macOS filesystems are normally case-insensitive and mix NFC/NFD, so both
    are neutralised. A key shared by several different library entries is
    treated as ambiguous by the matcher, never guessed.
    """
    p = unicodedata.normalize("NFC", path).replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/")
    return p.casefold()


def resolve_location(location: str) -> Optional[str]:
    """Convert an iTunes/Rekordbox location into an absolute macOS path.

    Handles: file:// URLs (percent-decoded), "Macintosh HD/Users/..." style
    paths, "/Volumes/..." and plain absolute paths. Plain paths are NOT
    percent-decoded, because '%' can legitimately appear in file names.
    Returns None if the location is empty or cannot be made absolute.
    """
    loc = (location or "").strip()
    if not loc:
        return None
    if loc.lower().startswith("file:"):
        parsed = urlparse(loc)
        if parsed.netloc not in ("", "localhost"):
            return None  # remote host: not a local file
        path = unquote(parsed.path)
        return unicodedata.normalize("NFC", path) if path.startswith("/") else None
    loc = loc.replace("\\", "/") if "/" not in loc else loc
    if loc.startswith("/"):
        return unicodedata.normalize("NFC", loc)
    # "<Volume>/rest/of/path"
    volume, _, rest = loc.partition("/")
    if not rest:
        return None
    if volume in BOOT_VOLUME_NAMES or _is_boot_volume(volume):
        return unicodedata.normalize("NFC", "/" + rest)
    return unicodedata.normalize("NFC", f"/Volumes/{volume}/{rest}")


def _is_boot_volume(name: str) -> bool:
    try:
        return os.path.realpath(f"/Volumes/{name}") == "/"
    except OSError:
        return False


# --------------------------------------------------------------------------
# Playlist TXT
# --------------------------------------------------------------------------

def read_text_any_encoding(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-16") if b"\x00" in raw[:64] else raw.decode("latin-1")
    # iTunes on macOS exports CR-only line endings.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_playlist(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = read_text_any_encoding(path)
    reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("playlist file is empty")
    header = [h.strip() for h in rows[0]]
    if "Location" not in header:
        raise ValueError(f"no 'Location' column; columns found: {header}")
    entries = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        entries.append(dict(zip(header, r)))
    return header, entries


# --------------------------------------------------------------------------
# Rekordbox sources
# --------------------------------------------------------------------------

@dataclass
class RbTrack:
    track_id: str
    location: str
    bpm_raw: Optional[float]  # BPM as real number, None when missing/invalid
    artist: str
    title: str
    year: Optional[int] = None


def parse_bpm(value) -> Optional[float]:
    """Return a positive BPM float or None."""
    if value is None:
        return None
    try:
        f = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return f


def parse_year(value) -> Optional[int]:
    """A plausible release year (1900..next year) or None. 0 and 1970 (epoch default) mean 'unknown'."""
    try:
        y = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= datetime.now().year + 1 and y != 1970 else None


def load_from_xml(xml_path: Path, wanted: set[str]) -> dict[str, list[RbTrack]]:
    """Stream a Rekordbox XML export; keep only tracks whose path is wanted.

    Structure (documented Rekordbox XML format): DJ_PLAYLISTS/COLLECTION/TRACK
    with attributes TrackID, Name, Artist, AverageBpm, Location
    (file://localhost/...). Tag names are matched without namespace.
    """
    found: dict[str, list[RbTrack]] = {}
    for _, el in ET.iterparse(str(xml_path), events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "TRACK" or "Location" not in el.attrib:
            continue
        loc = resolve_location(el.attrib.get("Location", ""))
        if loc:
            key = path_key(loc)
            if key in wanted:
                found.setdefault(key, []).append(RbTrack(
                    el.attrib.get("TrackID", ""), loc,
                    parse_bpm(el.attrib.get("AverageBpm")),
                    el.attrib.get("Artist", ""), el.attrib.get("Name", ""),
                    parse_year(el.attrib.get("Year"))))
        el.clear()
    return found


def load_from_master_db(db_path: Path, wanted: set[str]) -> dict[str, list[RbTrack]]:
    """Read master.db through a temporary copy (original is never opened)."""
    try:
        from pyrekordbox import MasterDatabase
    except ImportError:
        sys.exit("pyrekordbox is required to read master.db: pip install -r requirements.txt")
    if not db_path.is_file():
        sys.exit(f"Rekordbox database not found: {db_path}")
    found: dict[str, list[RbTrack]] = {}
    with tempfile.TemporaryDirectory(prefix="rbcopy_") as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(tmp) / (db_path.name + suffix))
        db = MasterDatabase(path=str(Path(tmp) / db_path.name))
        try:
            for c in db.get_content():
                loc = resolve_location(c.FolderPath or "")
                if not loc:
                    continue
                key = path_key(loc)
                if key in wanted:
                    # DjmdContent.BPM is stored as integer BPM * 100.
                    bpm = parse_bpm(c.BPM / 100.0) if c.BPM else None
                    found.setdefault(key, []).append(RbTrack(
                        str(c.ID), loc, bpm, c.ArtistName or "", c.Title or "",
                        parse_year(getattr(c, "ReleaseYear", None))))
        finally:
            try:
                db.close()
            except Exception:
                pass
    return found


def _open_db_copy(db_path: Path, tmp: str):
    try:
        from pyrekordbox import MasterDatabase
    except ImportError:
        sys.exit("pyrekordbox is required to read master.db: pip install -r requirements.txt")
    if not db_path.is_file():
        sys.exit(f"Rekordbox database not found: {db_path}")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.is_file():
            shutil.copy2(src, Path(tmp) / (db_path.name + suffix))
    return MasterDatabase(path=str(Path(tmp) / db_path.name))


def list_rekordbox_playlists(db_path: Path) -> list[dict]:
    """Normal playlists (not folders or smart playlists) with their folder path and track count."""
    with tempfile.TemporaryDirectory(prefix="rbcopy_") as tmp:
        db = _open_db_copy(db_path, tmp)
        try:
            pls = db.get_playlist().all()
            by_id = {p.ID: p for p in pls}
            out = []
            for p in pls:
                if p.Attribute != 0:
                    continue
                parts, cur = [p.Name], p
                while cur.ParentID in by_id:
                    cur = by_id[cur.ParentID]
                    parts.append(cur.Name)
                out.append({"id": str(p.ID), "name": p.Name, "path": " / ".join(reversed(parts)),
                            "count": len(p.Songs), "seq": p.Seq})
            return sorted(out, key=lambda x: x["path"].lower())
        finally:
            try:
                db.close()
            except Exception:
                pass


def rekordbox_playlist_entries(db_path: Path, playlist_id: str) -> list[dict[str, str]]:
    """Entries shaped like iTunes TXT rows (Name/Artist/Location), in playlist order."""
    with tempfile.TemporaryDirectory(prefix="rbcopy_") as tmp:
        db = _open_db_copy(db_path, tmp)
        try:
            pl = next((p for p in db.get_playlist().all() if str(p.ID) == str(playlist_id) and p.Attribute == 0), None)
            if pl is None:
                sys.exit("Rekordbox playlist not found (smart playlists and folders are not supported)")
            out = []
            for song in sorted(pl.Songs, key=lambda s: s.TrackNo or 0):
                c = song.Content
                if c is not None:
                    out.append({"Name": c.Title or "", "Artist": c.ArtistName or "", "Location": c.FolderPath or ""})
            return out
        finally:
            try:
                db.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# BPM / tag helpers
# --------------------------------------------------------------------------

def format_bpm(bpm: float, round_int: bool) -> str:
    """TBPM text, rounded to 1 decimal. Whole numbers -> '128'; else '109.97' -> '110', '140.46' -> '140.5'."""
    if round_int:
        return str(int(round(bpm)))
    r = round(bpm, 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}"


def read_existing_bpm(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (existing TBPM text or None, error or None)."""
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return None, None
    except Exception as e:  # unreadable/corrupt tag
        return None, f"cannot read tags: {e}"
    frame = tags.get("TBPM")
    text = str(frame.text[0]).strip() if frame and frame.text else ""
    return (text or None), None


def existing_counts_as_set(text: Optional[str]) -> bool:
    if not text:
        return False
    f = parse_bpm(text)
    return f is not None  # "0" or garbage counts as no usable BPM


def write_bpm(path: Path, text: str) -> None:
    """Set TBPM, keep every other frame, keep the tag's ID3v2 version."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TBPM
    try:
        tags = ID3(str(path))
        version = tags.version[1] if tags.version[1] in (3, 4) else 3
    except ID3NoHeaderError:
        tags = ID3()
        version = 3
    tags.setall("TBPM", [TBPM(encoding=3 if version == 4 else 1, text=[text])])
    tags.save(str(path), v2_version=version)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

@dataclass
class Row:
    index: int
    location: str
    path: Optional[str] = None
    artist: str = ""
    title: str = ""
    track_id: str = ""
    match: str = ""        # matched | unmatched | ambiguous | invalid location
    rb_bpm: Optional[float] = None
    bpm_text: str = ""     # proposed TBPM text
    existing: Optional[str] = None
    rb_year: Optional[int] = None
    action: str = ""
    detail: str = ""
    result: str = ""       # filled in with --write
    flags: set = field(default_factory=set)


def plan(entries: list[dict[str, str]], args, loader) -> list[Row]:
    rows: list[Row] = []
    for i, e in enumerate(entries, 1):
        r = Row(i, e.get("Location", ""), artist=e.get("Artist", ""), title=e.get("Name", ""))
        r.path = resolve_location(r.location)
        rows.append(r)
    wanted = {path_key(r.path) for r in rows if r.path}
    lib = loader(wanted)
    seen: dict[str, int] = {}

    for r in rows:
        if not r.path:
            r.match, r.action, r.detail = "invalid location", "SKIP", "location cannot be resolved to a local path"
            continue
        key = path_key(r.path)
        if key in seen:
            r.match, r.action, r.detail = "duplicate", "SKIP", f"same file as playlist row {seen[key]}"
            continue
        seen[key] = r.index
        hits = lib.get(key, [])
        if not hits:
            r.match, r.action, r.detail = "unmatched", "SKIP", "path not found in Rekordbox library"
            continue
        if len(hits) > 1:
            r.match, r.action = "ambiguous", "SKIP"
            r.detail = f"{len(hits)} Rekordbox tracks share this path"
            continue
        t = hits[0]
        r.match, r.track_id = "matched", t.track_id
        r.artist, r.title = t.artist or r.artist, t.title or r.title
        r.rb_bpm = t.bpm_raw
        r.rb_year = t.year
        if r.rb_bpm is None:
            r.action, r.detail = "SKIP", "Rekordbox BPM missing, zero or invalid"
            continue
        r.bpm_text = format_bpm(r.rb_bpm, args.round)
        p = Path(r.path)
        if p.suffix.lower() != ".mp3":
            r.action, r.detail = "SKIP", "not an MP3 file"
            continue
        if not p.is_file():
            r.action, r.detail = "SKIP", "file missing on disk"
            continue
        r.existing, err = read_existing_bpm(p)
        if err:
            r.action, r.detail = "SKIP", err
            continue
        has = existing_counts_as_set(r.existing)
        if has:
            r.flags.add("has_bpm")
        if has and not args.overwrite:
            r.action, r.detail = "SKIP", "existing BPM tag (use --overwrite)"
        elif has and parse_bpm(r.existing) == parse_bpm(r.bpm_text):
            r.action, r.detail = "SKIP", "existing BPM already identical"
        else:
            r.action = "UPDATE"
            r.detail = "overwrite existing" if has else "write new"
    return rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def print_summary(rows: list[Row]) -> None:
    matched = [r for r in rows if r.match == "matched"]
    print(f"Playlist entries            : {len(rows)}")
    print(f"Exact path matches          : {len(matched)}")
    print(f"Unmatched / invalid entries : {len(rows) - len(matched)}")
    print(f"Matched with valid BPM      : {sum(1 for r in matched if r.rb_bpm is not None)}")
    print(f"Files already having a BPM  : {sum(1 for r in rows if 'has_bpm' in r.flags)}")
    print(f"Files that would be updated : {sum(1 for r in rows if r.action == 'UPDATE')}")


def print_table(rows: list[Row], verbose: bool) -> None:
    shown = rows if verbose else [r for r in rows if r.match == "matched"]
    print()
    hdr = f"{'#':>3}  {'Artist':<22} {'Title':<34} {'RB BPM':>7} {'MP3 BPM':>8}  {'Action':<7} Detail"
    print(hdr)
    print("-" * len(hdr))
    for r in shown:
        rb = r.bpm_text or "-"
        print(f"{r.index:>3}  {clip(r.artist,22):<22} {clip(r.title,34):<34} {rb:>7} "
              f"{(r.existing or '-'):>8}  {r.action:<7} {r.detail}")
        if verbose:
            print(f"       path: {r.path}  [{r.match}]")
    if not verbose:
        others = len(rows) - len(shown)
        if others:
            print(f"\n({others} unmatched/invalid entries hidden; use --verbose or --report to list them)")


def write_report(rows: list[Row], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "location", "resolved_path", "match_status", "rekordbox_track_id",
                    "artist", "title", "rekordbox_bpm", "existing_mp3_bpm", "action", "detail", "result"])
        for r in rows:
            w.writerow([r.index, r.location, r.path or "", r.match, r.track_id, r.artist, r.title,
                        r.bpm_text, r.existing or "", r.action, r.detail, r.result])
    print(f"Report written: {path}")


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def make_backup_dir(base: Path) -> Path:
    """Create a new backup dir; never reuse an existing one."""
    n = 0
    while True:
        d = base if n == 0 else base.with_name(f"{base.name}_{n}")
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            n += 1


def apply_writes(rows: list[Row], backup_dir: Optional[Path]) -> tuple[int, int]:
    todo = [r for r in rows if r.action == "UPDATE"]
    if not todo:
        print("\nNothing to write.")
        return 0, 0
    if backup_dir is not None:
        backup_dir = make_backup_dir(backup_dir)
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
                    if dst.exists():
                        raise FileExistsError(f"backup already exists: {dst}")
                    shutil.copy2(src, dst)
                    if dst.stat().st_size != src.stat().st_size:
                        raise OSError("backup size mismatch; file not modified")
                    mw.writerow([str(dst), str(src)])
                    mf.flush()
                write_bpm(src, r.bpm_text)
                back, err = read_existing_bpm(src)
                if err or back is None or parse_bpm(back) != parse_bpm(r.bpm_text):
                    raise OSError(f"verification failed (read back {back!r}, expected {r.bpm_text!r})")
                r.result = "ok"
                ok += 1
                print(f"  ok     {src.name}  ->  {r.bpm_text}")
            except Exception as e:
                r.result = f"FAILED: {e}"
                failed += 1
                print(f"  FAILED {src.name}: {e}", file=sys.stderr)
    return ok, failed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("playlist", type=Path, help="iTunes playlist export (.txt)")
    p.add_argument("--write", action="store_true", help="actually write TBPM tags (default: dry run)")
    p.add_argument("--overwrite", action="store_true", help="also replace existing BPM tags")
    p.add_argument("--round", action="store_true", help="write whole-number BPM instead of 1 decimal")
    p.add_argument("--report", nargs="?", const="", metavar="CSV",
                   help="save CSV report (default name: <playlist>_bpm_report.csv next to the playlist)")
    p.add_argument("--backup-dir", type=Path, help="backup directory (default: next to the playlist, timestamped)")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"Rekordbox master.db (default: {DEFAULT_DB})")
    p.add_argument("--xml", type=Path, help="read a Rekordbox XML export instead of master.db")
    p.add_argument("--verbose", action="store_true", help="show every playlist row with its resolved path")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.playlist.is_file():
        sys.exit(f"Playlist not found: {args.playlist}")
    try:
        _, entries = parse_playlist(args.playlist)
    except ValueError as e:
        sys.exit(f"Playlist error: {e}")

    if args.xml:
        if not args.xml.is_file():
            sys.exit(f"XML not found: {args.xml}")
        loader = lambda wanted: load_from_xml(args.xml, wanted)
        print(f"Source: Rekordbox XML {args.xml}")
    else:
        loader = lambda wanted: load_from_master_db(args.db, wanted)
        print(f"Source: Rekordbox database {args.db} (read from a temporary copy)")

    rows = plan(entries, args, loader)
    print_summary(rows)
    print_table(rows, args.verbose)

    if args.report is not None:
        rp = Path(args.report) if args.report else args.playlist.with_name(args.playlist.stem + "_bpm_report.csv")

    ok = failed = 0
    if args.write:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = args.backup_dir or args.playlist.with_name(f"{args.playlist.stem}_bpm_backup_{stamp}")
        ok, failed = apply_writes(rows, bdir)
        print(f"\nDone. Updated: {ok}   Failed: {failed}   Skipped: {sum(1 for r in rows if r.action != 'UPDATE')}")
    else:
        print("\nDRY RUN: no files were modified. Re-run with --write to apply the updates above.")

    if args.report is not None:
        write_report(rows, rp)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
