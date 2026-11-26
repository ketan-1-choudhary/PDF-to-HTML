#!/usr/bin/env node
/**
 * Render a generated HTML+CSS page to a PNG at exactly width × height,
 * using a headless Chromium via puppeteer. Used by the verification
 * step to pixel-compare the generated page against the source render.
 *
 * Usage:
 *   node render_html.mjs --html <path-to-content.html-OR-fragment-file>
 *                       --css <path-to-page.css>   (optional if html is full)
 *                       --width 1240 --height 1754
 *                       --out <png>
 *                       [--executable-path <chrome-bin>]
 *
 * If --html points at a full HTML document, we load it as-is. If it's a
 * fragment with a separate --css path, we wrap both into a full doc.
 *
 * Reports an error and exits nonzero if Chromium can't launch.
 */
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
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

function buildFullDocument(html, css) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>Generated page</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #ffffff; color: #111; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  img { max-width: 100%; height: auto; display: block; }
  ${css}
  html, body { height: auto !important; min-height: 0 !important; }
  body { overflow-x: hidden; }
  .page-root { min-height: 0 !important; }
</style>
</head>
<body>
${html}
</body>
</html>`;
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.html || !args.out || !args.width || !args.height) {
    console.error(JSON.stringify({ ok: false, error: "Required: --html, --width, --height, --out" }));
    process.exit(2);
  }
  const width = Number(args.width);
  const height = Number(args.height);
  if (!Number.isSafeInteger(width) || width < 1 || !Number.isSafeInteger(height) || height < 1) {
    console.error(JSON.stringify({ ok: false, error: "width and height must be positive integers" }));
    process.exit(2);
  }
  const outPath = path.resolve(args.out);
  const htmlPath = path.resolve(args.html);
  const cssPath = args.css ? path.resolve(args.css) : null;

  let doc;
  const rawHtml = await readFile(htmlPath, "utf8");
  const isFullDocument = /^\s*<!doctype/i.test(rawHtml) || /<html[\s>]/i.test(rawHtml);
  if (isFullDocument) {
    doc = rawHtml; // already a full document
  } else {
    const css = cssPath ? await readFile(cssPath, "utf8") : "";
    doc = buildFullDocument(rawHtml, css);
  }

  let puppeteer;
  try {
    puppeteer = (await import("puppeteer")).default;
  } catch (err) {
    console.error(JSON.stringify({ ok: false, error: "puppeteer not installed: " + (err?.message ?? err) }));
    process.exit(1);
  }

  const executablePath = args["executable-path"]
    || process.env.PUPPETEER_EXECUTABLE_PATH
    || process.env.CHROME_PATH
    || undefined;

  let browser;
  try {
    browser = await puppeteer.launch({
      headless: true,
      executablePath,
      args: [
        "--no-sandbox", "--disable-setuid-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu",
        "--font-render-hinting=none",
      ],
    });
  } catch (err) {
    console.error(JSON.stringify({ ok: false, error: "Chromium launch failed: " + (err?.message ?? err) }));
    process.exit(1);
  }

  let page;
  try {
    page = await browser.newPage();
    await page.setViewport({ width, height, deviceScaleFactor: 1 });
    if (isFullDocument) {
      await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load", timeout: 15000 });
    } else {
      await page.setContent(doc, { waitUntil: "domcontentloaded", timeout: 15000 });
    }

    await Promise.race([
      page.evaluate(() => globalThis.document?.fonts?.ready ?? null).catch(() => null),
      new Promise((r) => setTimeout(r, 4000)),
    ]);
    await Promise.race([
      page.evaluate(async () => {
        const imgs = Array.from(globalThis.document.querySelectorAll("img"));
        await Promise.all(imgs.map((img) =>
          img.complete && img.naturalWidth > 0
            ? Promise.resolve()
            : img.decode().catch(() => undefined)
        ));
      }).catch(() => null),
      new Promise((r) => setTimeout(r, 4000)),
    ]);

    const fullPng = await page.screenshot({ type: "png", omitBackground: false, fullPage: true });

    // Normalise to exactly width × height: width-fill, then extract or extend.
    const meta = await sharp(fullPng).metadata();
    let normalised = fullPng;
    if ((meta.width ?? width) !== width) {
      normalised = await sharp(normalised).resize({ width, fit: "fill" }).png().toBuffer();
    }
    const meta2 = await sharp(normalised).metadata();
    const adjustedH = meta2.height ?? height;
    if (adjustedH > height) {
      normalised = await sharp(normalised).extract({ left: 0, top: 0, width, height }).png().toBuffer();
    } else if (adjustedH < height) {
      normalised = await sharp(normalised).extend({
        top: 0, bottom: height - adjustedH, left: 0, right: 0,
        background: { r: 255, g: 255, b: 255, alpha: 1 },
      }).png().toBuffer();
    }

    await mkdir(path.dirname(outPath), { recursive: true });
    await writeFile(outPath, normalised);

    console.log(JSON.stringify({
      ok: true,
      pngPath: outPath,
      width, height,
      byteLength: normalised.byteLength,
      renderedHeight: meta.height ?? height,
    }));
  } catch (err) {
    console.error(JSON.stringify({ ok: false, error: err?.message ?? String(err) }));
    process.exit(1);
  } finally {
    try { if (page) await page.close(); } catch { /* ignore */ }
    try { await browser.close(); } catch { /* ignore */ }
  }
}

main();
