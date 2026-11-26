/**
 * Shared pdfjs loader. Polyfills the DOM globals pdfjs's canvas renderer
 * needs (Path2D, DOMMatrix, …) and points GlobalWorkerOptions.workerSrc
 * at the resolved worker file on disk so the fake-worker path works
 * under Node.
 *
 * This is image-first: every entry point that calls into pdfjs through
 * here is designed to fall back gracefully when the PDF has no text
 * layer (scanned / image-only PDFs) — the rasteriser still works
 * regardless of whether there's any extractable text.
 */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";

let _pdfjs = null;

export async function loadPdfJs() {
  if (_pdfjs) return _pdfjs;
  await installCanvasGlobals();
  const mod = await import("pdfjs-dist/legacy/build/pdf.mjs");

  // Worker discovery — needed for the fake-worker code path under Node.
  if (!mod.GlobalWorkerOptions.workerSrc) {
    try {
      await import("pdfjs-dist/legacy/build/pdf.worker.mjs");
    } catch {
      /* side-effect import best-effort */
    }
    const here = path.dirname(fileURLToPath(import.meta.url));
    const candidates = [
      path.resolve(here, "node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs"),
      path.resolve(here, "../node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs"),
      path.resolve(process.cwd(), "node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs"),
    ];
    const found = candidates.find((p) => existsSync(p));
    if (found) mod.GlobalWorkerOptions.workerSrc = found;
  }
  _pdfjs = mod;
  return _pdfjs;
}

let _globalsInstalled = false;
async function installCanvasGlobals() {
  if (_globalsInstalled) return;
  const canvasMod = await import("@napi-rs/canvas");
  const g = globalThis;
  if (!g.Path2D && canvasMod.Path2D) g.Path2D = canvasMod.Path2D;
  if (!g.ImageData && canvasMod.ImageData) g.ImageData = canvasMod.ImageData;

  if (typeof g.DOMMatrix === "undefined") {
    class DOMMatrixShim {
      constructor(init) {
        this.a = 1; this.b = 0; this.c = 0; this.d = 1; this.e = 0; this.f = 0;
        if (Array.isArray(init) && init.length >= 6) {
          [this.a, this.b, this.c, this.d, this.e, this.f] = init;
        }
      }
      multiply(o) {
        const r = new DOMMatrixShim();
        r.a = this.a * o.a + this.c * o.b;
        r.b = this.b * o.a + this.d * o.b;
        r.c = this.a * o.c + this.c * o.d;
        r.d = this.b * o.c + this.d * o.d;
        r.e = this.a * o.e + this.c * o.f + this.e;
        r.f = this.b * o.e + this.d * o.f + this.f;
        return r;
      }
      translate(tx, ty) {
        const r = new DOMMatrixShim([this.a, this.b, this.c, this.d, this.e, this.f]);
        r.e = this.a * tx + this.c * ty + this.e;
        r.f = this.b * tx + this.d * ty + this.f;
        return r;
      }
      scale(sx, sy) {
        const _sy = sy ?? sx;
        const r = new DOMMatrixShim([this.a, this.b, this.c, this.d, this.e, this.f]);
        r.a *= sx; r.b *= sx; r.c *= _sy; r.d *= _sy;
        return r;
      }
      inverse() {
        const det = this.a * this.d - this.b * this.c;
        const r = new DOMMatrixShim();
        if (det === 0) return r;
        r.a = this.d / det; r.b = -this.b / det;
        r.c = -this.c / det; r.d = this.a / det;
        r.e = (this.c * this.f - this.d * this.e) / det;
        r.f = (this.b * this.e - this.a * this.f) / det;
        return r;
      }
      get is2D() { return true; }
      get isIdentity() {
        return this.a === 1 && this.b === 0 && this.c === 0 &&
               this.d === 1 && this.e === 0 && this.f === 0;
      }
    }
    g.DOMMatrix = DOMMatrixShim;
  }
  if (typeof g.DOMPoint === "undefined") {
    g.DOMPoint = class { constructor(x = 0, y = 0, z = 0, w = 1) { Object.assign(this, { x, y, z, w }); } };
  }
  if (typeof g.DOMRect === "undefined") {
    g.DOMRect = class {
      constructor(x = 0, y = 0, width = 0, height = 0) { Object.assign(this, { x, y, width, height }); }
      get top() { return this.y; } get left() { return this.x; }
      get right() { return this.x + this.width; } get bottom() { return this.y + this.height; }
    };
  }
  _globalsInstalled = true;
}

/**
 * Canvas factory pdfjs v4 needs. The class is instantiated by pdfjs with
 * no args — createCanvas reference lives at module scope, populated by
 * ensureCanvasFactory.
 */
let _createCanvas = null;
export async function ensureCanvasFactory() {
  if (_createCanvas) return;
  const canvasMod = await import("@napi-rs/canvas");
  _createCanvas = canvasMod.createCanvas;
  await installCanvasGlobals();
}

export class NodeCanvasFactory {
  create(width, height) {
    if (!_createCanvas) throw new Error("NodeCanvasFactory: call ensureCanvasFactory() first");
    const canvas = _createCanvas(width, height);
    return { canvas, context: canvas.getContext("2d") };
  }
  reset(cac, width, height) { cac.canvas.width = width; cac.canvas.height = height; }
  destroy(cac) {
    if (cac.canvas) { cac.canvas.width = 0; cac.canvas.height = 0; }
    cac.canvas = null; cac.context = null;
  }
}
