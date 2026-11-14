"""Estimate the real frequency cutoff (lowpass) of an audio file, like reading it off a Spek spectrogram.

Low-bitrate or transcoded MP3s have a sharp lowpass (e.g. 16 kHz for 128 kbps). We decode with ffmpeg,
average the power spectrum over the whole track and find where it falls to the noise floor.
Read-only: never touches the file. Results are cached in .quality_cache.json (keyed by path, size, mtime).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import numpy as np

RATE = 44100          # everything is resampled to this, so the highest measurable cutoff is ~22 kHz
NFFT = 4096
MAX_FRAMES = 600      # frames sampled evenly over the track
CACHE_FILE = Path(__file__).resolve().parent / ".quality_cache.json"
_lock = threading.Lock()


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).is_file()), None)


def cutoff_hz(samples: np.ndarray) -> Optional[float]:
    """Highest frequency (Hz) that still carries signal, from mono float samples at RATE."""
    n = (len(samples) - NFFT) // (NFFT // 2) + 1
    if n < 8:
        return None
    idx = np.unique(np.linspace(0, n - 1, min(n, MAX_FRAMES)).astype(int))
    win = np.hanning(NFFT).astype(np.float32)
    frames = np.stack([samples[i * (NFFT // 2): i * (NFFT // 2) + NFFT] * win for i in idx])
    rms = np.sqrt((frames ** 2).mean(axis=1))
    frames = frames[rms > max(rms.max() * 0.02, 1e-5)]      # ignore silence / fades
    if len(frames) < 4:
        return None
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(power + 1e-12)
    k = 9                                                      # smooth ~ +-90 Hz so single peaks don't count
    db = np.convolve(np.pad(db, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    freqs = np.fft.rfftfreq(NFFT, 1 / RATE)
    hi = freqs >= 10000
    # 1) a lowpass shows up as a cliff: find the steepest drop over ~200 Hz above 10 kHz
    w = 20
    drop = db[:-w] - db[w:]
    cand = np.where(hi[:-w])[0]
    if len(cand):
        j = cand[np.argmax(drop[cand])]
        if drop[j] >= 15:
            return float(freqs[j + w // 2])
    # 2) no cliff: report the top of the signal above the noise floor (about the Nyquist limit for full-band files)
    floor = np.percentile(db[hi], 5)
    above = np.where((db > floor + 10) & hi)[0]
    return float(freqs[above[-1]]) if len(above) else None


def analyse(path: str) -> dict:
    """{'khz': 16.0, 'label': '16 kHz'} or {'error': ...}."""
    ff = ffmpeg_path()
    if not ff:
        return {"error": "ffmpeg not installed (brew install ffmpeg)"}
    try:
        raw = subprocess.run([ff, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
                             capture_output=True, timeout=180, check=True).stdout
    except subprocess.CalledProcessError as e:
        return {"error": (e.stderr or b"decode failed").decode(errors="replace").strip()[:120]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    hz = cutoff_hz(np.frombuffer(raw, dtype=np.float32))
    if hz is None:
        return {"error": "not enough audio"}
    khz = round(hz / 500) * 0.5                                 # nearest 0.5 kHz
    full = khz >= 21.0
    return {"khz": khz, "label": ("≥21 kHz" if full else f"{khz:g} kHz")}


def _key(path: str) -> str:
    st = Path(path).stat()
    return f"{path}|{st.st_size}|{int(st.st_mtime)}"


def _load() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def cached_analyse(path: str) -> dict:
    key = _key(path)
    with _lock:
        hit = _load().get(key)
    if hit:
        return hit
    res = analyse(path)
    if "error" not in res:
        with _lock:
            c = _load(); c[key] = res
            CACHE_FILE.write_text(json.dumps(c))
    return res
