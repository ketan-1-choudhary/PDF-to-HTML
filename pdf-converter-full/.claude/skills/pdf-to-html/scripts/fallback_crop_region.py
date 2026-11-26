#!/usr/bin/env python3
"""
Python fallback for crop_region.mjs.

Crops one or many regions out of a source PNG. Same JSON shape as the
Node helper: prints a list of {id, path, width, height}.

Usage:
  python3 fallback_crop_region.py --image <png> --regions <json-array>
                                  --out-dir <dir>
"""
import argparse, json, os
from pathlib import Path
from PIL import Image

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', required=True)
    ap.add_argument('--regions', required=True)
    ap.add_argument('--out-dir', dest='out_dir', required=True)
    args = ap.parse_args()

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    im = Image.open(args.image).convert('RGB')
    W, H = im.size

    regions = json.loads(args.regions)
    crops = []
    for r in regions:
        b = r['bbox']
        x = max(0, min(int(b['x']), W))
        y = max(0, min(int(b['y']), H))
        x2 = max(0, min(int(b['x'] + b['width']), W))
        y2 = max(0, min(int(b['y'] + b['height']), H))
        if x2 <= x or y2 <= y:
            continue
        crop = im.crop((x, y, x2, y2))
        path = os.path.join(args.out_dir, f"{r['id']}.png")
        crop.save(path, optimize=True)
        crops.append({
            "id": r['id'],
            "path": path,
            "width": crop.size[0],
            "height": crop.size[1],
        })

    print(json.dumps({"ok": True, "engine": "python-pillow", "crops": crops}))

if __name__ == '__main__':
    main()
