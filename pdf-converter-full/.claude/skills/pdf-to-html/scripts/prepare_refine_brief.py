#!/usr/bin/env python3
"""
prepare_refine_brief.py — set up state for ONE refinement pass.

This is the skill's closest mechanical match to the web app's
`refineUnifiedPage` call shape. It prepares an isolated brief so each pass
can be run with a clean, bounded context.

Workflow per pass:
  1. (caller, before this script) Render current saved HTML, run pixel_diff
     to get diff.png + hot regions, score with verify_layered.py.
    2. THIS SCRIPT: write a self-contained refinement brief to
      <work-dir>/refine-pass-<N>/brief.md and create the I/O dirs the
      refinement step will write to.
    3. (caller) Run a refinement pass with the brief as prompt/context.
      The refinement step reads the referenced images, writes the new fragment
     to <work-dir>/refine-pass-<N>/fragment.html and CSS to
      <work-dir>/refine-pass-<N>/page.css, and returns a brief confirmation.
    4. (caller, after refinement returns) Run apply_refine_result.py to
     re-score and decide whether to commit or revert this pass.

Usage:
  python3 prepare_refine_brief.py \\
      --work-dir <work>/page_<N> \\
      --pass <N> \\
      --previous-score <0..1> \\
      --weakest-layer <text|layout|colour|pixel> \\
      --refinement-prompt <path to references/prompts.md "Refinement" section>
"""
import argparse, json, os, sys
from pathlib import Path


REFINE_RULES_FALLBACK = """\
You are refining HTML+CSS that was produced in a previous turn so that it
more faithfully matches a target design.

You will be shown FOUR kinds of imagery:
  1. The TARGET design — what your output should ultimately look like.
  2. The CURRENT render of the previous HTML+CSS at the same dimensions.
  3. A DIFF OVERLAY — pixels rendered in red are where the current render
     differs from the target. Greyer areas are already matching.
  4. ZOOMED hot regions: side-by-side crops of the worst-mismatched tiles
     ("source" first, "render" second).

For each red region in the diff, identify what's wrong and fix it. Common
causes, in roughly decreasing frequency:
  • MISSING CONTENT — the previous render's document was shorter than the
    source; the diff has a uniform red band at the bottom. Add the missing
    sections back from the source / positional text.
  • SPACING — section padding, gaps, vertical rhythm off by 8-32px.
  • TYPOGRAPHY — h1/h2 size or weight wrong, line-height too tight/loose.
  • COLOR — section background, text color, or accent off. Use measured
    palette; do NOT eyeball hex values.
  • LAYOUT — column widths, flex/grid wrap behavior, alignment.
  • DECORATION position / shape — corner curves, gutter bars wrong size.

Preserve everything that already matches well. Don't restructure sections
that are already correct — the diff tells you exactly where the problems
are.

HARD RULES:
- Copy text VERBATIM from the source. Do not paraphrase.
- Use measured colors over guesses.
- Section order matches the source.
- No <script> tags, no external stylesheets, no Tailwind.
- Every CSS selector scoped under .page-root, except self-contained .d-<id>
  selectors inside catalog design blocks.
- Preserve catalog design markers and CSS sentinel blocks. Use a matching
  reference as a structural prior; the current page image, text and palette
  remain authoritative.
- STAY FLUID: adjust clamp() caps / vw coefficients instead of swapping in
  bare px. No layout tables, no fixed px widths, no min-width on .page-root,
  do not delete media queries. Flowcharts/trees/concept maps
  (data-layout="diagram") keep all their columns at every width.
- Output a complete new fragment + complete new CSS — not a patch.
"""


def focused_design_references(work, focus_sections):
    if not focus_sections:
        return []
    regions_path = work / "regions.json"
    brief_path = work / "design_brief.json"
    if not regions_path.exists() and not brief_path.exists():
        return []
    if not regions_path.exists() or not brief_path.exists():
        raise FileNotFoundError("both regions.json and design_brief.json are required for design refinement")
    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    if not isinstance(regions, list) or not isinstance(brief.get("designs"), list):
        raise ValueError("invalid regions.json or design_brief.json")
    focus = set(focus_sections)
    design_ids = set()
    for region in regions:
        if region.get("id") in focus:
            ids = region.get("designIds", [])
            if not isinstance(ids, list):
                raise ValueError(f"region {region.get('id')} has invalid designIds")
            design_ids.update(ids)
    references = []
    for design in brief["designs"]:
        if design.get("designId") in design_ids and design.get("hasReference"):
            if not design.get("html") or not design.get("css"):
                raise ValueError(f"incomplete reference for {design.get('designId')}")
            references.append(design)
    return references


def sections_by_content_kind(work, content_kind):
    regions_path = work / "regions.json"
    if not regions_path.exists():
        raise FileNotFoundError("regions.json is required for kind-focused refinement")
    regions = json.loads(regions_path.read_text(encoding="utf-8"))
    if not isinstance(regions, list):
        raise ValueError("regions.json must be a JSON array")
    kind = content_kind.strip().lower()
    section_ids = []
    for region in regions:
        role = str(region.get("role") or "").strip().lower()
        region_kind = str(region.get("contentType") or "").strip().lower()
        if role == kind or region_kind == kind:
            section_ids.append(str(region.get("id")))
    return [section_id for section_id in section_ids if section_id]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True, help="<work>/page_<N> directory")
    ap.add_argument("--pass", dest="pass_n", type=int, required=True,
                    help="1-based refinement pass number")
    ap.add_argument("--previous-score", type=float, required=True)
    ap.add_argument("--weakest-layer", default="layout",
                    choices=["text", "layout", "colour", "pixel"])
    ap.add_argument("--refinement-prompt", default=None,
                    help="path to the 'Refinement' section text from prompts.md")
    ap.add_argument("--source", default=None, help="defaults to <work-dir>/source.png")
    ap.add_argument("--render", default=None, help="defaults to <work-dir>/render.png")
    ap.add_argument("--diff",   default=None, help="defaults to <work-dir>/diff.png")
    ap.add_argument("--fragment", default=None, help="defaults to <work-dir>/fragment.html")
    ap.add_argument("--css",      default=None, help="defaults to <work-dir>/page.css")
    ap.add_argument("--focus-sections", default=None,
            help="comma-separated section ids to prioritize (page-first flow)")
    ap.add_argument("--focus-kind", default=None,
            help="focus sections whose role or contentType matches this kind")
    ap.add_argument("--include-kind", default=None,
            help="append sections matching this role/contentType to the normal focus")
    args = ap.parse_args()

    work = Path(args.work_dir).resolve()
    n = args.pass_n
    pass_dir = work / f"refine-pass-{n}"
    pass_dir.mkdir(parents=True, exist_ok=True)

    src   = Path(args.source   or (work / "source.png")).resolve()
    ren   = Path(args.render   or (work / "render.png")).resolve()
    diff  = Path(args.diff     or (work / "diff.png")).resolve()
    frag  = Path(args.fragment or (work / "fragment.html")).resolve()
    css   = Path(args.css      or (work / "page.css")).resolve()

    out_frag = pass_dir / "fragment.html"
    out_css  = pass_dir / "page.css"

    focus_sections = []
    if args.focus_sections and args.focus_kind:
        ap.error("--focus-sections and --focus-kind are mutually exclusive")
    if args.focus_kind and args.include_kind:
        ap.error("--focus-kind and --include-kind are mutually exclusive")
    if args.focus_kind:
        focus_sections = sections_by_content_kind(work, args.focus_kind)
        if not focus_sections:
            ap.error(f"no regions match focus kind {args.focus_kind!r}")
    elif args.focus_sections:
      focus_sections = [s.strip() for s in args.focus_sections.split(",") if s.strip()]
    elif (work / "section_refine_plan.json").exists():
      plan = json.loads((work / "section_refine_plan.json").read_text(encoding="utf-8"))
      focus_sections = [str(x) for x in (plan.get("recommendedSectionIds") or []) if str(x).strip()]
    if args.include_kind:
        included = sections_by_content_kind(work, args.include_kind)
        focus_sections = list(dict.fromkeys(focus_sections + included))
    focused_designs = focused_design_references(work, focus_sections)
    if focused_designs:
        design_section = (
            "## Catalog references for hotspot designs\n\n"
            "Use these as structural priors, not as the source for this page's content, "
            "colours, fonts or spacing. Preserve data-design-id, d-<id> classes and "
            "CSS sentinel blocks; define the design colour variables for this page.\n\n"
            "```json\n"
            + json.dumps(focused_designs, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
    else:
        design_section = ""

    # Load refinement rules. Prefer the verbatim text from references/prompts.md
    # if a path was provided; otherwise fall back to the embedded version.
    if args.refinement_prompt and Path(args.refinement_prompt).exists():
        rules = Path(args.refinement_prompt).read_text(encoding="utf-8")
    else:
        rules = REFINE_RULES_FALLBACK

    # Discover hot-region crops if any exist in the work dir.
    hot_crops = sorted(work.glob("hot-region-*.png"))
    hot_lines = "\n".join(
        f"  - `{p}`"
        for p in hot_crops
    ) if hot_crops else "  (none — render the whole diff overlay if no hot crops are present)"

    prev_pct = f"{args.previous_score * 100:.2f}%"
    focus_line = ", ".join(focus_sections) if focus_sections else "(none specified)"
    diagram_only = str(args.focus_kind or "").strip().lower() == "diagram"
    included_diagrams = str(args.include_kind or "").strip().lower() == "diagram"
    diagram_instructions = """
## Diagram-only audit

Inspect only the focused diagram regions. Compare the source crop with the
current render for node/label placement, connectors, arrows, branch geometry,
and caption association. Keep real prose and captions as HTML text. If a
faithful HTML/CSS reconstruction is not stable for a small or visually dense
diagram, replace only that diagram with a tight source crop image. Do not crop
the surrounding section or unrelated prose, and do not rewrite non-diagram
content.
""" if diagram_only else ""
    if included_diagrams:
        diagram_instructions = """
## Diagram checks within this section pass

Diagram section IDs have been added to the focused section group so they are
audited in this pass alongside verifier-recommended hotspots. Inspect only
their source and render crops: verify node/label placement, connectors, arrows,
branch geometry, and caption association. A small, visually dense diagram may
be replaced with a tight source crop when HTML/CSS cannot faithfully preserve
its geometry. Keep captions and surrounding prose as HTML.
"""

    brief = f"""# Refinement pass {n} — clean-context brief

  Treat this as an isolated pass. Use only THIS BRIEF and the files it
  references as context.

## Your task

Refine the HTML+CSS at the paths below so that the next pixel-diff
against the source scores higher than the previous {prev_pct}. The
weakest layer in the previous scoring run was **{args.weakest_layer}**.

Page-first refinement focus:
- PRIORITIZE fixes inside these hotspot sections first: {focus_line}
- If this list is non-empty, avoid broad page-wide rewrites unless needed
  for text continuity or global consistency.

{diagram_instructions}
{design_section}
## Verbatim refinement rules

{rules}

## Inputs (read these with the `Read` tool — use your vision)

- TARGET (source PDF page render): `{src}`
- CURRENT render of previous HTML+CSS: `{ren}`
- DIFF overlay (red = mismatched pixels): `{diff}`
- HOT region crops (worst-mismatched tiles, source + render pairs):
{hot_lines}
- PREVIOUS fragment HTML: `{frag}`
- PREVIOUS scoped CSS: `{css}`

Read every image above before writing any HTML.

## Strict output contract

1. Write the **complete new HTML fragment** to: `{out_frag}`
   - Must start with `<div class="page-root"` and end with `</div>`.
   - No `<!doctype>`, `<html>`, `<head>`, `<body>`, or `<script>` tags.
   - Every text section in the source MUST be reconstructed as real HTML
     text (`<p>`, `<li>`, `<h*>`, etc.). Do NOT embed prose sections as
     a single `<img>` — that's the disallowed shortcut.
   - `<img src="./crops/...">` is permitted ONLY for media-tier assets
     (photographs, illustrations, charts, hand-drawn schemas with
     colored overlays). Decorations must be CSS or inline SVG.

2. Write the **complete new CSS** to: `{out_css}`
   - Every selector scoped under `.page-root`, except self-contained `.d-<id>`
     selectors inside catalog design blocks.
   - `.page-root` must `width: 100%; max-width: 100%; margin: 0;
     position: relative; overflow-x: hidden;`.
   - Columns are CSS grid with the source ratio that stack to one column
     at <= 768px. Never `<table role="presentation">` for layout.
   - Sizes follow `output-contract.md` "Fluid layout contract":
     `clamp(floor, calc(px / sourceWidth * 100vw), px)`.
   - Flowcharts / trees / concept maps keep their columns inside a
     `.scroll-x` wrapper; never collapse them to a single column.

3. After writing both files, return a SINGLE message to the parent in
   this exact format:

   ```
   DONE pass={n}
   fragment_path={out_frag}
   css_path={out_css}
   summary: <1-2 sentences describing the changes you made>
   ```

   Do not include the HTML/CSS contents in the return message — they are
   already on disk. The parent will read them from the paths above.

## What NOT to do

- Do not emit a patch / diff. Emit the COMPLETE new fragment + CSS.
- Do not paraphrase or shorten the source text.
- Do not introduce sections the source doesn't have.
- Do not write to any path other than the two specified above.
- Do not ask follow-up questions. If something is ambiguous, choose the
  most faithful interpretation and proceed.
"""

    brief_path = pass_dir / "brief.md"
    brief_path.write_text(brief, encoding="utf-8")

    out = {
        "ok": True,
        "pass": n,
        "passDir": str(pass_dir),
        "briefPath": str(brief_path),
        "fragmentOutPath": str(out_frag),
        "cssOutPath": str(out_css),
        "weakestLayer": args.weakest_layer,
        "previousScore": args.previous_score,
        "previousScorePct": prev_pct,
        "focusSections": focus_sections,
        "focusKind": args.focus_kind,
        "includeKind": args.include_kind,
        "focusedDesigns": [design["designId"] for design in focused_designs],
        "instructionForCaller": (
          f"Run one refinement pass using the contents of {brief_path} as the prompt. "
          f"When the refinement returns, run "
            f"apply_refine_result.py --pass {n} --work-dir {work}."
        ),
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
