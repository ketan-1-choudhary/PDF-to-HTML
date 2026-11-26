#!/usr/bin/env python3
"""
Python fallback for extract_text.mjs.

Uses pdfminer.six to walk the PDF page's text layer with per-run font
sizes and font names — same data extract_text.mjs returns. Emits the
same JSON shape so callers don't need to branch.

Usage:
  python3 fallback_extract_text.py --pdf <path> --page <N> [--max-runs 120]
"""
import argparse, json, sys
from collections import Counter

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf', required=True)
    ap.add_argument('--page', required=True, type=int)
    ap.add_argument('--max-runs', dest='max_runs', default=120, type=int)
    args = ap.parse_args()

    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer, LTTextLine, LTChar, LTImage, LTFigure
    except Exception as e:
        # Cannot extract — return hasTextLayer:false so caller falls back to OCR.
        print(json.dumps({
            "ok": True, "hasTextLayer": False,
            "extractError": f"pdfminer unavailable: {e}",
            "engine": "python-pdfminer"
        }))
        return

    page_idx = args.page - 1
    runs = []
    embedded_images = []
    page_width = page_height = None

    try:
        for i, page_layout in enumerate(extract_pages(args.pdf, page_numbers=[page_idx])):
            page_width = float(page_layout.width)
            page_height = float(page_layout.height)
            # Collect image references (any LTImage / LTFigure).
            stack = list(page_layout)
            while stack:
                el = stack.pop()
                if isinstance(el, LTImage):
                    embedded_images.append({
                        "x": round(float(el.x0), 2),
                        "y": round(float(page_height - el.y1), 2),
                        "width": round(float(el.x1 - el.x0), 2),
                        "height": round(float(el.y1 - el.y0), 2),
                    })
                elif hasattr(el, '__iter__'):
                    try:
                        stack.extend(list(el))
                    except TypeError:
                        pass
            # Walk text runs.
            for textbox in page_layout:
                if not isinstance(textbox, LTTextContainer):
                    continue
                for line in textbox:
                    if not isinstance(line, LTTextLine):
                        continue
                    chars = [c for c in line if isinstance(c, LTChar)]
                    if not chars:
                        continue
                    text = line.get_text().rstrip('\n')
                    if not text.strip():
                        continue
                    # Aggregate fonts/sizes for the line.
                    fontname = Counter(c.fontname for c in chars).most_common(1)[0][0]
                    size = round(sum(c.size for c in chars) / len(chars), 2)
                    x = round(min(c.x0 for c in chars), 2)
                    y_pdf = round(max(c.y1 for c in chars), 2)  # top in PDF coords
                    y_top = round(page_height - y_pdf, 2)        # convert to image-space top
                    runs.append({
                        "text": text,
                        "x": x,
                        "y": y_top,
                        "fontSize": size,
                        "fontName": fontname,
                    })
            break
    except Exception as e:
        print(json.dumps({
            "ok": True, "hasTextLayer": False,
            "extractError": f"pdfminer parse error: {e}",
            "engine": "python-pdfminer"
        }))
        return

    if not runs:
        print(json.dumps({
            "ok": True, "hasTextLayer": False,
            "engine": "python-pdfminer",
            "embeddedImages": embedded_images,
            "pageWidth": page_width, "pageHeight": page_height
        }))
        return

    runs.sort(key=lambda r: (r['y'], r['x']))
    capped = runs[: args.max_runs]
    full_text = "\n".join(r['text'] for r in runs)

    # Map PDF font names to web stacks (heuristic).
    def stack_for(font):
        f = (font or '').lower()
        if 'mono' in f or 'courier' in f or 'consolas' in f:
            return '"JetBrains Mono", "Source Code Pro", ui-monospace, monospace'
        if 'garamond' in f:
            return '"EB Garamond", Georgia, "Times New Roman", serif'
        if 'baskerville' in f:
            return '"Libre Baskerville", Georgia, serif'
        if 'caslon' in f or 'minion' in f or 'georgia' in f or 'times' in f or 'serif' in f:
            return '"Source Serif 4", "Source Serif Pro", Georgia, "Times New Roman", serif'
        if 'helvetica' in f or 'arial' in f or 'sans' in f or 'inter' in f or 'roboto' in f:
            return '"Inter", "Helvetica Neue", Arial, sans-serif'
        if 'meta' in f:
            return '"Source Serif 4", "PT Serif", Georgia, serif'
        return '"Inter", "Helvetica Neue", Arial, sans-serif'

    fonts_seen = sorted({r['fontName'] for r in runs if r['fontName']})
    stacks = sorted({stack_for(f) for f in fonts_seen})

    positional_lines = []
    for r in capped:
        positional_lines.append(
            f"@({r['x']:.0f},{r['y']:.0f}) size={r['fontSize']:.1f} font={r['fontName']} :: {r['text']}"
        )

    print(json.dumps({
        "ok": True,
        "hasTextLayer": True,
        "engine": "python-pdfminer",
        "pageWidth": page_width,
        "pageHeight": page_height,
        "text": full_text,
        "positionalText": "\n".join(positional_lines),
        "runs": capped,
        "totalRuns": len(runs),
        "fontsSeen": fonts_seen,
        "suggestedFontStacks": stacks,
        "embeddedImages": embedded_images
    }))

if __name__ == '__main__':
    main()
