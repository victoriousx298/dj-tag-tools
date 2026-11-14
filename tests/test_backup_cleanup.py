import os, sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backup_cleanup


class PruneTests(unittest.TestCase):
    def test_prunes_only_old_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, age in (("old", 40), ("new", 5), ("edge", 29)):
                f = root / name / "sub" / "x.mp3"; f.parent.mkdir(parents=True); f.write_text("x")
                t = time.time() - age * 86400
                for p in (f, f.parent, f.parent.parent): os.utime(p, (t, t))
            (root / "note.txt").write_text("keep")
            self.assertEqual(backup_cleanup.prune(root), ["old"])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["edge", "new", "note.txt"])

    def test_missing_root(self):
        self.assertEqual(backup_cleanup.prune(Path("/nonexistent/backups")), [])


if __name__ == "__main__":
    unittest.main()
