#!/usr/bin/env python3
"""
render_chromium.py — render a generated HTML+CSS fragment to a PNG using a
system Chromium / Chrome, with WeasyPrint as a final fallback.

Why this exists: render_html.mjs uses Puppeteer's bundled Chromium, but the
binary is large (~150 MB) and the npm install of @puppeteer/browsers fails
silently in air-gapped sandboxes. fallback_render_html.py uses WeasyPrint,
which is reliable but silently ignores @font-face size-adjust / ascent-override
descriptors — that means our font-metric proxy strategy (the highest-leverage
fix for layout score) is a no-op under WeasyPrint.

This helper closes the gap:

  1. Use a system-Chromium binary for verification. The CLI works without
     Node modules at widths of at least 500px. Narrower viewports use
     Puppeteer because the Chromium CLI otherwise lays them out at 500px.
  2. If Chromium fails, try WeasyPrint, then the Node helper. Output JSON
     includes the engine used so the caller can log it.

The Chromium path honours --executable-path, the PUPPETEER_EXECUTABLE_PATH /
CHROME_PATH env vars, and then probes a curated list.

Usage:
  python3 render_chromium.py \\
      --html <path-to-content.html> \\
      --width 1308 --height 1761 \\
      --out <png>
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

CHROME_CANDIDATES = [
    # explicit env vars are checked first by main()
    "google-chrome-stable",
    "google-chrome",
    "chromium-browser",
    "chromium",
    "chrome",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Common Linux paths
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]


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
        text = src.read_text(encoding="utf-8", errors="replace")
        has_html = "<html" in text.lower()
        base_href = src.parent.as_uri()
        if not base_href.endswith("/"):
                base_href += "/"
        injected = ""
        if neutralize_screen_frame:
                injected = f"<style>{FRAME_NEUTRALIZE_CSS}</style>"

        if has_html:
                out = text
                lower = out.lower()
                if "<base " not in lower:
                        head = re.search(r"<head\b[^>]*>", out, re.IGNORECASE)
                        if head:
                                i = head.end()
                                out = out[:i] + f"<base href='{base_href}'>" + out[i:]
                                lower = out.lower()
                        else:
                                out = f"<head><base href='{base_href}'></head>" + out
                                lower = out.lower()
                if injected:
                        if "</head>" in lower:
                                i = lower.rfind("</head>")
                                out = out[:i] + injected + out[i:]
                        else:
                                out += injected
        else:
                out = f"""<!doctype html><html><head><meta charset='utf-8'>
<base href='{base_href}'>
{injected}
</head><body>{text}</body></html>"""

        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False)
        tmp.write(out.encode("utf-8"))
        tmp.close()
        return tmp.name


def find_chromium(explicit=None):
    if explicit and Path(explicit).exists():
        return explicit
    for env_var in ("PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH", "CHROMIUM_PATH"):
        v = os.environ.get(env_var)
        if v and Path(v).exists():
            return v
    for cand in CHROME_CANDIDATES:
        # absolute path?
        if cand.startswith("/") and Path(cand).exists():
            return cand
        # name-on-PATH?
        found = shutil.which(cand)
        if found:
            return found
    return None


def render_with_chromium(chrome_path, html_path, width, height, out_path, neutralize_screen_frame=True):
    """Drive headless Chromium via CLI flags. No Puppeteer needed."""
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prepared_html = prepare_render_html(html_path, neutralize_screen_frame=neutralize_screen_frame)
    # We need a file:// URL so Chromium resolves relative <img src>.
    file_url = Path(prepared_html).resolve().as_uri()

    # Chromium has a stable --screenshot CLI flag.
    # --window-size sets the viewport; --hide-scrollbars keeps overflow clean.
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp_png = Path(td) / "shot.png"
            # Use new headless when available; quietly fall back to legacy headless.
            for headless_flag in ("--headless=new", "--headless"):
                cmd = [
                    chrome_path, headless_flag,
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--disable-gpu",
                    "--no-first-run", "--no-default-browser-check",
                    f"--user-data-dir={Path(td) / 'chrome-profile'}",
                    "--hide-scrollbars",
                    f"--window-size={width},{height}",
                    f"--screenshot={tmp_png}",
                    "--virtual-time-budget=5000",
                    "--font-render-hinting=none",
                    file_url,
                ]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if tmp_png.exists() and tmp_png.stat().st_size > 0:
                        break
                except subprocess.TimeoutExpired:
                    continue
            else:
                return False, "chromium timed out or produced no PNG"

            # Post-process to exact dimensions.
            try:
                from PIL import Image
                im = Image.open(tmp_png).convert("RGB")
                if im.size != (width, height):
                    # If shorter, extend white; if taller, crop to top portion.
                    target = Image.new("RGB", (width, height), (255, 255, 255))
                    if im.size[0] != width:
                        im = im.resize((width, int(im.size[1] * width / im.size[0])), Image.LANCZOS)
                    if im.size[1] > height:
                        im = im.crop((0, 0, width, height))
                    target.paste(im, (0, 0))
                    target.save(out_path, optimize=True)
                else:
                    im.save(out_path, optimize=True)
            except Exception as e:
                return False, f"PNG post-processing failed: {e}"

            return True, str(out_path)
    finally:
        try:
            os.remove(prepared_html)
        except Exception:
            pass


def render_with_weasyprint(html_path, width, height, out_path, neutralize_screen_frame=True):
    """Fallback: WeasyPrint via PDF intermediate."""
    try:
        import weasyprint
    except Exception as e:
        return False, f"weasyprint unavailable: {e}"

    html_path = Path(html_path).resolve()
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prepared_html = prepare_render_html(str(html_path), neutralize_screen_frame=neutralize_screen_frame)
    page_css = f"""
@page {{ size: {width}px {height}px; margin: 0; }}
html, body {{ margin: 0; padding: 0; background: #fff; }}
"""

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name
    try:
        weasyprint.HTML(filename=prepared_html).write_pdf(
            pdf_path,
            stylesheets=[weasyprint.CSS(string=page_css)],
        )
        dpi = 144
        with tempfile.TemporaryDirectory() as td:
            tmp_root = os.path.join(td, "p")
            subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_path, tmp_root],
                           check=True, capture_output=True)
            from PIL import Image
            png = tmp_root + ".png"
            im = Image.open(png).convert("RGB")
            im = im.resize((width, height), Image.LANCZOS)
            im.save(out_path, optimize=True)
        return True, str(out_path)
    except Exception as e:
        return False, f"weasyprint render failed: {e}"
    finally:
        try: os.remove(pdf_path)
        except Exception: pass
        try: os.remove(prepared_html)
        except Exception: pass


def _extract_first_json_object(text: str):
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def render_with_node_helper(
    html_path, width, height, out_path, executable_path=None, neutralize_screen_frame=True
):
    helper = Path(__file__).with_name("render_html.mjs")
    if not helper.exists():
        return False, "render_html.mjs helper not found"

    prepared_html = prepare_render_html(html_path, neutralize_screen_frame=neutralize_screen_frame)
    try:
        cmd = [
            "node",
            str(helper),
            "--html",
            prepared_html,
            "--width",
            str(width),
            "--height",
            str(height),
            "--out",
            str(Path(out_path).resolve()),
        ]
        if executable_path:
            cmd.extend(["--executable-path", str(executable_path)])
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=45)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"node helper failed to execute: {e}"
    finally:
        Path(prepared_html).unlink(missing_ok=True)

    out_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    err_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    parsed = _extract_first_json_object(out_text)
    if parsed and parsed.get("ok"):
        return True, str(parsed.get("pngPath") or out_path)

    detail = parsed.get("error") if isinstance(parsed, dict) else None
    msg = detail or err_text.strip() or out_text.strip() or "unknown node render error"
    return False, msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--width", required=True, type=int)
    ap.add_argument("--height", required=True, type=int)
    ap.add_argument("--out", required=True)
    ap.add_argument("--executable-path", default=None,
                    help="explicit path to Chromium/Chrome binary")
    ap.add_argument("--force-engine", choices=["chromium", "weasyprint"], default=None,
                    help="skip auto-detection; force one engine")
    ap.add_argument(
        "--screen-frame-mode",
        choices=["neutralize", "preserve"],
        default="neutralize",
        help="neutralize: ignore screen card framing for verification geometry; preserve: render raw screen styles",
    )
    args = ap.parse_args()
    if args.width <= 0 or args.height <= 0:
        ap.error("width and height must be positive")
    neutralize = args.screen_frame_mode != "preserve"

    start = time.time()

    # Try Chromium first unless forced to WeasyPrint
    if args.force_engine != "weasyprint":
        chrome = find_chromium(args.executable_path)
        if chrome:
            if args.width < 500:
                # The Chromium CLI enforces a 500px minimum layout width.
                ok, info = render_with_node_helper(
                    args.html, args.width, args.height, args.out,
                    executable_path=chrome, neutralize_screen_frame=neutralize,
                )
                renderer = "puppeteer"
            else:
                ok, info = render_with_chromium(
                    chrome, args.html, args.width, args.height, args.out,
                    neutralize_screen_frame=neutralize,
                )
                renderer = "cli"
            if ok:
                print(json.dumps({
                    "ok": True, "engine": "chromium",
                    "renderer": renderer,
                    "executable": chrome,
                    "pngPath": info,
                    "width": args.width, "height": args.height,
                    "elapsedMs": int((time.time() - start) * 1000),
                }))
                return
            print(json.dumps({
                "ok": False, "engine": "chromium",
                "executable": chrome,
                "error": info,
                "willFallback": args.force_engine != "chromium",
            }), file=sys.stderr)
            if args.force_engine == "chromium":
                sys.exit(2)
        elif args.force_engine == "chromium":
            print(json.dumps({"ok": False, "engine": "chromium", "error": "Chromium not found"}), file=sys.stderr)
            sys.exit(2)

    # WeasyPrint fallback
    ok, info = render_with_weasyprint(
        args.html,
        args.width,
        args.height,
        args.out,
        neutralize_screen_frame=neutralize,
    )
    if ok:
        print(json.dumps({
            "ok": True, "engine": "weasyprint",
            "pngPath": info,
            "width": args.width, "height": args.height,
            "elapsedMs": int((time.time() - start) * 1000),
        }))
    else:
        # Last-resort fallback when system Chromium and WeasyPrint are unavailable.
        n_ok, n_info = render_with_node_helper(
            args.html, args.width, args.height, args.out,
            executable_path=args.executable_path, neutralize_screen_frame=neutralize,
        )
        if n_ok:
            print(json.dumps({
                "ok": True,
                "engine": "node-puppeteer",
                "pngPath": n_info,
                "width": args.width,
                "height": args.height,
                "elapsedMs": int((time.time() - start) * 1000),
            }))
            return
        print(json.dumps({
            "ok": False,
            "engine": "weasyprint",
            "error": info,
            "nodeFallbackError": n_info,
        }))
        sys.exit(2)


if __name__ == "__main__":
    main()
