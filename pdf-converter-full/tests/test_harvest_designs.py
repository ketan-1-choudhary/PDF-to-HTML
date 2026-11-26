import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "pdf-to-html" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_designs


class HarvestTests(unittest.TestCase):
    def test_named_colors_gradients_and_inline_styles_are_tokenized_without_urls(self):
        css = (
            '.d-box { --design-bg: #fff; color: red; border-color: rebeccapurple; '
            'background: linear-gradient(#123456, rgb(1 2 3)); background-image: url("red.svg"); }'
        )
        fixed, replacements = harvest_designs.tokenize_colors(css)
        self.assertNotIn("color: red", fixed)
        self.assertNotIn("rebeccapurple", fixed)
        self.assertNotIn("#123456", fixed)
        self.assertNotIn("rgb(1 2 3)", fixed)
        self.assertNotIn("--design-bg:", fixed)
        self.assertIn('url("red.svg")', fixed)
        self.assertIn("--design-text", fixed)
        self.assertIn("--design-border", fixed)
        self.assertGreaterEqual(len(replacements), 4)

        html, replacements = harvest_designs.tokenize_inline_styles(
            "<aside style='--design-bg: #fff; color: blue'>example</aside>"
        )
        self.assertNotIn("#fff", html)
        self.assertNotIn("blue", html)
        self.assertNotIn("--design-bg:", html)
        self.assertIn("--design-text", html)
        self.assertTrue(replacements)

    def test_nested_designs_and_edge_occurrences_are_kept(self):
        html = '<div data-design-id="box.outer"><span data-design-id="box.inner">ok</span></div>'
        found = harvest_designs.extract_html_occurrences(html)
        self.assertEqual(found["box-outer"], [html])
        self.assertEqual(found["box-inner"], ['<span data-design-id="box.inner">ok</span>'])
        self.assertEqual(harvest_designs.pick_occurrence([{"x": 0, "y": 1, "width": 5, "height": 5}], 10, 10)[0], 0)

    def test_selector_scan_does_not_match_another_design_id(self):
        css = ".d-box-other { color: red; } .d-box { color: blue; }"
        result = harvest_designs.extract_css_fallback(css, "d-box")
        self.assertIn(".d-box {", result)
        self.assertNotIn(".d-box-other", result)

    def test_missing_class_and_ancestor_rooted_css_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = [
                {"designId": "box-a", "instances": []},
                {"designId": "box-b", "instances": []},
            ]
            (root / "design_harvest_targets.json").write_text(json.dumps(targets), encoding="utf-8")
            (root / "content.html").write_text(
                '<div><aside data-design-id="box-a"></aside>'
                '<aside data-design-id="box-b" class="d-box-b"></aside></div>',
                encoding="utf-8",
            )
            (root / "page.css").write_text(
                "/* @design box-a */ .d-box-a { color: red; } /* @end box-a */\n"
                "/* @design box-b */ .page-root .d-box-b { color: blue; } /* @end box-b */",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harvest_designs.py"), "--html", str(root / "content.html"),
                 "--css", str(root / "page.css"), "--work-dir", str(root), "--score", "0.9"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            skipped = {item["designId"]: item["reason"] for item in json.loads(proc.stdout)["skipped"]}
            self.assertIn("missing d-box-a", skipped["box-a"])
            self.assertIn("not self-contained", skipped["box-b"])

    def test_occurrence_count_mismatch_warns_and_uses_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = [{"designId": "box-a", "instances": [
                {"x": 1, "y": 1, "width": 5, "height": 5},
                {"x": 20, "y": 20, "width": 5, "height": 5},
            ]}]
            (root / "design_harvest_targets.json").write_text(json.dumps(targets), encoding="utf-8")
            (root / "content.html").write_text(
                '<aside data-design-id="box-a" class="d-box-a">one</aside>', encoding="utf-8"
            )
            (root / "page.css").write_text(
                "/* @design box-a */ .d-box-a { color: red; } /* @end box-a */", encoding="utf-8"
            )
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harvest_designs.py"), "--html", str(root / "content.html"),
                 "--css", str(root / "page.css"), "--work-dir", str(root), "--score", "0.9"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertIn("2 bboxes vs 1 rendered", result["warnings"][0])
            captured = json.loads((root / "harvested_designs.json").read_text(encoding="utf-8"))
            self.assertEqual(captured[0]["occurrenceIndex"], 0)

    def test_score_is_required_and_must_be_finite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design_harvest_targets.json").write_text("[]", encoding="utf-8")
            args = [sys.executable, str(SCRIPTS / "harvest_designs.py"), "--html", root / "x.html",
                    "--css", root / "x.css", "--work-dir", root]
            missing = subprocess.run(list(map(str, args)), capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            invalid = subprocess.run(list(map(str, args + ["--score", "nan"])), capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)

    def test_below_floor_reports_reason_without_harvesting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = [{"designId": "box-a", "instances": [{"x": 0, "y": 0, "width": 5, "height": 5}]}]
            (root / "design_harvest_targets.json").write_text(json.dumps(targets), encoding="utf-8")
            (root / "score.json").write_text(json.dumps({"page": 1, "score": 0.8499}), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harvest_designs.py"), "--html", str(root / "x.html"),
                 "--css", str(root / "x.css"), "--work-dir", str(root),
                 "--score-file", str(root / "score.json"), "--page", "1"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["harvested"], [])
            self.assertIn("below", result["skipped"][0]["reason"])
            self.assertEqual(json.loads((root / "harvested_designs.json").read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
