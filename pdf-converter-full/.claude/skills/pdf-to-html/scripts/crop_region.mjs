#!/usr/bin/env node
/**
 * Crop one or more rectangular regions from the source PNG to separate
 * PNG files. The skill uses these crops as zoomed-in references for
 * each detected component.
 *
 * Usage (single):
 *   node crop_region.mjs --image <png> --x 0 --y 0 --width 800 --height 200 --out <png>
 *
 * Usage (batch):
 *   node crop_region.mjs --image <png> --regions '<json>' --out-dir <dir>
 * where `regions` is:
 *   [{ "id":"hero","bbox":{"x":0,"y":0,"width":800,"height":200} }, ...]
 */
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

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

async function cropOne(buf, imgW, imgH, bbox, outPath) {
  const left = Math.max(0, Math.min(Math.round(bbox.x), imgW - 1));
  const top = Math.max(0, Math.min(Math.round(bbox.y), imgH - 1));
  const width = Math.max(1, Math.min(Math.round(bbox.width), imgW - left));
  const height = Math.max(1, Math.min(Math.round(bbox.height), imgH - top));
  const out = await sharp(buf).extract({ left, top, width, height }).png().toBuffer();
  await mkdir(path.dirname(outPath), { recursive: true });
  await writeFile(outPath, out);
  return { pngPath: outPath, x: left, y: top, width, height, byteLength: out.byteLength };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.image) {
    console.error(JSON.stringify({ ok: false, error: "Required: --image" }));
    process.exit(2);
  }
  const buf = await readFile(path.resolve(args.image));
  const meta = await sharp(buf).metadata();
  const imgW = meta.width || 0;
  const imgH = meta.height || 0;

  if (args.regions) {
    const regions = JSON.parse(args.regions);
    if (!args["out-dir"]) {
      console.error(JSON.stringify({ ok: false, error: "Required with --regions: --out-dir" }));
      process.exit(2);
    }
    const dir = path.resolve(args["out-dir"]);
    const crops = [];
    for (const r of regions) {
      const outPath = path.join(dir, `${r.id || "region"}.png`);
      const c = await cropOne(buf, imgW, imgH, r.bbox, outPath);
      crops.push({ id: r.id, ...c });
    }
    console.log(JSON.stringify({ ok: true, crops, imageWidth: imgW, imageHeight: imgH }));
    return;
  }

  if (!args.out || args.x == null || args.y == null || args.width == null || args.height == null) {
    console.error(JSON.stringify({ ok: false, error: "Required: --x --y --width --height --out (or --regions + --out-dir)" }));
    process.exit(2);
  }
  const c = await cropOne(buf, imgW, imgH, {
    x: parseInt(args.x, 10), y: parseInt(args.y, 10),
    width: parseInt(args.width, 10), height: parseInt(args.height, 10),
  }, path.resolve(args.out));
  console.log(JSON.stringify({ ok: true, imageWidth: imgW, imageHeight: imgH, ...c }));
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
  process.exit(1);
});
