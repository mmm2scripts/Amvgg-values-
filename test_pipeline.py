"""End-to-end pipeline tests using --html-file (offline) and a temp data dir."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import history  # noqa: E402
import parser as page_parser  # noqa: E402
import storage  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "sample_pets.html").read_text(encoding="utf-8")


def make_pets(n=150, bump=None, drop=None):
    """Build a realistic-size synthetic page: n cards + the declared-total footer.

    bump maps pet index -> demand stars (out of 3); every other pet has 2 stars.
    """
    cards = []
    for i in range(n):
        if drop and i in drop:
            continue
        v = round(1 + i * 0.01, 4)  # only used so the card text matches; never stored
        stars = bump[i] if bump and i in bump else 2
        s = "★" * stars + "☆" * (3 - stars)
        cards.append(f'<div><p>Pet Number {i} Value {v} Demand {s} F R N M 3 days ago</p></div>')
    total = n - (len(drop) if drop else 0)
    return "<html><body>" + "".join(cards) + f"<p>Showing all {total} pets</p></body></html>"


def run(data_dir, html, *flags):
    html_path = Path(data_dir) / "page.html"
    html_path.write_text(html, encoding="utf-8")
    env = dict(os.environ, AMVGG_DATA_DIR=str(Path(data_dir) / "data"))
    return subprocess.run(
        [sys.executable, str(ROOT / "scraper.py"), "--html-file", str(html_path), *flags],
        capture_output=True, text=True, env=env, cwd=str(data_dir),
    )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.pets_file = self.dir / "data" / "pets.json"
        self.hist = self.dir / "data" / "history"

    def tearDown(self):
        self.tmp.cleanup()

    def snaps(self):
        return history.list_snapshot_files(self.hist)

    def test_first_run_writes_pets_and_full_snapshot(self):
        r = run(self.dir, make_pets())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        payload = json.loads(self.pets_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["total_pets"], 150)
        self.assertEqual(payload["source"]["url"], "https://amvgg.com/values/pets")
        self.assertTrue(payload["last_updated"].endswith("Z"))
        self.assertEqual(len(self.snaps()), 1)
        self.assertEqual(json.loads(self.snaps()[0].read_text())["type"], "full")

    def test_unchanged_rerun_writes_nothing_new(self):
        run(self.dir, make_pets())
        before = self.pets_file.read_bytes()
        r = run(self.dir, make_pets())
        self.assertEqual(r.returncode, 0)
        self.assertIn("No changes", r.stdout)
        self.assertEqual(self.pets_file.read_bytes(), before)
        self.assertEqual(len(self.snaps()), 1)

    def test_demand_change_writes_delta_snapshot(self):
        run(self.dir, make_pets())
        r = run(self.dir, make_pets(bump={0: 3, 1: 1}))
        self.assertEqual(r.returncode, 0, r.stdout)
        snaps = self.snaps()
        self.assertEqual(len(snaps), 2)
        delta = json.loads(snaps[-1].read_text())
        self.assertEqual(delta["type"], "delta")
        # pet 0: demand 2 -> 3 stars (up); pet 1: 2 -> 1 stars (down)
        self.assertEqual(delta["summary"]["demand_increases"], 1)
        self.assertEqual(delta["summary"]["demand_decreases"], 1)
        dirs = {c["direction"] for e in delta["changed"] for c in e["changes"]}
        self.assertEqual(dirs, {"up", "down"})

    def test_added_and_removed_detected(self):
        run(self.dir, make_pets())
        r = run(self.dir, make_pets(n=151, drop={3}))
        self.assertEqual(r.returncode, 0, r.stdout)
        delta = json.loads(self.snaps()[-1].read_text())
        self.assertEqual(delta["summary"]["added"], 1)
        self.assertEqual(delta["summary"]["removed"], 1)

    def test_incomplete_scrape_does_not_overwrite(self):
        run(self.dir, make_pets())
        before = self.pets_file.read_bytes()
        # Page says 150 but only 120 cards present -> incomplete
        broken = make_pets()
        for i in range(130, 150):
            broken = broken.replace(f"Pet Number {i} Value", f"Pet Number {i} Broken")
        r = run(self.dir, broken)
        self.assertEqual(r.returncode, 1)
        self.assertIn("incomplete scrape", r.stdout)
        self.assertEqual(self.pets_file.read_bytes(), before)
        self.assertEqual(len(self.snaps()), 1)

    def test_blocked_page_does_not_overwrite(self):
        run(self.dir, make_pets())
        before = self.pets_file.read_bytes()
        r = run(self.dir, "<html><body><h1>Just a moment...</h1></body></html>")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.pets_file.read_bytes(), before)

    def test_big_drop_rejected_unless_forced(self):
        run(self.dir, make_pets())
        # Consistent page (declares 100) but 33% fewer pets than before
        r = run(self.dir, make_pets(n=150, drop=set(range(50))))
        self.assertEqual(r.returncode, 1)
        self.assertIn("dropped", r.stdout)
        r = run(self.dir, make_pets(n=150, drop=set(range(50))), "--force")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_dry_run_writes_nothing(self):
        r = run(self.dir, make_pets(), "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.pets_file.exists())
        self.assertEqual(self.snaps(), [])

    def test_check_writes_nothing_and_reports(self):
        r = run(self.dir, make_pets(), "--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CHECK OK", r.stdout)
        self.assertFalse(self.pets_file.exists())

    def test_check_fails_on_bad_page(self):
        r = run(self.dir, "<html><body>nothing here</body></html>", "--check")
        self.assertEqual(r.returncode, 1)

    def test_history_command(self):
        run(self.dir, make_pets())
        run(self.dir, make_pets(bump={0: 3}))
        env = dict(os.environ, AMVGG_DATA_DIR=str(self.dir / "data"))
        out = subprocess.run([sys.executable, str(ROOT / "scraper.py"), "--history"],
                             capture_output=True, text=True, env=env).stdout
        self.assertIn("2 snapshot(s)", out)
        out = subprocess.run([sys.executable, str(ROOT / "scraper.py"), "--history", "--pet", "Pet Number 0"],
                             capture_output=True, text=True, env=env).stdout
        self.assertIn("baseline", out)
        self.assertIn("2 -> 3", out)

    def test_inspect_writes_report(self):
        html_path = self.dir / "page.html"
        html_path.write_text(FIXTURE, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scraper.py"), "--inspect", "--html-file", str(html_path)],
            capture_output=True, text=True, env=dict(os.environ, AMVGG_DATA_DIR=str(self.dir / "data")),
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("Pet cards found by text pattern", r.stdout)


class DiffTests(unittest.TestCase):
    def pet(self, pid, stars=2, total=3):
        return {"id": pid, "name": pid.title(),
                "demand": f"{stars}/{total}", "demand_stars": stars}

    def test_diff_directions(self):
        d = history.diff_pets([self.pet("a", 2), self.pet("b", 2)],
                              [self.pet("a", 3), self.pet("b", 1)])
        a = d["changed"][0]["changes"][0]
        self.assertEqual((a["field"], a["direction"], a["old"], a["new"]), ("demand_stars", "up", 2, 3))
        b = d["changed"][1]["changes"][0]
        self.assertEqual(b["direction"], "down")
        self.assertEqual(d["summary"]["demand_increases"], 1)
        self.assertEqual(d["summary"]["demand_decreases"], 1)
        self.assertEqual(d["summary"]["other_changes"], 0)

    def test_each_demand_change_counts_once(self):
        d = history.diff_pets([self.pet("a", 2)], [self.pet("a", 3)])
        self.assertEqual(len(d["changed"][0]["changes"]), 1)

    def test_hash_is_order_independent(self):
        a, b = self.pet("a", 1), self.pet("b", 2)
        self.assertEqual(storage.content_hash([a, b]), storage.content_hash([b, a]))

    def test_no_diff_when_identical(self):
        p = [self.pet("a", 1)]
        self.assertFalse(history.has_changes(history.diff_pets(p, p)))


if __name__ == "__main__":
    unittest.main()
