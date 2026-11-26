import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter


SCRIPTS = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "pdf-to-html" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze_page


@unittest.skipUnless(os.environ.get("PROFILER_RENDER_SCRIPT"), "set PROFILER_RENDER_SCRIPT to compare renderers")
class ProfilerRenderTests(unittest.TestCase):
    def test_converter_matches_profiler_for_real_and_small_landscape_pages(self):
        profiler = Path(os.environ["PROFILER_RENDER_SCRIPT"])
        real_pdf = Path(os.environ["PROFILER_PDF"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            small_pdf = root / "small.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=240, height=120)
            with small_pdf.open("wb") as stream:
                writer.write(stream)
            for index, (pdf, page) in enumerate(((real_pdf, 1), (real_pdf, 2), (small_pdf, 1))):
                profiler_work = root / f"profiler-{index}"
                converter_work = root / f"converter-{index}"
                result = subprocess.run(
                    [sys.executable, str(profiler), "--pdf", str(pdf), "--page", str(page),
                     "--work-dir", str(profiler_work)],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                converter_path, size = analyze_page.render_source_png(
                    str(pdf), page, str(converter_work), 2.0, normalized_long_edge=1540
                )
                with Image.open(profiler_work / "page.png") as expected, Image.open(converter_path) as actual:
                    self.assertEqual(size, expected.size)
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.convert("RGB").tobytes(), expected.convert("RGB").tobytes())
                self.assertEqual(max(size), 1540)
                if pdf == real_pdf and os.environ.get("PROFILER_FIXTURE"):
                    fixture = Path(os.environ["PROFILER_FIXTURE"])
                    catalog = json.loads((fixture / "catalog.json").read_text(encoding="utf-8"))
                    page_designs = json.loads(
                        (fixture / "pages" / str(page) / "designs.json").read_text(encoding="utf-8")
                    )
                    (converter_work / "designs.json").write_text(json.dumps(page_designs), encoding="utf-8")
                    brief = subprocess.run(
                        [sys.executable, str(SCRIPTS / "design_catalog.py"), "brief",
                         "--pdf-id", catalog["pdfId"], "--page", str(page), "--work-dir", str(converter_work),
                         "--catalog-dir", str(fixture), "--no-s3"],
                        capture_output=True, text=True, encoding="utf-8",
                    )
                    self.assertEqual(brief.returncode, 0, brief.stderr)
                    regions_path = converter_work / "regions.json"
                    regions_path.write_text(json.dumps([{
                        "id": "page", "role": "content",
                        "bbox": {"x": 0, "y": 0, "width": size[0], "height": size[1]},
                    }]), encoding="utf-8")
                    regions = subprocess.run(
                        [sys.executable, str(SCRIPTS / "postprocess_regions.py"), "--regions", str(regions_path),
                         "--image-width", str(size[0]), "--image-height", str(size[1]),
                         "--designs", str(converter_work / "designs_normalized.json")],
                        capture_output=True, text=True, encoding="utf-8",
                    )
                    self.assertEqual(regions.returncode, 0, regions.stderr)
                    normalized = json.loads((converter_work / "designs_normalized.json").read_text(encoding="utf-8"))
                    region = json.loads(regions_path.read_text(encoding="utf-8"))[0]
                    self.assertEqual(set(region["designIds"]), {d["designId"] for d in normalized["designs"]})


if __name__ == "__main__":
    unittest.main()
