import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".claude" / "skills" / "pdf-to-html" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import design_catalog


def run_script(name, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class BriefTests(unittest.TestCase):
    def test_missing_page_design_fails(self):
        catalog = {"pdfId": "book", "designs": {}}
        page = [{"designId": "box-notice", "bbox": {"x": 0, "y": 0, "width": 5, "height": 5}}]
        with self.assertRaisesRegex(ValueError, "box-notice"):
            design_catalog.build_brief(catalog, page, 60000)

    def test_budget_counts_utf8_bytes_and_does_not_target_existing_reference(self):
        html = "<div>éé</div>"
        css = ".d-box-notice { color: red; }"
        catalog = {
            "pdfId": "book",
            "designs": {
                "box-notice": {
                    "designId": "box-notice",
                    "description": "Notice",
                    "reusable": True,
                    "html": html,
                    "css": css,
                }
            },
        }
        page = [{"designId": "box-notice", "bbox": {"x": 0, "y": 0, "width": 5, "height": 5}}]
        brief, targets, _ = design_catalog.build_brief(catalog, page, len(html) + len(css))
        self.assertFalse(brief["designs"][0]["hasReference"])
        self.assertTrue(brief["designs"][0]["droppedForBudget"])
        self.assertNotIn("html", brief["designs"][0])
        self.assertNotIn("css", brief["designs"][0])
        self.assertEqual(targets, [])

    def test_icon_designs_are_flagged_for_paste_as_is(self):
        catalog = {
            "pdfId": "book",
            "designs": {
                "icon-media-video": {"designId": "icon-media-video", "html": "<span><svg></svg></span>",
                                     "css": ".d-icon-media-video {}"},
                "box-notice": {"designId": "box-notice"},
            },
        }
        bbox = {"x": 0, "y": 0, "width": 5, "height": 5}
        page = [{"designId": "icon-media-video", "bbox": bbox}, {"designId": "box-notice", "bbox": bbox}]
        brief, _, _ = design_catalog.build_brief(catalog, page, 60000)
        flags = {d["designId"]: d["isIcon"] for d in brief["designs"]}
        self.assertEqual(flags, {"icon-media-video": True, "box-notice": False})

    def test_non_reusable_design_is_neither_a_reference_nor_a_target(self):
        catalog = {
            "pdfId": "book",
            "designs": {
                "box-notice": {
                    "designId": "box-notice",
                    "reusable": False,
                    "html": "<div>old</div>",
                    "css": ".d-box-notice {}",
                }
            },
        }
        page = [{"designId": "box-notice", "bbox": {"x": 0, "y": 0, "width": 5, "height": 5}}]
        brief, targets, _ = design_catalog.build_brief(catalog, page, 60000)
        self.assertEqual(brief["designs"], [])
        self.assertEqual(targets, [])
        self.assertEqual(brief["nonReusableDesigns"][0]["designId"], "box-notice")
        self.assertNotIn("html", brief["nonReusableDesigns"][0])
        self.assertNotIn("css", brief["nonReusableDesigns"][0])

    def test_normalization_collision_fails_instead_of_discarding_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp) / "catalog.json",
                {
                    "pdfId": "book",
                    "designs": {
                        "box.a-b": {"designId": "box.a-b"},
                        "box-a.b": {"designId": "box-a.b"},
                    },
                },
            )
            store = design_catalog.CatalogStore("book", tmp, use_s3=False)
            with self.assertRaisesRegex(ValueError, "box-a-b"):
                store.load()

    def test_s3_load_refreshes_existing_catalog_and_page_files(self):
        class FakeS3:
            def __init__(self):
                self.objects = {
                    "book/designs/catalog.json": {
                        "pdfId": "book",
                        "designs": {"box.a": {"designId": "box.a", "description": "fresh"}},
                    },
                    "book/pages/1/designs.json": {
                        "designs": [{"designId": "box.a", "bbox": {"x": 1, "y": 1, "width": 2, "height": 2}}]
                    },
                }
                self.downloads = []

            def download_file(self, bucket, key, filename):
                self.downloads.append((bucket, key))
                write_json(Path(filename), self.objects[key])

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"S3_BUCKET": "bucket"}):
            root = Path(tmp)
            write_json(root / "catalog.json", {"pdfId": "old-book", "designs": {}})
            write_json(root / "work" / "designs.json", {"designs": []})
            store = design_catalog.CatalogStore("book", root)
            fake = FakeS3()
            with patch.object(store, "_client", return_value=fake):
                self.assertEqual(store.load()["designs"]["box-a"]["description"], "fresh")
                self.assertEqual(store.load_page_designs(1, root / "work")[0]["designId"], "box-a")
                fake.objects["book/designs/catalog.json"]["designs"]["box.a"]["description"] = "newer"
                self.assertEqual(store.load()["designs"]["box-a"]["description"], "newer")
            self.assertEqual(len(fake.downloads), 3)


class CommitTests(unittest.TestCase):
    def test_only_verified_score_at_or_above_floor_is_committed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = {
                "pdfId": "book",
                "designs": {
                    "box.notice": {
                        "designId": "box.notice",
                        "description": "Notice",
                        "reusable": True,
                        "html": "",
                        "css": "",
                    }
                },
            }
            write_json(root / "catalog.json", catalog)
            harvested = root / "harvested.json"
            item = {"designId": "box-notice", "html": "<aside>one</aside>", "css": ".d-box-notice {}", "score": 0.84}
            write_json(harvested, [item])
            args = ["commit", "--pdf-id", "book", "--page", "1", "--catalog-dir", root,
                    "--harvested", harvested, "--page-width", "1155", "--page-height", "1540", "--no-s3"]
            low = run_script("design_catalog.py", *args)
            self.assertEqual(low.returncode, 0, low.stderr)
            self.assertEqual(json.loads(low.stdout)["written"], [])
            self.assertIn("score", json.loads(low.stdout)["skipped"][0]["reason"])

            item["score"] = 0.85
            write_json(harvested, [item])
            good = run_script("design_catalog.py", *args)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(json.loads(good.stdout)["written"], ["box-notice"])
            saved = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            self.assertNotIn("pageWidth", saved)
            self.assertNotIn("pageHeight", saved)

            item["html"] = "<aside>replacement</aside>"
            write_json(harvested, [item])
            repeated = run_script("design_catalog.py", *args)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["skipped"][0]["reason"], "already captured")
            saved = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["designs"]["box-notice"]["html"], "<aside>one</aside>")

    def test_missing_harvest_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "catalog.json", {"pdfId": "book", "designs": {}})
            proc = run_script(
                "design_catalog.py", "commit", "--pdf-id", "book", "--page", "1",
                "--catalog-dir", root, "--harvested", root / "missing.json", "--no-s3",
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing.json", proc.stderr)

    def test_s3_commit_refreshes_uploads_and_never_overwrites(self):
        class FakeS3:
            def __init__(self):
                self.objects = {
                    "book/designs/catalog.json": {
                        "pdfId": "book",
                        "designs": {"box.a": {
                            "designId": "box.a", "reusable": True, "html": "", "css": "",
                        }},
                    }
                }
                self.uploads = []

            def download_file(self, bucket, key, filename):
                write_json(Path(filename), self.objects[key])

            def upload_file(self, filename, bucket, key, ExtraArgs):
                self.uploads.append((bucket, key, ExtraArgs))
                self.objects[key] = json.loads(Path(filename).read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"S3_BUCKET": "bucket"}):
            root = Path(tmp)
            harvested = root / "harvested.json"
            write_json(harvested, [{
                "designId": "box-a", "html": "<aside>one</aside>",
                "css": ".d-box-a {}", "score": 0.9,
            }])
            args = SimpleNamespace(
                pdf_id="book", catalog_dir=root, no_s3=False, harvested=harvested,
                page=1, page_width=1155, page_height=1540, min_score=0.85,
            )
            fake = FakeS3()
            with patch.object(design_catalog.CatalogStore, "_client", return_value=fake):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(design_catalog.cmd_commit(args), 0)
                self.assertEqual(fake.uploads[0][1], "book/designs/catalog.json")
                self.assertEqual(fake.objects["book/designs/catalog.json"]["designs"]["box-a"]["sourceScore"], 0.9)
                write_json(root / "catalog.json", {"pdfId": "book", "designs": {}})
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(design_catalog.cmd_commit(args), 0)
                self.assertEqual(json.loads(output.getvalue())["skipped"][0]["reason"], "already captured")
                self.assertEqual(len(fake.uploads), 1)


@unittest.skipUnless(os.environ.get("PROFILER_FIXTURE"), "set PROFILER_FIXTURE to test a real export")
class RealProfilerExportTests(unittest.TestCase):
    def test_shared_design_is_harvested_then_reused_on_next_page(self):
        fixture = Path(os.environ["PROFILER_FIXTURE"])
        catalog = json.loads((fixture / "catalog.json").read_text(encoding="utf-8"))
        first = json.loads((fixture / "pages" / "1" / "designs.json").read_text(encoding="utf-8"))
        second = json.loads((fixture / "pages" / "2" / "designs.json").read_text(encoding="utf-8"))
        first_ids = {d["designId"] for d in first["designs"]}
        second_ids = {d["designId"] for d in second["designs"]}
        shared = next(
            did for did in sorted(first_ids & second_ids)
            if catalog["designs"][did]["reusable"] and not catalog["designs"][did]["html"]
        )
        canonical = design_catalog.normalize_design_id(shared)
        pdf_id = catalog["pdfId"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "catalog.json", catalog)
            work1, work2 = root / "page1", root / "page2"
            write_json(work1 / "designs.json", first)
            write_json(work2 / "designs.json", second)
            common = ["--pdf-id", pdf_id, "--catalog-dir", root, "--no-s3"]
            before = run_script("design_catalog.py", "brief", *common, "--page", "1", "--work-dir", work1)
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertIn(canonical, json.loads(before.stdout)["harvestTargets"])
            second_before = run_script(
                "design_catalog.py", "brief", *common, "--page", "2", "--work-dir", work2
            )
            self.assertEqual(second_before.returncode, 0, second_before.stderr)
            second_targets = json.loads(second_before.stdout)["harvestTargets"]
            self.assertIn(canonical, second_targets)

            html = f'<div class="page-root"><aside class="d-{canonical}" data-design-id="{canonical}">example</aside></div>'
            css = f"/* @design {canonical} */\n.d-{canonical} {{ color: red; }}\n/* @end {canonical} */"
            (work1 / "content.html").write_text(html, encoding="utf-8")
            (work1 / "page.css").write_text(css, encoding="utf-8")
            write_json(work1 / "final_verification.json", {"page": 1, "score": 0.9})
            harvested = run_script(
                "harvest_designs.py", "--html", work1 / "content.html", "--css", work1 / "page.css",
                "--work-dir", work1, "--score-file", work1 / "final_verification.json", "--page", "1",
            )
            self.assertEqual(harvested.returncode, 0, harvested.stderr)
            self.assertIn(canonical, json.loads(harvested.stdout)["harvested"])
            committed = run_script(
                "design_catalog.py", "commit", *common, "--page", "1",
                "--harvested", work1 / "harvested_designs.json",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertIn(canonical, json.loads(committed.stdout)["written"])
            after = run_script("design_catalog.py", "brief", *common, "--page", "2", "--work-dir", work2)
            self.assertEqual(after.returncode, 0, after.stderr)
            result = json.loads(after.stdout)
            self.assertGreater(result["withReference"], 0)
            self.assertNotIn(canonical, result["harvestTargets"])
            self.assertEqual(set(second_targets) - set(result["harvestTargets"]), {canonical})
            self.assertEqual(len(result["harvestTargets"]), len(second_targets) - 1)
            brief = json.loads((work2 / "design_brief.json").read_text(encoding="utf-8"))
            reference = next(d for d in brief["designs"] if d["designId"] == canonical)
            self.assertTrue(reference["hasReference"])
            self.assertIn("--design-text", reference["css"])
            self.assertNotIn("color: red", reference["css"])


if __name__ == "__main__":
    unittest.main()
