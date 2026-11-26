#!/usr/bin/env python3
"""
verify_layered.py — layered fidelity scorer for the pdf-to-html skill.

Replaces the old pixel-diff verifier (which is dominated by font-metric
drift when we use substitute fonts). Computes three independent layers
and a combined score:

    Layer A  Text fidelity        — does the rendered HTML say the
                                    same words as the source PDF?
                                    OCR the render, compare to either
                                    `pdftotext` (when the source has a
                                    text layer) or `tesseract` on the
                                    source raster.
    Layer B  Layout fidelity      — do the visual blocks (text + image
                                    regions) sit in the same positions
                                    and sizes? Detect blocks on both
                                    renders with simple morphology and
                                    compute mean IoU of matched blocks.
    Layer C  Colour fidelity      — does each per-region average colour
                                    match? Sample the source palette
                                    once, then check that every region
                                    in the render is the closest palette
                                    colour to the source region.
    Layer D  Pixel diff           — the legacy metric, kept as a
                                    tiebreaker and visualisation.

Each layer is in [0, 1]. The combined score is a weighted mix that
weights structural correctness over per-pixel alignment:

    combined = 0.40 * text + 0.30 * layout + 0.20 * colour + 0.10 * pixel

Outputs a single JSON record on stdout, writes per-layer artefacts
(diff overlay, block visualisations, OCR text) to the work dir.

Usage:
    verify_layered.py --source <work>/source.png \\
                      --render <work>/render.png \\
                      --pdf <source.pdf> --page N \\
                      --work-dir <work>
"""

import argparse, json, os, sys, subprocess, re, warnings, math
from html.parser import HTMLParser
from collections import Counter
from PIL import Image
import numpy as np
from checkpoint_state import checkpoint_update

warnings.filterwarnings("ignore")
np.seterr(all="ignore")


# -------------------------------- TEXT ----------------------------------

def normalise_text(s: str) -> list[str]:
    """Aggressive normalisation so OCR vs text-layer comparison is fair.

    Tesseract OCR drops or mangles diacritics and confuses similar glyphs
    (è → e, ò → o, '=' → vs '_', sub-2 → 2, etc.) when the rendered font
    differs from the training set. We normalise BOTH sides identically so
    those purely-cosmetic differences don't show up as missing content.
    """
    import unicodedata
    # Soft hyphen, narrow hyphens, smart quotes
    s = s.replace("­", "").replace("‐", "-").replace("‑", "-")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("«", '"').replace("»", '"')
    # Strip combining accents
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Subscript/superscript digits → plain digits (CO₂ → co2)
    sub = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹", "01234567890123456789")
    s = s.translate(sub)
    # Keep word chars and a small punctuation set; collapse the rest
    s = re.sub(r"[^a-zA-Z0-9\s%°/+\-=<>.,]", " ", s)
    tokens = s.lower().split()
    # Drop pure-punctuation 1-char tokens that OCR often hallucinates
    return [t for t in tokens if t and (len(t) > 1 or t.isalnum())]


def text_from_pdf(pdf_path: str, page: int) -> str:
    """pdftotext when there's a text layer; tesseract OCR otherwise."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-f", str(page), "-l", str(page), pdf_path, "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        if out.strip():
            return out
    except Exception:
        pass
    # Fallback: rasterise and OCR
    import tempfile, pytesseract
    with tempfile.TemporaryDirectory() as td:
        root = os.path.join(td, "p")
        subprocess.run(["pdftoppm", "-r", "200", "-f", str(page), "-l", str(page), "-png", pdf_path, root], check=True)
        png = next((f"{root}-{p}.png" for p in (page, f"{page:02d}", f"{page:03d}") if os.path.exists(f"{root}-{p}.png")), None)
        if png:
            return pytesseract.image_to_string(Image.open(png), lang="ita+eng")
    return ""


def text_from_render(render_path: str) -> str:
    """OCR the rendered PNG with tesseract."""
    try:
        import pytesseract
        return pytesseract.image_to_string(Image.open(render_path), lang="ita+eng")
    except Exception as e:
        return ""


def text_fidelity(source_tokens: list[str], render_tokens: list[str]) -> dict:
    """Token-set Jaccard + ordered-bigram coverage as a measure of fidelity.

    Also lists up to 30 source tokens that are MISSING from the render —
    actionable feedback for the model ("you skipped these words").
    """
    if not source_tokens:
        return {"score": 1.0, "note": "source had no extractable text"}
    s_set = Counter(source_tokens)
    r_set = Counter(render_tokens)
    overlap = sum((s_set & r_set).values())
    coverage = overlap / max(sum(s_set.values()), 1)
    extra    = sum((r_set - s_set).values()) / max(sum(r_set.values()), 1) if render_tokens else 0
    def bigrams(toks): return Counter(zip(toks, toks[1:]))
    s_bg, r_bg = bigrams(source_tokens), bigrams(render_tokens)
    bg_overlap = sum((s_bg & r_bg).values())
    bg_cov = bg_overlap / max(sum(s_bg.values()), 1)
    score = 0.5 * coverage + 0.5 * bg_cov
    # Token diff: source - render
    missing = (s_set - r_set)
    extra_in_render = (r_set - s_set)
    return {
        "score": round(score, 4),
        "tokenCoverage": round(coverage, 4),
        "bigramCoverage": round(bg_cov, 4),
        "extraTokensRate": round(extra, 4),
        "sourceTokens": len(source_tokens),
        "renderTokens": len(render_tokens),
        "missingFromRender": [t for t, _ in missing.most_common(30)],
        "extraInRender":     [t for t, _ in extra_in_render.most_common(20)],
    }


# -------------------------------- DOM -----------------------------------

def _read_json_if_exists(path: str):
    try:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except UnicodeDecodeError:
                with open(path, "r", encoding="cp1252", errors="replace") as f:
                    return json.load(f)
    except Exception:
        pass
    return None


def _safe_score(value, default=0.0):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return float(default)
        return max(0.0, min(1.0, v))
    except Exception:
        return float(default)


class _DOMTextStructParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.counts = {
            "headings": 0,
            "listItems": 0,
            "tableRows": 0,
            "paragraphs": 0,
            "figures": 0,
            "captions": 0,
            "images": 0,
        }

    def handle_starttag(self, tag, attrs):
        t = (tag or "").lower()
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.counts["headings"] += 1
        elif t == "li":
            self.counts["listItems"] += 1
        elif t == "tr":
            self.counts["tableRows"] += 1
        elif t == "p":
            self.counts["paragraphs"] += 1
        elif t == "figure":
            self.counts["figures"] += 1
        elif t == "figcaption":
            self.counts["captions"] += 1
        elif t == "img":
            self.counts["images"] += 1

    def handle_data(self, data):
        if data and data.strip():
            self.text_parts.append(data.strip())


def _structure_similarity(source_counts: dict, render_counts: dict) -> float:
    keys = ["headings", "listItems", "tableRows", "paragraphs"]
    sims = []
    for k in keys:
        s = int(source_counts.get(k, 0) or 0)
        r = int(render_counts.get(k, 0) or 0)
        if s == 0 and r == 0:
            sims.append(1.0)
        else:
            sims.append(min(s, r) / max(s, r, 1))
    return float(np.mean(sims)) if sims else 0.0


def _choose_source_semantics(work_dir: str, fallback_source_text: str):
    routing = _read_json_if_exists(os.path.join(work_dir, "routing.json")) or {}
    route = routing.get("route") or "hybrid"
    docling = _read_json_if_exists(os.path.join(work_dir, "docling.json")) or {}
    ocr = _read_json_if_exists(os.path.join(work_dir, "ocr_text.json")) or {}
    extracted = _read_json_if_exists(os.path.join(work_dir, "extracted_text.json")) or {}

    doc_text = (docling.get("text") or "").strip()
    ocr_text = (ocr.get("text") or "").strip()
    ext_text = (extracted.get("text") or extracted.get("positionalText") or "").strip()

    if route == "docling-primary":
        source_text = doc_text or ext_text or ocr_text or fallback_source_text
    elif route == "vision-primary":
        source_text = ocr_text or ext_text or doc_text or fallback_source_text
    else:
        source_text = "\n".join(x for x in [doc_text, ocr_text] if x).strip() or ext_text or fallback_source_text

    source_struct = docling.get("structuralCounts") or {
        "headings": 0,
        "listItems": 0,
        "tableRows": 0,
        "paragraphs": 0,
    }
    if not any(int(source_struct.get(k, 0) or 0) > 0 for k in ("headings", "listItems", "tableRows", "paragraphs")):
        lines = [ln for ln in source_text.splitlines() if ln.strip()]
        source_struct = {
            "headings": sum(1 for ln in lines if len(ln.strip()) <= 80 and ln.strip()[:1].isupper()),
            "listItems": sum(1 for ln in lines if re.match(r"^\s*([-*+]\s+|\d+[.)]\s+)", ln)),
            "tableRows": sum(1 for ln in lines if ln.count("|") >= 2),
            "paragraphs": max(0, len(lines) // 2),
        }

    return route, source_text, source_struct, routing


def dom_fidelity(work_dir: str, html_path: str, fallback_source_text: str):
    route, source_text, source_struct, routing = _choose_source_semantics(work_dir, fallback_source_text)
    parser = _DOMTextStructParser()
    if html_path and os.path.exists(html_path):
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                parser.feed(f.read())
            parser.close()
        except Exception:
            parser = _DOMTextStructParser()

    source_tokens = normalise_text(source_text)
    render_tokens = normalise_text(" ".join(parser.text_parts))
    txt = text_fidelity(source_tokens, render_tokens)
    struct = _structure_similarity(source_struct, parser.counts)
    score = 0.70 * txt["score"] + 0.30 * struct

    signal = 0.0
    if source_tokens:
        signal += 0.4
    if render_tokens:
        signal += 0.3
    if sum(int(source_struct.get(k, 0) or 0) for k in ("headings", "listItems", "tableRows", "paragraphs")) > 0:
        signal += 0.3
    confidence = round(max(0.0, min(1.0, signal)), 4)

    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "confidence": confidence,
        "route": route,
        "source": {
            "tokenCount": len(source_tokens),
            "structuralCounts": source_struct,
        },
        "render": {
            "tokenCount": len(render_tokens),
            "structuralCounts": parser.counts,
            "htmlPath": html_path,
        },
        "text": txt,
        "structureScore": round(struct, 4),
        "routing": routing,
    }


def weighted_final_score(route: str, dom_conf: float, layers: dict):
    if route == "docling-primary":
        weights = {"dom": 0.45, "text": 0.15, "layout": 0.20, "colour": 0.15, "pixel": 0.05}
    elif route == "vision-primary":
        weights = {"dom": 0.20, "text": 0.20, "layout": 0.25, "colour": 0.15, "pixel": 0.20}
    else:
        weights = {"dom": 0.35, "text": 0.20, "layout": 0.20, "colour": 0.15, "pixel": 0.10}

    if dom_conf < 0.45:
        shift = min(0.15, 0.45 - dom_conf)
        weights["dom"] = max(0.05, weights["dom"] - shift)
        weights["pixel"] += shift * 0.6
        weights["layout"] += shift * 0.4

    s = sum(weights.values())
    if s <= 0:
        weights = {"dom": 0.0, "text": 0.35, "layout": 0.30, "colour": 0.20, "pixel": 0.15}
        s = sum(weights.values())
    weights = {k: v / s for k, v in weights.items()}

    safe_layers = {k: _safe_score(layers.get(k, 0.0), 0.0) for k in weights}
    combined = sum(weights[k] * safe_layers[k] for k in weights)
    return round(combined, 4), {k: round(v, 4) for k, v in weights.items()}


# -------------------------------- LAYOUT --------------------------------

def detect_blocks(image_path: str, target_size: tuple = (1200, 1600), min_area_frac: float = 0.002):
    """Return a list of dark-content blocks as (x, y, w, h) using morphology.

    Critical for fair comparison: normalise BOTH images to the same canvas
    size before running morphology so source-at-200-DPI and render-at-144-DPI
    don't produce different block counts purely because of resolution.
    Returns blocks in NORMALISED image coordinates (target_size space).
    """
    import cv2
    img = Image.open(image_path).convert("L")
    img = img.resize(target_size, Image.LANCZOS)
    arr = np.asarray(img)
    h, w = arr.shape
    bw = (arr < 240).astype(np.uint8) * 255
    # Horizontal dilate to merge characters in a line; vertical dilate to merge lines
    k1 = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 35), 1))
    k2 = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(6, h // 100)))
    glued = cv2.dilate(bw, k1, iterations=1)
    glued = cv2.dilate(glued, k2, iterations=1)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(glued, 8)
    min_area = int(min_area_frac * w * h)
    blocks = []
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < min_area: continue
        if ww < 30 or hh < 12: continue
        blocks.append((int(x), int(y), int(ww), int(hh)))
    return blocks


def bbox_iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def layout_fidelity(src_blocks, ren_blocks, image_size):
    """Greedy block-matching: for each source block, find its best match in
    render by IoU. Score = mean matched IoU * (1 - unmatched_penalty)."""
    if not src_blocks:
        return {"score": 1.0, "note": "source has no detectable blocks"}
    used = [False] * len(ren_blocks)
    matched_ious = []
    for sb in src_blocks:
        best_i, best_iou = -1, 0.0
        for j, rb in enumerate(ren_blocks):
            if used[j]: continue
            iou = bbox_iou(sb, rb)
            if iou > best_iou:
                best_iou, best_i = iou, j
        if best_i >= 0 and best_iou > 0.1:
            used[best_i] = True
            matched_ious.append(best_iou)
    matched = len(matched_ious)
    mean_iou = float(np.mean(matched_ious)) if matched_ious else 0.0
    src_match_rate = matched / len(src_blocks)
    extra_in_render = sum(1 for u in used if not u) / max(len(ren_blocks), 1)
    score = mean_iou * src_match_rate * (1 - 0.5 * extra_in_render)
    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "sourceBlocks": len(src_blocks),
        "renderBlocks": len(ren_blocks),
        "matched": matched,
        "meanMatchedIoU": round(mean_iou, 4),
        "sourceMatchRate": round(src_match_rate, 4),
        "extraInRender": round(extra_in_render, 4),
    }


# -------------------------------- COLOUR --------------------------------

def colour_fidelity(source_path: str, render_path: str, src_blocks, ren_blocks):
    """For each matched block, compare mean RGB. Score = 1 - mean ΔE/100,
    where ΔE here is the simple Euclidean RGB distance (good-enough)."""
    if not src_blocks:
        return {"score": 1.0, "note": "no source blocks"}
    src = np.asarray(Image.open(source_path).convert("RGB"))
    ren = np.asarray(Image.open(render_path).convert("RGB").resize(src.shape[:2][::-1]))
    src_h, src_w = src.shape[:2]
    ren_h, ren_w = ren.shape[:2]
    # Re-match (greedy IoU) so we can pair regions.
    used = [False] * len(ren_blocks)
    deltas = []
    for sb in src_blocks:
        best_j, best_iou = -1, 0.1
        for j, rb in enumerate(ren_blocks):
            if used[j]: continue
            iou = bbox_iou(sb, rb)
            if iou > best_iou:
                best_iou, best_j = iou, j
        if best_j < 0: continue
        used[best_j] = True
        sx, sy, sw, sh = sb; rx, ry, rw, rh = ren_blocks[best_j]
        sx0, sy0 = max(0, int(sx)), max(0, int(sy))
        sx1, sy1 = min(src_w, int(sx + sw)), min(src_h, int(sy + sh))
        rx0, ry0 = max(0, int(rx)), max(0, int(ry))
        rx1, ry1 = min(ren_w, int(rx + rw)), min(ren_h, int(ry + rh))
        if sx1 <= sx0 or sy1 <= sy0 or rx1 <= rx0 or ry1 <= ry0:
            continue
        src_patch = src[sy0:sy1, sx0:sx1].reshape(-1, 3)
        ren_patch = ren[ry0:ry1, rx0:rx1].reshape(-1, 3)
        if src_patch.size == 0 or ren_patch.size == 0:
            continue
        src_mean = src_patch.mean(axis=0)
        ren_mean = ren_patch.mean(axis=0)
        deltas.append(float(np.linalg.norm(src_mean - ren_mean)))
    if not deltas:
        return {"score": 0.0, "note": "no matched blocks for colour comparison"}
    mean_de = float(np.mean(deltas))
    # Map ΔE 0..60 → 1..0
    score = max(0.0, 1.0 - mean_de / 60.0)
    return {"score": round(score, 4), "meanDeltaE": round(mean_de, 2), "matchedBlocks": len(deltas)}


# -------------------------------- PIXEL ---------------------------------

def pixel_diff(source_path: str, render_path: str, work_dir: str, hot_region_count: int = 6, tile_grid: int = 8):
    src = Image.open(source_path).convert("RGB")
    ren = Image.open(render_path).convert("RGB").resize(src.size, Image.LANCZOS)
    a = np.asarray(src, dtype=np.int16)
    b = np.asarray(ren, dtype=np.int16)
    delta = np.abs(a - b).max(axis=2)
    diff_pct = float((delta > 32).mean())
    score = max(0.0, 1.0 - diff_pct * 1.5)
    # diff overlay
    overlay = np.asarray(src).copy()
    red = np.array([255, 0, 0], dtype=np.uint8)
    mask = delta > 32
    overlay[mask] = (overlay[mask] * 0.4 + red * 0.6).astype(np.uint8)
    Image.fromarray(overlay).save(os.path.join(work_dir, "diff.png"), optimize=True)

    # Coarse hot-region extraction for targeted section refinement.
    h, w = mask.shape
    tile_w = max(1, w // max(1, tile_grid))
    tile_h = max(1, h // max(1, tile_grid))
    tiles = []
    for ty in range(tile_grid):
        for tx in range(tile_grid):
            x0 = tx * tile_w
            y0 = ty * tile_h
            x1 = w if tx == tile_grid - 1 else min(w, x0 + tile_w)
            y1 = h if ty == tile_grid - 1 else min(h, y0 + tile_h)
            sub = mask[y0:y1, x0:x1]
            area = max(1, sub.size)
            count = int(sub.sum())
            mismatch = count / area
            tiles.append({
                "x": int(x0), "y": int(y0), "width": int(x1 - x0), "height": int(y1 - y0),
                "count": count, "mismatch": round(float(mismatch), 4),
            })
    tiles.sort(key=lambda t: t["count"], reverse=True)
    hot = [
        {"x": t["x"], "y": t["y"], "width": t["width"], "height": t["height"], "mismatch": t["mismatch"]}
        for t in tiles if t["mismatch"] > 0.01
    ][:max(1, hot_region_count)]

    return {
        "score": round(score, 4),
        "diffPct": round(diff_pct, 4),
        "hotRegions": hot,
        "tileGrid": tile_grid,
    }


def _rect_iou(a, b):
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["width"], by1 + b["height"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    au = max(1, a["width"] * a["height"])
    bu = max(1, b["width"] * b["height"])
    union = au + bu - inter
    return inter / max(1, union)


def derive_section_refine_plan(work_dir: str, hot_regions: list):
    regions_path = os.path.join(work_dir, "regions.json")
    if not os.path.exists(regions_path):
        return {
            "available": False,
            "reason": "regions.json missing",
            "recommendedSectionIds": [],
            "hotRegionCount": len(hot_regions or []),
        }
    try:
        regions = json.load(open(regions_path, "r", encoding="utf-8"))
    except Exception as e:
        return {
            "available": False,
            "reason": f"failed reading regions.json: {e}",
            "recommendedSectionIds": [],
            "hotRegionCount": len(hot_regions or []),
        }

    ranked = {}
    for r in regions:
        bb = (r or {}).get("bbox") or {}
        if not bb:
            continue
        rid = str((r or {}).get("id") or "")
        if not rid:
            continue
        rb = {
            "x": int(bb.get("x", 0)), "y": int(bb.get("y", 0)),
            "width": int(bb.get("width", 0)), "height": int(bb.get("height", 0)),
        }
        if rb["width"] <= 0 or rb["height"] <= 0:
            continue
        score = 0.0
        for h in hot_regions or []:
            iou = _rect_iou(rb, h)
            if iou > 0:
                score += float(h.get("mismatch", 0.0) or 0.0) * iou
        if score > 0:
            ranked[rid] = score

    ordered = [k for k, _ in sorted(ranked.items(), key=lambda kv: kv[1], reverse=True)]
    return {
        "available": True,
        "reason": "ok" if ordered else "no section intersects hot regions",
        "recommendedSectionIds": ordered,
        "hotRegionCount": len(hot_regions or []),
    }


# -------------------------------- MAIN ----------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="source rasterised PNG of the PDF page")
    ap.add_argument("--render", required=True, help="rendered PNG of the rebuilt HTML page")
    ap.add_argument("--pdf",    required=False, help="path to the source PDF (for text-layer extraction)")
    ap.add_argument("--html",   required=False, help="rendered HTML path for DOM semantic comparison")
    ap.add_argument("--page",   type=int, default=1)
    ap.add_argument("--work-dir", required=True, help="dir for diff overlay and ocr.txt")
    args = ap.parse_args()

    os.makedirs(args.work_dir, exist_ok=True)
    checkpoint_update(
        args.work_dir,
        "verify_layered",
        "running",
        {
            "page": args.page,
            "source": args.source,
            "render": args.render,
            "html": args.html,
        },
        next_action="Use score and refinementPlan to decide page-level or section-level refinement",
    )

    # Layer A — text
    src_text = ""
    if args.pdf and os.path.exists(args.pdf):
        src_text = text_from_pdf(args.pdf, args.page)
    if not src_text.strip():
        # OCR the source raster as last resort
        try:
            import pytesseract
            src_text = pytesseract.image_to_string(Image.open(args.source), lang="ita+eng")
        except Exception:
            src_text = ""
    with open(os.path.join(args.work_dir, "source_text.txt"), "w", encoding="utf-8", errors="replace") as f:
        f.write(src_text)

    ren_text = text_from_render(args.render)
    with open(os.path.join(args.work_dir, "render_text.txt"), "w", encoding="utf-8", errors="replace") as f:
        f.write(ren_text)

    html_path = args.html
    if not html_path:
        candidate = os.path.join(args.work_dir, "content.html")
        if os.path.exists(candidate):
            html_path = candidate

    # Layer A — DOM semantic diff (primary when confidence is high)
    dom_layer = dom_fidelity(args.work_dir, html_path or "", src_text)

    # Layer B — OCR text fidelity from render image
    text_layer = text_fidelity(normalise_text(src_text), normalise_text(ren_text))

    # Layer C — layout
    src_blocks = detect_blocks(args.source)
    ren_blocks = detect_blocks(args.render)
    img = Image.open(args.source)
    layout_layer = layout_fidelity(src_blocks, ren_blocks, img.size)

    # Layer D — colour
    colour_layer = colour_fidelity(args.source, args.render, src_blocks, ren_blocks)

    # Layer E — pixel diff fallback
    pixel_layer = pixel_diff(args.source, args.render, args.work_dir)
    refine_plan = derive_section_refine_plan(args.work_dir, pixel_layer.get("hotRegions", []))

    layers_scores = {
        "dom":    _safe_score(dom_layer.get("score", 0.0), 0.0),
        "text":   _safe_score(text_layer.get("score", 0.0), 0.0),
        "layout": _safe_score(layout_layer.get("score", 0.0), 0.0),
        "colour": _safe_score(colour_layer.get("score", 0.0), 0.0),
        "pixel":  _safe_score(pixel_layer.get("score", 0.0), 0.0),
    }
    combined, weights = weighted_final_score(
        dom_layer.get("route", "hybrid"),
        float(dom_layer.get("confidence", 0.0) or 0.0),
        layers_scores,
    )

    # Keep refinement compatibility (existing loop expects text/layout/colour/pixel).
    actionable_scores = {
        "text": layers_scores["text"],
        "layout": layers_scores["layout"],
        "colour": layers_scores["colour"],
        "pixel": layers_scores["pixel"],
    }
    weakest = min(actionable_scores, key=actionable_scores.get)
    weakest_signal = min(layers_scores, key=layers_scores.get)

    result = {
        "page": args.page,
        "score": round(combined, 4),
        "weights": weights,
        "route": dom_layer.get("route", "hybrid"),
        "domConfidence": dom_layer.get("confidence", 0.0),
        "weakestSignal": weakest_signal,
        "weakestLayer": weakest,
        "refinementPlan": refine_plan,
        "layers": {
            "dom":    dom_layer,
            "text":   text_layer,
            "layout": layout_layer,
            "colour": colour_layer,
            "pixel":  pixel_layer,
        },
        "artefacts": {
            "diffPng":     os.path.join(args.work_dir, "diff.png"),
            "sourceText":  os.path.join(args.work_dir, "source_text.txt"),
            "renderText":  os.path.join(args.work_dir, "render_text.txt"),
            "semanticJson": os.path.join(args.work_dir, "dom_semantic.json"),
            "sectionRefinePlan": os.path.join(args.work_dir, "section_refine_plan.json"),
        },
    }
    with open(os.path.join(args.work_dir, "dom_semantic.json"), "w", encoding="utf-8") as f:
        json.dump(dom_layer, f, indent=2, ensure_ascii=False)
    with open(os.path.join(args.work_dir, "section_refine_plan.json"), "w", encoding="utf-8") as f:
        json.dump(refine_plan, f, indent=2, ensure_ascii=False)

    checkpoint_update(
        args.work_dir,
        "verify_layered",
        "completed",
        {
            "score": result.get("score"),
            "route": result.get("route"),
            "weakestLayer": result.get("weakestLayer"),
            "recommendedSectionIds": (refine_plan or {}).get("recommendedSectionIds", []),
        },
        next_action="If score below target, run refinement loop; otherwise save final output",
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
