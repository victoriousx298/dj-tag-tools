"""HTTP routes for the "Import" tab. Hooked into webui.Handler."""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs

import convert_tool as ct
import genre_suggest as genre_tool
import quality_tool
import year_tool

PAGE = Path(__file__).resolve().parent / "web" / "convert.html"
BACKUP_ROOT = Path(__file__).resolve().parent / "backups"
CACHE = Path(__file__).resolve().parent / ".genre_cache.sqlite"
CONTACT_FILE = Path(__file__).resolve().parent / ".musicbrainz_contact"
import settings
DEFAULT_FOLDER = settings.music_folder()
JOBS: dict[str, "CJob"] = {}


class CJob:
    def __init__(self, folder: Path, recursive: bool, quality: bool = False, want_year: bool = True):
        self.want_year, self.year, self.y_done, self.y_total, self.y_running = want_year, {}, 0, 0, False
        self.want_quality, self.quality, self.q_done, self.q_total, self.q_running = quality, {}, 0, 0, False
        self.id = secrets.token_urlsafe(8)
        self.folder, self.recursive = folder, recursive
        self.state, self.message, self.error = "scanning", "Starting…", ""   # scanning | ready | applying | done | error
        self.done = self.total = 0
        self.rows: list[dict] = []
        self.results: list[dict] = []
        self.backup_dir = ""
        self.lock = threading.Lock()

    def progress(self, d: int, t: int, m: str) -> None:
        self.done, self.total, self.message = d, t, m

    def to_json(self) -> dict:
        d = {"id": self.id, "state": self.state, "message": self.message, "done": self.done, "total": self.total,
             "error": self.error, "folder": str(self.folder), "backup_dir": self.backup_dir, "results": self.results}
        if self.state in ("ready", "applying", "done"):
            d["rows"] = [{k: r[k] for k in ("id", "kind", "name", "rel", "artist", "title", "bitrate", "bitrate_text",
                                            "error", *ct.EXTRA_KEYS)} for r in self.rows]
            d["plan"] = ct.plan(self.rows, {}, self.year)
        return d


def _scan(job: CJob) -> None:
    try:
        job.rows = ct.scan_folder(job.folder, job.recursive, job.progress)
        job.state = "ready"
    except Exception as e:
        job.state, job.error = "error", str(e)
        return
    if job.want_year:
        threading.Thread(target=_year_lookup, args=(job,), daemon=True).start()
    if job.want_quality:
        job.q_total, job.q_done, job.q_running = len(job.rows), 0, True
        try:
            for r in job.rows:
                try:
                    job.quality[r["id"]] = quality_tool.cached_analyse(r["path"])
                except Exception as e:  # file moved/renamed meanwhile
                    job.quality[r["id"]] = {"error": str(e)[:100]}
                job.q_done += 1
        finally:
            job.q_running = False


def _contact() -> str:
    import os
    if os.environ.get("MB_CONTACT"):
        return os.environ["MB_CONTACT"]
    return settings.contact() or (CONTACT_FILE.read_text().strip() if CONTACT_FILE.is_file() else "")


def _year_lookup(job: CJob) -> None:
    """Background: MusicBrainz year for files whose year tag is empty or the 1970 placeholder (same rule as BPM & Fixes)."""
    todo = [r for r in job.rows if not r["error"] and year_tool.year_replaceable(r["year"] or None)
            and ct.normalize_title(r["title"]) and r["artist"]]
    job.y_total, job.y_done, job.y_running = len(todo), 0, True
    try:
        mb = genre_tool.MusicBrainz(CACHE, _contact())
        for r in todo:
            try:
                y, d = year_tool.musicbrainz_year(mb, ct.artist_for_name(r["artist"]), ct.normalize_title(r["title"]))
                job.year[r["id"]] = {"year": y, "detail": d, "ok": True}
            except Exception as e:
                job.year[r["id"]] = {"year": None, "detail": f"lookup failed: {e}"[:100], "ok": False}
            job.y_done += 1
    except Exception as e:
        for r in todo:
            job.year.setdefault(r["id"], {"year": None, "detail": f"lookup failed: {e}"[:100], "ok": False})
    finally:
        job.y_running = False


def _apply(job: CJob, edits: dict, selected: set[int]) -> None:
    try:
        job.results, job.backup_dir = ct.apply(job.rows, edits, selected, BACKUP_ROOT, job.progress, job.year)
        job.state = "done"
    except Exception as e:
        job.state, job.error = "error", str(e)


def pick_folder(start: str) -> str:
    """Native macOS folder dialog (the server runs on the user's own Mac). '' if cancelled."""
    s = Path(start).expanduser()
    s = s if s.is_dir() else DEFAULT_FOLDER
    script = ('on run argv\nPOSIX path of (choose folder with prompt "Choose a folder with WAV / MP3 files" '
              'default location POSIX file (item 1 of argv))\nend run')
    r = subprocess.run(["osascript", "-e", script, str(s)], capture_output=True, text=True, timeout=600)
    return r.stdout.strip() if r.returncode == 0 else ""


def _edits(body: dict) -> tuple[dict[int, dict], set[int]]:
    edits: dict[int, dict] = {}
    sel: set[int] = set()
    for r in body.get("rows", []):
        i = int(r["id"])
        e = {}
        for k in ("artist", "title", "year", "filename"):
            if isinstance(r.get(k), str):
                e[k] = r[k]
        edits[i] = e
        if r.get("selected"):
            sel.add(i)
    return edits, sel


def handle_get(h, u, q) -> bool:
    p = u.path
    if p in ("/", "/convert", "/convert.html"):
        h._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
    elif p == "/columns.js":
        h._send(200, (PAGE.parent / "columns.js").read_bytes(), "text/javascript; charset=utf-8")
    elif p == "/api/convert/config":
        h._json(200, {"default_folder": str(DEFAULT_FOLDER), "ffmpeg": bool(ct.FFMPEG), "backup_root": str(BACKUP_ROOT)})
    elif p == "/api/convert/quality":
        job = JOBS.get(q.get("id", [""])[0])
        h._json(200, {"running": job.q_running, "done": job.q_done, "total": job.q_total, "results": job.quality,
                                          "year": {"running": job.y_running, "done": job.y_done, "total": job.y_total, "results": job.year}}) if job \
            else h._json(404, {"error": "unknown job"})
    elif p == "/api/convert/job":
        job = JOBS.get(q.get("id", [""])[0])
        h._json(200, job.to_json()) if job else h._json(404, {"error": "unknown job"})
    elif p in ("/api/convert/details", "/api/convert/cover"):
        job = JOBS.get(q.get("id", [""])[0])
        try:
            row = job.rows[int(q.get("row", ["-1"])[0])] if job else None
        except (ValueError, IndexError):
            row = None
        if row is None or int(q["row"][0]) < 0:
            return h._json(404, {"error": "unknown track"}) or True
        path = Path(row["path"])
        try:
            if p.endswith("details"):
                h._json(200, ct.read_details(path))
            else:
                c = ct.read_cover(path)
                h._send(200, c[0], c[1]) if c else h._json(404, {"error": "no cover"})
        except Exception as e:
            h._json(500, {"error": str(e)})
    else:
        return False
    return True


def handle_post(h, u, q, raw: bytes) -> bool:
    p = u.path
    if not p.startswith("/api/convert/"):
        return False
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        h._json(400, {"error": "bad json"})
        return True
    if p == "/api/convert/pick":
        h._json(200, {"path": pick_folder(str(body.get("start") or DEFAULT_FOLDER))})
    elif p == "/api/convert/scan":
        folder = Path(str(body.get("path") or "")).expanduser()
        if not folder.is_dir():
            return h._json(400, {"error": f"not a folder: {folder}"}) or True
        job = CJob(folder.resolve(), bool(body.get("recursive", True)), bool(body.get("quality")), bool(body.get("year", True)))
        JOBS[job.id] = job
        threading.Thread(target=_scan, args=(job,), daemon=True).start()
        h._json(200, {"id": job.id})
    elif p in ("/api/convert/plan", "/api/convert/apply"):
        job = JOBS.get(q.get("id", [""])[0])
        if not job:
            return h._json(404, {"error": "unknown job"}) or True
        edits, sel = _edits(body)
        if p.endswith("plan"):
            h._json(200, {"plan": ct.plan(job.rows, edits, job.year)})
        else:
            with job.lock:
                if job.state != "ready":
                    return h._json(409, {"error": f"job is {job.state}; scan the folder again for another run"}) or True
                job.state = "applying"
            threading.Thread(target=_apply, args=(job, edits, sel), daemon=True).start()
            h._json(200, {"id": job.id})
    else:
        h._json(404, {"error": "not found"})
    return True
