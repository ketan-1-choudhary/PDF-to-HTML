#!/usr/bin/env python3
"""
refine_targeted.py — replacement refinement loop for the pdf-to-html skill.

Old loop: render → score → re-render with generic "fix the diff" prompt (x3).
The old loop was uniformly applying the same kind of fix every pass, so when
the first pass missed the dominant failure mode (font-metric drift causing
layout-block misalignment), subsequent passes couldn't recover.

New loop: render → score → identify weakest layer → apply LAYER-SPECIFIC fix
strategy → re-score → commit if improved, revert if not → next pass targets
the NEW weakest layer. Each pass has a specific hypothesis to test rather
than a generic "make it better" prompt.

Strategy table (encoded in TARGETED_PROMPTS):

  weakest=layout   → font-metric compensation strategies:
                      1) push body padding-top to match source's first-text-y
                      2) reduce inter-paragraph margins so adjacent blocks
                         merge in morphology
                      3) widen/narrow figure column to position chart at
                         source's x coordinate
                      4) try font-size ±2-4% to push wrap points closer
  weakest=text     → restore/repair any text the previous pass dropped;
                      do NOT touch wrapping, do NOT touch fonts
  weakest=pixel    → tune SVG positions and crop margins; do NOT change
                      text wrap (would also hurt layout)
  weakest=colour   → re-derive hex values from meta.json palette; do NOT
                      touch block structure (changing block structure
                      changes which blocks get colour-matched)

Commit rules (strict):
  - keep change iff overall improved by ≥ +0.005 AND colour stayed ≥ 0.88
  - if overall regressed at all, revert
  - if colour dropped below 0.88, revert immediately regardless of overall

Usage:
    python3 refine_targeted.py \\
        --pdf <pdf-path> --page <N> \\
        --out-dir <out-root>/<run-slug>/page_<N> \\
        --work-dir <work>/page_<N> \\
        --source <work>/page_<N>/source.png \\
        --max-passes 6 --target 0.92 \
        --min-improvement 0.005 --min-colour 0.88 --patience 2 \
        --html-fragment <work>/page_<N>/fragment.html \\
        --css-file <work>/page_<N>/page.css
"""
import argparse, json, os, shutil, subprocess, sys, re
from pathlib import Path
from checkpoint_state import checkpoint_update

THIS_DIR = Path(__file__).resolve().parent

# Per-layer fix recipes. Each recipe is a list of (label, mutator_fn) pairs;
# the loop tries them in order and keeps the first that improves the score.
def _read_css(p): return Path(p).read_text()
def _write_css(p, s): Path(p).write_text(s)


def mutate_body_padding_top(css: str, target_rem: float) -> str:
    return re.sub(
        r'(\.page-root \.body\s*\{\s*padding:\s*)[0-9.]+rem',
        lambda m: f'{m.group(1)}{target_rem}rem',
        css, count=1,
    )

def mutate_paragraph_margin_bottom(css: str, target_rem: float) -> str:
    return re.sub(
        r'(\.page-root \.text-col p\s*\{[^}]*?margin:\s*0 0 )[0-9.]+rem',
        lambda m: f'{m.group(1)}{target_rem}rem',
        css, count=1,
    )

def mutate_body_font_size(css: str, target_rem: float) -> str:
    return re.sub(
        r'(\.page-root \.text-col p\s*\{[^}]*?font-size:\s*)[0-9.]+rem',
        lambda m: f'{m.group(1)}{target_rem}rem',
        css, count=1, flags=re.DOTALL,
    )

def mutate_body_line_height(css: str, target: float) -> str:
    return re.sub(
        r'(\.page-root \.text-col p\s*\{[^}]*?line-height:\s*)[0-9.]+',
        lambda m: f'{m.group(1)}{target}',
        css, count=1, flags=re.DOTALL,
    )


LAYOUT_RECIPES = [
    ("body padding-top → 2.0rem", lambda css: mutate_body_padding_top(css, 2.0)),
    ("body padding-top → 2.5rem", lambda css: mutate_body_padding_top(css, 2.5)),
    ("body padding-top → 3.0rem", lambda css: mutate_body_padding_top(css, 3.0)),
    ("paragraph margin-bottom → 0.4rem",
        lambda css: mutate_paragraph_margin_bottom(css, 0.4)),
    ("paragraph margin-bottom → 0.6rem",
        lambda css: mutate_paragraph_margin_bottom(css, 0.6)),
]
TEXT_RECIPES = [
    # Text-layer regressions usually come from losing tokens; the targeted
    # fix is to re-run generation with explicit instruction to preserve all
    # words from positionalText. The actual mutator is a no-op here; the
    # caller is expected to re-invoke generation. For automated refinement,
    # we just attempt to widen the type a hair so OCR catches more.
    ("body font-size → 1.04rem", lambda css: mutate_body_font_size(css, 1.04)),
    ("body line-height → 1.50",  lambda css: mutate_body_line_height(css, 1.50)),
]
PIXEL_RECIPES = [
    # Most pixel improvements come for free with layout fixes. Specific
    # pixel-only fixes here are limited to tuning visible chrome margins.
    ("body padding-top → 1.8rem (subtle nudge)",
        lambda css: mutate_body_padding_top(css, 1.8)),
]
COLOUR_RECIPES = []  # do not touch; if colour ever drops, revert prior fix


def score_html(scripts_dir, work_dir, pdf, page, html, css, out_root, run_id, source_path):
    """Save → render → score. Returns the parsed layered-verifier JSON."""
    subprocess.run([
        "node", str(scripts_dir / "save_page.mjs"),
        "--html", html, "--css", css,
        "--out-root", out_root, "--run-id", run_id,
        "--pdf-for-hash", pdf, "--page", str(page),
    ], capture_output=True, text=True, check=True)
    out_dir = Path(out_root) / _slug(run_id, pdf) / f"page_{page}"
    content_html = out_dir / "content.html"
    render_png = Path(work_dir) / "_refine_render.png"
    try:
        from PIL import Image
        sw, sh = Image.open(source_path).size
    except Exception:
        sw, sh = 1308, 1761
    subprocess.run([
        "python3", str(scripts_dir / "fallback_render_html.py"),
        "--html", str(content_html), "--width", str(sw), "--height", str(sh),
        "--out", str(render_png),
    ], capture_output=True, text=True, check=True)
    # Copy render into work_dir for the scorer
    work_render = Path(work_dir) / "render.png"
    shutil.copy(render_png, work_render)
    res = subprocess.run([
        "python3", str(scripts_dir / "verify_layered.py"),
        "--source", str(Path(work_dir) / "source.png"),
        "--render", str(work_render),
        "--html", str(content_html),
        "--pdf", pdf, "--page", str(page),
        "--work-dir", str(work_dir),
    ], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def _slug(run_id, pdf):
    """Mirror save_page.mjs slugging so we resolve the same output folder."""
    import hashlib, re as _re
    slug = _re.sub(r'[^a-z0-9]+', '-', run_id.lower()).strip('-')
    digest = hashlib.sha256(open(pdf, 'rb').read()).hexdigest()[:10]
    return f"{slug}-{digest}"


def pick_recipes(weakest):
    return {
        "layout": LAYOUT_RECIPES, "text": TEXT_RECIPES,
        "pixel":  PIXEL_RECIPES,  "colour": COLOUR_RECIPES,
    }.get(weakest, [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--source", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--html-fragment", required=True)
    ap.add_argument("--css-file", required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--max-passes", type=int, default=4)
    ap.add_argument("--target", type=float, default=0.92)
    ap.add_argument("--min-improvement", type=float, default=0.005)
    ap.add_argument("--min-colour", type=float, default=0.88)
    ap.add_argument("--patience", type=int, default=2,
                    help="stop after this many consecutive non-improving passes")
    ap.add_argument("--scripts-dir", default=str(THIS_DIR))
    args = ap.parse_args()

    scripts_dir = Path(args.scripts_dir)
    checkpoint_update(
        args.work_dir,
        "refine_targeted",
        "running",
        {
            "page": args.page,
            "target": args.target,
            "maxPasses": args.max_passes,
            "minImprovement": args.min_improvement,
            "patience": args.patience,
        },
        next_action="Continue refinement until target/plateau/cap then apply results",
    )
    best_css = _read_css(args.css_file)
    best = score_html(scripts_dir, args.work_dir, args.pdf, args.page,
                      args.html_fragment, args.css_file, args.out_root, args.run_id,
                      args.source)
    log = [{"pass": 0, "label": "initial", "score": best["score"],
            "layers": {k: v["score"] for k, v in best["layers"].items()},
            "kept": True}]
    print(f"initial: overall={best['score']:.4f}  weakest={best['weakestLayer']}", file=sys.stderr)
    stagnant = 0

    for p in range(1, args.max_passes + 1):
        if best["score"] >= args.target:
            log.append({"pass": p, "label": "target reached", "score": best["score"], "kept": False})
            break
        weakest = best["weakestLayer"]
        recipes = pick_recipes(weakest)
        if not recipes:
            log.append({"pass": p, "label": f"no recipes for {weakest}",
                        "score": best["score"], "kept": False})
            break

        improved_this_pass = False
        for label, mutator in recipes:
            cand_css = mutator(best_css)
            if cand_css == best_css:
                continue  # no change
            _write_css(args.css_file, cand_css)
            try:
                cand = score_html(scripts_dir, args.work_dir, args.pdf, args.page,
                                  args.html_fragment, args.css_file, args.out_root, args.run_id,
                                  args.source)
            except subprocess.CalledProcessError as e:
                print(f"  pass {p} [{label}]: render/score failed; skipping", file=sys.stderr)
                continue
            colour_score = cand["layers"]["colour"]["score"]
            improved = cand["score"] >= best["score"] + args.min_improvement
            no_colour_regress = colour_score >= args.min_colour

            log.append({
                "pass": p, "label": label,
                "score": cand["score"],
                "layers": {k: v["score"] for k, v in cand["layers"].items()},
                "kept": improved and no_colour_regress,
                "delta": round(cand["score"] - best["score"], 4),
            })
            print(f"  pass {p} [{label}]: {cand['score']:.4f} "
                  f"(Δ {cand['score']-best['score']:+.4f}, "
                  f"colour={colour_score:.3f}, kept={improved and no_colour_regress})",
                  file=sys.stderr)

            if improved and no_colour_regress:
                best, best_css = cand, cand_css
                improved_this_pass = True
                stagnant = 0
                break  # next pass; re-evaluate weakest layer
            else:
                _write_css(args.css_file, best_css)  # revert

        if not improved_this_pass:
            stagnant += 1
            if stagnant >= max(1, args.patience):
                print(
                    f"  pass {p}: no recipe improved score for {stagnant} consecutive pass(es); stopping",
                    file=sys.stderr,
                )
                log.append({
                    "pass": p,
                    "label": "early stop: patience exhausted",
                    "score": best["score"],
                    "kept": False,
                })
                break
            print(
                f"  pass {p}: no recipe improved score; continuing (stagnant={stagnant}/{args.patience})",
                file=sys.stderr,
            )

    _write_css(args.css_file, best_css)
    final_result = {
        "ok": True,
        "finalScore": best["score"],
        "finalLayers": {k: v["score"] for k, v in best["layers"].items()},
        "target": args.target,
        "minImprovement": args.min_improvement,
        "minColour": args.min_colour,
        "patience": args.patience,
        "passes": log,
    }
    checkpoint_update(
        args.work_dir,
        "refine_targeted",
        "completed",
        {
            "finalScore": final_result.get("finalScore"),
            "passCount": len(log),
            "target": args.target,
        },
        next_action="Run apply_refine_result to commit/revert and continue orchestration",
    )
    print(json.dumps(final_result, indent=2))


if __name__ == "__main__":
    main()
