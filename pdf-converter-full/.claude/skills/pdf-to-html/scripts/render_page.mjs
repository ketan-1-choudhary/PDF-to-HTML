#!/usr/bin/env node
/**
 * Render ONE page of a PDF to a PNG. Image-first contract: this works
 * for every PDF — text-layer-backed or scanned/image-only — because
 * pdfjs rasterises the page bitmap regardless of whether there is any
 * extractable text underneath it.
 *
 * Usage:
 *   node render_page.mjs --pdf <path> --page <1-based> --out <png-path>
 *                       [--scale <1..4>] [--max-long-edge <px>]
 *
 * Prints a single line of JSON to stdout:
 *   {
 *     "pngPath": "...",
 *     "pageNumber": 3,
 *     "pageCount": 1042,
 *     "width": 1240, "height": 1754,
 *     "scale": 1.78,
 *     "ok": true
 *   }
 *
 * Exits non-zero with `{ "ok": false, "error": "..." }` on failure.
 */
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { loadPdfJs, ensureCanvasFactory, NodeCanvasFactory } from "./_pdfjs.mjs";

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

async function main() {
  const args = parseArgs(process.argv);
  if (!args.pdf || !args.page || !args.out) {
    console.error(JSON.stringify({ ok: false, error: "Required: --pdf, --page, --out" }));
    process.exit(2);
  }
  const pdfPath = path.resolve(args.pdf);
  const pageOneBased = parseInt(args.page, 10);
  if (!Number.isFinite(pageOneBased) || pageOneBased < 1) {
    console.error(JSON.stringify({ ok: false, error: "--page must be a positive integer (1-based)" }));
    process.exit(2);
  }
  const outPath = path.resolve(args.out);
  const requestedScale = args.scale ? Math.max(1, Math.min(4, parseFloat(args.scale))) : 2;
  const maxLongEdge = args["max-long-edge"] ? parseInt(args["max-long-edge"], 10) : 2200;

  const bytes = await readFile(pdfPath);

  const pdfjs = await loadPdfJs();
  await ensureCanvasFactory();

  // Defensive copy — pdfjs may transfer the underlying buffer.
  const data = new Uint8Array(bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength
  ));
  const loadingTask = pdfjs.getDocument({
    data,
    useSystemFonts: false,
    disableFontFace: true,
    isEvalSupported: false,
    stopAtErrors: false,
    CanvasFactory: NodeCanvasFactory,
  });
  const doc = await loadingTask.promise;
  try {
    const pageCount = doc.numPages;
    const pageNumber = Math.max(1, Math.min(pageOneBased, pageCount));
    const page = await doc.getPage(pageNumber);

    // Cap the scale so neither dimension exceeds maxLongEdge.
    const base = page.getViewport({ scale: 1 });
    const longEdge = Math.max(base.width, base.height);
    const maxAllowedScale = maxLongEdge / longEdge;
    const effectiveScale = Math.max(1, Math.min(requestedScale, Math.max(1, maxAllowedScale)));
    const viewport = page.getViewport({ scale: effectiveScale });

    const factory = new NodeCanvasFactory();
    const cac = factory.create(viewport.width, viewport.height);
    // White background — many PDFs assume a white canvas and pdfjs's
    // default is transparent. A transparent PNG would mislead the
    // downstream palette + diff steps.
    cac.context.fillStyle = "#ffffff";
    cac.context.fillRect(0, 0, viewport.width, viewport.height);

    await page.render({ canvasContext: cac.context, viewport }).promise;

    const pngBuf = cac.canvas.toBuffer("image/png");
    await mkdir(path.dirname(outPath), { recursive: true });
    await writeFile(outPath, pngBuf);

    factory.destroy(cac);

    console.log(JSON.stringify({
      ok: true,
      pngPath: outPath,
      pageNumber,
      pageCount,
      width: Math.round(viewport.width),
      height: Math.round(viewport.height),
      scale: effectiveScale,
      byteLength: pngBuf.byteLength,
    }));
  } finally {
    try { await doc.cleanup(); } catch { /* ignore */ }
    try { await doc.destroy(); } catch { /* ignore */ }
  }
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
  process.exit(1);
});
