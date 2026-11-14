import http.client, json, os, sys, tempfile, threading, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp()
os.environ.setdefault("DJ_TOOLS_SETTINGS", os.path.join(_TMP, "settings.json"))  # never touch a real settings.json
import settings
import webui


class WebUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings.save({"setup_done": True})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse(); return r.status, r.read()

    def test_index_and_config(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        s, b = self.req("GET", "/api/config"); self.assertEqual(s, 200); self.assertIn("backup_root", json.loads(b))

    def test_foreign_host_rejected(self):
        self.assertEqual(self.req("GET", "/", host="evil.example")[0], 403)

    def test_post_needs_header_and_same_origin(self):
        self.assertEqual(self.req("POST", "/api/preview?bpm=1", body=b"x")[0], 403)
        self.assertEqual(self.req("POST", "/api/preview?bpm=1", {"X-DJ-Tools": "1", "Origin": "https://evil.example"}, b"x")[0], 403)

    def test_needs_a_tool_selected(self):
        self.assertEqual(self.req("POST", "/api/preview?bpm=0&genre=0", {"X-DJ-Tools": "1"}, b"x")[0], 400)

    def test_bad_playlist_gives_error_job(self):
        s, b = self.req("POST", "/api/preview?genre=1", {"X-DJ-Tools": "1"}, b"Name\tArtist\nA\tB\n")
        jid = json.loads(b)["id"]
        import time
        for _ in range(50):
            j = json.loads(self.req("GET", "/api/job?id=" + jid)[1])
            if j["state"] != "running": break
            time.sleep(0.1)
        self.assertEqual(j["state"], "error"); self.assertIn("Location", j["error"])

    def test_first_run_redirects_to_setup_then_save_works(self):
        sf = settings.settings_file()
        sf.unlink()
        try:
            self.assertEqual(self.req("GET", "/")[0], 302)
            self.assertEqual(self.req("GET", "/setup")[0], 200)
            h = {"X-DJ-Tools": "1"}
            self.assertEqual(self.req("POST", "/api/setup/save", h, json.dumps({"music_folder": "/nope/x"}).encode())[0], 400)
            folder = tempfile.mkdtemp()
            s, _ = self.req("POST", "/api/setup/save", h, json.dumps({"music_folder": folder, "contact": "a@b.c"}).encode())
            self.assertEqual(s, 200)
            self.assertEqual(self.req("GET", "/")[0], 200)
            self.assertEqual(json.loads(self.req("GET", "/api/setup")[1])["music_folder"], folder)
            self.assertEqual(webui.convert_web.DEFAULT_FOLDER, Path(folder))
        finally:
            settings.save({"setup_done": True})


if __name__ == "__main__":
    unittest.main()
