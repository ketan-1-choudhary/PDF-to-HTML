import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "pdf-to-html" / "scripts"


class RefinementTests(unittest.TestCase):
    def test_hotspot_brief_contains_only_matching_reusable_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "section_refine_plan.json").write_text(
                json.dumps({"recommendedSectionIds": ["hot"]}), encoding="utf-8"
            )
            (root / "regions.json").write_text(json.dumps([
                {"id": "hot", "designIds": ["box-a"]},
                {"id": "cold", "designIds": ["box-b"]},
            ]), encoding="utf-8")
            (root / "design_brief.json").write_text(json.dumps({"designs": [
                {"designId": "box-a", "hasReference": True, "html": "HOT-REFERENCE", "css": ".d-box-a {}"},
                {"designId": "box-b", "hasReference": True, "html": "COLD-REFERENCE", "css": ".d-box-b {}"},
            ]}), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "prepare_refine_brief.py"), "--work-dir", str(root),
                 "--pass", "1", "--previous-score", "0.8", "--weakest-layer", "layout"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            brief = Path(result["briefPath"]).read_text(encoding="utf-8")
            self.assertIn("HOT-REFERENCE", brief)
            self.assertNotIn("COLD-REFERENCE", brief)
            self.assertIn("box-a", result["focusedDesigns"])


if __name__ == "__main__":
    unittest.main()
