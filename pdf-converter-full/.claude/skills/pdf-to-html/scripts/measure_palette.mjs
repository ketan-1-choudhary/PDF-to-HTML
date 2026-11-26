#!/usr/bin/env node
/**
 * Sample real design tokens from the rendered PNG: top colours, page
 * background, per-bbox dominant/background hex + a coarse corner-radius
 * estimate. Works on ANY PNG (scanned or vector-rendered) — this is the
 * image-first foundation for grounding the model's hex choices.
 *
 * Usage:
 *   node measure_palette.mjs --image <png-path>
 *                            [--regions '<json-array>']
 *                            [--top-n 8]
 *
 * `--regions` is an optional JSON array of:
 *   [{ "id": "hero", "bbox": { "x":0,"y":0,"width":1240,"height":420 } }]
 *
 * Stdout JSON:
 *   {
 *     "ok": true,
 *     "imageWidth": 1240, "imageHeight": 1754,
 *     "pageBackgroundHex": "#ffffff",
 *     "palette": [{ "hex": "#f37020", "coverage": 0.18, "rgb":[243,112,32] }, ...],
 *     "perRegion": [{ "id":"hero","backgroundHex":"#fff","dominantHex":"#222","cornerRadiusPx":8 }],
 *     "prompt": "human-readable summary block ready to paste into an LLM prompt"
 *   }
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import sharp from "sharp";

const SAMPLE_LONG_EDGE = 220;
const QUANT_STEP = 24;
const TOP_N_DEFAULT = 8;

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

const hex = (n) => n.toString(16).padStart(2, "0");
const rgbToHex = (r, g, b) => `#${hex(r)}${hex(g)}${hex(b)}`;

function quantize(raw, channels, topN) {
  const counts = new Map();
  let kept = 0;
  for (let i = 0; i < raw.length; i += channels) {
    if (channels === 4 && raw[i + 3] < 16) continue;
    const r = raw[i], g = raw[i + 1], b = raw[i + 2];
    const br = Math.min(255, Math.round(r / QUANT_STEP) * QUANT_STEP);
    const bg = Math.min(255, Math.round(g / QUANT_STEP) * QUANT_STEP);
    const bb = Math.min(255, Math.round(b / QUANT_STEP) * QUANT_STEP);
    const key = (br << 16) | (bg << 8) | bb;
    const e = counts.get(key);
    if (e) { e.count++; e.r += r; e.g += g; e.b += b; }
    else counts.set(key, { count: 1, r, g, b });
    kept++;
  }
  if (kept === 0) return [];
  return [...counts.values()]
    .sort((a, b) => b.count - a.count)
    .slice(0, topN)
    .map((e) => {
      const r = Math.round(e.r / e.count);
      const g = Math.round(e.g / e.count);
      const b = Math.round(e.b / e.count);
      return { hex: rgbToHex(r, g, b), rgb: [r, g, b], coverage: e.count / kept };
    });
}

function medianRgb(raw, channels) {
  const rs = [], gs = [], bs = [];
  for (let i = 0; i < raw.length; i += channels) {
    if (channels === 4 && raw[i + 3] < 16) continue;
    rs.push(raw[i]); gs.push(raw[i + 1]); bs.push(raw[i + 2]);
  }
  const pick = (a) => { if (a.length === 0) return 0; a.sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; };
  return [pick(rs), pick(gs), pick(bs)];
}

async function cornerBackground(buffer, bbox, imgW, imgH) {
  const PATCH = 8;
  const cx = Math.max(0, Math.min(bbox.x, imgW - PATCH));
  const cy = Math.max(0, Math.min(bbox.y, imgH - PATCH));
  const rx = Math.max(0, Math.min(bbox.x + bbox.width - PATCH, imgW - PATCH));
  const ry = Math.max(0, Math.min(bbox.y + bbox.height - PATCH, imgH - PATCH));
  const corners = [
    { left: cx, top: cy }, { left: rx, top: cy },
    { left: cx, top: ry }, { left: rx, top: ry },
  ];
  let r = 0, g = 0, b = 0, n = 0;
  for (const c of corners) {
    try {
      const { data, info } = await sharp(buffer)
        .extract({ left: c.left, top: c.top, width: PATCH, height: PATCH })
        .raw().toBuffer({ resolveWithObject: true });
      const ch = info.channels;
      for (let i = 0; i < data.length; i += ch) {
        if (ch === 4 && data[i + 3] < 16) continue;
        r += data[i]; g += data[i + 1]; b += data[i + 2]; n++;
      }
    } catch { /* fall through */ }
  }
  if (n === 0) return [255, 255, 255];
  return [Math.round(r / n), Math.round(g / n), Math.round(b / n)];
}

async function estimateRadius(buffer, bbox, bg) {
  const SIZE = 32;
  if (bbox.width < SIZE || bbox.height < SIZE) return 0;
  try {
    const { data, info } = await sharp(buffer)
      .extract({ left: bbox.x, top: bbox.y, width: bbox.width, height: bbox.height })
      .resize(SIZE, SIZE, { fit: "fill" })
      .raw().toBuffer({ resolveWithObject: true });
    const ch = info.channels;
    let stepsToBg = SIZE;
    for (let d = 0; d < SIZE; d++) {
      const idx = (d * SIZE + d) * ch;
      const dr = data[idx] - bg[0], dg = data[idx + 1] - bg[1], db = data[idx + 2] - bg[2];
      const dist = Math.sqrt(dr * dr + dg * dg + db * db);
      if (dist < 24) { stepsToBg = d; break; }
    }
    const ratio = stepsToBg / SIZE;
    const radiusPx = Math.round(ratio * Math.min(bbox.width, bbox.height));
    const snaps = [0, 4, 8, 12, 16, 20, 24, 32, 48, 64];
    return snaps.reduce((a, b) => Math.abs(b - radiusPx) < Math.abs(a - radiusPx) ? b : a);
  } catch { return 0; }
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.image) {
    console.error(JSON.stringify({ ok: false, error: "Required: --image" }));
    process.exit(2);
  }
  const buf = await readFile(path.resolve(args.image));
  const topN = args["top-n"] ? Math.max(1, parseInt(args["top-n"], 10)) : TOP_N_DEFAULT;
  const meta = await sharp(buf).metadata();
  const imageWidth = meta.width || 0;
  const imageHeight = meta.height || 0;

  // Global palette — downsample then quantize.
  const longEdge = Math.max(imageWidth, imageHeight);
  const scale = Math.min(1, SAMPLE_LONG_EDGE / longEdge);
  const sw = Math.max(1, Math.round(imageWidth * scale));
  const sh = Math.max(1, Math.round(imageHeight * scale));
  const { data: globalRaw, info: globalInfo } = await sharp(buf)
    .resize(sw, sh, { fit: "fill" }).removeAlpha().raw()
    .toBuffer({ resolveWithObject: true });
  const palette = quantize(globalRaw, globalInfo.channels, topN);

  const pageBg = await cornerBackground(buf, { x: 0, y: 0, width: imageWidth, height: imageHeight }, imageWidth, imageHeight);
  const pageBackgroundHex = rgbToHex(...pageBg);

  const regions = args.regions ? JSON.parse(args.regions) : [];
  const perRegion = [];
  for (const c of regions) {
    try {
      const { data: crop, info: cInfo } = await sharp(buf)
        .extract({ left: c.bbox.x, top: c.bbox.y, width: c.bbox.width, height: c.bbox.height })
        .resize(64, 64, { fit: "fill" }).raw()
        .toBuffer({ resolveWithObject: true });
      const med = medianRgb(crop, cInfo.channels);
      const bg = await cornerBackground(buf, c.bbox, imageWidth, imageHeight);
      const radius = await estimateRadius(buf, c.bbox, bg);
      perRegion.push({
        id: c.id,
        backgroundHex: rgbToHex(...bg),
        dominantHex: rgbToHex(...med),
        cornerRadiusPx: radius,
      });
    } catch {
      perRegion.push({ id: c.id, backgroundHex: pageBackgroundHex, dominantHex: pageBackgroundHex, cornerRadiusPx: 0 });
    }
  }

  // Pretty-printed block ready to drop straight into an LLM prompt.
  const palettePretty = palette
    .map((p) => `  ${p.hex}  (${(p.coverage * 100).toFixed(1)}% of pixels) rgb(${p.rgb.join(", ")})`)
    .join("\n");
  const regionPretty = perRegion
    .map((c) => `  ${c.id}: bg=${c.backgroundHex}  dominant=${c.dominantHex}  radius≈${c.cornerRadiusPx}px`)
    .join("\n");
  const prompt =
`Page background (sampled from corners): ${pageBackgroundHex}

Top colours in the source image (real measurements — prefer these to anything you'd guess):
${palettePretty || "  (no colours measured)"}

Per-region measurements (sampled from the source image):
${regionPretty || "  (no regions supplied yet)"}`;

  console.log(JSON.stringify({
    ok: true,
    imageWidth, imageHeight,
    pageBackgroundHex,
    palette,
    perRegion,
    prompt,
  }));
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
  process.exit(1);
});
