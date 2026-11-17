# Rekordbox BPM → MP3 TBPM sync

Copies the BPM that Rekordbox already analysed into the `TBPM` ID3 tag of the MP3 files
listed in an iTunes-exported playlist TXT. It never analyses audio and never guesses.

## How it works

1. Reads the playlist TXT (tab separated, CR/LF/UTF-8/UTF-16 handled) and its `Location` column.
2. Converts each location to an absolute macOS path (`Macintosh HD/Users/...` → `/Users/...`,
   `file://` URLs are percent-decoded, Unicode is NFC-normalised, comparison is case-insensitive like APFS).
3. Looks the path up in Rekordbox. **Matching is by full path only** - no filename or artist/title fallback.
   A path shared by several Rekordbox entries is reported as ambiguous and skipped.
4. Uses Rekordbox's BPM as the only source (`master.db` stores BPM ×100; XML has `AverageBpm`).
   Missing, zero or non-numeric BPM → skipped with a reason.
5. Dry run by default. With `--write`, each file is backed up first, tagged, then read back to verify.

### Rekordbox source

- **Default: `master.db`** (`~/Library/Pioneer/rekordbox/master.db`). It is encrypted
  (SQLCipher) and read through [pyrekordbox](https://github.com/dylanljones/pyrekordbox).
  The script copies `master.db`, `-wal` and `-shm` to a temporary folder and reads the **copy**,
  so the original is never opened and Rekordbox can stay running. The copy is deleted afterwards.
  Tracks added or analysed in Rekordbox after the last WAL checkpoint are included.
- **Alternative: `--xml PATH`** for a Rekordbox XML export (File → Export Collection in xml format).
  XML support follows the documented format (`DJ_PLAYLISTS/COLLECTION/TRACK`, `AverageBpm`, `Location`)
  and is covered by a fixture test, but has not been run against a real export of yours.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# dry run (default): summary + preview table, nothing is modified
python3 rekordbox_bpm_sync.py "/path/to/playlist.txt"

# also save a CSV of every playlist row
python3 rekordbox_bpm_sync.py "/path/to/playlist.txt" --report

# apply: backs up each MP3 that will change, writes TBPM, verifies
python3 rekordbox_bpm_sync.py "/path/to/playlist.txt" --write

# replace existing BPM tags too
python3 rekordbox_bpm_sync.py "/path/to/playlist.txt" --write --overwrite
```

| Option | Meaning |
|---|---|
| `--write` | Actually modify MP3s. Without it nothing is changed. |
| `--overwrite` | Replace existing BPM tags (default: skip files that already have one). |
| `--round` | Write whole numbers (`110`) instead of 1 decimal (`140.6`). |
| `--report [CSV]` | CSV with every playlist row, resolved path, match status, BPMs, action, result. |
| `--backup-dir PATH` | Where backups go (default: `<playlist>_bpm_backup_<timestamp>/` next to the playlist). |
| `--db PATH` / `--xml PATH` | Alternative `master.db` or an XML export. |
| `--verbose` | Show every row including unmatched ones, with resolved paths. |

There is deliberately **no `--force`**: there is no safe case for bypassing path validation.

## BPM format

`TBPM` is a text frame. Whole numbers are written as `128`; fractional values are rounded to one
decimal (`140.6`). Some players only understand integers - use `--round` for those.
An existing `TBPM` of `0` or non-numeric text counts as "no BPM".

## Safety

- Dry run unless `--write`. Rekordbox database/XML and the playlist are never modified; only `.mp3` files are touched.
- Backups are copied (size-checked) before each write into a new directory that is never reused;
  `manifest.csv` maps each backup to its original. To restore, copy the backup file over the original.
- Only the `TBPM` frame is replaced; all other frames and the file's ID3v2 version (2.3/2.4) are kept.
- A failed file is reported and the run continues. Exit code is 1 if any write failed.
- Each written value is read back and compared.

## Tests

```bash
python3 -m unittest discover -s tests
```

Tests use synthetic MP3s in temp folders only.

## Web interface

Double-click **`Start DJ Tools.command`** (or run `python3 webui.py`). It starts a small server on `http://127.0.0.1:8765` and opens the page.
A plain `.html` file opened from disk cannot read or tag files, so the page is served by this local server (standard library only,
reachable only from this Mac; requests from other websites are rejected).

1. **Pick a playlist:** upload an iTunes `.txt`, or choose a playlist straight from your Rekordbox database (tab "Rekordbox playlist";
   smart playlists and folders are not listed).
2. **Tick what to do:** BPM, Year, Genres, and optionally Tag cleanup.
3. **Run** shows a preview only. Adjust it: untick rows, click genre cards, add your own genres.
4. **Write changes** (optionally backing up each file first via the "Make backup" checkbox, off by default), writes, and verifies. Backups go to `backups/<playlist>_<timestamp>/{bpm,year,genre,cleanup}/`.

Changes can be written once per preview; run again for a new preview. "Download CSV" saves the table.

### Genre cards
Green = high confidence (pre-selected, written), dashed amber = medium (shown, click to add; the best medium genre is pre-selected only when a track has no high-confidence genre), purple = genres you add yourself
(type in "+ add genre"; official MusicBrainz names are suggested). Click any card to select/deselect it; maximum 5 per track.
Genre tags that already have content are never replaced.

### Year
Written only when the file has **no year or the year is 1970**; any other existing year is never changed.
Source: **always MusicBrainz, never Rekordbox**: the earliest release date among recordings with the same artist and exact title
(ambiguous same-name artists are ignored). Every row can be unticked.
CLI: `python3 year_tool.py playlist.txt [--write]`.

### Tag cleanup (optional, off by default)
Only runs when you tick it. Rules (each change is shown as old → new and can be unticked):
- remove download junk from artist/title/album: `SpotiDownloader.com - `, `(SPOTISAVER)`, `(Official Video)` etc.
  DJ-relevant words like `(Clean)`, `(Dirty)`, `(Extended)`, `(Intro)` are never removed, and **bitrate info like `(128kbps)` is left alone**;
- trim and collapse repeated spaces;
- if the artist is empty or "Unknown Artist", take it from an `Artist - Title.mp3` file name; if the title is empty, take the title from it.
  Existing artist/title text is never replaced with file-name text.

CLI: `python3 tag_cleanup.py playlist.txt [--write]`.

## Genre suggestions (`genre_suggest.py`)

Fills **empty** genre tags (`TCON`) from MusicBrainz. No API key. **A genre tag that already has content is never replaced**
(there is no overwrite option).

```bash
python3 genre_suggest.py "/path/to/playlist.txt"                  # dry run: table of suggestions
python3 genre_suggest.py "/path/to/playlist.txt" --report         # + CSV with all candidates
python3 genre_suggest.py "/path/to/playlist.txt" --write-genre    # backup, write, verify
```

- Lookup is by primary artist + cleaned title (`(Clean)`, `[128kbps]`, `SpotiDownloader.com - ` removed), then, if nothing is found,
  by the title without remix brackets (one confidence level lower). Metadata search is used here only because genre lookup has no file path.
- Only names on MusicBrainz's official genre list are used (free-text tags like "club" or "seen live" are ignored), shown as the source gives them
  (capitalised). Genres are joined with `, ` in a single `TCON` value, like your existing tags ("Bass, House").
- **At most 5 genres**; all **high** confidence genres are written; if a track has none, the single best **medium** one is. Other medium ones are shown (CLI: "maybe"; web: clickable cards).
  - recording match: ≥3 votes (or 1+ vote also on the artist) = high, 2 votes = medium, 1 = low
  - artist-level genre (not track-specific): ≥3 votes = medium, otherwise low; never high
  - found only via title without remix info: one level lower
  - several MusicBrainz artists with the same name (e.g. two "Eve"): everything from that artist is low, so nothing is written
- Requests are limited to ~1/second and cached in `.genre_cache.sqlite` (next to the script), so reruns are fast. Optional `--contact`
  (or env `MB_CONTACT`, or a one-line git-ignored file `.musicbrainz_contact` next to the script) adds a contact to the User-Agent, as MusicBrainz asks.
- Same safety as the BPM tool: dry run by default, backups + `manifest.csv` before each write, only `TCON` touched, read-back verification.
- Beatport is **not** used (no public API; scraping terms unconfirmed). Many edits/bootlegs will get no suggestion.
- Suggestions are community votes, so they can be wrong: review the dry run before `--write-genre`.

## Backups

Every write run copies the files it is about to change into its own folder under `backups/`
(`<playlist>_<timestamp>/` for the tag tools, `convert_<timestamp>/` for the Import tab, each with a
manifest / `log.csv`). Backup folders whose newest file is older than 30 days are deleted automatically
each time `webui.py` starts (`backup_cleanup.py`).
