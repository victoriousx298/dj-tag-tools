"""HTTP routes for the first-run setup page. Hooked into webui.Handler."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import settings

PAGE = Path(__file__).resolve().parent / "web" / "setup.html"


def _osascript(script: str, start: str) -> str:
    """Native macOS chooser dialog (the server runs on the user's own Mac). '' if cancelled or unavailable."""
    try:
        r = subprocess.run(["osascript", "-e", script, start], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def pick_folder(start: str, prompt: str) -> str:
    s = Path(start).expanduser()
    s = s if s.is_dir() else Path.home()
    return _osascript('on run argv\nPOSIX path of (choose folder with prompt "%s" default location POSIX file (item 1 of argv))\nend run' % prompt, str(s))


def pick_file(start: str, prompt: str) -> str:
    s = Path(start).expanduser()
    s = s if s.is_dir() else (s.parent if s.parent.is_dir() else Path.home())
    return _osascript('on run argv\nPOSIX path of (choose file with prompt "%s" default location POSIX file (item 1 of argv))\nend run' % prompt, str(s))


def state() -> dict:
    s = settings.load()
    db = s.get("rekordbox_db") or settings.detect_rekordbox_db()
    folder = s.get("music_folder") or str(Path.home() / "Downloads")
    return {
        "configured": bool(s.get("setup_done")),
        "rekordbox_db": db,
        "rekordbox_detected": bool(settings.detect_rekordbox_db()),
        "music_folder": folder,
        "contact": s.get("contact", ""),
    }


def handle_get(h, u, q) -> bool:
    if u.path == "/setup":
        h._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
    elif u.path == "/api/setup":
        h._json(200, state())
    else:
        return False
    return True


def handle_post(h, u, q, raw: bytes, on_saved=None) -> bool:
    if not u.path.startswith("/api/setup/"):
        return False
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        h._json(400, {"error": "bad json"})
        return True
    if u.path == "/api/setup/pick-folder":
        h._json(200, {"path": pick_folder(str(body.get("start") or ""), "Choose your music folder")})
    elif u.path == "/api/setup/pick-db":
        h._json(200, {"path": pick_file(str(body.get("start") or Path(settings.rekordbox_db()).parent), "Choose the Rekordbox master.db file")})
    elif u.path == "/api/setup/save":
        db = str(body.get("rekordbox_db") or "").strip()
        folder = str(body.get("music_folder") or "").strip()
        contact = str(body.get("contact") or "").strip()
        if db and not Path(db).expanduser().is_file():
            h._json(400, {"error": f"Rekordbox database not found: {db}"})
            return True
        if folder and not Path(folder).expanduser().is_dir():
            h._json(400, {"error": f"Music folder not found: {folder}"})
            return True
        settings.save({"rekordbox_db": str(Path(db).expanduser()) if db else "",
                       "music_folder": str(Path(folder).expanduser()) if folder else "",
                       "contact": contact, "setup_done": True})
        if on_saved:
            on_saved()
        h._json(200, {"ok": True})
    else:
        h._json(404, {"error": "not found"})
    return True
