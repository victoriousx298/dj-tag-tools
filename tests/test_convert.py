import http.client, json, subprocess, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ.setdefault("DJ_TOOLS_SETTINGS", os.path.join(tempfile.mkdtemp(), "settings.json"))  # never touch a real settings.json
import settings
settings.save({"setup_done": True})
import convert_tool as ct
import webui


def wav(path, **tags):
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    subprocess.run([ct.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=d=1", *meta, str(path)], check=True)


class NameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ct.make_filename("A/B", "T: x/y"), "A, B - T- xy.mp3")
        self.assertEqual(ct.make_filename("A / B/C", "T"), "A, B, C - T.mp3")
        self.assertIsNone(ct.make_filename("", "T"))


class TitleYearTests(unittest.TestCase):
    def test_normalize_title(self):
        n = ct.normalize_title
        self.assertEqual(n("That Acid - Dimension & Subsonic Remix"), "That Acid (Dimension & Subsonic Remix)")
        self.assertEqual(n("Closer - Edit"), "Closer (Edit)")
        self.assertEqual(n("Who Is Alice - Remastered"), "Who Is Alice (Remastered)")
        self.assertEqual(n("Bass Cannon (Flux's Version) - Crankdat Remix"), "Bass Cannon (Flux's Version) [Crankdat Remix]")
        self.assertEqual(n("Lady Marmalade - From Soundtrack"), "Lady Marmalade - From Soundtrack")
        self.assertEqual(n("Already (Fine Remix)"), "Already (Fine Remix)")

    def test_year_rules(self):
        base = dict(kind="mp3", artist="A", title="T", name="x.mp3", path="/nonexistent/x.mp3", error="", bitrate=0, id=0)
        pl = lambda year, props=None, ed=None: ct.plan([{**base, "year": year}], {0: ed or {}}, props)[0]
        ok = {0: {"year": 2019, "ok": True}}
        self.assertEqual(pl("", ok)["year_write"], "2019")
        self.assertEqual(pl("1970", ok)["year_write"], "2019")
        self.assertIsNone(pl("2001", ok)["year_write"])                       # existing real year is protected
        self.assertEqual(pl("1970", {0: {"year": None, "ok": True}})["year_write"], "")   # 1970 cleared
        self.assertIsNone(pl("1970", {0: {"year": None, "ok": False}})["year_write"])     # failed lookup: keep
        self.assertEqual(pl("2001", None, {"year": "1999"})["year_write"], "1999")        # manual edit wins
        self.assertIsNone(pl("2001", None, {"year": "abc"})["year_write"])

    def test_year_written(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); wav(p / "a.wav", artist="P", title="S - Dub Mix", date="1970")
            rows = ct.scan_folder(p, False)
            with mock.patch.object(ct, "trash", lambda q: q.unlink()):
                res, _ = ct.apply(rows, {}, {0}, p / "bk", proposals={0: {"year": 2015, "ok": True}})
            self.assertEqual(res[0]["result"], "ok")
            out = p / "P - S (Dub Mix).mp3"
            self.assertTrue(out.is_file())
            r = ct.read_basic(out)
            self.assertEqual((r["year"], r["title"]), ("2015", "S (Dub Mix)"))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.p = Path(self.d.name)
    def tearDown(self): self.d.cleanup()

    def test_collision_and_notags(self):
        wav(self.p / "a.wav", artist="X", title="Y"); wav(self.p / "b.wav", artist="X", title="Y"); wav(self.p / "c.wav")
        (self.p / "c.mp3").write_bytes(b"x")  # c.wav -> c.mp3 would overwrite
        rows = ct.scan_folder(self.p, False)
        pl = {p["id"]: p for p in ct.plan(rows, {})}
        acts = sorted(pl[i]["action"] for i in (0, 1))
        self.assertEqual(acts, ["CONVERT", "SKIP"])
        self.assertEqual(pl[2]["action"], "SKIP")

    def test_apply_converts_and_trashes(self):
        wav(self.p / "a.wav", artist="P/Q", title="Song")
        rows = ct.scan_folder(self.p, False)
        with mock.patch.object(ct, "trash", lambda p: p.unlink()):
            res, bk = ct.apply(rows, {0: {"title": "Song 2"}}, {0}, self.p / "bk")
        self.assertEqual(res[0]["result"], "ok")
        out = self.p / "P, Q - Song 2.mp3"
        self.assertTrue(out.is_file()); self.assertFalse((self.p / "a.wav").exists())
        info = ct.read_basic(out)
        self.assertEqual((round(info["bitrate"], -3), info["title"], info["artist"]), (320000, "Song 2", "P, Q"))

    def test_failed_verification_keeps_wav(self):
        wav(self.p / "a.wav", artist="P", title="S")
        rows = ct.scan_folder(self.p, False)
        with mock.patch.object(ct, "convert_wav", side_effect=OSError("boom")):
            res, _ = ct.apply(rows, {}, {0}, self.p / "bk")
        self.assertTrue(res[0]["result"].startswith("FAILED")); self.assertTrue((self.p / "a.wav").exists())
        self.assertEqual([x.name for x in self.p.glob("*.mp3")], [])


class HttpTests(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as d:
            wav(Path(d) / "a.wav", artist="P", title="S")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler); port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            def req(m, path, body=None, h=True):
                c = http.client.HTTPConnection("127.0.0.1", port)
                c.request(m, path, body=json.dumps(body) if body is not None else None,
                          headers={"Host": f"127.0.0.1:{port}", **({"X-DJ-Tools": "1"} if h else {})})
                r = c.getresponse(); data = r.read(); c.close(); return r.status, json.loads(data or b"{}") if r.getheader("Content-Type") == "application/json" else data
            self.assertEqual(req("GET", "/convert")[0], 200); self.assertEqual(req("GET", "/bpm")[0], 200)
            self.assertEqual(req("POST", "/api/convert/scan", {"path": d}, h=False)[0], 403)
            self.assertEqual(req("POST", "/api/convert/scan", {"path": d + "/nope"})[0], 400)
            _, j = req("POST", "/api/convert/scan", {"path": d, "recursive": False})
            for _ in range(50):
                _, job = req("GET", "/api/convert/job?id=" + j["id"])
                if job["state"] == "ready": break
                time.sleep(.1)
            self.assertEqual(job["plan"][0]["filename"], "P - S.mp3")
            s, det = req("GET", f"/api/convert/details?id={j['id']}&row=0"); self.assertEqual(s, 200)
            self.assertEqual(req("GET", f"/api/convert/details?id={j['id']}&row=9")[0], 404)
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
