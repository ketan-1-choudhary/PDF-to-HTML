#!/usr/bin/env python3
"""
Python fallback for measure_palette.mjs.

Quantises an image's colours, returns the top palette + page background
+ per-region (background, dominant, corner-radius) measurements. Same
JSON shape as the Node helper.

Usage:
  python3 fallback_measure_palette.py --image <png>
                                      [--regions <json-array>]
"""
import argparse, json, sys
from collections import Counter
import numpy as np
from PIL import Image

def quantise(arr, levels=24):
    """Bucket RGB to a coarse palette so similar shades aggregate."""
    step = 256 // levels
    q = (arr // step) * step + step // 2
    return q

def palette_from(arr, top=8):
    pixels = arr.reshape(-1, 3)
    q = quantise(pixels)
    keys = (q[:, 0].astype(np.uint32) << 16) | (q[:, 1].astype(np.uint32) << 8) | q[:, 2].astype(np.uint32)
    counts = Counter(keys.tolist())
    total = len(keys)
    out = []
    for k, n in counts.most_common(top):
        r, g, b = (k >> 16) & 0xFF, (k >> 8) & 0xFF, k & 0xFF
        out.append({
            "hex": f"#{r:02x}{g:02x}{b:02x}",
            "coverage": round(n / total, 4)
        })
    return out

def page_bg(arr):
    """Sample the four edges, pick the most common colour."""
    h, w = arr.shape[:2]
    edges = np.concatenate([
        arr[:8, :, :].reshape(-1, 3),
        arr[-8:, :, :].reshape(-1, 3),
        arr[:, :8, :].reshape(-1, 3),
        arr[:, -8:, :].reshape(-1, 3),
    ])
    q = quantise(edges)
    keys = (q[:, 0].astype(np.uint32) << 16) | (q[:, 1].astype(np.uint32) << 8) | q[:, 2].astype(np.uint32)
    if len(keys) == 0:
        return "#ffffff"
    k, _ = Counter(keys.tolist()).most_common(1)[0]
    r, g, b = (k >> 16) & 0xFF, (k >> 8) & 0xFF, k & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"

def region_metrics(arr, bbox):
    x, y, w, h = bbox['x'], bbox['y'], bbox['width'], bbox['height']
    H, W = arr.shape[:2]
    x = max(0, min(x, W)); y = max(0, min(y, H))
    x2 = max(0, min(x + w, W)); y2 = max(0, min(y + h, H))
    if x2 <= x or y2 <= y:
        return None
    sub = arr[y:y2, x:x2]
    if sub.size == 0:
        return None
    # Background = sample interior corners (4 small patches)
    pad = min(8, sub.shape[0] // 4, sub.shape[1] // 4) or 1
    corners = np.concatenate([
        sub[:pad, :pad, :].reshape(-1, 3),
        sub[:pad, -pad:, :].reshape(-1, 3),
        sub[-pad:, :pad, :].reshape(-1, 3),
        sub[-pad:, -pad:, :].reshape(-1, 3),
    ])
    q = quantise(corners)
    keys = (q[:, 0].astype(np.uint32) << 16) | (q[:, 1].astype(np.uint32) << 8) | q[:, 2].astype(np.uint32)
    bg_k, _ = Counter(keys.tolist()).most_common(1)[0]
    bg = f"#{((bg_k >> 16) & 0xff):02x}{((bg_k >> 8) & 0xff):02x}{(bg_k & 0xff):02x}"
    # Dominant = the most common colour in the entire region that ISN'T the bg.
    palette = palette_from(sub, top=4)
    dominant = next((p['hex'] for p in palette if p['hex'] != bg), palette[0]['hex'] if palette else bg)
    # Rough corner radius: count how many pixels of the top-left corner differ from the bg.
    corner_test = sub[:min(40, sub.shape[0]), :min(40, sub.shape[1])]
    bg_rgb = np.array([(bg_k >> 16) & 0xFF, (bg_k >> 8) & 0xFF, bg_k & 0xFF])
    delta = np.abs(corner_test.astype(np.int16) - bg_rgb).max(axis=2)
    radius_px = int(np.sqrt((delta > 32).sum())) if (delta > 32).any() else 0
    radius_px = min(32, radius_px)
    return {
        "backgroundHex": bg,
        "dominantHex": dominant,
        "cornerRadiusPx": radius_px,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', required=True)
    ap.add_argument('--regions', default='')
    args = ap.parse_args()

    im = Image.open(args.image).convert('RGB')
    arr = np.asarray(im, dtype=np.uint8)

    palette = palette_from(arr, top=8)
    bg = page_bg(arr)

    per_region = []
    if args.regions:
        try:
            regions = json.loads(args.regions)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"regions parse failed: {e}"}))
            sys.exit(2)
        for r in regions:
            metrics = region_metrics(arr, r['bbox']) or {}
            per_region.append({"id": r.get('id'), **metrics})

    # Pre-formatted prompt-friendly summary the model can drop straight into context.
    lines = [f"Page background: {bg}",
             "Top palette (hex / coverage):"]
    for p in palette:
        lines.append(f"  • {p['hex']}  ({p['coverage']:.1%})")
    if per_region:
        lines.append("Per-region tokens:")
        for r in per_region:
            lines.append(
                f"  • {r.get('id','?')}: bg={r.get('backgroundHex','-')} dominant={r.get('dominantHex','-')} radius≈{r.get('cornerRadiusPx',0)}px"
            )
    prompt = "\n".join(lines)

    print(json.dumps({
        "ok": True,
        "engine": "python-pillow",
        "image": args.image,
        "imageSize": list(im.size),
        "pageBackgroundHex": bg,
        "palette": palette,
        "perRegion": per_region,
        "prompt": prompt,
    }))

if __name__ == '__main__':
    main()
