"""Design catalog storage + reference-brief builder.

Two subcommands:

  brief   resolve this page's designs.json against catalog.json and write
          $WORK/design_brief.json (references to inject at STEP 5) plus
          $WORK/design_harvest_targets.json (designs still missing html/css).

  commit  merge harvested html/css back into catalog.json. Entries are
          immutable: a design that already has html is never overwritten.

Storage is local-file-first. catalog.json lives in --catalog-dir (default: the
CWD / skill root, NOT $WORK, so it survives the Step 11 cleanup within a run)
and is pulled from / pushed to S3 when S3_BUCKET is set.

Keys (matching get_profile.py):
  catalog   {pdfId}/designs/catalog.json
  per page  {pdfId}/pages/{page}/designs.json
"""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

# Designs with reusable=false are one-offs: never referenced, never harvested.
# Colour roles a harvested snippet is allowed to parameterize.
DESIGN_COLOR_VARS = (
    "--design-accent",
    "--design-bg",
    "--design-border",
    "--design-text",
)


def normalize_design_id(design_id):
    """Canonical form is hyphen-only.

    Profiler exports may use dotted namespaces (``box.map-node-rounded``).
    Normalize them on read for consistent HTML markers and CSS class names.
    """
    return str(design_id or "").strip().replace(".", "-")


def class_name_for(design_id):
    return "d-" + normalize_design_id(design_id)


def validate_page_score(value, label="page score"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number between 0 and 1")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f"{label} must be a finite number between 0 and 1")
    return score


def validate_design_id(design_id):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", design_id):
        raise ValueError(f"invalid designId: {design_id!r}")


def _warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


class CatalogStore:
    """catalog.json on local disk, optionally backed by S3."""

    def __init__(self, pdf_id, catalog_dir=None, use_s3=True):
        self.pdf_id = pdf_id
        self.dir = Path(catalog_dir) if catalog_dir else Path.cwd()
        self.path = self.dir / "catalog.json"
        self.bucket = os.environ.get("S3_BUCKET") if use_s3 else None
        self._s3 = None

    @property
    def catalog_key(self):
        return f"{self.pdf_id}/designs/catalog.json"

    def _client(self):
        if self._s3 is None:
            import boto3  # lazy: local runs need no AWS deps

            self._s3 = boto3.client("s3")
        return self._s3

    def load(self):
        if self.bucket:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._client().download_file(self.bucket, self.catalog_key, str(self.path))
        if not self.path.exists():
            raise FileNotFoundError(f"catalog.json not found at {self.path}")
        catalog = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or not isinstance(catalog.get("designs"), dict):
            raise ValueError("catalog.json must contain a designs object")
        if catalog.get("pdfId") != self.pdf_id:
            raise ValueError(
                f"catalog pdfId {catalog.get('pdfId')!r} does not match {self.pdf_id!r}"
            )

        # Normalize ids once, here, so every consumer sees hyphen-only keys.
        normalized = {}
        for key, entry in catalog["designs"].items():
            if not isinstance(entry, dict):
                raise ValueError(f"catalog entry {key!r} must be an object")
            did = normalize_design_id(entry.get("designId") or key)
            validate_design_id(did)
            entry["designId"] = did
            if did in normalized:
                raise ValueError(f"duplicate designId after normalization: {did}")
            normalized[did] = entry
        catalog["designs"] = normalized
        return catalog

    def save(self, catalog):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.bucket:
            self._client().upload_file(
                str(self.path),
                self.bucket,
                self.catalog_key,
                ExtraArgs={"ContentType": "application/json"},
            )
            return self.catalog_key
        return None

    def load_page_designs(self, page, work_dir):
        """Per-page designs.json. Lives in $WORK: regenerated every page."""
        path = Path(work_dir) / "designs.json"
        if self.bucket:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._client().download_file(
                self.bucket,
                f"{self.pdf_id}/pages/{page}/designs.json",
                str(path),
            )
        if not path.exists():
            raise FileNotFoundError(f"designs.json not found at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("designs"), list):
            raise ValueError("designs.json must contain a designs array")
        out = []
        for item in data["designs"]:
            if not isinstance(item, dict) or not isinstance(item.get("bbox"), dict):
                raise ValueError("each page design must contain a designId and bbox")
            design_id = normalize_design_id(item.get("designId"))
            validate_design_id(design_id)
            out.append(
                {
                    "designId": design_id,
                    "bbox": item["bbox"],
                }
            )
        return out


# --------------------------------------------------------------------------
# brief
# --------------------------------------------------------------------------


def _is_reusable(entry):
    return bool(entry.get("reusable", True))


def _has_reference(entry):
    return bool((entry.get("html") or "").strip())


def _bbox_area(bbox):
    try:
        return float(bbox.get("width", 0)) * float(bbox.get("height", 0))
    except (TypeError, ValueError):
        return 0.0


def build_brief(catalog, page_designs, max_bytes, page_width=None, page_height=None):
    """Group page occurrences by design and attach catalog references.

    Returns (brief, harvest_targets, warnings).
    """
    warnings = []
    if max_bytes < 0:
        raise ValueError("max-bytes must be non-negative")
    grouped = {}
    for occurrence in page_designs:
        did = normalize_design_id(occurrence["designId"])
        validate_design_id(did)
        grouped.setdefault(did, []).append(occurrence["bbox"])

    designs = catalog.get("designs") or {}

    items = []
    for did, boxes in grouped.items():
        entry = designs.get(did)
        if entry is None:
            raise ValueError(f"designId not in catalog: {did}")
        if _is_reusable(entry) and bool((entry.get("html") or "").strip()) != bool(
            (entry.get("css") or "").strip()
        ):
            raise ValueError(f"incomplete html/css reference for designId: {did}")
        items.append(
            {
                "designId": did,
                "className": class_name_for(did),
                "description": entry.get("description") or "",
                "occurrenceCount": entry.get("occurrenceCount") or len(boxes),
                "reusable": _is_reusable(entry),
                "instances": boxes,
                "html": entry.get("html") or "",
                "css": entry.get("css") or "",
            }
        )

    # Most-reused designs earn their prompt budget first.
    items.sort(key=lambda it: (-it["occurrenceCount"], it["designId"]))

    budget = max_bytes
    references = []
    non_reusable = []
    for item in items:
        if not item["reusable"]:
            non_reusable.append({
                "designId": item["designId"],
                "className": item["className"],
                "description": item["description"],
                "instances": item["instances"],
            })
            continue
        ref = {
            "designId": item["designId"],
            "className": item["className"],
            "description": item["description"],
            "occurrenceCount": item["occurrenceCount"],
            "isIcon": item["designId"].startswith("icon-"),
            "instances": item["instances"],
            "hasReference": bool(item["html"].strip()),
        }
        if ref["hasReference"]:
            cost = len(item["html"].encode("utf-8")) + len(item["css"].encode("utf-8"))
            if cost <= budget:
                ref["html"] = item["html"]
                ref["css"] = item["css"]
                budget -= cost
            else:
                ref["hasReference"] = False
                ref["droppedForBudget"] = True
                warnings.append(
                    f"reference dropped for prompt budget: {item['designId']}"
                )
        references.append(ref)

    references.sort(key=lambda r: r["designId"])

    harvest_targets = [
        {
            "designId": r["designId"],
            "className": r["className"],
            "description": r["description"],
            # Prefer an occurrence away from the page edge when possible.
            "instances": r["instances"],
        }
        for r in references
        if not r["hasReference"] and not r.get("droppedForBudget")
    ]

    brief = {
        "pdfId": catalog.get("pdfId"),
        "pageWidth": page_width,
        "pageHeight": page_height,
        "colorVars": list(DESIGN_COLOR_VARS),
        "designs": references,
        "nonReusableDesigns": non_reusable,
        "warnings": warnings,
    }
    return brief, harvest_targets, warnings


def cmd_brief(args):
    store = CatalogStore(args.pdf_id, args.catalog_dir, use_s3=not args.no_s3)
    catalog = store.load()
    page_designs = store.load_page_designs(args.page, args.work_dir)

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    page_width = page_height = None
    meta_path = work / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("page") != args.page:
            raise ValueError("meta.json page does not match the requested page")
        page_width, page_height = meta.get("imageWidth"), meta.get("imageHeight")
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
               for v in (page_width, page_height)):
            raise ValueError("meta.json must contain positive imageWidth and imageHeight")
    brief, targets, warnings = build_brief(
        catalog, page_designs, args.max_bytes, page_width, page_height
    )
    (work / "designs_normalized.json").write_text(
        json.dumps({"designs": page_designs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (work / "design_brief.json").write_text(
        json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (work / "design_harvest_targets.json").write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for msg in warnings:
        _warn(msg)

    print(
        json.dumps(
            {
                "ok": True,
                "designsOnPage": len(brief["designs"]),
                "profiledDesignsOnPage": len(brief["designs"]) + len(brief["nonReusableDesigns"]),
                "nonReusableOnPage": len(brief["nonReusableDesigns"]),
                "nonReusable": [d["designId"] for d in brief["nonReusableDesigns"]],
                "withReference": sum(1 for d in brief["designs"] if d["hasReference"]),
                "referenced": [d["designId"] for d in brief["designs"] if d["hasReference"]],
                "harvestTargets": [t["designId"] for t in targets],
                "catalogPath": str(store.path),
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------


def cmd_commit(args):
    store = CatalogStore(args.pdf_id, args.catalog_dir, use_s3=not args.no_s3)
    catalog = store.load()
    designs = catalog.setdefault("designs", {})

    harvested_path = Path(args.harvested)
    if not harvested_path.exists():
        raise FileNotFoundError(f"harvested designs not found at {harvested_path}")
    harvested = json.loads(harvested_path.read_text(encoding="utf-8"))
    if isinstance(harvested, dict):
        harvested = harvested.get("designs")
    if not isinstance(harvested, list):
        raise ValueError("harvested designs must be an array")
    min_score = validate_page_score(args.min_score, "minimum score")
    if (args.page_width is None) != (args.page_height is None):
        raise ValueError("page-width and page-height must be provided together")
    if args.page_width is not None and (args.page_width <= 0 or args.page_height <= 0):
        raise ValueError("page dimensions must be positive")

    written, skipped = [], []
    for item in harvested:
        did = normalize_design_id(item.get("designId"))
        html = (item.get("html") or "").strip()
        css = (item.get("css") or "").strip()
        if not did or not html:
            skipped.append({"designId": did, "reason": "empty html"})
            continue
        entry = designs.get(did)
        if entry is None:
            skipped.append({"designId": did, "reason": "not in catalog"})
            continue
        if not _is_reusable(entry):
            skipped.append({"designId": did, "reason": "reusable=false"})
            continue
        if _has_reference(entry):
            # Immutable: the first good capture is the reference forever.
            skipped.append({"designId": did, "reason": "already captured"})
            continue
        if not css:
            skipped.append({"designId": did, "reason": "empty css"})
            continue
        try:
            score = validate_page_score(item.get("score"))
        except ValueError as exc:
            _warn(f"{did}: {exc}")
            skipped.append({"designId": did, "reason": f"invalid or missing page score: {exc}"})
            continue
        if score < min_score:
            skipped.append(
                {"designId": did, "reason": f"page score {score} below floor {min_score}"}
            )
            continue
        entry["html"] = html
        entry["css"] = css
        entry["sourcePage"] = args.page
        entry["sourceScore"] = score
        if args.page_width is not None:
            entry["sourcePageWidth"] = args.page_width
            entry["sourcePageHeight"] = args.page_height
        written.append(did)

    key = store.save(catalog) if written else None

    print(
        json.dumps(
            {
                "ok": True,
                "written": written,
                "skipped": skipped,
                "catalogPath": str(store.path),
                "s3Key": key,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--pdf-id", required=True)
        p.add_argument(
            "--catalog-dir",
            help="where catalog.json lives (default: CWD / skill root, not $WORK)",
        )
        p.add_argument(
            "--no-s3", action="store_true", help="local catalog.json only"
        )

    b = sub.add_parser("brief", help="build STEP 5 reference brief")
    common(b)
    b.add_argument("--page", type=int, required=True)
    b.add_argument("--work-dir", required=True)
    b.add_argument(
        "--max-bytes",
        type=int,
        default=60000,
        help="prompt budget for injected html+css across all references",
    )
    b.set_defaults(func=cmd_brief)

    c = sub.add_parser("commit", help="merge harvested designs into catalog.json")
    common(c)
    c.add_argument("--page", type=int, required=True)
    c.add_argument("--harvested", required=True)
    c.add_argument("--page-width", type=int)
    c.add_argument("--page-height", type=int)
    c.add_argument("--min-score", type=float, default=0.85)
    c.set_defaults(func=cmd_commit)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:  # fail loudly, JSON on stderr
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
