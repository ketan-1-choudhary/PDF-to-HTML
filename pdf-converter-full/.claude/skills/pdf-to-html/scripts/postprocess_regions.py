#!/usr/bin/env python3
"""
postprocess_regions.py — deterministic clean-up of the regions.json the
model produced at SKILL.md Step 4.

This is a direct Python port of the web app's post-processing in
agent-pdf-html-main/lib/claude.ts (analyzeLayout, lines ~350-441). The web
app runs this in code so it's guaranteed; the skill previously left it to
the model to "apply in prose", which is unreliable. Run this script on the
model's raw regions.json so downstream crops/measurements are consistent.

Operations, in order (matching the web app exactly):
  1. Filter out entries with missing / non-finite bbox fields.
  2. Clamp every bbox to the image bounds (0 <= x,y; x+w <= W; y+h <= H).
  3. Split into content tier vs decoration tier
     (decoration roles = decoration, gutter, chrome, background).
  4. Sort content top-to-bottom, then left-to-right.
  5. Drop content siblings that overlap >50% of their own area with an
     already-kept content sibling (strips duplicate inner sections).
  6. Sort decorations top-to-bottom for stable order; keep all.
  7. Concatenate content (sorted) + decorations; re-id sequentially.
  8. When profiler designs are supplied, keep every design bbox whole by
     expanding or seeding a region, then tag containing regions with IDs.

Usage:
  python3 postprocess_regions.py \
      --regions <work>/page_<N>/regions.json \
      --image-width <W> --image-height <H> \
      [--designs <work>/page_<N>/designs_normalized.json] \
      [--out <work>/page_<N>/regions.json]   # defaults to in-place

Emits the cleaned JSON array to --out (or overwrites --regions) and prints
a one-line summary to stdout.
"""
import argparse, json, math, sys

from design_catalog import normalize_design_id, validate_design_id

DECORATION_ROLES = {"decoration", "gutter", "chrome", "background"}


def finite(*vals):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


def load_profiled_designs(path, width, height):
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("designs"), list):
        raise ValueError("profiled designs must contain a designs array")
    designs = []
    for item in data["designs"]:
        if not isinstance(item, dict):
            raise ValueError("each profiled design must be an object")
        design_id = normalize_design_id(item.get("designId"))
        validate_design_id(design_id)
        box = item.get("bbox")
        if not isinstance(box, dict):
            raise ValueError(f"{design_id}: bbox must be an object")
        values = [box.get(key) for key in ("x", "y", "width", "height")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) or not float(v).is_integer() for v in values):
            raise ValueError(f"{design_id}: bbox coordinates must be finite integers")
        x, y, w, h = map(int, values)
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError(f"{design_id}: bbox is outside the {width}x{height} page")
        designs.append({"designId": design_id, "bbox": {"x": x, "y": y, "width": w, "height": h}})
    return designs


def _contains(outer, inner):
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
    )


def _intersection_area(a, b):
    width = max(0, min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"]))
    height = max(0, min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"]))
    return width * height


def reconcile_design_regions(regions, designs):
    snapped = seeded = 0
    used_ids = {region["id"] for region in regions}
    for index, design in enumerate(designs, 1):
        box = design["bbox"]
        if any(_contains(region["bbox"], box) for region in regions):
            continue
        overlaps = [
            (area, -(region["bbox"]["width"] * region["bbox"]["height"]), -i, region)
            for i, region in enumerate(regions)
            if (area := _intersection_area(region["bbox"], box)) > 0
        ]
        if overlaps:
            region = max(overlaps, key=lambda match: match[:3])[3]
            old = region["bbox"]
            x, y = min(old["x"], box["x"]), min(old["y"], box["y"])
            right = max(old["x"] + old["width"], box["x"] + box["width"])
            bottom = max(old["y"] + old["height"], box["y"] + box["height"])
            region["bbox"] = {"x": x, "y": y, "width": right - x, "height": bottom - y}
            snapped += 1
        else:
            region_id = f"profiled-design-{index}"
            while region_id in used_ids:
                region_id += "-new"
            used_ids.add(region_id)
            regions.append({
                "id": region_id,
                "name": f"Profiled {design['designId']}",
                "role": "other",
                "description": f"Profiled design {design['designId']}",
                "bbox": dict(box),
            })
            seeded += 1

    for region in regions:
        region["designIds"] = sorted({
            design["designId"] for design in designs if _contains(region["bbox"], design["bbox"])
        })
    return snapped, seeded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", required=True, help="path to model's regions.json")
    ap.add_argument("--image-width", type=int, required=True)
    ap.add_argument("--image-height", type=int, required=True)
    ap.add_argument("--designs", default=None, help="profiler page designs in source-image pixels")
    ap.add_argument("--out", default=None, help="output path (default: in-place)")
    args = ap.parse_args()

    W, H = args.image_width, args.image_height
    out_path = args.out or args.regions

    with open(args.regions, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        print(json.dumps({"ok": False, "error": "regions.json is not a JSON array"}))
        sys.exit(2)

    # 1 + 2. Filter invalid, clamp to image bounds.
    cleaned = []
    for idx, c in enumerate(raw):
        bb = (c or {}).get("bbox") or {}
        if not finite(bb.get("x"), bb.get("y"), bb.get("width"), bb.get("height")):
            continue
        x = max(0, min(round(bb["x"]), W - 1))
        y = max(0, min(round(bb["y"]), H - 1))
        w = max(1, min(round(bb["width"]), W - x))
        h = max(1, min(round(bb["height"]), H - y))
        cleaned.append({
            "id": c.get("id") or f"component-{idx+1}",
            "name": c.get("name") or f"Component {idx+1}",
            "role": c.get("role") or "other",
            "contentType": (
                "diagram"
                if str(c.get("role") or "").lower() == "diagram"
                else (c.get("contentType") or "other")
            ),
            "description": c.get("description") or "",
            "bbox": {"x": x, "y": y, "width": w, "height": h},
        })

    if not cleaned:
        print(json.dumps({"ok": False, "error": "zero valid components after cleaning"}))
        sys.exit(2)

    def is_decoration(c):
        return (c.get("role") or "").lower() in DECORATION_ROLES

    content = [c for c in cleaned if not is_decoration(c)]
    decoration = [c for c in cleaned if is_decoration(c)]

    # 4. Sort content top-to-bottom, then left-to-right.
    content.sort(key=lambda c: (c["bbox"]["y"], c["bbox"]["x"]))

    # 5. Dedupe content by >50% self-area overlap with an already-kept sibling.
    deduped = []
    for c in content:
        if (c.get("role") or "").lower() == "diagram" or (
            c.get("contentType") or ""
        ).lower() == "diagram":
            deduped.append(c)
            continue
        ca = c["bbox"]["width"] * c["bbox"]["height"]
        heavy = False
        for e in deduped:
            ix = max(c["bbox"]["x"], e["bbox"]["x"])
            iy = max(c["bbox"]["y"], e["bbox"]["y"])
            axx = min(c["bbox"]["x"] + c["bbox"]["width"], e["bbox"]["x"] + e["bbox"]["width"])
            ayy = min(c["bbox"]["y"] + c["bbox"]["height"], e["bbox"]["y"] + e["bbox"]["height"])
            if axx <= ix or ayy <= iy:
                continue
            inter = (axx - ix) * (ayy - iy)
            if ca > 0 and inter / ca > 0.5:
                heavy = True
                break
        if not heavy:
            deduped.append(c)

    # 6. Decorations: sort top-to-bottom for stable order, keep all.
    decoration.sort(key=lambda c: (c["bbox"]["y"], c["bbox"]["x"]))

    # 7. Content first, decorations after; re-id sequentially.
    final = deduped + decoration
    for i, c in enumerate(final):
        c["id"] = c.get("id") or f"section-{i+1}"

    design_summary = {}
    if args.designs:
        try:
            designs = load_profiled_designs(args.designs, W, H)
            snapped, seeded = reconcile_design_regions(final, designs)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            sys.exit(2)
        final.sort(key=lambda region: (
            is_decoration(region), region["bbox"]["y"], region["bbox"]["x"]
        ))
        design_summary = {
            "profiledInstances": len(designs),
            "snappedDesigns": snapped,
            "seededDesigns": seeded,
            "taggedRegions": sum(bool(region["designIds"]) for region in final),
        }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    print(json.dumps({
        "ok": True,
        "input": len(raw),
        "kept": len(final),
        "content": len(deduped),
        "decoration": len(decoration),
        "droppedInvalid": len(raw) - len(cleaned),
        "droppedDuplicateContent": len(content) - len(deduped),
        "out": out_path,
        **design_summary,
    }))


if __name__ == "__main__":
    main()
