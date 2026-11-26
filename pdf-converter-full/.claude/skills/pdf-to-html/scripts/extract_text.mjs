#!/usr/bin/env node
/**
 * BEST-EFFORT text-layer + font extraction for ONE page.
 *
 * Image-first contract: this script ALWAYS exits 0 with structured JSON,
 * even when the PDF has no text layer (scanned PDFs, photo-pasted
 * pages, image-only exports). In those cases `hasTextLayer` is false
 * and `textRuns` / `fonts` / `embeddedImages` are empty. The skill
 * treats text extraction as a bonus, never a requirement — the
 * rendered PNG remains ground truth.
 *
 * Usage:
 *   node extract_text.mjs --pdf <path> --page <1-based>
 *                        [--max-runs <N>] [--timeout-ms <N>]
 *
 * Stdout JSON shape:
 *   {
 *     "ok": true,
 *     "hasTextLayer": false,
 *     "pageWidth": 612, "pageHeight": 792,
 *     "text": "",
 *     "positionalText": "(no text layer — read text visually from the rendered PNG)",
 *     "textRuns": [ { text, x, y, width, height, fontSize, fontFamily, italic, bold } ],
 *     "fonts": [...],
 *     "embeddedImages": [ { width, height, kind } ],
 *     "suggestedFontStacks": [...]
 *   }
 */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { loadPdfJs } from "./_pdfjs.mjs";

function emitJson(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function routeLogsToStderr() {
  const original = {
    log: console.log,
    warn: console.warn,
    error: console.error,
  };
  const write = (...args) => {
    try {
      process.stderr.write(`${args.map((x) => String(x)).join(" ")}\n`);
    } catch {
      // Ignore log-routing failures; JSON output remains primary.
    }
  };
  console.log = write;
  console.warn = write;
  console.error = write;
  return () => {
    console.log = original.log;
    console.warn = original.warn;
    console.error = original.error;
  };
}

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

const SUGGESTIONS = {
  helvetica: "Inter, system-ui, sans-serif",
  arial: "Inter, system-ui, sans-serif",
  "times new roman": '"Source Serif 4", Georgia, serif',
  times: '"Source Serif 4", Georgia, serif',
  georgia: "Georgia, serif",
  courier: '"JetBrains Mono", "Courier New", monospace',
  "courier new": '"JetBrains Mono", "Courier New", monospace',
  verdana: "Inter, system-ui, sans-serif",
  "trebuchet ms": "Inter, system-ui, sans-serif",
  palatino: '"Source Serif 4", Georgia, serif',
  garamond: '"EB Garamond", Georgia, serif',
  consolas: '"JetBrains Mono", monospace',
  inter: "Inter, system-ui, sans-serif",
  roboto: "Roboto, system-ui, sans-serif",
  "open sans": '"Open Sans", system-ui, sans-serif',
  montserrat: "Montserrat, system-ui, sans-serif",
  poppins: "Poppins, system-ui, sans-serif",
  lato: "Lato, system-ui, sans-serif",
  raleway: "Raleway, system-ui, sans-serif",
  nunito: "Nunito, system-ui, sans-serif",
};

function suggestWebFonts(fonts) {
  const out = new Set();
  for (const raw of fonts) {
    const key = (raw || "")
      .toLowerCase()
      .replace(/[-,].*$/, "")
      .replace(/(mt|ms|psmt|bold|italic|oblique|black|heavy|light|regular)/g, "")
      .trim();
    out.add(SUGGESTIONS[key] || raw);
  }
  return [...out];
}

function formatPositional(page, maxRuns) {
  if (!page.textRuns || page.textRuns.length === 0) return "";
  const sorted = [...page.textRuns].sort((a, b) => a.y - b.y || a.x - b.x);
  const lines = [];
  for (const run of sorted) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(last[0].y - run.y) < Math.max(2, run.fontSize * 0.5)) last.push(run);
    else lines.push([run]);
  }
  const out = [];
  let used = 0;
  for (const line of lines) {
    if (used >= maxRuns) {
      out.push(`… (${page.textRuns.length - used} more runs truncated)`);
      break;
    }
    line.sort((a, b) => a.x - b.x);
    const merged = line.map((r) => r.text).join(" ").replace(/\s+/g, " ").trim();
    if (!merged) continue;
    const f = line[0];
    out.push(
      `  @(${Math.round(f.x)},${Math.round(f.y)}) size=${f.fontSize.toFixed(1)} ${
        f.bold ? "bold " : ""
      }${f.italic ? "italic " : ""}font="${f.fontFamily}" :: ${merged}`
    );
    used += line.length;
  }
  return out.join("\n");
}

async function analyzePage(pdfjs, doc, pageNumber) {
  const page = await doc.getPage(pageNumber);
  const viewport = page.getViewport({ scale: 1 });
  const pageWidth = viewport.width;
  const pageHeight = viewport.height;

  let textContent;
  try {
    textContent = await page.getTextContent({
      includeMarkedContent: false,
      disableNormalization: false,
    });
  } catch {
    textContent = { items: [], styles: {} };
  }
  const styles = textContent.styles || {};
  const runs = [];
  const textParts = [];
  const distinctFonts = new Set();

  for (const item of textContent.items || []) {
    if (!item || typeof item.str !== "string") continue;
    const str = item.str;
    if (str.length === 0 && !item.hasEOL) continue;
    if (str.length > 0) textParts.push(str);
    if (item.hasEOL) textParts.push("\n");

    const t = item.transform;
    if (!t || t.length < 6) continue;
    const a = t[0], d = t[3], e = t[4], f = t[5];
    const fontSize = Math.abs(d) || Math.abs(a) || 0;
    const x = e;
    const yBottom = f;
    const width = item.width || str.length * fontSize * 0.5;
    const height = item.height || fontSize;
    const yTop = pageHeight - yBottom - height;

    const styleKey = item.fontName || "";
    const style = styles[styleKey] || {};
    const fontFamily = style.fontFamily || styleKey || "unknown";
    if (fontFamily) distinctFonts.add(fontFamily);
    const italic = !!style.italic || /italic|oblique/i.test(fontFamily);
    const bold = !!style.bold || /bold|black|heavy/i.test(fontFamily);

    runs.push({ text: str, x, y: yTop, width, height, fontSize, fontFamily, italic, bold });
  }

  // Best-effort embedded raster image scan — also useful even on scanned
  // PDFs (a scanned PDF is typically ONE big embedded image per page).
  const embeddedImages = [];
  try {
    const opList = await page.getOperatorList();
    const OPS = pdfjs.OPS;
    const imageOps = new Set(
      [OPS.paintImageXObject, OPS.paintImageXObjectRepeat, OPS.paintInlineImageXObject]
        .filter((n) => typeof n === "number")
    );
    const seen = new Set();
    const fnArray = opList.fnArray || [];
    const argsArray = opList.argsArray || [];
    for (let i = 0; i < fnArray.length; i++) {
      if (!imageOps.has(fnArray[i])) continue;
      const first = (argsArray[i] || [])[0];
      if (typeof first === "string") {
        if (seen.has(first)) continue;
        seen.add(first);
        try {
          const img = page.objs?.has?.(first) ? page.objs.get(first) : null;
          if (img && img.width && img.height) {
            embeddedImages.push({ width: img.width, height: img.height, kind: img.kind || "image" });
          }
        } catch { /* image not yet resolved */ }
      } else if (first && first.width && first.height) {
        embeddedImages.push({ width: first.width, height: first.height, kind: first.kind || "inline" });
      }
    }
  } catch { /* operator-list scan best-effort */ }

  const text = textParts.join("").replace(/\s+\n/g, "\n").trim();
  return {
    pageWidth,
    pageHeight,
    text,
    textRuns: runs,
    fonts: [...distinctFonts],
    embeddedImages,
    hasTextLayer: text.length > 0,
  };
}

async function main() {
  const restoreLogs = routeLogsToStderr();
  const args = parseArgs(process.argv);
  if (!args.pdf || !args.page) {
    restoreLogs();
    emitJson({
      ok: false,
      error: "Required: --pdf, --page",
      hasTextLayer: false,
      textRuns: [], fonts: [], embeddedImages: [],
      suggestedFontStacks: [],
      positionalText: "(no text layer — read text visually from the rendered PNG)",
      text: "",
    });
    process.exit(2);
  }
  const pdfPath = path.resolve(args.pdf);
  const pageOneBased = parseInt(args.page, 10) || 1;
  const maxRuns = args["max-runs"] ? parseInt(args["max-runs"], 10) : 120;
  const timeoutMs = args["timeout-ms"] ? parseInt(args["timeout-ms"], 10) : 25000;

  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; }, timeoutMs);

  let result;
  try {
    const bytes = await readFile(pdfPath);
    const pdfjs = await loadPdfJs();
    const data = new Uint8Array(bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength
    ));
    const doc = await pdfjs.getDocument({
      data,
      useSystemFonts: false,
      disableFontFace: true,
      isEvalSupported: false,
      stopAtErrors: false,
    }).promise;
    try {
      const pageNumber = Math.max(1, Math.min(pageOneBased, doc.numPages));
      const page = await analyzePage(pdfjs, doc, pageNumber);
      result = {
        ok: true,
        hasTextLayer: page.hasTextLayer,
        pageWidth: page.pageWidth,
        pageHeight: page.pageHeight,
        text: page.text,
        positionalText: page.hasTextLayer
          ? formatPositional(page, maxRuns)
          : "(no text layer — read text visually from the rendered PNG)",
        textRuns: page.textRuns,
        fonts: page.fonts,
        embeddedImages: page.embeddedImages,
        suggestedFontStacks: suggestWebFonts(page.fonts),
        timedOut,
      };
    } finally {
      try { await doc.cleanup(); } catch { /* ignore */ }
      try { await doc.destroy(); } catch { /* ignore */ }
    }
  } catch (err) {
    // Image-first: a failure here is NOT fatal. The skill still has the
    // PNG to work from. Emit a structured empty record.
    result = {
      ok: true,
      hasTextLayer: false,
      pageWidth: 0,
      pageHeight: 0,
      text: "",
      positionalText: "(text extraction failed — read text visually from the rendered PNG)",
      textRuns: [],
      fonts: [],
      embeddedImages: [],
      suggestedFontStacks: [],
      extractError: err?.message ?? String(err),
      timedOut,
    };
  } finally {
    clearTimeout(timer);
  }
  restoreLogs();
  emitJson(result);
}

main();
