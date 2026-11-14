"""WAV -> 320 kbps CBR MP3 converter and "ARTIST - TITLE.mp3" renamer.

Everything here is plain functions so the web UI (convert_web.py) and the tests share them.

Safety rules:
  * a WAV is only moved to the Trash after its MP3 was written, re-opened and verified
    (CBR 320 kbps, duration matches);
  * nothing is ever overwritten: name clashes are skipped and reported;
  * MP3s whose tags are edited are copied to backups/ first, and a log.csv of every rename is kept.
"""
from __future__ import annotations

import csv
import json
import os
import re
import secrets
import shutil
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path

from mutagen.id3 import ID3, TDRC, TIT2, TPE1, ID3NoHeaderError
from mutagen.mp3 import MP3

BITRATE = 320_000
EXTRA_KEYS = ("album", "album_artist", "year", "genre", "bpm", "key", "track", "comment", "label", "cover",
              "duration", "sample_rate", "size_mb", "seconds")
MAX_FILES = 5000
SEP_RE = re.compile(r"\s*/\s*")


def _which(name: str) -> str:
    return shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}")
                                       if os.path.exists(p)), name)


FFMPEG, FFPROBE = _which("ffmpeg"), _which("ffprobe")


# ---------------------------------------------------------------- names
def artist_for_name(artist: str) -> str:
    """'A/B' -> 'A, B' (empty parts dropped)."""
    return ", ".join(p for p in (s.strip() for s in SEP_RE.split(artist.replace("\x00", "/"))) if p)


_VERSION_WORDS = (r"remix|re-?edit|edit|remaster(?:ed)?(?:\s+\d{4})?|(?:\d{4}\s+)?remaster(?:ed)?|mix|version|bootleg|rework|flip|"
                  r"vip|dub|instrumental|acoustic|acapella|a cappella|live|radio|extended|club|original|refix|mashup|"
                  r"edit|reprise|stripped|clean|dirty|intro|outro")
_VERSION_RE = re.compile(rf"(?i)\b(?:{_VERSION_WORDS})\b")


def normalize_title(title: str) -> str:
    """'Song - Some Remix' -> 'Song (Some Remix)'; 'Song (Live) - Radio Edit' -> 'Song (Live) [Radio Edit]'.

    Only a trailing ' - ...' part that names a remix/edit/version/remaster is bracketed."""
    t = (title or "").strip()
    head, sep, tail = t.rpartition(" - ")
    head, tail = head.strip(), tail.strip()
    if not sep or not head or not tail or not _VERSION_RE.search(tail):
        return t
    if "(" in head and ")" in head:
        return f"{head} [{tail}]"
    return f"{head} ({tail})"


def _y4(s) -> str:
    m = re.match(r"\s*(\d{4})", str(s or ""))
    return m.group(1) if m else ""


def valid_year(s: str) -> bool:
    return s == "" or (re.fullmatch(r"\d{4}", s) is not None and 1900 <= int(s) <= datetime.now().year + 1)


def sanitize(stem: str) -> str:
    s = unicodedata.normalize("NFC", stem)
    s = s.replace("/", "")  # a slash in a name is skipped, never replaced
    s = re.sub(r"[:\x00-\x1f]", "-", s)
    s = re.sub(r"\s+", " ", s).strip().lstrip(".").rstrip(". ")
    while len(s.encode()) > 240:
        s = s[:-1]
    return s.strip()


def make_filename(artist: str, title: str) -> str | None:
    a, t = artist_for_name(artist or ""), (title or "").strip()
    if not a or not t:
        return None
    stem = sanitize(f"{a} - {t}")
    return stem + ".mp3" if stem else None


def clean_custom_filename(name: str) -> str | None:
    name = name.strip()
    if name.lower().endswith(".mp3"):
        name = name[:-4]
    stem = sanitize(name)
    return stem + ".mp3" if stem else None


def _key(p: Path) -> tuple[str, str]:  # APFS is case-insensitive and NFC/NFD-agnostic
    return str(p.parent), unicodedata.normalize("NFC", p.name).casefold()


# ---------------------------------------------------------------- reading
def ffprobe(path: Path) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise OSError(r.stderr.strip() or "ffprobe failed")
    return json.loads(r.stdout or "{}")


def _txt(tags, *frames) -> str:
    for f in frames:
        if tags is not None and f in tags:
            vals = [str(v) for v in getattr(tags[f], "text", [])]
            if vals:
                return "/".join(vals) if f in ("TPE1", "TPE2") else "; ".join(vals)
    return ""


def read_basic(path: Path) -> dict:
    """artist / title / bitrate etc. for the table. Never raises: problems go into `error`."""
    kind = path.suffix.lower().lstrip(".")
    d = {"kind": kind, "artist": "", "title": "", "bitrate": 0, "bitrate_text": "", "error": "",
         **{k: "" for k in EXTRA_KEYS}, "seconds": 0}
    try:
        d["size_mb"] = f"{path.stat().st_size / 1_048_576:.1f} MB"
    except OSError:
        pass
    try:
        if kind == "mp3":
            m = MP3(path)
            d["artist"], d["title"] = _txt(m.tags, "TPE1"), _txt(m.tags, "TIT2")
            d["bitrate"] = int(m.info.bitrate)
            mode = str(m.info.bitrate_mode).split(".")[-1]
            d["bitrate_text"] = f"{d['bitrate'] // 1000} {'' if mode == 'UNKNOWN' else mode}".strip()
            tg = m.tags
            d.update(album=_txt(tg, "TALB"), album_artist=_txt(tg, "TPE2"), year=_txt(tg, "TDRC", "TYER"),
                     genre=_txt(tg, "TCON"), bpm=_txt(tg, "TBPM"), key=_txt(tg, "TKEY"), track=_txt(tg, "TRCK"),
                     label=_txt(tg, "TPUB"), seconds=float(m.info.length), duration=_dur(m.info.length), sample_rate=f"{m.info.sample_rate} Hz",
                     cover="yes" if tg and any(k.startswith("APIC") for k in tg.keys()) else "",
                     comment=" | ".join(c for c in ("; ".join(str(v) for v in f.text) for k, f in (tg or {}).items()
                                                    if k.startswith("COMM")) if c))
        else:
            info = ffprobe(path)
            tags = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
            d["artist"], d["title"] = tags.get("artist", ""), tags.get("title", "")
            st = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
            br = int(st.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
            d["bitrate"] = br
            d["bitrate_text"] = f"{br // 1000} WAV" if br else "WAV"
            d.update(album=tags.get("album", ""), year=tags.get("date", ""), genre=tags.get("genre", ""),
                     track=tags.get("track", ""), comment=tags.get("comment", ""), label=tags.get("publisher", ""),
                     sample_rate=f"{st.get('sample_rate', '?')} Hz",
                     seconds=float(info.get("format", {}).get("duration") or 0),
                     duration=_dur(float(info.get("format", {}).get("duration") or 0)))
    except Exception as e:  # corrupt / unreadable file
        d["error"] = str(e) or e.__class__.__name__
    return d


def read_details(path: Path) -> dict:
    """Everything the sidebar shows. Sections -> list of [label, value]."""
    kind = path.suffix.lower().lstrip(".")
    st = path.stat()
    core: list[list[str]] = []
    tech: list[list[str]] = [["Size", f"{st.st_size / 1_048_576:.1f} MB"]]
    frames: list[list[str]] = []
    has_cover = False
    if kind == "mp3":
        m = MP3(path)
        t = m.tags
        pairs = [("Title", ("TIT2",)), ("Artist", ("TPE1",)), ("Album", ("TALB",)), ("Album artist", ("TPE2",)),
                 ("Year", ("TDRC", "TYER")), ("Genre", ("TCON",)), ("BPM", ("TBPM",)), ("Key", ("TKEY",)),
                 ("Track", ("TRCK",)), ("Disc", ("TPOS",)), ("Label", ("TPUB",)), ("ISRC", ("TSRC",)),
                 ("Composer", ("TCOM",)), ("Remixer", ("TPE4",)), ("Grouping", ("TIT1",))]
        for label, fr in pairs:
            core.append([label, _txt(t, *fr)])
        core.append(["Comment", " | ".join(c for c in (
            "; ".join(str(v) for v in f.text) for k, f in (t or {}).items() if k.startswith("COMM")) if c)])
        has_cover = bool(t) and any(k.startswith("APIC") for k in t.keys())
        mode = str(m.info.bitrate_mode).split(".")[-1]
        tech += [["Bitrate", f"{m.info.bitrate // 1000} kbps {mode}"], ["Sample rate", f"{m.info.sample_rate} Hz"],
                 ["Channels", str(m.info.channels)], ["Duration", _dur(m.info.length)],
                 ["Encoder", str(getattr(m.info, "encoder_info", "") or "") or "-"],
                 ["ID3 version", "2.%d.%d" % t.version[1:] if t else "none"]]
        for k, f in (t or {}).items():
            if k.startswith("APIC"):
                frames.append([k, f"{f.mime}, {len(f.data) // 1024} KB"])
            elif hasattr(f, "text"):
                frames.append([k, "; ".join(str(v) for v in f.text)[:200]])
            else:
                frames.append([k, str(f)[:200]])
    else:
        info = ffprobe(path)
        tags = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
        for label, k in (("Title", "title"), ("Artist", "artist"), ("Album", "album"), ("Year", "date"),
                         ("Genre", "genre"), ("Track", "track"), ("Comment", "comment")):
            core.append([label, tags.get(k, "")])
        a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
        tech += [["Format", f"{a.get('codec_name', '?')} {a.get('bits_per_sample') or ''}-bit".replace(" -bit", "")],
                 ["Bitrate", f"{int(a.get('bit_rate') or 0) // 1000} kbps"],
                 ["Sample rate", f"{a.get('sample_rate', '?')} Hz"], ["Channels", str(a.get("channels", "?"))],
                 ["Duration", _dur(float(info.get("format", {}).get("duration") or 0))]]
        frames = [[k, str(v)[:200]] for k, v in tags.items()]
    return {"name": path.name, "path": str(path), "kind": kind, "core": core, "tech": tech, "frames": frames,
            "has_cover": has_cover}


def _dur(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def read_cover(path: Path) -> tuple[bytes, str] | None:
    if path.suffix.lower() != ".mp3":
        return None
    t = MP3(path).tags
    for k, f in (t or {}).items():
        if k.startswith("APIC"):
            return f.data, f.mime or "image/jpeg"
    return None


# ---------------------------------------------------------------- scanning
def scan_folder(folder: Path, recursive: bool, progress=lambda d, t, m: None) -> list[dict]:
    files: list[Path] = []
    if recursive:
        for dp, dn, fn in os.walk(folder):
            dn[:] = sorted(d for d in dn if not d.startswith("."))
            files += [Path(dp) / f for f in sorted(fn)]
    else:
        files = sorted(p for p in folder.iterdir() if p.is_file())
    files = [p for p in files if p.suffix.lower() in (".wav", ".mp3") and not p.name.startswith(".")]
    if len(files) > MAX_FILES:
        raise ValueError(f"{len(files)} audio files found; limit is {MAX_FILES}. Pick a smaller folder.")
    rows = []
    for i, p in enumerate(files):
        progress(i, len(files), f"Reading {p.name}")
        d = read_basic(p)
        d.update(id=i, path=str(p), rel=str(p.relative_to(folder)), name=p.name)
        rows.append(d)
    progress(len(files), len(files), "Done")
    return rows


# ---------------------------------------------------------------- planning
def plan(rows: list[dict], edits: dict[int, dict], proposals: dict[int, dict] | None = None) -> list[dict]:
    """Decide what would happen to every row. edits: id -> {artist,title,filename?}.

    Pure except for 'does this target name already exist' checks.
    """
    out: dict[int, dict] = {}
    claimed: dict[tuple[str, str], int] = {}
    order = sorted(rows, key=lambda r: (r["kind"] != "mp3", r["id"]))  # existing MP3s claim names before WAVs
    for r in order:
        e = edits.get(r["id"], {})
        artist = artist_for_name(e.get("artist", r["artist"]) or "")  # "A/B" -> "A, B" goes into the tag too
        title = normalize_title(e.get("title", r["title"]) or "")   # "Song - X Remix" -> "Song (X Remix)" goes into the tag too
        cur = _y4(r["year"])
        pr = (proposals or {}).get(r["id"])
        ny, ynote = cur, ""
        if "year" in e and valid_year((e["year"] or "").strip()):
            ny, ynote = (e["year"] or "").strip(), "edited"
        elif cur in ("", "1970") and pr and pr.get("ok"):
            if pr.get("year"):
                ny, ynote = str(pr["year"]), "MusicBrainz"
            elif cur == "1970":
                ny, ynote = "", "1970 cleared (no year on MusicBrainz)"
        year_write = ny if ny != cur else None
        custom = (e.get("filename") or "").strip()
        src = Path(r["path"])
        tags_changed = ((artist != r["artist"] or title != r["title"]) and bool(artist or title)) or year_write is not None
        o = {"id": r["id"], "artist": artist, "title": title, "action": "OK", "note": "", "filename": r["name"],
             "tags_changed": tags_changed, "custom": bool(custom), "year": ny if year_write is not None else r["year"],
             "year_write": year_write, "year_note": ynote if year_write is not None else ""}
        if r["error"]:
            o.update(action="SKIP", note=f"unreadable: {r['error'][:120]}")
            out[r["id"]] = o
            continue
        auto = make_filename(artist, title)
        target_name = (clean_custom_filename(custom) if custom else auto)
        if target_name is None:
            # no artist/title: never rename, but a WAV can still be converted to <same name>.mp3
            target_name = src.with_suffix(".mp3").name if r["kind"] == "wav" else r["name"]
            o["note"] = "no artist/title tag; name kept"
        o["filename"] = target_name
        target = src.parent / target_name
        k = _key(target)
        own = r["kind"] == "mp3" and _key(src) == k
        clash = None
        if k in claimed and claimed[k] != r["id"]:
            clash = "another file in this run gets this name"
        elif not own and target.exists() and not (r["kind"] == "mp3" and target.samefile(src)):
            clash = "a file with this name already exists"
        if clash:
            o.update(action="SKIP", note=clash)
            out[r["id"]] = o
            continue
        claimed[k] = r["id"]
        if r["kind"] == "wav":
            o["action"] = "CONVERT"
        elif target_name != r["name"]:
            o["action"] = "RENAME"
        elif tags_changed:
            o["action"] = "TAGS"
        if r["kind"] == "mp3" and target_name != r["name"] and tags_changed:
            o["action"] = "RENAME"
        out[r["id"]] = o
    return [out[r["id"]] for r in rows]


# ---------------------------------------------------------------- applying
def _write_tags(path: Path, artist: str, title: str, year: str | None = None) -> None:
    """year: None = leave alone, "" = remove, "2019" = set."""
    try:
        t = ID3(path)
    except ID3NoHeaderError:
        t = ID3()
    ver = 3 if t.version[1] == 3 else 4
    if title:
        t.setall("TIT2", [TIT2(encoding=3, text=[title])])
    if artist:
        t.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    if year is not None:
        t.delall("TDRC")
        if year:
            t.add(TDRC(encoding=3 if ver == 4 else 1, text=[year]))
    t.save(path, v2_version=ver)


def convert_wav(src: Path, dst: Path) -> None:
    info = ffprobe(src)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not a:
        raise OSError("no audio stream")
    rate = int(a.get("sample_rate") or 44100)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-map", "0:v?",
           "-map_metadata", "0", "-c:a", "libmp3lame", "-b:a", "320k", "-c:v", "copy",
           "-id3v2_version", "3", "-write_id3v2", "1", "-f", "mp3"]
    if rate > 48000:
        cmd += ["-ar", "48000" if rate % 48000 == 0 else "44100"]
    if int(a.get("channels") or 2) > 2:
        cmd += ["-ac", "2"]
    cmd.append(str(dst))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode or not dst.is_file():
        raise OSError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")
    m = MP3(dst)
    if abs(int(m.info.bitrate) - BITRATE) > 1000 or "CBR" not in str(m.info.bitrate_mode):
        raise OSError(f"output is {m.info.bitrate // 1000} kbps {m.info.bitrate_mode}, expected 320 CBR")
    want = float(info.get("format", {}).get("duration") or 0)
    if want and abs(m.info.length - want) > 1.0:
        raise OSError(f"duration mismatch ({m.info.length:.1f}s vs {want:.1f}s)")


def trash(path: Path) -> None:
    from send2trash import send2trash
    send2trash(str(path))
    if path.exists():
        raise OSError("file is still there after moving to Trash")


def apply(rows: list[dict], edits: dict[int, dict], selected: set[int], backup_root: Path,
          progress=lambda d, t, m: None, proposals: dict[int, dict] | None = None) -> tuple[list[dict], str]:
    """Run the plan for the selected rows. Returns (per-row results, backup/log dir)."""
    plans = {p["id"]: p for p in plan(rows, edits, proposals)}
    by_id = {r["id"]: r for r in rows}
    todo = [i for i in sorted(selected) if i in plans and plans[i]["action"] in ("CONVERT", "RENAME", "TAGS")]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = backup_root / f"convert_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with (out_dir / "log.csv").open("w", newline="", encoding="utf-8") as lf:
        log = csv.writer(lf)
        log.writerow(["action", "original_path", "new_path", "result"])
        for n, i in enumerate(todo, 1):
            p, r = plans[i], by_id[i]
            src = Path(r["path"])
            dst = src.parent / p["filename"]
            progress(n - 1, len(todo), f"{p['action']} {src.name}")
            res = {"id": i, "result": "ok", "new_path": str(dst), "new_name": dst.name}
            try:
                if dst.exists() and not (r["kind"] == "mp3" and dst.samefile(src)):
                    raise OSError("target name exists now; skipped")
                if r["kind"] == "wav":
                    tmp = src.parent / f".dj-convert-{secrets.token_hex(4)}.mp3"
                    try:
                        convert_wav(src, tmp)
                        if p["artist"] or p["title"] or p["year_write"] is not None:
                            _write_tags(tmp, p["artist"], p["title"], p["year_write"])
                        if dst.exists():
                            raise OSError("target name exists now; skipped")
                        os.rename(tmp, dst)
                    finally:
                        tmp.unlink(missing_ok=True)
                    chk = read_basic(dst)
                    if chk["error"] or abs(chk["bitrate"] - BITRATE) > 1000:
                        raise OSError("verification of the new MP3 failed; WAV kept")
                    try:
                        trash(src)
                    except Exception as e:
                        res["result"] = f"converted, but WAV NOT trashed: {e}"
                else:
                    if p["tags_changed"]:
                        bk = out_dir / f"{n:04d}_{src.name}"
                        shutil.copy2(src, bk)
                        if bk.stat().st_size != src.stat().st_size:
                            raise OSError("backup size mismatch; file untouched")
                        _write_tags(src, p["artist"], p["title"], p["year_write"])
                    if dst.name != src.name:
                        os.rename(src, dst)
                    chk = read_basic(dst)
                    if chk["error"] or (p["tags_changed"] and (chk["artist"], chk["title"]) != (p["artist"], p["title"])):
                        raise OSError("read-back verification failed")
                res["bitrate_text"] = read_basic(dst)["bitrate_text"]
            except Exception as e:
                res["result"] = f"FAILED: {e}"
            log.writerow([p["action"], str(src), str(dst), res["result"]])
            lf.flush()
            results.append(res)
    progress(len(todo), len(todo), "Done")
    return results, str(out_dir)
