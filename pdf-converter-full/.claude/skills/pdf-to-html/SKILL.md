---
name: pdf-to-html
description: >-
  Convert a single PDF page into visually faithful, fluid, reflowable HTML + CSS using a
  Docling+OCR hybrid and section-based refinement. Use for high-fidelity PDF page reconstruction
  after whole-book profiling: reuse captured design structures, preserve current-page content
  and styling, verify the result, and harvest reusable components. Works for scanned and
  text-layer PDFs; the rendered page is ground truth.
---

# PDF → Faithful Responsive HTML (divide-and-merge)

This is a **desktop-faithful, fluid-first, hybrid text+vision** converter designed for both native and scanned PDFs.
The output must match the source at the source width AND re-flow cleanly down to a 320px phone — reflowable HTML is the product; a fixed desktop replica is a failure even if its pixel score is high.
Instead of generating the whole page in one shot, it uses a **structured pipeline**:

```
EXTRACT   Docling structure + OCR baseline  → docling/extracted transcript + OCR transcript
ROUTE     quality gate per page             → docling-primary | hybrid | vision-primary
RENDER    page to PNG, divide into sections → regions.json + section crops
ANALYSE   page + sections                  → OCR, palette, fonts, coordinates
GENERATE  full page first                  → initial unified HTML + CSS draft
VERIFY    DOM-first + visual fallback      → score + section_refine_plan.json
REFINE    page-level convergence first     → targeted fixes until target/plateau/cap
REFINE    hotspot sections or diagram QA   → section plan wins; otherwise audit diagrams
RESPONSIVE deterministic DOM checks        → phone/tablet/desktop fluid QA + fixes
OUTPUT    single folder                    → final HTML + CSS + section images + section HTML/CSS
```

**Why this approach:** 
- Docling-first extraction gives better structure on native PDFs.
- OCR baseline keeps scanned/screenshot pages supported.
- Route-per-page avoids over-committing to one parser for mixed documents.
- Page-first synthesis preserves global hierarchy and reading flow early.
- Section refinement is only applied where hotspots remain.
- Score-based refinement with rollback + convergence guards prevents regression.
- Fluid sizing (`clamp(floor, px/W·100vw, px)`) resolves to the exact source px at the verification width and shrinks below it, so pixel fidelity and reflow are not in conflict. See `references/output-contract.md` § "Fluid layout contract".
- Single coherent output folder for clean delivery.

## Runtime environment (headless container)

This skill runs **unattended, one page per container run** (invoked by an
external trigger such as an SQS message driving a Fargate task). There is no
human to answer questions mid-run.

**Working directory.** All commands below use paths **relative to the skill
root** (the directory that holds this `SKILL.md`, `scripts/`, and
`references/`). The container's project root is `/app`; change to
`/app/.claude/skills/pdf-to-html` before running these commands. When the
project root is configured differently, use its `.claude/skills/pdf-to-html`
directory instead. No `CLAUDE_PLUGIN_ROOT` env var is required.

The following environment variables MUST be set by the container entrypoint at
startup:

Required:
- `S3_BUCKET` — bucket for uploaded media assets (Step 11).
- `DDB_TABLE_NAME` — single-table DynamoDB table (PK=`user_id`, SK=`type`) that
  holds the page html/css and the PDF `completed_pages` counter (Step 11).

Optional:
- `S3_PREFIX` — key prefix for uploaded assets (e.g. `renders/`).
- `S3_PUBLIC_BASE_URL` / `ASSET_BASE_URL` — prefix applied to rewritten `<img src>`.
- `AWS_REGION` — standard AWS SDK region resolution.

The DynamoDB attribute/key names are fixed by the app schema (`user_id`, `type`,
`html`, `css`, `status`, `completed_pages`) and are not configurable.

**Preflight (fail loudly, do not ask a human).** At the very start of a run,
verify the CWD is the skill root and the required env vars are present. If any
are missing, write an error status and exit non-zero instead of prompting:

```bash
test -d scripts \
  || { echo '{"ok":false,"error":"run from the skill root: scripts/ not found in CWD"}' >&2; exit 2; }
for v in S3_BUCKET DDB_TABLE_NAME; do
  eval "val=\${$v:-}"
  test -n "$val" || { echo "{\"ok\":false,\"error\":\"missing env $v\"}" >&2; exit 2; }
done
```

## One-time setup

The container image already installs the helper deps at build time. If you run
outside the image and `scripts/node_modules` is missing, install them once:

```bash
( cd scripts && npm ci )
```

Pulls `pdfjs-dist`, `@napi-rs/canvas`, `sharp`, `pixelmatch`, `pngjs`,
`puppeteer`, `@aws-sdk/client-s3`, `@aws-sdk/client-dynamodb`, and
`@aws-sdk/lib-dynamodb`. The image uses its system Chromium via
`PUPPETEER_EXECUTABLE_PATH`; local setups may set that variable to an existing
Chromium executable. Report browser or responsive-check failures explicitly.

**Python + system tools the helpers need:**

- `pdfplumber`, `pypdf`, `numpy`, `Pillow`, `tinycss2` — `pip install pdfplumber pypdf numpy pillow tinycss2`.
- `docling` (recommended primary parser) — `pip install docling`.
- **poppler** (`pdftoppm`, `pdfinfo`, `pdftotext`) — used by `analyze_page.py`
  and `verify_layered.py`. `analyze_page.py` auto-locates poppler via
  `$POPPLER_PATH`, then a conda `Library/bin`, then PATH — so a
  `conda install -c conda-forge poppler` works without touching PATH. Other
  scripts call it by bare name, so put poppler on PATH (or set `$POPPLER_PATH`).
- **tesseract + `pytesseract` (REQUIRED for verification and refinement scoring).**
  `verify_layered.py` uses OCR text as one of its verification layers and keeps
  pixel diff as fallback — so it needs the `tesseract` binary,
  the `pytesseract` wrapper, AND the `ita`+`eng` language data. Install:
  `conda install -c conda-forge tesseract pytesseract`, then point tesseract at
  its data with `export TESSDATA_PREFIX=<conda>/share/tessdata` (the conda
  `tesseract` package ships the `.traineddata` there but does not set the env
  var). Without this, Steps 1–7 still work and the page still ships — only the
  fidelity score in Step 8 fails. Verify with `tesseract --list-langs` (should
  list `eng` and `ita`).

## Inputs

These are provided up front as fixed task parameters by the container
entrypoint — **never ask a human**:
1. Path to the PDF.
2. Which page (1-based).
3. User ID and PDF ID (used for S3 keys and the DynamoDB items).
4. Work directory (scratch space for this run; deleted on exit).

If any required input is missing or invalid, write an error status and exit
non-zero immediately. Do not attempt to prompt for it — there is no interactive
user in this environment.

**Whole-book prerequisite.** `book-style-profiler` must finish and upload the
final catalog and all page assignments before conversion begins. The
orchestrator must serialize conversion pages for each PDF, including across
worker instances; no two pages of the same PDF may run concurrently. Do not
re-profile the book after conversion starts, because that would replace captured
references. The catalog keys are `{pdfId}/designs/catalog.json` and
`{pdfId}/pages/{N}/designs.json`; `S3_PREFIX` applies to published assets, not
these profiler keys.

## Routing policy (Docling + OCR hybrid)

Each page MUST produce `$WORK/routing.json` with:

```json
{
  "route": "docling-primary | hybrid | vision-primary",
  "docling": {
    "textCoverage": 0.0,
    "structuralCoverage": 0.0,
    "bboxCoverage": 0.0,
    "readingOrderConfidence": 0.0
  },
  "ocr": {
    "avgConfidence": 0.0,
    "lineCount": 0
  },
  "hybridScore": 0.0,
  "notes": []
}
```

Use this score:

$$
S = 0.35\cdot T + 0.25\cdot U + 0.20\cdot B + 0.20\cdot R
$$

- $T$: Docling text coverage (0..1)
- $U$: Structural coverage (headings/lists/tables/paragraph blocks found vs expected)
- $B$: Bbox coverage ratio (page area covered by mapped blocks)
- $R$: Reading-order confidence

Route thresholds:
- `docling-primary` when $S \ge 0.80$ and OCR avg confidence is strong (typically $\ge 0.85$) or mismatch risk is low.
- `hybrid` when $0.45 \le S < 0.80$.
- `vision-primary` when $S < 0.45$ or Docling returns sparse/empty structure.

## Workflow — per page

This container run processes **exactly the one page it was told to**. Looping
across pages is handled externally by the orchestrator, so there is no
continue/skip/stop prompt: run the page start to finish, then report status and
end. Optional extraction helpers may fail recoverably, but catalog loading,
ID/geometry validation, and catalog commit failures are not optional: report the
error and exit non-zero. On an unrecoverable failure, write an error status and
exit non-zero.

Keep runtime output concise: prefer JSON-only helper outputs, avoid verbose narration, and print only step status + actionable warnings/errors.

In-run progress log:
- Every major stage writes progress to `$WORK/pipeline_state.json` and may also
  stream stage status to stdout (captured by CloudWatch logs).
- This is an in-run debugging/logging aid only. It is **not** a cross-run resume
  point: the work dir (including `pipeline_state.json`) is deleted on exit
  (Step 11), and each page runs in its own ephemeral container, so there is no
  later session that could resume from it.

Set `WORK=<work-dir>` to the exact scratch directory supplied by the
entrypoint; it is already specific to this page.

```bash
python3 -c "import os; w=os.environ['WORK']; [os.makedirs(p, exist_ok=True) for p in [w, os.path.join(w,'sections'), os.path.join(w,'crops'), os.path.join(w,'assets')]]"
```

### STEP 1 — Fallback positional extraction (compatibility signal)

Keep the positional text extraction as a compatibility fallback:

```bash
node "scripts/extract_text.mjs" \
  --pdf <pdf-path> --page <N> --max-runs 500 \
  > "$WORK/extracted_text.json"
```

### STEP 2 — Render + analyze + route (single script entrypoint)

**Run the main analyzer. It renders the page and now also writes hybrid artefacts.**

```bash
python3 "scripts/analyze_page.py" \
  --pdf <pdf-path> --page <N> --work-dir "$WORK" --normalized-long-edge 1540
```

This command now produces:
- `$WORK/meta.json`
- `$WORK/source.png`
- `$WORK/docling.json`
- `$WORK/ocr_text.json`
- `$WORK/routing.json`

The profiler and converter use the same Poppler DPI, RGB conversion and
1540-pixel-long-edge normalization. Width and height can differ between page
shapes, but the page's bboxes always use its exact normalized image pixels;
never rescale the bboxes.

Required behavior by route:
- `docling-primary`: Use Docling structure as canonical text/order source; use OCR only for gap-fill and confidence checks.
- `hybrid`: Use Docling structure where confidence is good, patch missing/low-confidence spans from OCR, keep section-level visual verification strict.
- `vision-primary`: Treat OCR + rendered page as primary source of truth; use Docling only as optional hinting.

**Load the design catalog for this page.** The `book-style-profiler` skill has
already profiled the whole book and knows which recurring components appear on
this page, with bboxes. Resolve them against the catalog before segmenting:

```bash
python3 "scripts/design_catalog.py" brief \
  --pdf-id "<pdfId>" --page <N> --work-dir "$WORK" \
  > "$WORK/design_brief_report.json" || exit 1
cat "$WORK/design_brief_report.json"
```

The worker also runs this validation before starting the agent; run it again
here after rendering so the brief records this page's image dimensions.
Every S3-backed invocation refreshes `catalog.json` into the skill root
(**not** `$WORK`) and `$WORK/designs.json`; it never trusts an earlier PDF's
local cache. It then writes:
- `$WORK/designs_normalized.json` — all page instances and bboxes, with
  hyphen-only IDs, including non-reusable designs for segmentation and marking.
- `$WORK/design_brief.json` — per design: `designId`, `className`,
  `description`, page `instances` (bboxes), `hasReference`, and the captured
  `html`/`css` only when a reusable reference is available within the prompt
  budget. `nonReusableDesigns` contains metadata only.
- `$WORK/design_harvest_targets.json` — reusable designs on this page that have
  no reference yet, i.e. the ones Step 11 will harvest.

A `designId` present on the page but missing from the catalog is a profiler/
converter mismatch: **fail the run**. Also fail on ID collisions after dot-to-
hyphen normalization, a mismatched PDF ID, or an invalid profiled bbox.

Read `meta.json` and `source.png`. Apply layout analysis from `references/prompts.md` § "Layout analysis".
Supply the normalized profiler bboxes and design descriptions as a second,
authoritative segmentation source alongside the page image and route-specific
text hints. Seed or snap boundaries to avoid splitting a profiled component.
Write to `$WORK/regions.json`, then clean and reconcile it:

```bash
python3 "scripts/postprocess_regions.py" \
  --regions "$WORK/regions.json" \
  --image-width <meta.imageWidth> --image-height <meta.imageHeight> \
  --designs "$WORK/designs_normalized.json" || exit 1
```

The postprocessor rejects out-of-bounds profiler coordinates, expands or seeds
regions when needed, and adds a `designIds` array to each region. Do not clamp
or rescale profiler boxes to hide a geometry mismatch.

Crop each region:

```bash
node "scripts/crop_region.mjs" \
  --image "$WORK/source.png" \
  --regions "$(cat "$WORK/regions.json" | jq -c 'map({id, bbox})')" \
  --out-dir "$WORK/crops"
```

Record all section bboxes to `$WORK/section_coordinates.json` for layout verification later.

```bash
python3 -c "import json, os; w=os.environ['WORK']; r=json.load(open(os.path.join(w,'regions.json'), encoding='utf-8')); json.dump([{'id':x.get('id'),'bbox':x.get('bbox')} for x in r], open(os.path.join(w,'section_coordinates.json'),'w', encoding='utf-8'), ensure_ascii=False, indent=2)"
```

This avoids a `jq` dependency on first run.

Route-aware guidance:
- If `route=docling-primary`, seed region boundaries from Docling block grouping first, then reconcile with visual boundaries.
- If `route=hybrid`, union Docling block hints with visual segmentation and prefer visual boundaries when they disagree.
- If `route=vision-primary`, rely on visual segmentation as canonical and keep Docling hints non-blocking.

### STEP 3 — Per-section analysis

**For each section in reading order, run a section analysis pass to analyze:**
- OCR on the section crop (text lines, blocks, reading order, confidence)
- Colour palette (dominant colours, background hex)
- Font identification (families, sizes, weights)
- Section coordinates and page size (default A4)

Use `$WORK/routing.json` to select text source priority per section:
- `docling-primary`: section text source order = Docling -> extracted_text positional runs -> OCR.
- `hybrid`: section text source order = Docling for high-confidence spans, OCR for missing/low-confidence spans.
- `vision-primary`: section text source order = OCR -> extracted_text positional runs -> Docling hints.

**Prepare a brief with the section crop image. The model should return:**

```json
{
  "sectionId": "section-id",
  "contentType": "paragraph",
  "ocr_text": "full text extracted",
  "ocr_lines": [
    { "text": "line text", "bbox": {...}, "confidence": 0.95, "fontSize": 16, "fontFamily": "..." }
  ],
  "palette": {
    "dominantHex": "#...",
    "accentHex": "#...",
    "backgroundHex": "#...",
    "colorUsage": [...]
  },
  "fonts": [
    { "family": "font-name", "sizes": [16, 12, 14], "weights": [400, 700] }
  ],
  "pageSize": "A4",
  "sectionBbox": { "x": ..., "y": ..., "width": ..., "height": ... }
}
```

Include `contentType` in every section analysis. Use `diagram` for flowcharts,
trees, concept maps, scientific schematics, charts, or other geometry-bearing
figures; otherwise use the appropriate value (`paragraph`, `image`, `table`,
`heading`, `list`, or `other`). Preserve the matching region's `contentType`
when it was explicitly assigned during layout analysis.

**Run analysis inline in the current model/session (no required subagents):**
Pass the brief verbatim. Save returned JSON to `$WORK/sections/<section-id>/analysis.json`.

### STEP 4 — OCR verification against extracted text

**Cross-check section OCR against full-page extracted text.**

Use a deterministic comparison in the current run:
- Pull candidate text for the section based on `route` from `$WORK/docling.json`, `$WORK/ocr_text.json`, and `$WORK/extracted_text.json`.
- Compare normalized text (casefolded, collapsed whitespace) against `analysis.json` OCR text and retain mismatch examples.
- Record match ratio and mismatch examples back into `analysis.json` under `ocrVerification`.

Append verification results to `analysis.json`.
- confidence > 0.90 + text match → "OCR verified"
- confidence < 0.85 or divergence → log discrepancies

### STEP 5 — Page-first generation

Generate a complete page draft first (`$WORK/content.html` + `$WORK/page.css`) using:
- full-page source image
- route-aware text source (`docling/ocr/extracted`)
- section analyses (`$WORK/sections/<section-id>/analysis.json`) as hints, not hard boundaries
- `$WORK/regions.json`, `$WORK/designs_normalized.json`, and the complete
  `$WORK/design_brief.json` as the catalog grounding block

Pass every design's description and page bboxes; include `html` and `css` only
when `hasReference` is true. Non-reusable designs provide identification
metadata only: never use them as references or harvest them. A reference is a
structural prior, not a template: take this page's content, colours, fonts and
spacing from its source image and analysis. **Icon designs** (`isIcon: true`)
are the exception: paste a referenced icon's `html` exactly as stored and
generate only its CSS; draw an unreferenced icon as an inline `<svg>`, never as
label text. Follow the design marker,
CSS-sentinel, component-scope and colour-variable rules in
`references/output-contract.md` and the generation brief in
`references/prompts.md`.

This first pass should optimize for global reading order, heading hierarchy,
table continuity, and figure-caption grouping across the entire page.

Immediately materialize a linked work-page so later steps always consume
`$WORK/content.html` + `$WORK/css/page.css` without manual HTML/CSS linking.
Keep `--out-root` inside `$WORK` so this scratch copy is removed by the Step 11
cleanup along with the rest of the work dir:

```bash
node "scripts/save_page.mjs" \
  --html "$WORK/content.html" \
  --css "$WORK/page.css" \
  --out-root "$WORK/_scratch_out" --run-id "<pdf-filename>" \
  --pdf-for-hash <pdf-path> --page <N> \
  --work-dir "$WORK"
```

After **every** HTML or CSS edit, including a rollback, run this same command
again before rendering or scoring. It refreshes the linked
`$WORK/css/page.css` from `$WORK/page.css` and accepts the full
`$WORK/content.html` produced by an earlier save. Harvest and publish the same
HTML and source CSS that were actually verified.

### STEP 6 — Page-first verification + hotspot extraction

Render and score the unified page draft:

```bash
python3 "scripts/render_chromium.py" \
  --html "$WORK/content.html" \
  --width <meta.imageWidth> --height <meta.imageHeight> \
  --out "$WORK/render.png"

python3 "scripts/verify_layered.py" \
  --source "$WORK/source.png" \
  --render "$WORK/render.png" \
  --html "$WORK/content.html" \
  --pdf <pdf-path> --page <N> \
  --work-dir "$WORK"
```

`verify_layered.py` now writes:
- `$WORK/section_refine_plan.json` containing `recommendedSectionIds`
- `layers.pixel.hotRegions` for visual localization

### STEP 7 — Page-level score convergence first

Use score-based convergence instead of fixed pass count.

Stop when any condition is met:
- target score reached (for example `>= 0.92`)
- no meaningful improvement (`delta < minImprovement`, for example `0.005`)
- patience exhausted (for example 2 consecutive non-improving attempts)
- max pass cap reached (safety cap, for example 6)

Per pass (page level):
1. Re-score current output.
2. Identify weakest layer (`text|layout|colour|pixel`).
3. Apply only targeted edits for that weakest layer.
4. Re-render and re-score.
5. Commit only if score improves and colour floor is preserved; otherwise revert.

Then run layer-aware verification again and continue only if convergence criteria allow another pass.

### STEP 8 — Targeted section refinement, with diagram-only fallback

Choose **exactly one** branch after Step 7:

1. **Section-refinement branch:** If Step 7 stopped below target (without
   regression-only rollback) and `$WORK/section_refine_plan.json` contains at
   least one `recommendedSectionIds`, refine only those sections. Diagram
   sections are handled inside this pass with the rest of the recommended
   sections; do **not** also run a separate diagram-only pass.
2. **Diagram-only branch:** If the page reached the target score, stopped at a
   plateau/pass cap with no recommended sections, or otherwise has no eligible
   section-refinement pass, identify every region marked `contentType:
   "diagram"` (or `role: "diagram"`). If any exist, run one diagram-only
   audit/refinement pass and focus only on those section IDs. This guarantees
   that a high page score cannot hide a poorly reconstructed diagram.
3. **Skip branch:** If no diagram sections exist and section refinement is not
   eligible, continue to Step 9.

Do not use the diagram-only branch after a section-refinement branch. A
diagram-only pass is a bounded visual correctness check, not permission to
rewrite unrelated text or layout.

Section-targeted pass rules:
- Keep untouched sections stable.
- Patch only hotspot sections first (in listed priority order).
- Re-verify after each patched section group.

Use `prepare_refine_brief.py` in page-first mode; it auto-loads
`section_refine_plan.json`, maps hotspot region `designIds` to
`design_brief.json`, and includes any available structural references:

```bash
python3 "scripts/prepare_refine_brief.py" \
  --work-dir "$WORK" --pass <pass> --previous-score <score> \
  --weakest-layer <text|layout|colour|pixel> \
  --include-kind diagram \
  --fragment "$WORK/content.html" --css "$WORK/page.css"
```

For the diagram-only branch, explicitly select diagram regions instead of the
verifier recommendations:

```bash
python3 "scripts/prepare_refine_brief.py" \
  --work-dir "$WORK" --pass <pass> --previous-score <score> \
  --weakest-layer pixel --focus-kind diagram \
  --fragment "$WORK/content.html" --css "$WORK/page.css"
```

In either branch, inspect the source crop and current render crop for every
focused diagram. Confirm geometry, labels, connectors/arrows, and caption
association. Recreate it with semantic HTML/CSS where faithful and stable. If
the diagram is small, visually dense, or not faithfully reconstructable without
distorting its geometry, replace only that diagram's CSS/HTML reconstruction
with a tight source crop `<img>` from `$WORK/crops/<section-id>.png`; keep its
caption and surrounding prose as real HTML text. Apply the media-asset rules in
`references/output-contract.md`: crop only the diagram, add meaningful `alt`,
and preserve responsive scaling/scroll behavior. Commit a candidate only when it
does not regress the page's meaningful checks; a cropped diagram is preferred
to a visibly wrong CSS drawing.

`--include-kind diagram` unions diagram section IDs into the verifier's
recommended section group. Thus diagrams that are not hotspots are still
checked once, as part of the same section-refinement pass, without creating a
separate diagram pass.

For a tagged hotspot with a reference, refine toward its structure instead of
free-styling; preserve its markers and sentinel blocks while matching this
page's image, content and palette. Apply the resulting fragment and CSS to the
working files, refresh the linked work page as in Step 5, and re-verify.

After convergence, section/page is complete. **Never use absolute positioning to patch pixel values.**
Always use fluid units (`clamp()` with `vw`, `rem`, `fr`, grid/flex) for responsive stability. Never raise the score by
swapping fluid values for bare px, adding layout tables, or deleting media queries.

### STEP 9 — DOM-first verification with visual fallback

**Verify the combined page with semantic-first scoring, then visual fallback.**

```bash
python3 "scripts/render_chromium.py" \
  --html "$WORK/content.html" \
  --width <meta.imageWidth> --height <meta.imageHeight> \
  --out "$WORK/render.png"

python3 "scripts/verify_layered.py" \
  --source "$WORK/source.png" \
  --render "$WORK/render.png" \
  --html "$WORK/content.html" \
  --pdf <pdf-path> --page <N> \
  --work-dir "$WORK" > "$WORK/final_verification.json"
```

Interpretation:
- Use `layers.dom` + `score` as primary verification signal.
- If `domConfidence` is low, the scorer automatically shifts weight toward layout/pixel layers.
- Keep `diff.png` and hot regions for targeted visual fixes.

Use `$WORK/section_coordinates.json` to validate section order and approximate placement during review.
Report semantic mismatches first (missing text/structure), then alignment/spacing issues.
For pages that used the diagram-only branch, record which diagram section IDs
were audited and whether each remained HTML/CSS or was replaced by a tight
image crop.
**Do NOT use absolute positioning to patch pixels. Report misalignments for review or use relative units for targeted refinement.**

### STEP 10 — Responsive QA (fluid mode)

Run deterministic responsive checks on the final refined HTML at desktop,
tablet, and phone sizes. The page must re-flow: horizontal page overflow,
clipped content, and crushed text columns are failures at every width. The
only allowed horizontal scroll is inside a shape-preserving `.scroll-x`
block (`data-layout="diagram"` / `"table"`).

```bash
node "scripts/responsive_dom_checks.mjs" \
  --html "$WORK/content.html" \
  --work-dir "$WORK" \
  --mode fluid
```

This writes `$WORK/responsive_dom_report.json` with per-viewport findings.

Responsive correction policy:
- Maximum correction loops at this stage: **3**.
- Stop early when no issues remain or score improvement is `< 0.01`.
- After each loop, re-run Step 9 at the source width; keep the change only if
  the desktop score drops by less than `0.02`. A small desktop-score loss is an
  acceptable price for a page that actually re-flows.
- After the last accepted change or rollback, refresh the linked work page and
  run Step 9 once more. `$WORK/final_verification.json` must describe the
  retained final HTML/CSS, not a rejected candidate or an earlier pass.

How to fix each finding:
- `overflowOffenders` / `clippedHorizontally` / `globalHorizontalScroll` →
  replace the fixed px width/padding/min-width with a fluid value, or wrap a
  genuinely shape-preserving block in `.scroll-x`.
- `crampedTextColumns` → a multi-column layout did not stack; switch it to
  grid and collapse to `1fr` at `≤ 768px` (or remove a layout table).
- `flowchartColumnCollapse` / `flowchartAspectDrift` → a diagram was
  flattened; restore its columns/branches and put it in `.scroll-x` instead.
- `tinyTextNodes` → raise the `clamp()` floor.

Rules for this stage:
- Prefer fixes inside tablet/phone media queries, but converting a base desktop
  rule from bare px to the equivalent fluid `clamp()` is allowed (it renders
  identically at the source width).
- For flowchart/tree/concept-map sections (for example 1 heading -> 3
  subheadings -> 2 topics each), preserve the desktop shape at every width:
  do NOT collapse the branches into a single column. Keep the geometry and let
  the block scroll inside `.scroll-x`.
- Everything that is NOT a shape-preserving block must fit the viewport.

### STEP 11 — Publish to S3 + DynamoDB, then clean up

After the page passes final verification, publish the durable outputs and delete
everything else. Do **not** write a local output-folder tree — this container is
ephemeral and the only consumers are S3 (image assets) and DynamoDB (HTML/CSS).

**Harvest and commit before publication deletes `$WORK`.** Use the final
verification score, not a previous pass. The default page-level floor is 0.85;
there is no per-component hotspot gate. The harvester writes an empty result
and explicit skip reasons below the floor; commit independently enforces the
same floor and never overwrites an existing reference.

```bash
if test -s "$WORK/final_verification.json"; then
  python3 "scripts/harvest_designs.py" \
    --html "$WORK/content.html" --css "$WORK/page.css" --work-dir "$WORK" \
    --score-file "$WORK/final_verification.json" --page <N> --min-score 0.85 \
    --page-width <meta.imageWidth> --page-height <meta.imageHeight> \
    > "$WORK/design_harvest_report.json" || exit 1
  python3 "scripts/design_catalog.py" commit \
    --pdf-id "<pdfId>" --page <N> --harvested "$WORK/harvested_designs.json" \
    --min-score 0.85 --page-width <meta.imageWidth> --page-height <meta.imageHeight> \
    > "$WORK/design_commit_report.json" || exit 1
  cat "$WORK/design_brief_report.json" "$WORK/design_harvest_report.json" "$WORK/design_commit_report.json"
else
  echo '{"warning":"final verification unavailable; design harvest and commit skipped"}' >&2
  cat "$WORK/design_brief_report.json"
fi
```

Never harvest with a missing, invalid, stale, or wrong-page score. If final
verification is unavailable, skip harvesting and record the reason; any
decision to publish the page itself follows the existing verification policy.
Keep the referenced, harvested, committed and skipped summaries in the run
context or stdout before cleanup so Step 12 can report them.

`publish_page.mjs`:
- uploads only the true media assets referenced by `<img>` tags in the final
  `content.html` (raster files that resolve to a real file inside `$WORK`, e.g.
  `assets/` or `crops/`) to S3 under key
  `{S3_PREFIX?}{pdfId}/pages/{page}/assets/files/{filename}`, and rewrites each
  uploaded `<img src>` to that key (optionally prefixed with
  `S3_PUBLIC_BASE_URL`/`ASSET_BASE_URL`);
- writes the final HTML + CSS into the single-table DynamoDB layout
  (PK=`user_id`, SK=`type`) in one transaction: the **page item**
  (`type=PDFDATA#{pdfId}#{page}`) gets `html`, `css`, `status=successful`, and
  the **PDF item** (`type=PDFDATA#{pdfId}`) has `completed_pages` incremented by
  1. The counter only moves the first time a page becomes `successful`, so
  re-runs / SQS redelivery never double-count;
- deletes the entire work dir (and any `--extra-cleanup` dirs) so nothing except
  the S3 assets and the DynamoDB items persists.

The bucket and table come from the environment (see "Runtime environment"); the
DynamoDB key/attribute names are fixed by the schema. The script fails loudly
(non-zero exit + JSON error) if a required env var is missing.

```bash
node "scripts/publish_page.mjs" \
  --html "$WORK/content.html" \
  --css "$WORK/page.css" \
  --user-id "<userId>" \
  --pdf-id "<pdfId>" \
  --page <N> \
  --work-dir "$WORK"
```

**Durable outputs after this step (nothing else persists):**

- **S3** — `{S3_PREFIX?}{pdfId}/pages/{page}/assets/files/{asset-filename}` for each
  media asset referenced by an `<img>` in the final page, plus the updated
  `{pdfId}/designs/catalog.json` when a reference was committed.
- **DynamoDB** — the page item (`user_id` + `PDFDATA#{pdfId}#{page}`) holding the
  final `html`/`css`/`status`, plus a bumped `completed_pages` on the PDF item.

Everything written during the run — `sections/`, `crops/`, per-section
`analysis.json` files, intermediate work-dir files, `pipeline_state.json`,
`routing.json`, `render.png`, `diff.png`, etc. — is deleted by this step. If the
orchestrator passed additional scratch directories (e.g. a separate out-root),
pass them via `--extra-cleanup <dir>` so they are removed too.

### STEP 12 — Report

Emit a final status to stdout (captured by CloudWatch logs) and end the run — do
**not** wait for any further input:
- Publish result: S3 keys of uploaded assets and the DynamoDB items touched
  (page item + PDF `completed_pages`).
- Summary: N sections processed, M flaws detected, final verification status.
- Design IDs referenced, harvested and committed; include budget, score, CSS,
  marker and other skip reasons from the brief/harvest/commit reports.
- Any warnings (OCR discrepancies, layout misalignments, font substitutions).
- Confirmation that the work dir was deleted (cleanup result from Step 11).

Exit zero on success, non-zero on unrecoverable failure. This container handles
exactly one page; the orchestrator is responsible for any remaining pages.
