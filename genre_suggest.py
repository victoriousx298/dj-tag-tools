#!/usr/bin/env python3
"""Suggest genres for the MP3s in an iTunes playlist TXT and write them to TCON.

- Source: MusicBrainz public API (no key). Only names on MusicBrainz's official
  genre list are used, exactly as the source returns them (capitalised).
- At most 5 genres per track; all HIGH confidence genres are written; with none, the top MEDIUM one.
- A genre tag that already has any content is NEVER replaced: such files are skipped.
- Dry run by default; MP3s are only changed with --write-genre (after a backup).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import rekordbox_bpm_sync as bpm_tool

MAX_GENRES = 5
MB = "https://musicbrainz.org/ws/2"
MIN_INTERVAL = 1.1  # seconds between uncached requests (MusicBrainz: ~1 req/s)
SMALL_WORDS = {"and", "of", "the", "in", "on", "for", "to"}
LEVELS = ["low", "medium", "high"]


# --------------------------------------------------------------------------
# Title / artist cleaning
# --------------------------------------------------------------------------

_NOISE = re.compile(
    r"\s*[\(\[]\s*(clean|dirty|extended( mix)?|radio edit|short|intro|outro|\d+\s*k?bps|\d+\s*kbit[^)\]]*|"
    r"(?:hh|dj|ds)[^)\]]*(?:clean|dirty|intro|short)[^)\]]*|spotisaver|[^)\]]*acapella[^)\]]*)\s*[\)\]]",
    re.I)
_PREFIX = re.compile(r"^(spotidownloader\.com|spotisaver[^-]*)\s*-\s*", re.I)


def clean_title(title: str) -> str:
    t = unicodedata.normalize("NFC", title or "").strip()
    t = _PREFIX.sub("", t)
    prev = None
    while prev != t:
        prev, t = t, _NOISE.sub("", t).strip()
    return re.sub(r"\s{2,}", " ", t)


def base_title(title: str) -> str:
    """Title without any bracketed remix/edit info and feat. credits."""
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title)
    t = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", t, flags=re.I)
    return t.strip()


def primary_artist(artist: str) -> str:
    a = re.split(r",|\s+(?:&|x|ft\.?|feat\.?|featuring|vs\.?|and)\s+", artist or "", maxsplit=1, flags=re.I)[0]
    return a.strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


ACRONYMS = {"edm", "r&b", "uk", "us", "idm", "ebm", "rnb", "k-pop", "j-pop"}


def pretty_genre(name: str) -> str:
    out = []
    for i, w in enumerate(name.split()):
        if w != w.lower():
            out.append(w)
        elif w in ACRONYMS:
            out.append("-".join(x.upper() if len(x) == 1 else x.capitalize() for x in w.split("-")) if "-" in w else w.upper())
        elif w in SMALL_WORDS and i > 0:
            out.append(w)
        else:
            out.append("-".join(x[:1].upper() + x[1:] for x in w.split("-")))
    return " ".join(out)


# --------------------------------------------------------------------------
# MusicBrainz client with on-disk cache
# --------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """python.org builds on macOS ship without CA certs; use certifi's bundle when present."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class MusicBrainz:
    def __init__(self, cache_path: Path, contact: str = "", verbose: bool = False):
        self.db = sqlite3.connect(str(cache_path))
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, body TEXT, fetched TEXT)")
        ua = "DJTagTools/0.1"
        self.ua = f"{ua} ( {contact} )" if contact else f"{ua} (personal-use script)"
        self.verbose = verbose
        self._last = 0.0
        self.requests = 0
        self._genres: Optional[set[str]] = None

    def _get(self, path: str, params: dict, text: bool = False):
        url = f"{MB}/{path}?" + urllib.parse.urlencode(params)
        row = self.db.execute("SELECT body FROM cache WHERE url=?", (url,)).fetchone()
        if row:
            body = row[0]
        else:
            body = None
            for attempt in range(4):
                wait = MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
                req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
                        body = r.read().decode("utf-8")
                    self.requests += 1
                    break
                except urllib.error.HTTPError as e:
                    if e.code in (429, 503) and attempt < 3:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raise
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (url, body, datetime.now().isoformat()))
            self.db.commit()
        return body if text else json.loads(body)

    @property
    def genre_names(self) -> set[str]:
        """Official MusicBrainz genre list (lower-case); anything else is a free-text tag."""
        if self._genres is None:
            body = self._get("genre/all", {"fmt": "txt"}, text=True)
            self._genres = {g.strip().lower() for g in body.splitlines() if g.strip()}
        return self._genres

    def search_recordings(self, artist: str, title: str) -> list[dict]:
        q = f'artist:"{artist}" AND recording:"{title}"'.replace("\\", "")
        return self._get("recording", {"query": q, "fmt": "json", "limit": 8}).get("recordings", [])

    def artist_genres(self, name: str) -> dict[str, int]:
        res = self._get("artist", {"query": f'artist:"{name}"', "fmt": "json", "limit": 3}).get("artists", [])
        exact = [a for a in res if a.get("score", 0) >= 95 and norm(a.get("name", "")) == norm(name)]
        if len(exact) > 1:
            return {}  # several artists with this exact name: cannot tell which one
        if not exact:
            return {}
        full = self._get(f"artist/{exact[0]['id']}", {"inc": "genres+tags", "fmt": "json"})
        votes: dict[str, int] = {}
        for item in (full.get("genres") or []) + (full.get("tags") or []):
            n = item["name"].lower()
            if n in self.genre_names:
                votes[n] = max(votes.get(n, 0), item.get("count", 0))
        return votes


# --------------------------------------------------------------------------
# Suggestion logic
# --------------------------------------------------------------------------

@dataclass
class Suggestion:
    genre: str
    confidence: str      # high | medium | low
    votes: int
    basis: str           # recording | recording(base title) | artist


def _artist_matches(rec: dict, artist: str) -> bool:
    names = " ".join(norm(c.get("name", "")) for c in rec.get("artist-credit", []) if isinstance(c, dict))
    return bool(norm(artist)) and norm(artist) in names


def recording_votes(mb: MusicBrainz, artist: str, title: str) -> tuple[dict[str, int], bool]:
    """Return (votes, ambiguous). Ambiguous = matching recordings belong to several different
    MusicBrainz artists that share the name (e.g. two artists called "Eve")."""
    votes: dict[str, int] = {}
    artist_ids: set[str] = set()
    for rec in mb.search_recordings(artist, title):
        if rec.get("score", 0) < 90 or norm(rec.get("title", "")) != norm(title) or not _artist_matches(rec, artist):
            continue
        for c in rec.get("artist-credit", []):
            if isinstance(c, dict) and norm(artist) in norm(c.get("name", "")) and c.get("artist", {}).get("id"):
                artist_ids.add(c["artist"]["id"])
        for item in (rec.get("genres") or []) + (rec.get("tags") or []):
            n = item["name"].lower()
            if n in mb.genre_names:
                votes[n] = votes.get(n, 0) + item.get("count", 0)
    return votes, len(artist_ids) > 1


def grade(votes: int, basis: str, confirmed_by_artist: bool = False) -> str:
    """Confidence rules (documented in README).

    recording: >=3 votes, or >=1 vote also confirmed at artist level -> high; 2 votes -> medium; 1 -> low
    artist:    >=3 votes -> medium; otherwise low (never high: not track-specific)
    base title (remix info dropped): one level lower than a recording match.
    """
    if basis.endswith("ambiguous"):
        return "low"  # several different artists share this name: never trusted
    if basis.startswith("recording"):
        lvl = 2 if (votes >= 3 or (votes >= 1 and confirmed_by_artist)) else 1 if votes == 2 else 0
        if basis != "recording":
            lvl = max(lvl - 1, 0)
    else:
        lvl = 1 if votes >= 3 else 0
    return LEVELS[lvl]


def suggest(mb: MusicBrainz, artist: str, title: str) -> list[Suggestion]:
    artist1 = primary_artist(artist)
    full = clean_title(title)
    if not artist1 or not full:
        return []
    basis = "recording"
    rec, amb = recording_votes(mb, artist1, full)
    if not rec:
        b = base_title(full)
        if b and norm(b) != norm(full):
            rec, amb = recording_votes(mb, artist1, b)
            basis = "recording(base title)"
    if amb:
        basis += " ambiguous"
    art = mb.artist_genres(artist1)
    out: dict[str, Suggestion] = {}
    for g, v in rec.items():
        out[g] = Suggestion(g, grade(v, basis, art.get(g, 0) > 0), v, basis)
    for g, v in art.items():
        if g not in out:
            out[g] = Suggestion(g, grade(v, "artist"), v, "artist")
    ranked = sorted(out.values(), key=lambda s: (-LEVELS.index(s.confidence), -s.votes, s.genre))
    return ranked


def selected(sugs: list[Suggestion]) -> list[Suggestion]:
    """Genres written by default: all HIGH confidence ones (max 5); if there are none,
    the single best MEDIUM confidence genre."""
    high = [s for s in sugs if s.confidence == "high"][:MAX_GENRES]
    if high:
        return high
    return [s for s in sugs if s.confidence == "medium"][:1]


def medium_candidates(sugs: list[Suggestion]) -> list[Suggestion]:
    """All medium-confidence genres: offered as options (the best is pre-selected only when there is no high one)."""
    return [s for s in sugs if s.confidence == "medium"]


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------

def read_genre(path: Path) -> tuple[Optional[str], Optional[str]]:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        return None, None
    except Exception as e:
        return None, f"cannot read tags: {e}"
    f = tags.get("TCON")
    # iTunes/Rekordbox store several genres comma-separated (possibly with or without a space)
    parts = [p.strip() for t in (f.text if f else []) for p in str(t).split(",")]
    text = ", ".join(p for p in parts if p)
    return (text or None), None


def write_genre(path: Path, text: str) -> None:
    """Set TCON only; keep all other frames and the ID3v2 version."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TCON
    try:
        tags = ID3(str(path))
        version = tags.version[1] if tags.version[1] in (3, 4) else 3
    except ID3NoHeaderError:
        tags, version = ID3(), 3
    tags.setall("TCON", [TCON(encoding=3 if version == 4 else 1, text=[text])])
    tags.save(str(path), v2_version=version)


# --------------------------------------------------------------------------
# Planning / output
# --------------------------------------------------------------------------

@dataclass
class Row:
    index: int
    location: str
    path: Optional[str]
    artist: str
    title: str
    existing: Optional[str] = None
    suggestions: list[Suggestion] = field(default_factory=list)
    chosen: list[Suggestion] = field(default_factory=list)      # pre-selected: high, else the top medium
    candidates: list[Suggestion] = field(default_factory=list)  # all medium confidence genres (opt-in)
    override: Optional[str] = None   # final genre text chosen by the user (web interface)
    action: str = ""   # KEPT (existing tag) | WRITE (high found) | REVIEW (medium only) | NONE (nothing found) | SKIP (not editable)
    detail: str = ""
    result: str = ""

    @property
    def text(self) -> str:
        if self.override is not None:
            return self.override
        return ", ".join(pretty_genre(s.genre) for s in self.chosen)


def plan(entries: list[dict], mb: MusicBrainz, progress=None) -> list[Row]:
    rows, seen = [], set()
    for i, e in enumerate(entries, 1):
        if progress:
            progress(i - 1, len(entries), f"{e.get('Artist', '')} - {e.get('Name', '')}")
        loc = e.get("Location", "")
        r = Row(i, loc, bpm_tool.resolve_location(loc), e.get("Artist", ""), e.get("Name", ""))
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
            r.existing, err = read_genre(p)
            if err:
                r.action, r.detail = "SKIP", err
            else:
                try:
                    r.suggestions = suggest(mb, r.artist, r.title)
                except (urllib.error.URLError, TimeoutError) as ex:
                    r.action, r.detail = "SKIP", f"lookup failed: {ex}"
                    continue
                r.chosen = selected(r.suggestions)
                r.candidates = medium_candidates(r.suggestions)
                if r.existing:
                    # kept unless the user edits it in the web interface
                    r.action, r.detail = "KEPT", "genre tag already set (kept unless you edit it)"
                elif r.chosen:
                    r.action, r.detail = "WRITE", ""
                elif r.candidates:
                    r.action, r.detail = "REVIEW", "only medium-confidence genres: not written automatically"
                else:
                    r.action, r.detail = "NONE", "no suggestion"
    return rows


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def print_results(rows: list[Row], verbose: bool) -> None:
    print(f"Playlist entries             : {len(rows)}")
    print(f"Genre tag already set        : {sum(1 for r in rows if r.existing)}")
    print(f"With high-confidence genres  : {sum(1 for r in rows if any(s.confidence == 'high' for s in r.chosen))}")
    print(f"With only a medium genre     : {sum(1 for r in rows if r.chosen and all(s.confidence == 'medium' for s in r.chosen))}")
    print(f"Files that would be updated  : {sum(1 for r in rows if r.action == 'WRITE')}\n")
    hdr = f"{'#':>3}  {'Artist':<20} {'Title':<28} {'Existing':<12} {'Suggested genres (confidence)':<76} Action"
    print(hdr + "\n" + "-" * len(hdr))
    for r in rows:
        if not verbose and r.detail.startswith(("location", "file missing", "duplicate", "not an MP3")):
            continue
        sug = ", ".join(f"{pretty_genre(s.genre)} ({s.confidence[0].upper()}{s.votes})" for s in r.chosen) or "-"
        maybe = [s for s in r.candidates if s not in r.chosen]
        if maybe:
            sug += (" | maybe: " if r.chosen else "maybe: ") + ", ".join(f"{pretty_genre(s.genre)} (M{s.votes})" for s in maybe)
        act = r.action + (f" - {r.detail}" if r.detail else "")
        print(f"{r.index:>3}  {clip(r.artist,20):<20} {clip(r.title,28):<28} {clip(r.existing or '-',12):<12} "
              f"{clip(sug,76):<76} {act}")
        if verbose:
            low = [s for s in r.suggestions if s.confidence == "low"]
            if low:
                print("       low confidence, ignored: " + ", ".join(f"{s.genre} ({s.confidence}, {s.votes}, {s.basis})" for s in low))
    print("\nLegend: H = high, M = medium (only the top one is written, and only when there is no high one; pick others in the web interface), number = MusicBrainz votes.")


def write_report(rows: list[Row], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "resolved_path", "artist", "title", "existing_genre", "proposed_genres",
                    "all_candidates(genre|confidence|votes|basis)", "action", "detail", "result"])
        for r in rows:
            w.writerow([r.index, r.path or "", r.artist, r.title, r.existing or "", r.text,
                        "; ".join(f"{s.genre}|{s.confidence}|{s.votes}|{s.basis}" for s in r.suggestions),
                        r.action, r.detail, r.result])
    print(f"Report written: {path}")


def apply_writes(rows: list[Row], backup_dir: Path) -> tuple[int, int]:
    import shutil
    todo = [r for r in rows if r.action == "WRITE"]
    if not todo:
        print("\nNothing to write.")
        return 0, 0
    backup_dir = bpm_tool.make_backup_dir(backup_dir)
    print(f"\nBackups go to: {backup_dir}")
    ok = failed = 0
    with (backup_dir / "manifest.csv").open("w", newline="", encoding="utf-8") as mf:
        mw = csv.writer(mf)
        mw.writerow(["backup_file", "original_path"])
        for n, r in enumerate(todo, 1):
            src, dst = Path(r.path), backup_dir / f"{n:04d}_{Path(r.path).name}"
            try:
                shutil.copy2(src, dst)
                if dst.stat().st_size != src.stat().st_size:
                    raise OSError("backup size mismatch; file not modified")
                mw.writerow([str(dst), str(src)]); mf.flush()
                write_genre(src, r.text)
                back, err = read_genre(src)
                if err or back != r.text:
                    raise OSError(f"verification failed (read back {back!r}, expected {r.text!r})")
                r.result = "ok"; ok += 1
                print(f"  ok     {src.name}  ->  {r.text}")
            except Exception as e:
                r.result = f"FAILED: {e}"; failed += 1
                print(f"  FAILED {src.name}: {e}", file=sys.stderr)
    return ok, failed


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Suggest genres (MusicBrainz) and write them into EMPTY MP3 genre tags.")
    p.add_argument("playlist", type=Path)
    p.add_argument("--write-genre", action="store_true", help="actually write TCON (default: dry run)")
    p.add_argument("--report", nargs="?", const="", metavar="CSV", help="save CSV report")
    p.add_argument("--backup-dir", type=Path)
    p.add_argument("--cache", type=Path, default=Path(__file__).resolve().parent / ".genre_cache.sqlite",
                   help="lookup cache (default: next to the script)")
    p.add_argument("--contact", default=os.environ.get("MB_CONTACT", ""),
                   help="contact (URL/email) for the User-Agent, as MusicBrainz asks; or env MB_CONTACT, or the git-ignored file .musicbrainz_contact")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    if not a.playlist.is_file():
        sys.exit(f"Playlist not found: {a.playlist}")
    try:
        _, entries = bpm_tool.parse_playlist(a.playlist)
    except ValueError as e:
        sys.exit(f"Playlist error: {e}")
    contact = a.contact
    cfile = Path(__file__).resolve().parent / ".musicbrainz_contact"  # local, git-ignored
    if not contact and cfile.is_file():
        contact = cfile.read_text().strip()
    mb = MusicBrainz(a.cache, contact, a.verbose)
    print("Source: MusicBrainz (no API key; ~1 request/second, cached)")
    rows = plan(entries, mb)
    print_results(rows, a.verbose)
    ok = failed = 0
    if a.write_genre:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ok, failed = apply_writes(rows, a.backup_dir or a.playlist.with_name(f"{a.playlist.stem}_genre_backup_{stamp}"))
        print(f"\nDone. Updated: {ok}   Failed: {failed}   Skipped: {sum(1 for r in rows if r.action != 'WRITE')}")
    else:
        print("\nDRY RUN: no files were modified. Re-run with --write-genre to apply.")
    if a.report is not None:
        write_report(rows, Path(a.report) if a.report else a.playlist.with_name(a.playlist.stem + "_genre_report.csv"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
