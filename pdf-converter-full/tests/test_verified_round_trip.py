import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "pdf-to-html" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze_page


REQUIRED_ENV = ("DESIGN_TEST_CHROME", "DESIGN_TEST_POPPLER", "DESIGN_TEST_TESSERACT")


@unittest.skipUnless(all(os.environ.get(key) for key in REQUIRED_ENV), "set local Chromium, Poppler and Tesseract paths")
class VerifiedRoundTripTests(unittest.TestCase):
    def run_command(self, command, env, cwd=None):
        result = subprocess.run(
            list(map(str, command)), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, env=env, cwd=cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_scored_page_is_captured_and_reused_with_a_new_palette(self):
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join([
            env["DESIGN_TEST_POPPLER"], env["DESIGN_TEST_TESSERACT"], env.get("PATH", ""),
        ])
        chrome = env["DESIGN_TEST_CHROME"]
        env["PUPPETEER_EXECUTABLE_PATH"] = chrome

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env):
            root = Path(tmp)
            pdf = root / "synthetic.pdf"
            writer = PdfWriter()
            font = writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }))
            for color in ("0.9 0.1 0.1", "0.1 0.1 0.9"):
                page = writer.add_blank_page(width=200, height=300)
                page[NameObject("/Resources")] = DictionaryObject({
                    NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
                })
                stream = DecodedStreamObject()
                stream.set_data(
                    f"q {color} rg 20 180 160 60 re f Q\n"
                    "BT /F1 12 Tf 20 270 Td (Sample panel with color) Tj ET".encode("ascii")
                )
                page[NameObject("/Contents")] = writer._add_object(stream)
            with pdf.open("wb") as stream:
                writer.write(stream)

            catalog = {
                "pdfId": "synthetic",
                "designs": {"box.panel": {
                    "designId": "box.panel", "description": "A rectangular panel",
                    "occurrenceCount": 2, "reusable": True, "html": "", "css": "",
                }},
            }
            (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            work_dirs = [root / "page1", root / "page2"]
            sizes = []
            for page_num, work in enumerate(work_dirs, 1):
                work.mkdir()
                _, size = analyze_page.render_source_png(
                    str(pdf), page_num, str(work), 2.0, normalized_long_edge=1540
                )
                sizes.append(size)
                width, height = size
                (work / "meta.json").write_text(json.dumps({
                    "page": page_num, "imageWidth": width, "imageHeight": height,
                }), encoding="utf-8")
                (work / "designs.json").write_text(json.dumps({"designs": [{
                    "designId": "box.panel",
                    "bbox": {
                        "x": round(width * 0.1), "y": round(width * 0.3),
                        "width": round(width * 0.8), "height": round(width * 0.3),
                    },
                }]}), encoding="utf-8")

            def brief(page_num):
                return self.run_command([
                    sys.executable, SCRIPTS / "design_catalog.py", "brief",
                    "--pdf-id", "synthetic", "--page", page_num, "--catalog-dir", root,
                    "--work-dir", work_dirs[page_num - 1], "--no-s3",
                ], env)

            self.assertEqual(brief(1)["harvestTargets"], ["box-panel"])
            self.assertEqual(brief(2)["harvestTargets"], ["box-panel"])

            def render_and_score(page_num, html, css):
                work = work_dirs[page_num - 1]
                width, height = sizes[page_num - 1]
                (work / "fragment.html").write_text(html, encoding="utf-8")
                (work / "page.css").write_text(css, encoding="utf-8")
                self.run_command([
                    "node", SCRIPTS / "save_page.mjs", "--html", work / "fragment.html",
                    "--css", work / "page.css", "--out-root", work / "out",
                    "--run-id", "synthetic", "--page", page_num, "--work-dir", work,
                ], env)
                self.run_command([
                    sys.executable, SCRIPTS / "render_chromium.py", "--html", work / "content.html",
                    "--width", width, "--height", height, "--out", work / "render.png",
                    "--force-engine", "chromium", "--executable-path", chrome,
                ], env)
                score = self.run_command([
                    sys.executable, SCRIPTS / "verify_layered.py", "--source", work / "source.png",
                    "--render", work / "render.png", "--html", work / "content.html",
                    "--pdf", pdf, "--page", page_num, "--work-dir", work,
                ], env)
                (work / "final_verification.json").write_text(json.dumps(score), encoding="utf-8")
                self.assertEqual(score["page"], page_num)
                self.assertGreaterEqual(score["score"], 0.85, score)
                return score

            fragment = (
                '<div class="page-root"><section><h1>Sample panel with color</h1>'
                '<aside class="d-box-panel" data-design-id="box-panel"></aside></section></div>'
            )
            page_root = (
                ".page-root { width: 100%; margin: 0; overflow-x: hidden; position: relative; }\n"
                ".page-root h1 { font: normal 6vw/1 Arial, sans-serif; margin: 0 10%; padding-top: 9%; }\n"
            )
            original_css = (
                page_root + "/* @design box-panel */\n"
                ".d-box-panel { --design-bg: #e61a1a; width: 80%; aspect-ratio: 8 / 3; "
                "margin: 15% auto 0; background: var(--design-bg, transparent); }\n"
                "/* @end box-panel */"
            )
            render_and_score(1, fragment, original_css)
            first = work_dirs[0]
            harvested = self.run_command([
                sys.executable, SCRIPTS / "harvest_designs.py", "--html", first / "content.html",
                "--css", first / "page.css", "--work-dir", first, "--score-file", first / "final_verification.json",
                "--page", 1, "--page-width", sizes[0][0], "--page-height", sizes[0][1],
            ], env)
            self.assertEqual(harvested["harvested"], ["box-panel"])
            committed = self.run_command([
                sys.executable, SCRIPTS / "design_catalog.py", "commit", "--pdf-id", "synthetic",
                "--page", 1, "--catalog-dir", root, "--harvested", first / "harvested_designs.json", "--no-s3",
            ], env)
            self.assertEqual(committed["written"], ["box-panel"])

            next_brief = brief(2)
            self.assertEqual(next_brief["withReference"], 1)
            self.assertEqual(next_brief["harvestTargets"], [])
            reference = json.loads((work_dirs[1] / "design_brief.json").read_text(encoding="utf-8"))["designs"][0]
            self.assertNotIn("#e61a1a", reference["css"])
            second_css = (
                page_root + "/* @design box-panel */\n" + reference["css"]
                + "\n.d-box-panel { --design-bg: #1a1ae6; }\n/* @end box-panel */"
            )
            render_and_score(
                2, f'<div class="page-root"><section><h1>Sample panel with color</h1>{reference["html"]}</section></div>',
                second_css,
            )

            qa = self.run_command([
                "node", SCRIPTS / "responsive_dom_checks.mjs", "--html", work_dirs[1] / "content.html",
                "--work-dir", work_dirs[1], "--mode", "fluid",
            ], env)
            self.assertTrue(qa["pass"], qa["summary"])
            phone = self.run_command([
                "node", "--input-type=module", "-e", """
                    import puppeteer from "puppeteer";
                    import { pathToFileURL } from "node:url";
                    const browser = await puppeteer.launch({ headless: true });
                    try {
                      const page = await browser.newPage();
                      await page.setViewport({ width: 320, height: 500, deviceScaleFactor: 1 });
                      await page.goto(pathToFileURL(process.argv[1]).href, { waitUntil: "networkidle0" });
                      const result = await page.evaluate(() => {
                        const box = document.querySelector('[data-design-id="box-panel"]');
                        const rect = box.getBoundingClientRect();
                        return {
                          width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth,
                          left: rect.left, right: rect.right, boxWidth: rect.width,
                          background: getComputedStyle(box).backgroundColor
                        };
                      });
                      console.log(JSON.stringify(result));
                    } finally {
                      await browser.close();
                    }
                """, work_dirs[1] / "content.html",
            ], env, cwd=SCRIPTS)
            self.assertEqual(phone["width"], 320)
            self.assertEqual(phone["background"], "rgb(26, 26, 230)")
            self.assertGreater(phone["boxWidth"], 200)
            self.assertGreaterEqual(phone["left"], 0)
            self.assertLessEqual(phone["right"], 320)
            self.assertLessEqual(phone["scrollWidth"], 320)


if __name__ == "__main__":
    unittest.main()
