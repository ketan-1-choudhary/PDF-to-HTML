#!/usr/bin/env node
/**
 * Pixel-diff source PNG against rendered PNG. Returns a similarity
 * score, the diff overlay PNG, and the worst N tile regions so the
 * skill can ask Claude to refine those areas.
 *
 * Usage:
 *   node pixel_diff.mjs --source <png> --rendered <png>
 *                       --out-diff <png>
 *                       [--hot-region-count 4]
 *                       [--tile-grid 8]
 *                       [--threshold 0.18]
 *
 * Stdout JSON:
 *   {
 *     "ok": true,
 *     "score": 0.93,
 *     "diffPixels": 12345,
 *     "totalPixels": 2400000,
 *     "diffPath": "/.../diff.png",
 *     "hotRegions": [{ "x":0,"y":0,"width":150,"height":219,"mismatch":0.61 }]
 *   }
 */
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (!k.startsWith("--")) continue;
    const v = argv[i + 1];
    out[k.slice(2)] = v;
    i++;
  }
  return out;
}

async function normaliseToRgba(buf, w, h) {
  const { data } = await sharp(buf).resize(w, h, { fit: "fill" }).ensureAlpha().raw()
    .toBuffer({ resolveWithObject: true });
  return data;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.source || !args.rendered || !args["out-diff"]) {
    console.error(JSON.stringify({ ok: false, error: "Required: --source --rendered --out-diff" }));
    process.exit(2);
  }
  const srcBuf = await readFile(path.resolve(args.source));
  const renBuf = await readFile(path.resolve(args.rendered));
  const meta = await sharp(srcBuf).metadata();
  const width = meta.width || 0;
  const height = meta.height || 0;
  if (!width || !height) {
    console.error(JSON.stringify({ ok: false, error: "source has no dimensions" }));
    process.exit(1);
  }

  const hotRegionCount = args["hot-region-count"] ? parseInt(args["hot-region-count"], 10) : 4;
  const tileGrid = args["tile-grid"] ? parseInt(args["tile-grid"], 10) : 8;
  const threshold = args.threshold ? parseFloat(args.threshold) : 0.18;

  const src = await normaliseToRgba(srcBuf, width, height);
  const ren = await normaliseToRgba(renBuf, width, height);

  const diffData = Buffer.alloc(width * height * 4);
  const diffPixels = pixelmatch(src, ren, diffData, width, height, {
    threshold, includeAA: true, alpha: 0.4,
  });
  const totalPixels = width * height;
  const score = Math.max(0, Math.min(1, 1 - diffPixels / totalPixels));

  const tileW = Math.max(1, Math.floor(width / tileGrid));
  const tileH = Math.max(1, Math.floor(height / tileGrid));
  const tiles = [];
  for (let ty = 0; ty < tileGrid; ty++) {
    for (let tx = 0; tx < tileGrid; tx++) {
      const x0 = tx * tileW;
      const y0 = ty * tileH;
      const x1 = tx === tileGrid - 1 ? width : x0 + tileW;
      const y1 = ty === tileGrid - 1 ? height : y0 + tileH;
      let count = 0;
      for (let y = y0; y < y1; y++) {
        for (let x = x0; x < x1; x++) {
          const idx = (y * width + x) * 4;
          if (diffData[idx] >= 200 && diffData[idx + 1] <= 80 && diffData[idx + 2] <= 80) count++;
        }
      }
      const area = (x1 - x0) * (y1 - y0);
      tiles.push({
        x: x0, y: y0, width: x1 - x0, height: y1 - y0,
        count, mismatch: area > 0 ? count / area : 0,
      });
    }
  }
  tiles.sort((a, b) => b.count - a.count);
  const hotRegions = tiles
    .filter((t) => t.mismatch > 0.01)
    .slice(0, hotRegionCount)
    .map(({ x, y, width: w, height: h, mismatch }) => ({ x, y, width: w, height: h, mismatch }));

  const png = new PNG({ width, height });
  diffData.copy(png.data);
  const diffPng = PNG.sync.write(png);
  const diffPath = path.resolve(args["out-diff"]);
  await mkdir(path.dirname(diffPath), { recursive: true });
  await writeFile(diffPath, diffPng);

  console.log(JSON.stringify({
    ok: true,
    score, diffPixels, totalPixels,
    width, height,
    diffPath,
    hotRegions,
  }));
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
  process.exit(1);
});
