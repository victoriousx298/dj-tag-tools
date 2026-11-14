#!/usr/bin/env python3
"""Local web interface for the BPM, genre, year and tag-cleanup tools.

    python3 webui.py            # starts http://127.0.0.1:8765 and opens it in your browser

A plain .html file opened from disk cannot read or tag files, so this tiny server
(standard library only, bound to 127.0.0.1) serves the page and runs the same code
as the command-line tools. Flow: choose a playlist (iTunes TXT upload or a Rekordbox
playlist) -> preview (nothing is modified) -> adjust -> "Write changes" (backups first).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import backup_cleanup
import convert_web
import genre_suggest as genre_tool
import rekordbox_bpm_sync as bpm_tool
import tag_cleanup as cleanup_tool
import quality_tool
import settings
import setup_web
import year_tool

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"
BACKUP_ROOT = ROOT / "backups"
CACHE = ROOT / ".genre_cache.sqlite"
CONTACT_FILE = ROOT / ".musicbrainz_contact"
MAX_UPLOAD = 20 * 1024 * 1024
MAX_GENRES = genre_tool.MAX_GENRES
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()


def apply_settings() -> None:
    """Make a saved setup take effect without restarting the server."""
    bpm_tool.DEFAULT_DB = settings.rekordbox_db()
    convert_web.DEFAULT_FOLDER = settings.music_folder()


def contact() -> str:
    if os.environ.get("MB_CONTACT"):
        return os.environ["MB_CONTACT"]
    return settings.contact() or (CONTACT_FILE.read_text().strip() if CONTACT_FILE.is_file() else "")


_MEDIA: dict = {}


def media_info(path) -> tuple[str, float | None]:
    """(file extension, playtime in seconds or None); cached because rows_json() runs on every poll."""
    if not path:
        return "", None
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return p.suffix.lower(), None
    if key not in _MEDIA:
        secs = None
        try:
            import mutagen
            f = mutagen.File(str(p))
            secs = float(f.info.length) if f is not None and f.info else None
        except Exception:
            pass
        _MEDIA[key] = (p.suffix.lower(), secs)
    return _MEDIA[key]


class Job:
    def __init__(self, name: str, opts: dict):
        self.id = secrets.token_urlsafe(8)
        self.name = name
        self.want_bpm, self.want_genre = opts["bpm"], opts["genre"]
        self.want_year, self.want_cleanup = opts["year"], opts["cleanup"]
        self.overwrite = opts["overwrite"]
        self.want_quality = opts["quality"]
        self.state = "running"       # running | ready | applying | done | error
        self.message = "Starting…"
        self.done = self.total = 0
        self.error = ""
        self.entries: list[dict] = []
        self.bpm_rows: list = []
        self.genre_rows: list = []
        self.year_rows: list = []
        self.clean_rows: list = []
        self.backup_dir = ""
        self.quality: dict[int, dict] = {}   # row index -> cutoff result, filled in by a background thread
        self.q_done = self.q_total = 0
        self.q_running = False
        self.lock = threading.Lock()

    def progress(self, done: int, total: int, msg: str) -> None:
        self.done, self.total, self.message = done, total, msg

    # ---- serialisation ------------------------------------------------
    def to_json(self) -> dict:
        d = {"id": self.id, "state": self.state, "message": self.message, "done": self.done,
             "total": self.total, "error": self.error, "backup_dir": self.backup_dir,
             "want": {"bpm": self.want_bpm, "genre": self.want_genre, "year": self.want_year, "cleanup": self.want_cleanup, "quality": self.want_quality},
             "max_genres": MAX_GENRES, "q_running": self.q_running, "q_done": self.q_done, "q_total": self.q_total}
        if self.state in ("ready", "applying", "done"):
            d["rows"] = self.rows_json()
        return d

    def rows_json(self) -> list[dict]:
        out = []
        for i, e in enumerate(self.entries):
            b = self.bpm_rows[i] if self.bpm_rows else None
            g = self.genre_rows[i] if self.genre_rows else None
            y = self.year_rows[i] if self.year_rows else None
            c = self.clean_rows[i] if self.clean_rows else None
            src = b if (b and b.match == "matched") else (g or y or b)
            row = {"index": i + 1, "artist": (src.artist if src else e.get("Artist", "")),
                   "title": (src.title if src else e.get("Name", "")),
                   "path": next((x.path for x in (b, g, y, c) if x is not None), None)}
            row["quality"] = self.quality.get(i + 1)
            row["ext"], row["seconds"] = media_info(row["path"])
            if b and self.want_bpm:
                row["bpm"] = {"match": b.match, "rb": b.bpm_text, "existing": b.existing,
                              "action": b.action, "detail": b.detail, "result": b.result}
            if g:
                pg = genre_tool.pretty_genre
                row["genre"] = {
                    "existing": g.existing, "action": g.action, "detail": g.detail, "result": g.result,
                    "editable": g.action in ("WRITE", "REVIEW", "NONE", "KEPT"),
                    "high": [{"genre": pg(s.genre), "votes": s.votes} for s in g.suggestions if s.confidence == "high"][:MAX_GENRES],
                    "medium": [{"genre": pg(s.genre), "votes": s.votes, "on": s in g.chosen} for s in g.candidates],
                    "ignored": [f"{pg(s.genre)} ({s.votes})" for s in g.suggestions if s.confidence == "low"],
                    "written": g.override if g.result else ""}
            if y:
                row["year"] = {"existing": y.existing, "proposed": y.proposed, "source": y.source,
                               "action": y.action, "detail": y.detail, "result": y.result}
            if c:
                row["cleanup"] = {"action": c.action, "detail": c.detail, "result": c.result,
                                  "changes": [{"field": f, "old": c.current.get(f, ""), "new": v[0], "reason": v[1]}
                                              for f, v in c.changes.items()]}
            out.append(row)
        return out


# --------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------

def run_preview(job: Job, upload: bytes | None, playlist_id: str | None) -> None:
    try:
        if playlist_id:
            job.progress(0, 0, "Reading the Rekordbox playlist (from a temporary copy)…")
            job.entries = bpm_tool.rekordbox_playlist_entries(bpm_tool.DEFAULT_DB, playlist_id)
            if not job.entries:
                raise ValueError("this Rekordbox playlist is empty")
        else:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "playlist.txt"
                p.write_bytes(upload or b"")
                _, job.entries = bpm_tool.parse_playlist(p)
        n = len(job.entries)
        if job.want_bpm:
            job.progress(0, n, "Reading the Rekordbox library (from a temporary copy)…")
            args = SimpleNamespace(overwrite=job.overwrite, round=False)
            job.bpm_rows = bpm_tool.plan(job.entries, args,
                                         lambda wanted: bpm_tool.load_from_master_db(bpm_tool.DEFAULT_DB, wanted))
        mb = genre_tool.MusicBrainz(CACHE, contact()) if (job.want_genre or job.want_year) else None
        if job.want_genre:
            job.genre_rows = genre_tool.plan(job.entries, mb, progress=lambda d, t, m: job.progress(d, t, f"Looking up genres: {m}"))
        if job.want_year:
            job.year_rows = year_tool.plan(job.entries, job.bpm_rows or None, mb,
                                           progress=lambda d, t, m: job.progress(d, t, f"Looking up years: {m}"))
        if job.want_cleanup:
            job.clean_rows = cleanup_tool.plan(job.entries, progress=lambda d, t, m: job.progress(d, t, f"Checking tags: {m}"))
        job.progress(n, n, "Preview ready")
        job.state = "ready"
        if job.want_quality:
            threading.Thread(target=run_quality, args=(job,), daemon=True).start()
    except SystemExit as e:
        job.error, job.state = str(e.code), "error"
    except Exception as e:  # shown to the user, never silent
        job.error, job.state = f"{type(e).__name__}: {e}", "error"


def run_quality(job: Job) -> None:
    """Background: measure each track's frequency cutoff (read-only) while the user reviews the preview."""
    try:
        paths = {}
        for i, e in enumerate(job.entries, 1):
            p = bpm_tool.resolve_location(e.get("Location", ""))
            if p and Path(p).is_file():
                paths[i] = p
        job.q_total, job.q_done, job.q_running = len(paths), 0, True
        for i, p in paths.items():
            job.quality[i] = quality_tool.cached_analyse(p)
            job.q_done += 1
    except Exception:
        pass
    finally:
        job.q_running = False


def _editable_mp3(path) -> bool:
    return bool(path) and Path(path).suffix.lower() == ".mp3" and Path(path).is_file()


def parse_selection(job: Job, sel: dict) -> dict:
    """Validate what the page sends; raise ValueError for anything not allowed."""
    ints = lambda v: {int(x) for x in v}
    out = {"bpm": set(), "year": set(), "genre": {}, "cleanup": {}, "bpm_set": {}, "year_set": {}}
    n = len(job.entries)
    try:
        out["bpm"] = ints(sel.get("bpm", [])) if job.want_bpm else set()
        out["year"] = ints(sel.get("year", [])) if job.want_year else set()
        for k, names in (sel.get("genre", {}) if job.want_genre else {}).items():
            idx = int(k)
            if not 1 <= idx <= n:
                raise ValueError("bad row")
            row = job.genre_rows[idx - 1]
            if row.action not in ("WRITE", "REVIEW", "NONE", "KEPT"):
                raise ValueError(f"row {idx}: genre tag cannot be written")
            clean, seen = [], set()
            for nm in names:
                nm = re.sub(r"\s+", " ", str(nm)).strip()
                if not nm or len(nm) > 60 or re.search(r"[\x00-\x1f;]", nm):
                    raise ValueError(f"row {idx}: invalid genre name")
                if nm.casefold() not in seen:
                    seen.add(nm.casefold()); clean.append(nm)
            if len(clean) > MAX_GENRES:
                raise ValueError(f"row {idx}: at most {MAX_GENRES} genres per track")
            if clean:
                out["genre"][idx] = clean
        # manual edits of already-filled tags: {row: value}; "" clears a year
        for k, v in (sel.get("bpm_set", {}) if job.want_bpm else {}).items():
            idx = int(k)
            row = job.bpm_rows[idx - 1]
            if not _editable_mp3(row.path) or bpm_tool.parse_bpm(str(v)) is None:
                raise ValueError(f"row {idx}: BPM cannot be written")
            out["bpm_set"][idx] = str(v).strip()
        for k, v in (sel.get("year_set", {}) if job.want_year else {}).items():
            idx = int(k)
            row = job.year_rows[idx - 1]
            v = str(v).strip()
            if not _editable_mp3(row.path) or (v and not (re.fullmatch(r"\d{4}", v) and 1900 <= int(v) <= datetime.now().year + 1)):
                raise ValueError(f"row {idx}: year cannot be written")
            out["year_set"][idx] = int(v) if v else None
        for k, fields in (sel.get("cleanup", {}) if job.want_cleanup else {}).items():
            idx = int(k)
            row = job.clean_rows[idx - 1]
            fs = {f for f in fields if f in row.changes}
            if fs:
                out["cleanup"][idx] = fs
    except (TypeError, ValueError, IndexError, AttributeError) as e:
        raise ValueError(str(e) or "invalid selection")
    return out


def run_apply(job: Job, s: dict) -> None:
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w.-]+", "_", Path(job.name).stem)[:40] or "playlist"
        base = BACKUP_ROOT / f"{safe}_{stamp}"
        job.backup_dir = str(base)
        if job.want_bpm:
            for r in job.bpm_rows:
                if r.index in s["bpm_set"]:
                    r.bpm_text, r.action, r.detail = s["bpm_set"][r.index], "UPDATE", "edited by you"
                elif r.action == "UPDATE" and r.index not in s["bpm"]:
                    r.action, r.detail = "SKIP", "deselected"
        if job.want_year:
            for r in job.year_rows:
                if r.index in s["year_set"]:
                    r.proposed, r.action, r.detail = s["year_set"][r.index], "WRITE", "edited by you"
                elif r.action == "WRITE" and r.index not in s["year"]:
                    r.action, r.detail = "SKIP", "deselected"
        if job.want_genre:
            for r in job.genre_rows:
                if r.index in s["genre"] and ", ".join(s["genre"][r.index]) != (r.existing or ""):
                    r.override, r.action = ", ".join(s["genre"][r.index]), "WRITE"
                elif r.action == "WRITE":
                    r.action, r.detail = "SKIP", "deselected"
        if job.want_cleanup:
            for r in job.clean_rows:
                r.selected = s["cleanup"].get(r.index, set())
                if r.action == "UPDATE" and not r.selected:
                    r.action, r.detail = "SKIP", "deselected"
        steps = []
        if job.want_cleanup:
            steps.append(("Writing tag cleanup…", lambda: cleanup_tool.apply_writes(job.clean_rows, base / "cleanup")))
        if job.want_bpm:
            steps.append(("Writing BPM tags…", lambda: bpm_tool.apply_writes(job.bpm_rows, base / "bpm")))
        if job.want_year:
            steps.append(("Writing years…", lambda: year_tool.apply_writes(job.year_rows, base / "year")))
        if job.want_genre:
            steps.append(("Writing genres…", lambda: genre_tool.apply_writes(job.genre_rows, base / "genre")))
        for msg, fn in steps:
            job.message = msg
            fn()
        job.message, job.state = "Done", "done"
    except Exception as e:
        job.error, job.state = f"{type(e).__name__}: {e}", "error"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DJTagTools"

    def log_message(self, fmt, *args):  # quiet
        pass

    # Only accept requests addressed to localhost, and (for POST) from our own page.
    def _allowed(self, post: bool) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return False
            if self.headers.get("X-DJ-Tools") != "1":
                return False
        return True

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        if not self._allowed(False):
            return self._json(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if setup_web.handle_get(self, u, parse_qs(u.query)):
            return
        if u.path in ("/", "/bpm", "/index.html", "/convert", "/convert.html") and not settings.is_configured():
            self.send_response(302)  # first run: go to the setup page
            self.send_header("Location", "/setup")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if convert_web.handle_get(self, u, parse_qs(u.query)):
            return
        if u.path in ("/bpm", "/index.html"):
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/config":
            return self._json(200, {"db": str(bpm_tool.DEFAULT_DB), "db_exists": bpm_tool.DEFAULT_DB.is_file(),
                                    "contact_set": bool(contact()), "backup_root": str(BACKUP_ROOT)})
        if u.path == "/api/playlists":
            try:
                return self._json(200, {"playlists": bpm_tool.list_rekordbox_playlists(bpm_tool.DEFAULT_DB)})
            except SystemExit as e:
                return self._json(500, {"error": str(e.code)})
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/genres":  # official MusicBrainz genre names, for the "add genre" box
            try:
                names = sorted(genre_tool.pretty_genre(g) for g in genre_tool.MusicBrainz(CACHE, contact()).genre_names)
                return self._json(200, {"genres": names})
            except Exception:
                return self._json(200, {"genres": []})
        if u.path == "/api/audio":  # stream one of this job's own MP3s (with Range support for seeking)
            q = parse_qs(u.query)
            job = JOBS.get(q.get("id", [""])[0])
            try:
                idx = int(q.get("row", ["0"])[0])
                path = job.rows_json()[idx - 1]["path"] if job and 1 <= idx <= len(job.entries) else None
            except (ValueError, IndexError):
                path = None
            if not path or not os.path.isfile(path) or not path.lower().endswith(".mp3"):
                return self._json(404, {"error": "no audio"})
            size, start, end, code = os.path.getsize(path), 0, None, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if m and size:
                if m.group(1):
                    start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(size - int(m.group(2)), 0); end = size - 1
                end = min(end if end is not None else size - 1, size - 1)
                if start > end:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                code = 206
            else:
                end = size - 1
            self.send_response(code)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(max(end - start + 1, 0)))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start); left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk); left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if u.path == "/api/job":
            job = JOBS.get(parse_qs(u.query).get("id", [""])[0])
            return self._json(200, job.to_json()) if job else self._json(404, {"error": "unknown job"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed(True):
            return self._json(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "file too large"})
        body = self.rfile.read(length)
        flag = lambda k: q.get(k, ["0"])[0] == "1"
        if setup_web.handle_post(self, u, q, body, on_saved=apply_settings):
            return
        if convert_web.handle_post(self, u, q, body):
            return
        if u.path == "/api/preview":
            opts = {k: flag(k) for k in ("bpm", "genre", "year", "cleanup", "overwrite", "quality")}
            if not (opts["bpm"] or opts["genre"] or opts["year"] or opts["cleanup"]):
                return self._json(400, {"error": "choose at least one thing to tag"})
            playlist_id = q.get("playlist", [""])[0] or None
            job = Job(q.get("name", ["playlist.txt"])[0], opts)
            with JOBS_LOCK:
                JOBS[job.id] = job
            threading.Thread(target=run_preview, args=(job, body, playlist_id), daemon=True).start()
            return self._json(200, {"id": job.id})
        if u.path == "/api/apply":
            job = JOBS.get(q.get("id", [""])[0])
            if not job:
                return self._json(404, {"error": "unknown job"})
            try:
                selection = parse_selection(job, json.loads(body or b"{}"))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            with job.lock:
                if job.state != "ready":
                    return self._json(409, {"error": f"job is {job.state}; changes can be written once, after a preview"})
                job.state = "applying"
            threading.Thread(target=run_apply, args=(job, selection), daemon=True).start()
            return self._json(200, {"id": job.id})
        self._json(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    apply_settings()
    gone = backup_cleanup.prune(BACKUP_ROOT)
    if gone:
        print(f"Deleted {len(gone)} backup folder(s) older than {backup_cleanup.MAX_AGE_DAYS} days: {', '.join(gone)}")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot start on port {a.port}: {e}. Try --port 8766.")
    url = f"http://127.0.0.1:{a.port}/"
    print(f"DJ Tag Tools running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
