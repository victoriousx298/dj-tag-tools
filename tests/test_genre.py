import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import genre_suggest as g
from mutagen.id3 import ID3, TIT2, TCON

FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 400


class FakeMB:
    genre_names = {"hip hop", "pop", "soul"}
    def __init__(self, recs, art=None): self.recs, self.art = recs, art or {}
    def search_recordings(self, a, t): return self.recs
    def artist_genres(self, n): return self.art


def rec(title, artist_id, tags, score=100, artist="Eve"):
    return {"score": score, "title": title,
            "artist-credit": [{"name": artist, "artist": {"id": artist_id}}],
            "tags": [{"name": n, "count": c} for n, c in tags]}


class CleaningTests(unittest.TestCase):
    def test_title(self):
        self.assertEqual(g.clean_title("Yellow (Clean)"), "Yellow")
        self.assertEqual(g.clean_title("SpotiDownloader.com - ON ME"), "ON ME")
        self.assertEqual(g.clean_title("Song (Some Remix) [128kbps]"), "Song (Some Remix)")
    def test_base_title(self):
        self.assertEqual(g.base_title("Low (feat. T-Pain)"), "Low")
        self.assertEqual(g.base_title("Song ft. X"), "Song")
    def test_primary_artist(self):
        self.assertEqual(g.primary_artist("Flo Rida, T-Pain"), "Flo Rida")
        self.assertEqual(g.primary_artist("Skrillex & ISOxo"), "Skrillex")
    def test_pretty(self):
        self.assertEqual(g.pretty_genre("r&b"), "R&B")
        self.assertEqual(g.pretty_genre("dance-pop"), "Dance-Pop")
        self.assertEqual(g.pretty_genre("drum and bass"), "Drum and Bass")
        self.assertEqual(g.pretty_genre("edm"), "EDM")


class ConfidenceTests(unittest.TestCase):
    def test_grade(self):
        self.assertEqual(g.grade(3, "recording"), "high")
        self.assertEqual(g.grade(1, "recording", True), "high")
        self.assertEqual(g.grade(2, "recording"), "medium")
        self.assertEqual(g.grade(1, "recording"), "low")
        self.assertEqual(g.grade(3, "artist"), "medium")
        self.assertEqual(g.grade(9, "artist"), "medium")
        self.assertEqual(g.grade(3, "recording(base title)"), "medium")
        self.assertEqual(g.grade(9, "recording ambiguous"), "low")

    def test_only_official_genres_and_order(self):
        mb = FakeMB([rec("Tambourine", "A", [("hip hop", 3), ("club", 5), ("soul", 2)])])
        sugs = g.suggest(mb, "Eve", "Tambourine")
        self.assertEqual([s.genre for s in g.selected(sugs)], ["hip hop"])           # high wins
        self.assertEqual([s.genre for s in g.medium_candidates(sugs)], ["soul"])      # medium: opt-in
        self.assertNotIn("club", [s.genre for s in sugs])                             # not an official genre

    def test_top_medium_is_default_when_no_high(self):
        mb = FakeMB([rec("T", "A", [("soul", 2), ("pop", 2), ("hip hop", 1)])])
        sugs = g.suggest(mb, "Eve", "T")
        self.assertEqual(len(g.selected(sugs)), 1)                       # only the #1 medium
        self.assertEqual(g.selected(sugs)[0].confidence, "medium")
        self.assertEqual(len(g.medium_candidates(sugs)), 2)              # the other is still offered

    def test_same_name_artists_are_not_trusted(self):
        mb = FakeMB([rec("Tambourine", "A", [("hip hop", 4)]), rec("Tambourine", "B", [("pop", 4)])])
        self.assertEqual(g.selected(g.suggest(mb, "Eve", "Tambourine")), [])

    def test_max_five(self):
        mb = FakeMB([rec("T", "A", [(f"g{i}", 3) for i in range(8)])])
        mb.genre_names = {f"g{i}" for i in range(8)}
        self.assertEqual(len(g.selected(g.suggest(mb, "Eve", "T"))), 5)


class TagTests(unittest.TestCase):
    def test_write_keeps_frames_refuses_nonempty(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.mp3"; f.write_bytes(FAKE_MP3)
            t = ID3(); t.add(TIT2(encoding=3, text=["T"])); t.save(str(f), v2_version=3)
            g.write_genre(f, "Pop, Soul")
            self.assertEqual(g.read_genre(f)[0], "Pop, Soul")
            self.assertEqual(str(ID3(str(f))["TIT2"].text[0]), "T")
            g.write_genre(f, "Other")   # explicit edits of an existing genre are allowed
            self.assertEqual(g.read_genre(f)[0], "Other")


if __name__ == "__main__":
    unittest.main()
