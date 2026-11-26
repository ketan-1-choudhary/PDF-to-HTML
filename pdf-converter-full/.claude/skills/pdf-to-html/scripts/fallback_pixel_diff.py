#!/usr/bin/env python3
"""
Python fallback for pixel_diff.mjs.

Diffs two images pixel-by-pixel, computes a similarity score and
identifies hot regions where the rendered HTML disagrees with the
source. Same JSON shape as the Node helper.

Usage:
  python3 fallback_pixel_diff.py --source <png> --rendered <png>
                                 --out-diff <png>
                                 [--hot-region-count 4]
"""
import argparse, json, sys
import numpy as np
from PIL import Image

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--rendered', required=True)
    ap.add_argument('--out-diff', dest='out_diff', required=True)
    ap.add_argument('--hot-region-count', dest='hot_count', default=4, type=int)
    ap.add_argument('--threshold', default=32, type=int,
                    help='Per-channel delta above which a pixel is considered different.')
    args = ap.parse_args()

    src = Image.open(args.source).convert('RGB')
    ren = Image.open(args.rendered).convert('RGB')
    # Compare at the rendered dimensions; downscale source to match (preserves
    # the rebuild's chosen target width without unfairly penalising design width).
    rw, rh = ren.size
    if src.size != (rw, rh):
        src = src.resize((rw, rh), Image.LANCZOS)
    a = np.asarray(src, dtype=np.int16)
    b = np.asarray(ren, dtype=np.int16)
    delta = np.abs(a - b).max(axis=2)
    diff_mask = delta > args.threshold
    diff_pct = float(diff_mask.mean())
    score = max(0.0, 1.0 - diff_pct * 1.5)

    # Hot regions: a 12×8 grid; sort cells by their mean diff.
    H, W = diff_mask.shape
    bands_y, bands_x = 12, 8
    cell_h = H // bands_y or 1
    cell_w = W // bands_x or 1
    hot = []
    for iy in range(bands_y):
        for ix in range(bands_x):
            y0 = iy * cell_h
            y1 = (iy + 1) * cell_h if iy < bands_y - 1 else H
            x0 = ix * cell_w
            x1 = (ix + 1) * cell_w if ix < bands_x - 1 else W
            cell = diff_mask[y0:y1, x0:x1]
            sev = float(cell.mean())
            if sev > 0.05:
                hot.append({"bbox": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], "severity": round(sev, 4)})
    hot.sort(key=lambda h: -h['severity'])

    # Red overlay diff for visual inspection.
    overlay = np.asarray(src, dtype=np.uint8).copy()
    red = np.array([255, 0, 0], dtype=np.uint8)
    overlay[diff_mask] = (overlay[diff_mask] * 0.4 + red * 0.6).astype(np.uint8)
    Image.fromarray(overlay).save(args.out_diff, optimize=True)

    print(json.dumps({
        "ok": True,
        "engine": "python-numpy",
        "score": round(score, 4),
        "diffPct": round(diff_pct, 4),
        "diffPath": args.out_diff,
        "hotRegions": hot[: args.hot_count],
        "size": [W, H],
    }))

if __name__ == '__main__':
    main()
