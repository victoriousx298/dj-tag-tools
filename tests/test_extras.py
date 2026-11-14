import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import year_tool as y, tag_cleanup as c, webui, rekordbox_bpm_sync as b
from mutagen.id3 import ID3, TIT2, TPE1, TDRC, TCON

FAKE = b"\xff\xfb\x90\x00" + b"\x00" * 400


def mp3(d, name="a.mp3", **frames):
    f = Path(d) / name; f.write_bytes(FAKE)
    t = ID3()
    for fr in frames.values(): t.add(fr)
    t.save(str(f), v2_version=3); return f


class YearTests(unittest.TestCase):
    def test_replaceable(self):
        for v in (None, "", "0", "abc", "1970", "1970-01-01"): self.assertTrue(y.year_replaceable(v), v)
        for v in ("1984", "2007-10-08", "1969"): self.assertFalse(y.year_replaceable(v), v)

    def test_parse_year(self):
        self.assertEqual(b.parse_year(1984), 1984)
        for v in (0, 1970, None, "x", 1800, 2999): self.assertIsNone(b.parse_year(v))

    def test_write_fills_empty_and_1970_never_others(self):
        with tempfile.TemporaryDirectory() as d:
            f = mp3(d, t=TIT2(encoding=3, text=["T"]))
            y.write_year(f, 2007); self.assertEqual(y.read_year(f)[0], "2007")
            self.assertEqual(str(ID3(str(f))["TIT2"].text[0]), "T")
            y.write_year(f, 2010); self.assertEqual(y.read_year(f)[0], "2010")  # explicit edits are allowed
            y.write_year(f, None); self.assertIsNone(y.read_year(f)[0])           # None clears
            g = mp3(d, "b.mp3", y=TDRC(encoding=3, text=["1970"]))
            y.write_year(g, 1999); self.assertEqual(y.read_year(g)[0], "1999")  # 1970 replaced

    def test_year_comes_from_musicbrainz_not_rekordbox(self):
        class MB:
            def search_recordings(self, a, t):
                return [{"score": 100, "title": "T", "first-release-date": "2007-06", "artist-credit": [{"name": "A", "artist": {"id": "1"}}]},
                        {"score": 100, "title": "T", "first-release-date": "2013", "artist-credit": [{"name": "A", "artist": {"id": "1"}}]}]
        with tempfile.TemporaryDirectory() as d:
            f1, f2 = mp3(d, "1.mp3"), mp3(d, "2.mp3", y=TDRC(encoding=3, text=["2001"]))
            rows = [b.Row(1, str(f1), match="matched", rb_year=1984, artist="A", title="T"),
                    b.Row(2, str(f2), match="matched", rb_year=1984, artist="A", title="T")]
            out = y.plan([{"Location": str(f1)}, {"Location": str(f2)}], rows, MB())
            self.assertEqual((out[0].action, out[0].proposed, out[0].source), ("WRITE", 2007, "MusicBrainz"))  # not Rekordbox's 1984
            self.assertEqual(out[1].action, "SKIP")                                                            # 2001 is protected

    def test_1970_cleared_when_musicbrainz_has_no_year(self):
        class MB:
            def search_recordings(self, a, t): return []
        with tempfile.TemporaryDirectory() as d:
            f = mp3(d, "c.mp3", y=TDRC(encoding=3, text=["1970"]))
            out = y.plan([{"Location": str(f), "Artist": "A", "Name": "T"}], None, MB())
            self.assertEqual((out[0].action, out[0].proposed), ("WRITE", None))
            ok, failed = y.apply_writes(out, Path(d) / "bk")
            self.assertEqual((ok, failed), (1, 0)); self.assertIsNone(y.read_year(f)[0])


class CleanupTests(unittest.TestCase):
    def test_clean_text_keeps_dj_words(self):
        self.assertEqual(c.clean_text("SpotiDownloader.com - ON ME"), "ON ME")
        self.assertEqual(c.clean_text("Idols (EDM Mashup) (128kbps)"), "Idols (EDM Mashup) (128kbps)")  # bitrate left alone
        self.assertEqual(c.clean_text("Song [128kbit_AAC] (SPOTISAVER)"), "Song [128kbit_AAC]")
        self.assertEqual(c.clean_text("Song (Clean)"), "Song (Clean)")
        self.assertEqual(c.clean_text("Song (Extended Mix) [Official Video]"), "Song (Extended Mix)")
        self.assertEqual(c.clean_text("Nikita,  the Wicked"), "Nikita, the Wicked")

    def test_fill_from_filename_only_when_missing(self):
        ch = c.propose({"artist": "Unknown Artist", "title": "", "album": ""}, "Eve - Tambourine")
        self.assertEqual(ch["artist"][0], "Eve"); self.assertEqual(ch["title"][0], "Tambourine")
        ch = c.propose({"artist": "Real Artist", "title": "Real Title", "album": ""}, "Other - Name")
        self.assertEqual(ch, {})                                   # existing text never replaced

    def test_write_only_selected_fields(self):
        with tempfile.TemporaryDirectory() as d:
            f = mp3(d, "Eve - Tambourine.mp3", t=TIT2(encoding=3, text=["SpotiDownloader.com - X (128kbps)"]), a=TPE1(encoding=3, text=["Unknown Artist"]))
            r = c.plan([{"Location": str(f)}])[0]
            self.assertEqual(set(r.changes), {"title", "artist"})
            r.selected = {"title"}
            ok, failed = c.apply_writes([r], Path(d) / "bk")
            self.assertEqual((ok, failed), (1, 0))
            now, _ = c.read_fields(f)
            self.assertEqual((now["title"], now["artist"]), ("X (128kbps)", "Unknown Artist"))
            self.assertEqual(len(list((Path(d) / "bk").glob("*.mp3"))), 1)


class SelectionTests(unittest.TestCase):
    def job(self, d):
        import genre_suggest as g
        f = mp3(d)
        j = webui.Job("p.txt", dict(bpm=False, genre=True, year=False, cleanup=False, overwrite=False, quality=False))
        j.entries = [{"Location": str(f)}]
        j.genre_rows = [g.Row(1, str(f), str(f), "A", "T", action="REVIEW")]
        return j

    def test_valid_and_limits(self):
        with tempfile.TemporaryDirectory() as d:
            j = self.job(d)
            self.assertEqual(webui.parse_selection(j, {"genre": {"1": ["Pop", "pop", " Hip  Hop "]}})["genre"], {1: ["Pop", "Hip Hop"]})
            with self.assertRaises(ValueError): webui.parse_selection(j, {"genre": {"1": list("abcdef")}})   # > 5
            with self.assertRaises(ValueError): webui.parse_selection(j, {"genre": {"1": ["a;b"]}})
            with self.assertRaises(ValueError): webui.parse_selection(j, {"genre": {"9": ["Pop"]}})

    def test_existing_genre_can_be_edited_and_roundtrips_commas(self):
        import genre_suggest as g
        from mutagen.id3 import TCON
        with tempfile.TemporaryDirectory() as d:
            j = self.job(d); j.genre_rows[0].existing = "Rock"; j.genre_rows[0].action = "KEPT"
            self.assertEqual(webui.parse_selection(j, {"genre": {"1": ["Rock", "Pop"]}})["genre"], {1: ["Rock", "Pop"]})
            f = mp3(d, "g.mp3", t=TCON(encoding=3, text=["House,Techno"]))
            self.assertEqual(g.read_genre(f)[0], "House, Techno")
            g.write_genre(f, "Pop, Rock"); self.assertEqual(g.read_genre(f)[0], "Pop, Rock")

    def test_manual_bpm_and_year_edits_validated(self):
        with tempfile.TemporaryDirectory() as d:
            f = mp3(d)
            j = webui.Job("p.txt", dict(bpm=True, genre=False, year=True, cleanup=False, overwrite=False, quality=False))
            j.entries = [{}]
            j.bpm_rows = [b.Row(1, str(f), str(f))]; j.year_rows = [y.YearRow(1, str(f), "A", "T")]
            s = webui.parse_selection(j, {"bpm_set": {"1": "128"}, "year_set": {"1": "1999"}})
            self.assertEqual((s["bpm_set"], s["year_set"]), ({1: "128"}, {1: 1999}))
            self.assertEqual(webui.parse_selection(j, {"year_set": {"1": ""}})["year_set"], {1: None})
            with self.assertRaises(ValueError): webui.parse_selection(j, {"bpm_set": {"1": "abc"}})
            with self.assertRaises(ValueError): webui.parse_selection(j, {"year_set": {"1": "99"}})


if __name__ == "__main__":
    unittest.main()
