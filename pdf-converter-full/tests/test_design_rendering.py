import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "pdf-to-html" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import harvest_designs


@unittest.skipUnless(os.environ.get("DESIGN_TEST_CHROME"), "set DESIGN_TEST_CHROME for browser rendering")
class DesignRenderingTests(unittest.TestCase):
    def test_narrow_pages_render_at_the_requested_css_viewport(self):
        chrome = os.environ["DESIGN_TEST_CHROME"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.css").write_text(
                "html, body { margin: 0; } "
                ".box { width: 80vw; height: 20vw; margin: 30vw 10vw 0; background: #f00; }",
                encoding="utf-8",
            )
            (root / "page.html").write_text(
                '<!doctype html><html><head><link rel="stylesheet" href="page.css"></head>'
                '<body><div class="page-root"><div class="box"></div></div></body></html>',
                encoding="utf-8",
            )
            for width in (190, 320, 400):
                with self.subTest(width=width):
                    screenshot = root / f"screen-{width}.png"
                    rendered = subprocess.run(
                        [sys.executable, str(SCRIPTS / "render_chromium.py"), "--html", str(root / "page.html"),
                         "--width", str(width), "--height", "500", "--out", str(screenshot),
                         "--force-engine", "chromium", "--executable-path", chrome],
                        capture_output=True, text=True, errors="replace", timeout=60,
                    )
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    with Image.open(screenshot) as image:
                        self.assertEqual(image.size, (width, 500))
                        self.assertEqual(image.convert("RGB").getpixel((width // 2, int(width * 0.32))), (255, 0, 0))
                        self.assertEqual(image.convert("RGB").getpixel((int(width * 0.05), int(width * 0.32))),
                                         (255, 255, 255))

    def test_reference_uses_current_palette_and_visible_fallback(self):
        source_css = (
            ".d-box-a { --design-bg: #ff0000; width: 80px; height: 80px; "
            "background: var(--design-bg, transparent); }"
        )
        reference, _ = harvest_designs.tokenize_colors(source_css)
        self.assertNotIn("#ff0000", reference)
        self.assertNotIn("--design-bg:", reference)
        chrome = os.environ["DESIGN_TEST_CHROME"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, binding, expected in (
                ("fallback", "", (255, 255, 255)),
                ("current", ".d-box-a { --design-bg: #00ff00; }", (0, 255, 0)),
            ):
                work = root / name
                work.mkdir()
                (work / "fragment.html").write_text(
                    '<div class="page-root"><aside class="d-box-a" data-design-id="box-a"></aside></div>',
                    encoding="utf-8",
                )
                (work / "page.css").write_text(
                    f"/* @design box-a */\n{reference}\n{binding}\n/* @end box-a */",
                    encoding="utf-8",
                )
                saved = subprocess.run(
                    ["node", str(SCRIPTS / "save_page.mjs"), "--html", str(work / "fragment.html"),
                     "--css", str(work / "page.css"), "--out-root", str(work / "out"),
                     "--run-id", "test", "--page", "1", "--work-dir", str(work)],
                    capture_output=True, text=True,
                )
                self.assertEqual(saved.returncode, 0, saved.stderr)
                screenshot = work / "screen.png"
                rendered = subprocess.run(
                    [sys.executable, str(SCRIPTS / "render_chromium.py"), "--html", str(work / "content.html"),
                     "--width", "400", "--height", "400", "--out", str(screenshot),
                     "--force-engine", "chromium", "--executable-path", chrome],
                    capture_output=True, text=True, errors="replace", timeout=60,
                )
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                with Image.open(screenshot) as image:
                    self.assertEqual(image.convert("RGB").getpixel((10, 10)), expected)


if __name__ == "__main__":
    unittest.main()
