import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rekordbox_bpm_sync as s
from mutagen.id3 import ID3, TIT2, TPE1, TBPM

FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 400


class PathTests(unittest.TestCase):
    def test_macintosh_hd_prefix(self):
        self.assertEqual(s.resolve_location("Macintosh HD/Users/dj/Music/a b.mp3"), "/Users/dj/Music/a b.mp3")

    def test_file_url_percent_decoding(self):
        self.assertEqual(s.resolve_location("file://localhost/Users/t/Caf%C3%A9%20%26%20Co.mp3"), "/Users/t/Café & Co.mp3")

    def test_plain_path_not_percent_decoded(self):
        self.assertEqual(s.resolve_location("/Users/t/100%25.mp3"), "/Users/t/100%25.mp3")

    def test_remote_url_rejected(self):
        self.assertIsNone(s.resolve_location("file://server/share/a.mp3"))

    def test_nfc_nfd_and_case_equal_keys(self):
        nfd = "/Users/t/Café.mp3"
        self.assertEqual(s.path_key(nfd), s.path_key("/users/t/Café.mp3"))

    def test_empty(self):
        self.assertIsNone(s.resolve_location(""))


class PlaylistTests(unittest.TestCase):
    def test_cr_line_endings_and_columns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.txt"
            p.write_bytes("Name\tArtist\tLocation\rSong\tMe\tMacintosh HD/Users/x/a.mp3\r".encode())
            _, rows = s.parse_playlist(p)
            self.assertEqual(rows[0]["Location"], "Macintosh HD/Users/x/a.mp3")
            self.assertEqual(rows[0]["Artist"], "Me")

    def test_missing_location_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.txt"; p.write_text("Name\tArtist\nA\tB\n")
            with self.assertRaises(ValueError):
                s.parse_playlist(p)


class BpmTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(s.parse_bpm("128.5"), 128.5)
        for bad in (None, "", "0", "-3", "abc", "nan"):
            self.assertIsNone(s.parse_bpm(bad))

    def test_format(self):
        self.assertEqual(s.format_bpm(128.0, False), "128")
        self.assertEqual(s.format_bpm(109.97, False), "110")
        self.assertEqual(s.format_bpm(150.01, False), "150")
        self.assertEqual(s.format_bpm(145.46, False), "145.5")
        self.assertEqual(s.format_bpm(140.5, False), "140.5")
        self.assertEqual(s.format_bpm(109.97, True), "110")


class XmlTests(unittest.TestCase):
    def test_parse_xml_export(self):
        with tempfile.TemporaryDirectory() as d:
            x = Path(d) / "rb.xml"
            x.write_text('<?xml version="1.0"?><DJ_PLAYLISTS><COLLECTION Entries="2">'
                         '<TRACK TrackID="1" Name="T" Artist="A" AverageBpm="128.50" Location="file://localhost/Users/t/a%20b.mp3"/>'
                         '<TRACK TrackID="2" Name="U" Artist="B" AverageBpm="0.00" Location="file://localhost/Users/t/c.mp3"/>'
                         '</COLLECTION></DJ_PLAYLISTS>')
            wanted = {s.path_key("/Users/t/a b.mp3"), s.path_key("/Users/t/c.mp3")}
            found = s.load_from_xml(x, wanted)
            self.assertEqual(found[s.path_key("/Users/t/a b.mp3")][0].bpm_raw, 128.5)
            self.assertIsNone(found[s.path_key("/Users/t/c.mp3")][0].bpm_raw)


class WriteTests(unittest.TestCase):
    def test_write_preserves_frames_and_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.mp3"; f.write_bytes(FAKE_MP3)
            t = ID3(); t.add(TIT2(encoding=3, text=["Title"])); t.add(TPE1(encoding=3, text=["Artist"])); t.save(str(f), v2_version=3)
            s.write_bpm(f, "128.5")
            t2 = ID3(str(f))
            self.assertEqual(str(t2["TBPM"].text[0]), "128.5")
            self.assertEqual(str(t2["TIT2"].text[0]), "Title")
            self.assertEqual(str(t2["TPE1"].text[0]), "Artist")
            self.assertEqual(t2.version[1], 3)

    def test_plan_dry_run_never_modifies(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.mp3"; f.write_bytes(FAKE_MP3)
            before = f.read_bytes()
            lib = {s.path_key(str(f)): [s.RbTrack("1", str(f), 130.0, "A", "T")]}
            rows = s.plan([{"Location": str(f), "Artist": "A", "Name": "T"}, {"Location": "/nope.mp3"}],
                          SimpleNamespace(overwrite=False, round=False), lambda w: lib)
            self.assertEqual(rows[0].action, "UPDATE")
            self.assertEqual(rows[1].match, "unmatched")
            self.assertEqual(f.read_bytes(), before)

    def test_existing_bpm_skipped_unless_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.mp3"; f.write_bytes(FAKE_MP3)
            t = ID3(); t.add(TBPM(encoding=3, text=["100"])); t.save(str(f))
            lib = {s.path_key(str(f)): [s.RbTrack("1", str(f), 130.0, "A", "T")]}
            e = [{"Location": str(f)}]
            self.assertEqual(s.plan(e, SimpleNamespace(overwrite=False, round=False), lambda w: lib)[0].action, "SKIP")
            self.assertEqual(s.plan(e, SimpleNamespace(overwrite=True, round=False), lambda w: lib)[0].action, "UPDATE")


if __name__ == "__main__":
    unittest.main()
