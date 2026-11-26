#!/usr/bin/env python3
"""
analyze_page.py — one-shot layout analyser for the pdf-to-html skill.

Given a PDF file and a 1-based page number, emits a single JSON record
with everything the HTML-generation step needs to recreate the page
without re-deriving it from the rendered image:

    {
      "page": N,
      "pageCount": <int>,
      "pdfWidthPt":  <float>,            // PDF page size in points
      "pdfHeightPt": <float>,
      "imageWidth":  <int>,              // rasterised source image width in px
      "imageHeight": <int>,
      "scale": <float> | null,           // null for normalized rendering
      "renderDpi": <int>,
      "targetLongEdge": <int> | null,
      "contentBbox": { x, y, width, height },  // tight bbox of all non-white content
      "contentMargins": { left, right, top, bottom },  // page-edge → content edges
      "palette": [ "#rrggbb", ... ],     // 6–10 dominant colours
      "pageBackgroundHex": "#rrggbb",
      "hasTextLayer": <bool>,
      "textBlocks": [                    // present only when hasTextLayer
         { "id", "bbox": {x,y,w,h}, "text", "fontSize", "fontName", "color" }
      ],
      "embeddedImages": [                // raster images embedded in the PDF
         { "id", "bbox": {x,y,w,h}, "cropPath": "<work>/img_<id>.png" }
      ],
      "headerBand":  { "bbox": {x,y,w,h} } | null,   // detected top running-head band
      "footerBand":  { "bbox": {x,y,w,h} } | null,
      "renderPath":  "<work>/source.png",
      "notes": [ ... ]
    }

The bboxes are in source-image pixel space (not PDF points). The
HTML-generation step uses contentMargins as the asymmetric paddings for
.page-root content sections (see references/renderer-pitfalls.md § 1).

Exits 0 on success. On failure of any single sub-step, emits partial
data and a notes entry — never aborts the pipeline.
"""

import argparse, json, os, sys, subprocess, tempfile, traceback, shutil, glob, re
from PIL import Image
import numpy as np
from checkpoint_state import checkpoint_update


A4_WIDTH_PT = 595.0
A4_HEIGHT_PT = 842.0


def _find_poppler_tool(tool: str) -> str:
    """Resolve a poppler binary (pdftoppm / pdfinfo) without relying on PATH.

    Resolution order:
      1. $POPPLER_PATH (a directory containing the binaries), if set.
      2. The binary on the current PATH (shutil.which) — covers Linux/mac and
         Windows installs that did put poppler on PATH.
      3. Common Windows install locations: conda's Library\\bin (the folder a
         `conda install poppler` writes to) and typical poppler-for-Windows
         unzip dirs. Searched via a small set of globbed candidates.

    Returns the resolved path (or absolute path) if found, else the bare tool
    name so the caller still gets a clear FileNotFoundError from subprocess.
    """
    exe = tool + (".exe" if os.name == "nt" else "")

    # 1. explicit override
    env_dir = os.environ.get("POPPLER_PATH")
    if env_dir:
        cand = os.path.join(env_dir, exe)
        if os.path.isfile(cand):
            return cand

    # 2. on PATH already
    found = shutil.which(tool) or shutil.which(exe)
    if found:
        return found

    # 3. common install locations (Windows-heavy, harmless elsewhere)
    candidates = []
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, "miniconda3", "Library", "bin"),
        os.path.join(home, "Miniconda3", "Library", "bin"),
        os.path.join(home, "anaconda3", "Library", "bin"),
        os.path.join(home, "Anaconda3", "Library", "bin"),
        r"C:\ProgramData\miniconda3\Library\bin",
        r"C:\ProgramData\Anaconda3\Library\bin",
    ):
        candidates.append(os.path.join(base, exe))
    # poppler-for-windows style: C:\poppler*\...\bin\pdftoppm.exe
    for pat in (r"C:\poppler*\**\bin", r"C:\Program Files\poppler*\**\bin",
                os.path.join(home, "poppler*", "**", "bin")):
        for d in glob.glob(pat, recursive=True):
            candidates.append(os.path.join(d, exe))

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    return tool  # fall through — subprocess will raise a clear error


def render_dpi(pdf_path: str, page: int, scale: float, normalized_long_edge=None):
    if normalized_long_edge is None:
        return max(72, min(int(72 * scale), 288))
    if normalized_long_edge <= 0:
        raise ValueError("normalized long edge must be positive")
    width, height = get_pdfinfo_page_size_pt(pdf_path, page)
    if width and height and width > 0 and height > 0:
        return max(36, min(600, int(round(72.0 * normalized_long_edge / max(width, height)))))
    return 150


def render_source_png(
    pdf_path: str,
    page: int,
    work_dir: str,
    scale: float,
    max_long_edge: int = 1600,
    normalized_long_edge=None,
):
    """Rasterise the requested PDF page with pdftoppm. Return path + (w,h).

    After rendering, downscale so the long edge is <= max_long_edge. This
    matches the web app's lib/crop.ts MAX_LONG_EDGE = 1600: Anthropic's
    vision API downsamples anything past ~1568px internally, so sending a
    larger image adds latency without improving accuracy and introduces
    variance from the host's own resize path. 1600 is the web-app parity
    value; pass a larger number only if you specifically need more detail.
    When normalized_long_edge is set, match the book-style-profiler's DPI,
    RGB conversion and exact long-edge resizing, including upscaling.
    """
    os.makedirs(work_dir, exist_ok=True)
    dpi = render_dpi(pdf_path, page, scale, normalized_long_edge)
    out_path = os.path.join(work_dir, "source.png")
    with tempfile.TemporaryDirectory() as td:
        tmp_root = os.path.join(td, "p")
        subprocess.run(
            [_find_poppler_tool("pdftoppm"), "-r", str(dpi), "-f", str(page), "-l", str(page), "-png", pdf_path, tmp_root],
            check=True, capture_output=True,
        )
        candidates = [
            f"{tmp_root}-{page}.png", f"{tmp_root}-{page:02d}.png",
            f"{tmp_root}-{page:03d}.png", f"{tmp_root}-{page:04d}.png",
        ]
        rendered = next((p for p in candidates if os.path.exists(p)), None)
        if not rendered:
            rendered = next(iter(sorted(glob.glob(f"{tmp_root}-*.png"))), None)
        if not rendered:
            raise RuntimeError("pdftoppm produced no output")
        img = Image.open(rendered)
        if normalized_long_edge is not None:
            img = img.convert("RGB")
        long_edge = max(img.size)
        target = normalized_long_edge
        if target is None and max_long_edge and long_edge > max_long_edge:
            target = max_long_edge
        if target is not None and long_edge != target:
            ratio = target / float(long_edge)
            new_size = (max(1, round(img.size[0] * ratio)), max(1, round(img.size[1] * ratio)))
            img = img.resize(new_size, Image.LANCZOS)
        img.save(out_path, optimize=True)
        return out_path, img.size


def measure_content_bbox(image_path: str, white_thresh: int = 240):
    """Tight bbox of all non-white pixels — that's the printed content area.

    Returns (bbox, margins) where bbox is {x,y,width,height} and margins
    are page-edge offsets to that bbox.
    """
    arr = np.asarray(Image.open(image_path).convert("L"))
    h, w = arr.shape
    non_white = arr < white_thresh
    cols = non_white.any(axis=0)
    rows = non_white.any(axis=1)
    if not cols.any() or not rows.any():
        return None, None
    left  = int(cols.argmax())
    right = int(len(cols) - 1 - cols[::-1].argmax())
    top   = int(rows.argmax())
    bot   = int(len(rows) - 1 - rows[::-1].argmax())
    bbox  = {"x": left, "y": top, "width": right - left + 1, "height": bot - top + 1}
    margins = {"left": left, "right": w - 1 - right, "top": top, "bottom": h - 1 - bot}
    return bbox, margins


def detect_running_bands(image_path: str, white_thresh: int = 240, band_max_height_frac: float = 0.08):
    """Detect a top running-header band and bottom running-footer band, if any.

    Heuristic: a "running band" is a horizontal band near the top/bottom
    that contains some non-white pixels and is then followed by a band of
    pure whitespace before the main content. Returns dicts with bbox in
    image-pixel space, or None.
    """
    arr = np.asarray(Image.open(image_path).convert("L"))
    h, w = arr.shape
    rows = (arr < white_thresh).any(axis=1)
    row_density = (arr < white_thresh).sum(axis=1) / w  # 0..1 per row

    # Top band: walk down from y=0 while there's any content; once we hit
    # a dense row (>5% of width is non-white) we record the run, then if a
    # whitespace gap of >=8 px follows we declare it the running band.
    top_band = None
    if rows.any():
        first = int(rows.argmax())
        # walk forward while non-white density stays > 0
        y = first
        while y < h and rows[y]:
            y += 1
        band_end = y - 1
        # gap?
        gap_start = y
        while y < h and not rows[y]:
            y += 1
        gap = y - gap_start
        height = band_end - first + 1
        if height <= int(h * band_max_height_frac) and gap >= 6 and first < int(h * 0.18):
            top_band = {"bbox": {"x": 0, "y": first, "width": w, "height": height}}

    # Bottom band: mirror.
    bot_band = None
    rows_rev = rows[::-1]
    if rows_rev.any():
        first_r = int(rows_rev.argmax())
        y = first_r
        while y < h and rows_rev[y]:
            y += 1
        band_end_r = y - 1
        gap_start = y
        while y < h and not rows_rev[y]:
            y += 1
        gap = y - gap_start
        height = band_end_r - first_r + 1
        if height <= int(h * band_max_height_frac) and gap >= 6 and first_r < int(h * 0.18):
            actual_top = h - 1 - band_end_r
            bot_band = {"bbox": {"x": 0, "y": actual_top, "width": w, "height": height}}

    return top_band, bot_band


def extract_text_with_positions(pdf_path: str, page: int, image_size, pdf_size):
    """Use pdfplumber to extract text runs with bboxes. Empty list when no text layer."""
    try:
        import pdfplumber
    except Exception as e:
        return [], False, f"pdfplumber import failed: {e}"

    iw, ih = image_size
    pw, ph = pdf_size
    sx, sy = iw / pw, ih / ph

    blocks = []
    has_text = False
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page < 1 or page > len(pdf.pages):
                return [], False, f"page {page} out of range (1..{len(pdf.pages)})"
            p = pdf.pages[page - 1]
            words = p.extract_words(
                use_text_flow=True,
                extra_attrs=["size", "fontname"],
            )
            has_text = len(words) > 0
            # Group into lines by y0 proximity.
            words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
            lines = []
            cur = []
            cur_y = None
            for w_ in words:
                if cur_y is None or abs(w_["top"] - cur_y) <= 1.5:
                    cur.append(w_)
                    cur_y = w_["top"] if cur_y is None else cur_y
                else:
                    lines.append(cur); cur = [w_]; cur_y = w_["top"]
            if cur: lines.append(cur)
            for i, line in enumerate(lines):
                x0 = min(w_["x0"] for w_ in line)
                x1 = max(w_["x1"] for w_ in line)
                y0 = min(w_["top"] for w_ in line)
                y1 = max(w_["bottom"] for w_ in line)
                text = " ".join(w_["text"] for w_ in line)
                sizes = [w_.get("size") for w_ in line if w_.get("size")]
                fontnames = [w_.get("fontname") for w_ in line if w_.get("fontname")]
                blocks.append({
                    "id": f"line-{i:03d}",
                    "bbox": {
                        "x": int(round(x0 * sx)),
                        "y": int(round(y0 * sy)),
                        "width":  int(round((x1 - x0) * sx)),
                        "height": int(round((y1 - y0) * sy)),
                    },
                    "text": text,
                    "fontSize": round(max(sizes) * sy, 1) if sizes else None,
                    "fontName": fontnames[0] if fontnames else None,
                })
    except Exception as e:
        return [], False, f"pdfplumber extraction failed: {e}"
    return blocks, has_text, None


def extract_embedded_images(pdf_path: str, page: int, image_size, pdf_size, work_dir: str):
    """Find raster images embedded in the PDF page and crop them out of the rendered source."""
    try:
        import pdfplumber
    except Exception:
        return []
    iw, ih = image_size
    pw, ph = pdf_size
    sx, sy = iw / pw, ih / ph
    out = []
    try:
        src = Image.open(os.path.join(work_dir, "source.png"))
        with pdfplumber.open(pdf_path) as pdf:
            p = pdf.pages[page - 1]
            for i, im in enumerate(p.images):
                x0 = im.get("x0", 0); x1 = im.get("x1", 0)
                y0 = im.get("top", 0); y1 = im.get("bottom", 0)
                bx, by = int(round(x0 * sx)), int(round(y0 * sy))
                bw, bh = int(round((x1 - x0) * sx)), int(round((y1 - y0) * sy))
                if bw <= 4 or bh <= 4: continue
                crop_path = os.path.join(work_dir, f"img_{i:02d}.png")
                src.crop((bx, by, bx + bw, by + bh)).save(crop_path, optimize=True)
                out.append({"id": f"img-{i:02d}", "bbox": {"x": bx, "y": by, "width": bw, "height": bh},
                            "cropPath": crop_path})
    except Exception:
        pass
    return out


def sample_palette(image_path: str, k: int = 8):
    """Return up to k SATURATED palette colours + the background colour.

    The background colour is the dominant near-neutral. The palette is the k
    most common SATURATED buckets (chroma > 40), so we get brand reds, teals,
    pinks, etc. — not 8 shades of grey near the page background.
    """
    img = Image.open(image_path).convert("RGB").resize((400, 400), Image.LANCZOS)
    arr = np.asarray(img).reshape(-1, 3).astype(np.int16)
    # Quantise: 5-bit per channel = buckets of 8.
    q = ((arr // 8) * 8).astype(np.uint8)
    keys, counts = np.unique(q.view([("", q.dtype)] * 3), return_counts=True)
    keys = keys.view(q.dtype).reshape(-1, 3)
    # Background: the single most common bucket (usually near-white or page tint)
    bg_idx = counts.argmax()
    br, bg_, bb = keys[bg_idx]
    bg = f"#{int(br):02x}{int(bg_):02x}{int(bb):02x}"
    # Saturated palette: chroma = max - min > 40 AND not too dark / too light
    chroma = keys.max(axis=1).astype(int) - keys.min(axis=1).astype(int)
    light  = keys.mean(axis=1)
    mask = (chroma >= 40) & (light > 30) & (light < 245)
    sat_keys, sat_counts = keys[mask], counts[mask]
    order = sat_counts.argsort()[::-1]
    palette = []
    for idx in order:
        r, g, b = sat_keys[idx]
        palette.append(f"#{int(r):02x}{int(g):02x}{int(b):02x}")
        if len(palette) == k: break
    return palette, bg


def get_pdfinfo_page_size_pt(pdf_path: str, page: int):
    """Return pdfinfo's page dimensions, without an alternate parser."""
    try:
        out = subprocess.run([_find_poppler_tool("pdfinfo"), "-f", str(page), "-l", str(page), pdf_path],
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Page") and " size:" in line:
                # "Page    1 size: 595 x 842 pts" or "Page size:       595 x 842 pts"
                parts = line.split("size:", 1)[1].strip().split()
                return float(parts[0]), float(parts[2])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return None, None


def get_pdf_page_size_pt(pdf_path: str, page: int):
    """Return PDF page dimensions in points, with pypdf as a fallback."""
    width, height = get_pdfinfo_page_size_pt(pdf_path, page)
    if width and height:
        return width, height
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        p = reader.pages[page - 1]
        mb = p.mediabox
        return float(mb.width), float(mb.height)
    except Exception:
        pass
    return None, None


def run_docling_extract(pdf_path: str, page: int):
    """Best-effort Docling extraction for routing and text sourcing.

    Returns a stable dict even when Docling is unavailable or fails.
    """
    out = {
        "ok": False,
        "engine": "docling",
        "available": False,
        "page": page,
        "text": "",
        "markdown": "",
        "structuralCounts": {
            "headings": 0,
            "listItems": 0,
            "tableRows": 0,
            "paragraphs": 0,
        },
        "notes": [],
    }
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
        out["available"] = True
    except Exception as e:
        out["notes"].append(f"Docling import failed: {e}")
        return out

    try:
        converter = DocumentConverter()
        conv = converter.convert(pdf_path)
        doc = conv.document
        md = ""
        if hasattr(doc, "export_to_markdown"):
            md = doc.export_to_markdown() or ""
        # Keep this resilient across Docling API changes.
        text = re.sub(r"\n{3,}", "\n\n", md).strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        headings = sum(1 for ln in lines if ln.startswith("#"))
        list_items = sum(1 for ln in lines if re.match(r"^([-*+]\s+|\d+[.)]\s+)", ln))
        table_rows = sum(1 for ln in lines if ln.count("|") >= 2)
        paragraphs = sum(
            1
            for ln in lines
            if not ln.startswith("#")
            and not re.match(r"^([-*+]\s+|\d+[.)]\s+)", ln)
            and ln.count("|") < 2
        )

        out.update({
            "ok": bool(text),
            "text": text,
            "markdown": md,
            "structuralCounts": {
                "headings": headings,
                "listItems": list_items,
                "tableRows": table_rows,
                "paragraphs": paragraphs,
            },
        })
        if not text:
            out["notes"].append("Docling returned empty markdown/text.")
    except Exception as e:
        out["notes"].append(f"Docling conversion failed: {e}")

    return out


def run_ocr_baseline(image_path: str):
    """Best-effort OCR baseline on the rendered page image."""
    out = {
        "ok": False,
        "engine": "tesseract",
        "text": "",
        "avgConfidence": 0.0,
        "lineCount": 0,
        "wordCount": 0,
        "notes": [],
    }
    try:
        import pytesseract
        from pytesseract import Output
    except Exception as e:
        out["notes"].append(f"pytesseract import failed: {e}")
        return out

    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang="ita+eng") or ""
        data = pytesseract.image_to_data(img, lang="ita+eng", output_type=Output.DICT)
        confs = []
        words = 0
        for raw in data.get("conf", []):
            try:
                c = float(raw)
                if c >= 0:
                    confs.append(c / 100.0)
            except Exception:
                continue
        for w in data.get("text", []):
            if isinstance(w, str) and w.strip():
                words += 1
        line_count = len([ln for ln in text.splitlines() if ln.strip()])
        out.update({
            "ok": True,
            "text": text.strip(),
            "avgConfidence": round(float(np.mean(confs)) if confs else 0.0, 4),
            "lineCount": line_count,
            "wordCount": words,
        })
    except Exception as e:
        out["notes"].append(f"OCR extraction failed: {e}")

    return out


def estimate_bbox_coverage(text_blocks, image_size, content_bbox=None):
    """Estimate coverage ratio of text blocks over content area."""
    iw, ih = image_size
    if not text_blocks:
        return 0.0
    mask = np.zeros((ih, iw), dtype=np.uint8)
    for b in text_blocks:
        bb = b.get("bbox") or {}
        x = max(0, int(bb.get("x", 0)))
        y = max(0, int(bb.get("y", 0)))
        w = max(0, int(bb.get("width", 0)))
        h = max(0, int(bb.get("height", 0)))
        if w == 0 or h == 0:
            continue
        x2 = min(iw, x + w)
        y2 = min(ih, y + h)
        if x2 > x and y2 > y:
            mask[y:y2, x:x2] = 1

    num = int(mask.sum())
    if content_bbox:
        den = max(1, int(content_bbox["width"]) * int(content_bbox["height"]))
    else:
        den = max(1, iw * ih)
    return round(min(1.0, num / den), 4)


def estimate_reading_order_confidence(text_blocks, ocr_line_count):
    """Heuristic reading-order confidence in [0,1]."""
    if text_blocks and len(text_blocks) >= 2:
        ys = [int((b.get("bbox") or {}).get("y", 0)) for b in text_blocks]
        good = 0
        total = 0
        prev = ys[0]
        for y in ys[1:]:
            total += 1
            if y + 3 >= prev:
                good += 1
            prev = y
        return round(good / total if total else 0.5, 4)
    if ocr_line_count > 0:
        return 0.55
    return 0.2


def compute_route(docling_data, ocr_data, text_blocks, content_bbox, image_size):
    """Compute hybrid score + route decision for this page."""
    doc_text = (docling_data.get("text") or "").strip()
    ocr_text = (ocr_data.get("text") or "").strip()
    tb_text = "\n".join((b.get("text") or "") for b in text_blocks if b.get("text"))

    reference_chars = max(len(ocr_text), len(tb_text), 1)
    text_coverage = min(1.0, len(doc_text) / reference_chars) if doc_text else 0.0

    counts = docling_data.get("structuralCounts") or {}
    structural_hits = sum(
        1 for k in ("headings", "listItems", "tableRows", "paragraphs")
        if (counts.get(k) or 0) > 0
    )
    structural_coverage = structural_hits / 4.0

    bbox_coverage = estimate_bbox_coverage(text_blocks, image_size, content_bbox)
    reading_order_conf = estimate_reading_order_confidence(text_blocks, ocr_data.get("lineCount", 0))

    score = (
        0.35 * text_coverage
        + 0.25 * structural_coverage
        + 0.20 * bbox_coverage
        + 0.20 * reading_order_conf
    )
    score = round(float(max(0.0, min(1.0, score))), 4)

    notes = []
    if not docling_data.get("available"):
        notes.append("Docling unavailable, route biased toward vision-primary/hybrid.")
    if not doc_text:
        notes.append("Docling text is empty for this page.")
    if not ocr_data.get("ok"):
        notes.append("OCR baseline unavailable; confidence signal is weak.")

    # Use practical confidence gating: high score + good OCR confidence => docling-primary.
    ocr_conf = float(ocr_data.get("avgConfidence", 0.0) or 0.0)
    if score >= 0.80 and (ocr_conf >= 0.85 or not ocr_data.get("ok")) and doc_text:
        route = "docling-primary"
    elif score >= 0.45 and (doc_text or ocr_data.get("ok")):
        route = "hybrid"
    else:
        route = "vision-primary"

    return {
        "route": route,
        "docling": {
            "textCoverage": round(text_coverage, 4),
            "structuralCoverage": round(structural_coverage, 4),
            "bboxCoverage": bbox_coverage,
            "readingOrderConfidence": reading_order_conf,
        },
        "ocr": {
            "avgConfidence": round(ocr_conf, 4),
            "lineCount": int(ocr_data.get("lineCount", 0) or 0),
        },
        "hybridScore": score,
        "notes": notes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--work-dir", required=True, help="output dir for meta.json + source.png + crops")
    ap.add_argument("--scale", type=float, default=2.0, help="raster scale (1.0 = 72 DPI)")
    ap.add_argument("--max-long-edge", type=int, default=1600,
                    help="cap rendered source long edge (web-app parity = 1600)")
    ap.add_argument("--normalized-long-edge", type=int, default=None,
                    help="match profiler rendering at this exact long edge, including upscaling")
    args = ap.parse_args()
    if args.normalized_long_edge is not None and args.normalized_long_edge <= 0:
        ap.error("--normalized-long-edge must be positive")

    notes = []
    try:
        checkpoint_update(
            args.work_dir,
            "analyze_page",
            "running",
            {
                "pdf": args.pdf,
                "page": args.page,
                "scale": args.scale,
                "targetLongEdge": args.normalized_long_edge,
            },
            next_action="Run region postprocess and crop after analysis completes",
        )

        # 1. Source raster
        src_path, (iw, ih) = render_source_png(
            args.pdf, args.page, args.work_dir, args.scale, args.max_long_edge,
            args.normalized_long_edge,
        )

        # 2. PDF native size
        try:
            pw, ph = get_pdf_page_size_pt(args.pdf, args.page)
        except Exception as e:
            pw = ph = None
            notes.append(f"pdfinfo size fetch failed: {e}")
        if pw is None or ph is None or pw <= 0 or ph <= 0:
            pw, ph = A4_WIDTH_PT, A4_HEIGHT_PT
            notes.append("PDF page size unavailable; defaulting to A4 (595x842 pt).")

        # 3. Page count
        try:
            info = subprocess.run([_find_poppler_tool("pdfinfo"), args.pdf], capture_output=True, text=True, check=True).stdout
            page_count = int(next(l.split(":",1)[1].strip() for l in info.splitlines() if l.startswith("Pages:")))
        except Exception:
            page_count = None

        # 4. Content bbox + margins
        cbbox, margins = measure_content_bbox(src_path)

        # 5. Running header / footer detection
        top_band, bot_band = detect_running_bands(src_path)

        # 6. Palette
        palette, bg = sample_palette(src_path)

        # 7. Text layer
        blocks, has_text, err = extract_text_with_positions(args.pdf, args.page, (iw, ih), (pw, ph))
        if err: notes.append(err)

        # 8. Embedded images
        imgs = extract_embedded_images(args.pdf, args.page, (iw, ih), (pw, ph), args.work_dir)

        # 9. Docling + OCR + route artefacts (best-effort, non-fatal)
        docling_data = run_docling_extract(args.pdf, args.page)
        ocr_data = run_ocr_baseline(src_path)
        routing_data = compute_route(docling_data, ocr_data, blocks, cbbox, (iw, ih))

        docling_path = os.path.join(args.work_dir, "docling.json")
        ocr_path = os.path.join(args.work_dir, "ocr_text.json")
        routing_path = os.path.join(args.work_dir, "routing.json")
        with open(docling_path, "w", encoding="utf-8") as f:
            json.dump(docling_data, f, indent=2, ensure_ascii=False)
        with open(ocr_path, "w", encoding="utf-8") as f:
            json.dump(ocr_data, f, indent=2, ensure_ascii=False)
        with open(routing_path, "w", encoding="utf-8") as f:
            json.dump(routing_data, f, indent=2, ensure_ascii=False)

        result = {
            "page": args.page,
            "pageCount": page_count,
            "pdfWidthPt":  pw,
            "pdfHeightPt": ph,
            "imageWidth":  iw,
            "imageHeight": ih,
            "scale": args.scale if args.normalized_long_edge is None else None,
            "renderDpi": render_dpi(args.pdf, args.page, args.scale, args.normalized_long_edge),
            "targetLongEdge": args.normalized_long_edge,
            "contentBbox": cbbox,
            "contentMargins": margins,
            "palette": palette,
            "pageBackgroundHex": bg,
            "hasTextLayer": has_text,
            "textBlocks": blocks,
            "embeddedImages": imgs,
            "doclingPath": docling_path,
            "ocrPath": ocr_path,
            "routingPath": routing_path,
            "routing": routing_data,
            "headerBand": top_band,
            "footerBand": bot_band,
            "renderPath": src_path,
            "notes": notes,
        }
        meta_path = os.path.join(args.work_dir, "meta.json")
        with open(meta_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        checkpoint_update(
            args.work_dir,
            "analyze_page",
            "completed",
            {
                "metaPath": meta_path,
                "routingPath": routing_path,
                "route": routing_data.get("route"),
                "hybridScore": routing_data.get("hybridScore"),
            },
            next_action="Generate/refresh regions.json and continue page-first generation",
        )

        # Echo a summary to stdout
        print(json.dumps({
            "ok": True,
            "metaPath": meta_path,
            "page": args.page,
            "imageSize": [iw, ih],
            "contentMargins": margins,
            "hasTextLayer": has_text,
            "textBlocks": len(blocks),
            "embeddedImages": len(imgs),
            "paletteSamples": len(palette),
            "route": routing_data.get("route"),
            "hybridScore": routing_data.get("hybridScore"),
            "notes": notes,
        }, indent=2))
    except Exception as e:
        checkpoint_update(
            args.work_dir,
            "analyze_page",
            "failed",
            {"error": str(e)},
            next_action="Fix analyze_page failure and rerun this stage",
        )
        print(json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
