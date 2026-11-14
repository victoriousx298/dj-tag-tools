"""Per-user settings, stored in settings.json next to the code (git-ignored, never shipped).

Keys:
  rekordbox_db   path to Rekordbox's master.db ("" = not using Rekordbox playlists)
  music_folder   folder the Import page opens first (new downloads / library)
  contact        optional e-mail or URL for the MusicBrainz User-Agent (MusicBrainz asks for one)
  setup_done     true once the setup page was saved
Override the file location with the env var DJ_TOOLS_SETTINGS (used by the tests).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def settings_file() -> Path:
    return Path(os.environ.get("DJ_TOOLS_SETTINGS") or ROOT / "settings.json")


def load() -> dict:
    try:
        data = json.loads(settings_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(updates: dict) -> dict:
    data = {**load(), **updates}
    f = settings_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(f)
    return data


def is_configured() -> bool:
    return bool(load().get("setup_done"))


def detect_rekordbox_db() -> str:
    """Where Rekordbox usually keeps its database on this computer ('' if not found)."""
    home = Path.home()
    candidates = [home / "Library" / "Pioneer" / "rekordbox" / "master.db"]
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox" / "master.db")
    return next((str(c) for c in candidates if c.is_file()), "")


def rekordbox_db() -> Path:
    """The configured master.db; falls back to the auto-detected one, then to the usual Mac location."""
    s = load().get("rekordbox_db")
    if s:
        return Path(s).expanduser()
    return Path(detect_rekordbox_db() or Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db")


def music_folder() -> Path:
    s = load().get("music_folder")
    if s and Path(s).expanduser().is_dir():
        return Path(s).expanduser()
    return Path.home() / "Downloads"


def contact() -> str:
    return str(load().get("contact") or "").strip()
