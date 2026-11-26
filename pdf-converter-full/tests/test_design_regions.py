import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "pdf-to-html" / "scripts"


class RegionTests(unittest.TestCase):
    def run_regions(self, root, regions, designs):
        region_path = root / "regions.json"
        design_path = root / "designs.json"
        region_path.write_text(json.dumps(regions), encoding="utf-8")
        design_path.write_text(json.dumps({"designs": designs}), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "postprocess_regions.py"), "--regions", str(region_path),
             "--image-width", "100", "--image-height", "100", "--designs", str(design_path)],
            capture_output=True, text=True,
        )

    def test_designs_snap_or_seed_regions_and_are_tagged_with_canonical_ids(self):
        regions = [{"id": "top", "name": "Top", "role": "content",
                    "bbox": {"x": 0, "y": 0, "width": 40, "height": 40}}]
        designs = [
            {"designId": "box.notice", "bbox": {"x": 10, "y": 35, "width": 20, "height": 15}},
            {"designId": "furniture.marker", "bbox": {"x": 75, "y": 75, "width": 10, "height": 10}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = self.run_regions(root, regions, designs)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads((root / "regions.json").read_text(encoding="utf-8"))
            top = next(r for r in result if r["id"] == "top")
            self.assertGreaterEqual(top["bbox"]["y"] + top["bbox"]["height"], 50)
            self.assertIn("box-notice", top["designIds"])
            marker = next(r for r in result if "furniture-marker" in r["designIds"])
            self.assertEqual(marker["bbox"], designs[1]["bbox"])

    def test_out_of_bounds_profiled_bbox_fails_instead_of_clamping(self):
        regions = [{"id": "one", "bbox": {"x": 0, "y": 0, "width": 50, "height": 50}}]
        designs = [{"designId": "box.notice", "bbox": {"x": 95, "y": 10, "width": 10, "height": 10}}]
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_regions(Path(tmp), regions, designs)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("box-notice", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
