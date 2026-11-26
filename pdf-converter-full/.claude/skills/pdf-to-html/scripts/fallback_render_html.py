#!/usr/bin/env python3
"""
Python fallback for render_html.mjs.

Uses WeasyPrint (HTML → PDF) followed by pdftoppm (PDF → PNG) to
render a saved HTML page at a chosen viewport size. Same JSON output
shape as the Node helper: { pngPath, width, height, ok }.

Use this when puppeteer cannot launch — e.g. on sandboxes where the
bundled Chromium binary fails to start or @napi-rs/canvas bus-errors.

Usage:
  python3 fallback_render_html.py --html <path> --width 1240 --height 1670
                                  --out <png>
"""
import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path

FRAME_NEUTRALIZE_CSS = """
/* Verification render overrides: disable screen card framing. */
html, body {
    margin: 0 !important;
    padding: 0 !important;
    background: #ffffff !important;
}

@media screen {
    body {
        display: block !important;
        overflow: hidden !important;
        padding: 0 !important;
        background: #ffffff !important;
    }
    .page-root {
        width: 100% !important;
        max-width: 100% !important;
        min-width: 0 !important;
        margin: 0 !important;
        box-shadow: none !important;
        border-radius: 0 !important;
    }
}
"""


def prepare_render_html(html_path, neutralize_screen_frame=True):
        src = Path(html_path).resolve()
        text = src.read_text(encoding='utf-8', errors='replace')
        has_html = '<html' in text.lower()
        base_href = f"file://{str(src.parent).replace(os.sep, '/')}/"
        injected = f"<style>{FRAME_NEUTRALIZE_CSS}</style>" if neutralize_screen_frame else ""

        if has_html:
                out = text
                lower = out.lower()
                if '<base ' not in lower:
                        if '</head>' in lower:
                                i = lower.rfind('</head>')
                                out = out[:i] + f"<base href='{base_href}'>" + out[i:]
                                lower = out.lower()
                        else:
                                out = f"<head><base href='{base_href}'></head>" + out
                                lower = out.lower()
                if injected:
                        if '</head>' in lower:
                                i = lower.rfind('</head>')
                                out = out[:i] + injected + out[i:]
                        else:
                                out += injected
        else:
                out = f"""<!doctype html><html><head><meta charset='utf-8'>
<base href='{base_href}'>
{injected}
</head><body>{text}</body></html>"""

        tmp = tempfile.NamedTemporaryFile(suffix='.html', delete=False)
        tmp.write(out.encode('utf-8'))
        tmp.close()
        return tmp.name

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', required=True)
    ap.add_argument('--width', required=True, type=int)
    ap.add_argument('--height', required=True, type=int)
    ap.add_argument('--out', required=True)
    ap.add_argument(
        '--screen-frame-mode',
        choices=['neutralize', 'preserve'],
        default='neutralize',
        help='neutralize: ignore screen card framing for verification geometry; preserve: render raw screen styles',
    )
    args = ap.parse_args()
    neutralize = args.screen_frame_mode != 'preserve'

    try:
        import weasyprint
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"weasyprint unavailable: {e}"}))
        sys.exit(2)

    html_path = os.path.abspath(args.html)
    out_path  = os.path.abspath(args.out)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    prepared_html = prepare_render_html(html_path, neutralize_screen_frame=neutralize)
    page_css = f"""
@page {{ size: {args.width}px {args.height}px; margin: 0; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
"""

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        pdf_path = tmp.name
    try:
        weasyprint.HTML(filename=prepared_html).write_pdf(
            pdf_path,
            stylesheets=[weasyprint.CSS(string=page_css)],
        )
        # Rasterise at the CSS pixel size: pdftoppm uses DPI, so render at
        # 72 DPI (1 pt = 1 CSS px when @page is in px) then resize for clarity.
        dpi = 144
        tmp_root = out_path.rsplit('.png', 1)[0] + '__tmp'
        subprocess.run(['pdftoppm', '-r', str(dpi), '-png', '-singlefile', pdf_path, tmp_root], check=True, capture_output=True)
        from PIL import Image
        im = Image.open(tmp_root + '.png').convert('RGB')
        im = im.resize((args.width, args.height), Image.LANCZOS)
        im.save(out_path, optimize=True)
        os.remove(tmp_root + '.png')
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"render failed: {e}"}))
        sys.exit(2)
    finally:
        try: os.remove(pdf_path)
        except Exception: pass
        try: os.remove(prepared_html)
        except Exception: pass

    print(json.dumps({"ok": True, "engine": "python-weasyprint", "pngPath": out_path, "width": args.width, "height": args.height}))

if __name__ == '__main__':
    main()
