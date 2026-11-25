"""Parser tests run against a SYNTHETIC fixture (see the comment inside it).

They prove the parser logic works (pet name + demand only) on the verified text pattern. They do NOT
prove it matches AMVGG's real markup; only a live run can do that.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import parser as page_parser  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "sample_pets.html").read_text(encoding="utf-8")
BASE = "https://amvgg.com/values/pets"


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.result = page_parser.parse_page(FIXTURE, BASE)
        self.by_name = {p["name"]: p for p in self.result.pets}

    def test_counts_and_declared_total(self):
        self.assertEqual(len(self.result.pets), 6)
        self.assertEqual(self.result.declared_total, 6)

    def test_page_order_preserved(self):
        self.assertEqual(
            [p["name"] for p in self.result.pets],
            ["Shadow Dragon", "Giraffe", "Owl", "Mini Pig", "Mystery Pet", "Cow"],
        )

    def test_name_and_demand(self):
        self.assertEqual(self.by_name["Shadow Dragon"]["demand"], "3/3")
        self.assertEqual(self.by_name["Owl"]["demand"], "2/3")
        self.assertEqual(self.by_name["Owl"]["demand_stars"], 2)
        self.assertEqual(self.by_name["Mini Pig"]["demand"], "1/3")

    def test_split_inline_elements(self):
        g = self.by_name["Giraffe"]
        self.assertEqual(g["demand"], "3/3")

    def test_unparseable_value_does_not_matter(self):
        # The "Value" number is only used to recognise a card; it is never read.
        self.assertEqual(self.by_name["Mystery Pet"]["demand"], "2/3")
        self.assertFalse(any("Mystery Pet" in w for w in self.result.warnings))

    def test_records_contain_only_name_and_demand(self):
        for p in self.result.pets:
            self.assertEqual(set(p), {"id", "name", "demand", "demand_stars"})

    def test_script_content_ignored(self):
        self.assertFalse(any("SOMETHING" in p["name"] for p in self.result.pets))

    def test_ids_unique(self):
        ids = [p["id"] for p in self.result.pets]
        self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_names_get_unique_ids(self):
        html = FIXTURE.replace("Owl Value 1.355", "Cow Value 1.355")
        res = page_parser.parse_page(html, BASE)
        ids = [p["id"] for p in res.pets]
        self.assertEqual(len(ids), len(set(ids)))

    def test_record_validation_flags_missing_demand(self):
        # Strip every star -> no pet has a demand rating -> below the coverage threshold
        res = page_parser.parse_page(FIXTURE.replace("★", "").replace("☆", ""), BASE)
        errors = page_parser.validate_records(res.pets)
        self.assertTrue(any("demand rating" in e for e in errors))

    def test_record_validation_passes_on_good_fixture(self):
        self.assertEqual(page_parser.validate_records(self.result.pets), [])

    def test_no_cards_on_unrelated_html(self):
        res = page_parser.parse_page("<html><body><p>Just a moment...</p></body></html>", BASE)
        self.assertEqual(res.pets, [])
        self.assertIsNone(res.declared_total)

    def test_inspect_report_runs(self):
        report = page_parser.inspect_html(FIXTURE, BASE)
        self.assertIn("Pet cards found by text pattern: 6", report)
        # embedded Next.js payload is decoded and shown around the first pet name
        self.assertIn("Embedded page data", report)
        self.assertIn('First occurrence of \'Shadow Dragon\'', report)
        self.assertIn("synthetic", report)


if __name__ == "__main__":
    unittest.main()
