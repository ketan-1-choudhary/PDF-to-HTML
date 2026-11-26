#!/usr/bin/env python3
"""
Python fallback for render_page.mjs.

Uses poppler (pdftoppm) to rasterise a PDF page to PNG. Requires only
system tools that are present on most Linux/macOS sandboxes:
  - pdftoppm (from poppler-utils)
  - pdfinfo (from poppler-utils)
  - Pillow (pip install pillow)

Drops in for render_page.mjs when @napi-rs/canvas / pdfjs-dist bus-errors
in restricted sandboxes. Emits the same single-line JSON record.

Usage:
  python3 fallback_render_page.py --pdf <path> --page <N> --out <png>
                                  [--scale 2] [--max-long-edge 2200]
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

def parse_pdfinfo(pdf_path):
    out = subprocess.run(['pdfinfo', pdf_path], capture_output=True, text=True, check=True).stdout
    info = {}
    for line in out.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            info[k.strip()] = v.strip()
    pages = int(info.get('Pages', '0'))
    size = info.get('Page size', '')
    # "595.276 x 822.047 pts"
    w_pt = h_pt = None
    try:
        w_pt = float(size.split('x')[0].strip())
        h_pt = float(size.split('x')[1].split('pts')[0].strip())
    except Exception:
        pass
    return pages, w_pt, h_pt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pdf', required=True)
    ap.add_argument('--page', required=True, type=int)
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', default=2, type=float)
    ap.add_argument('--max-long-edge', dest='max_long_edge', default=2200, type=int)
    args = ap.parse_args()

    pdf_path = os.path.abspath(args.pdf)
    out_path = os.path.abspath(args.out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        page_count, w_pt, h_pt = parse_pdfinfo(pdf_path)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"pdfinfo failed: {e}"}))
        sys.exit(2)

    page = max(1, min(args.page, page_count))

    # DPI = 72 * scale; pdftoppm renders at that resolution.
    dpi = max(72, min(int(72 * args.scale), 288))
    # Write to a tmpdir first — some mounted filesystems break pdftoppm's
    # PNG writer (libpng "Write Error" / SIGSEGV on direct-to-mount writes).
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        tmp_root = os.path.join(td, 'p')
        cmd = ['pdftoppm', '-r', str(dpi), '-f', str(page), '-l', str(page), '-png', pdf_path, tmp_root]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(json.dumps({"ok": False, "error": f"pdftoppm failed: {e.stderr.decode(errors='ignore')[:200]}"}))
            sys.exit(2)
        candidates = [f"{tmp_root}-{page}.png", f"{tmp_root}-{page:02d}.png", f"{tmp_root}-{page:03d}.png"]
        rendered = next((p for p in candidates if os.path.exists(p)), None)
        if not rendered:
            print(json.dumps({"ok": False, "error": "pdftoppm produced no output"}))
            sys.exit(2)
        # Copy rendered file into the workspace so the next Pillow open works.
        staged = os.path.join(td, 'staged.png')
        shutil.copy(rendered, staged)
        rendered = staged

    from PIL import Image
    im = Image.open(rendered).convert('RGB')
    long_edge = max(im.size)
    if long_edge > args.max_long_edge:
        s = args.max_long_edge / long_edge
        im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))), Image.LANCZOS)
    im.save(out_path, optimize=True)
    os.remove(rendered)

    print(json.dumps({
        "ok": True,
        "pngPath": out_path,
        "pageNumber": page,
        "pageCount": page_count,
        "width": im.size[0],
        "height": im.size[1],
        "scale": round(im.size[0] / (w_pt or im.size[0]) * 72, 3) if w_pt else args.scale,
        "engine": "python-poppler"
    }))

if __name__ == '__main__':
    main()
