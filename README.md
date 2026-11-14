# DJ Tag Tools

Tidy up new tracks before they go into Rekordbox. Everything runs **on your own Mac**; nothing is uploaded anywhere except small look-ups to the free [MusicBrainz](https://musicbrainz.org) database (for years and genres).

- **Import**: pick a folder of new WAV / MP3 files. WAVs become 320 kbps MP3s, the Artist tag is tidied ("A/B" becomes "A, B"), files are renamed `ARTIST - TITLE.mp3`, missing years are filled in. You see a preview table first; nothing changes until you press *Apply*.
- **BPM & Fixes**: pick a Rekordbox playlist (or upload a playlist TXT) and copy Rekordbox's BPM, the year and MusicBrainz genres into the MP3 tags, with an optional artist/title cleanup.
- Every changed MP3 is **backed up first** (folder `backups/`, deleted automatically after 30 days). Your Rekordbox database is only ever read, never written.

## Install (first time, about 2 minutes)

1. You need **macOS** and **Python 3** (open *Terminal* and type `python3 --version`; if macOS offers to install developer tools, accept, then try again). Optional: [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg`) for WAV to MP3 conversion and the audio-quality column.
2. Unzip the folder anywhere (e.g. your Documents).
3. Double-click **`Start DJ Tools.command`**.
   - macOS may say it can't verify the file: right-click it, choose *Open*, then *Open* again (only needed once).
   - The first start downloads the libraries it needs (about a minute). Later starts are instant.
4. Your browser opens the **Setup** page. Choose
   - your **music folder** (where new tracks land),
   - your **Rekordbox `master.db`** (usually found automatically; leave empty if you don't use Rekordbox),
   - optionally your **e-mail**, which MusicBrainz asks API users to include.

   Press *Save and start*. You can change these any time with the *Setup* link at the top right.

Close the Terminal window (or press Ctrl+C in it) to stop the tool. Start it again with the same double-click.

## Using it

1. **Import** (home page): the folder you chose is pre-filled. Click *Scan (preview only)*, check the table (you can edit any cell, and click a row for details and playback), press *Apply*.
2. **BPM & Fixes**: choose a playlist, tick what you want (BPM, year, genre, cleanup), *Run (preview only)*, then *Write changes*.
3. Something wrong afterwards? Your original files are in `backups/`.


Windows/Linux: the tool is built and tested on macOS. The core should work elsewhere, but the "Browse…" buttons and the launcher script are Mac-only (type paths instead; start with `python3 webui.py`). Rekordbox's Windows file paths are untested.

## More

Technical details, command-line usage, matching rules, genre confidence and safety guarantees: [docs/DETAILS.md](docs/DETAILS.md). Run the tests with `.venv/bin/python -m unittest discover -s tests`.

## License

MIT, see [LICENSE](LICENSE).
