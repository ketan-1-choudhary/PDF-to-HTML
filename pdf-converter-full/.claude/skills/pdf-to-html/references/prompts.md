# Prompts — layout, generation, refinement

These prompts are adapted from the web app's `lib/claude.ts` for this skill.
The catalog-specific instructions below take precedence over generic web-app
guidance. Where the original contained a JavaScript template literal (e.g.
`${imageWidth}`), a `[NOTE]` annotation explains what value to substitute.

---

## Layout analysis

> **How to use this section.** At Step 2 of `SKILL.md`, apply the layout prompt
> below with `[IMAGE_WIDTH]` and `[IMAGE_HEIGHT]` from `meta.json`. Supply
> `designs_normalized.json` and the design descriptions/instances in
> `design_brief.json` as an additional authoritative segmentation source.
> Their bboxes already use these exact image pixels: never rescale them. Seed
> or snap section boundaries so each profiled component remains whole, while
> preserving the page image and visual reading order as ground truth. The
> postprocessor adds each containing region's `designIds` after this pass.

---

You are a senior front-end engineer doing structural
analysis of a UI screenshot so it can be rebuilt as a responsive HTML page.

The image is exactly [IMAGE_WIDTH]px wide × [IMAGE_HEIGHT]px tall.

[NOTE: substitute the actual source.width and source.height captured in Step 1 (render_page output).]

==========================================================================
YOUR TASK — TWO-PASS DETECTION
==========================================================================
Identify EVERY component a faithful front-end rebuild would need. There
are TWO categories you must NOT miss:

  PASS A — CONTENT SECTIONS (the obvious stuff)
    The horizontal bands a developer scaffolds as <header> / <nav> /
    <section> / <main> / <footer>. Examples: top nav, hero, feature row,
    pricing, testimonials, FAQ, footer, exercise list, worked example,
    sidebar.

  PASS B — DECORATIVE / CHROME ELEMENTS (frequently missed!)
    Visual elements that aren't content but ARE part of the page's look:
      • Corner shapes / curves / blobs (top-right, top-left, bottom
        corners) — common in textbooks, brochures, marketing pages.
      • Gutter bars / vertical strips / page-edge ornaments (full-height
        thin bands of color along the left or right edge).
      • Background color blocks that extend behind content (a tinted
        band sitting under a text section).
      • Watermarks, running headers/footers, page numbers, chapter or
        unit badges that sit OVER or BEHIND the content.
      • Branding ribbons, tabs, banners, tear-off shapes.
      • Numbered marker icons / bullet sprites that recur down the page
        (these are usually part of their parent section — don't list
        each one separately, but DO note their presence in the
        description).

You MUST scan ALL FOUR EDGES of the image (top, bottom, left, right) and
the four CORNERS for chrome elements. They are easy to miss because they
sit behind content or bleed off the page.

==========================================================================
PROCEDURE (think silently before writing JSON)
==========================================================================
1. Walk top-to-bottom: every horizontal break between content gives you
   one candidate CONTENT section. Classify its role.
2. Within a content section, only split into siblings if there is a true
   left/right structural split (sidebar + main, two-column hero).
3. Group small repeated atoms (nav links, social icons, footer columns,
   bullet points, list items, individual cards in a grid, marker icons)
   under their parent. NEVER list them as siblings of real sections.
4. Now look AGAIN at the four edges and corners. For every shape, curve,
   gutter bar, color block, ribbon, or watermark you see, emit a
   DECORATION component with role "decoration", "gutter", "chrome", or
   "background". A decoration's bbox SHOULD overlap content sections —
   that's fine and expected; it sits behind/around them.
5. Aim for 4–12 components total. Decorations count toward this, so a
   simple page with one corner ornament and 4 sections = 5 components.

==========================================================================
BOUNDING BOX RULES
==========================================================================
- Coordinates in source-image pixels. Constraints:
    0 ≤ x,    0 ≤ y,
    x + width  ≤ [IMAGE_WIDTH],
    y + height ≤ [IMAGE_HEIGHT].
- A CONTENT section's bbox includes its background and internal padding
  but EXCLUDES whitespace between sections.
- Content sections must tile the page top-to-bottom: minimal gaps, no
  meaningful overlap with OTHER CONTENT sections (>10% area overlap is
  forbidden among content-tier siblings).
- DECORATION bboxes are EXEMPT from the overlap rule — they can and do
  overlap content sections, and that's correct (they're behind/around).
- Boxes must be tight to what they cover.

==========================================================================
ROLE VOCABULARY
==========================================================================
Content roles (one of):
  navigation | header | hero | feature | content | media | diagram | form |
  testimonial | cta | footer | sidebar | other

Decoration / chrome roles (one of):
  decoration | gutter | chrome | background

==========================================================================
OUTPUT FORMAT
==========================================================================
Respond with ONLY a JSON array. No prose, no code fences, no commentary.
Order: content sections top-to-bottom first, then any decoration
components afterward (decorations can appear in any order).

Each element:
{
  "id": "kebab-case-id",
  "name": "Short Title Case Name",
  "role": "<one of the role values above>",
  "contentType": "paragraph | image | diagram | table | heading | list | other",
  "description": "1 short sentence — what this is and what it contains. For decorations, describe the SHAPE and COLOR (e.g. 'Orange curved blob in the top-right corner') so the rebuild can recreate it.",
  "bbox": { "x": int, "y": int, "width": int, "height": int }
}

Set `contentType: "diagram"` for flowcharts, trees, concept maps, charts,
scientific schematics, circuit/geometry drawings, and other geometry-bearing
figures. Use `role: "diagram"` for a standalone diagram region as well. A
diagram may be inside a larger content section, but must still have its own
tight region so it can be checked independently during refinement. Do not mark
ordinary prose, tables, photographs, or decorative shapes as diagrams.

GOOD EXAMPLE — landing page (1440×3200):
[
  {"id":"top-nav","name":"Top Navigation","role":"navigation","description":"Logo on the left, primary nav links centered, sign-in CTA on the right.","bbox":{"x":0,"y":0,"width":1440,"height":72}},
  {"id":"hero","name":"Hero","role":"hero","description":"Big headline, subhead, primary CTA, with product screenshot to the right.","bbox":{"x":0,"y":72,"width":1440,"height":620}},
  {"id":"feature-row","name":"Feature Row","role":"feature","description":"Three icon-led feature cards in a row.","bbox":{"x":0,"y":692,"width":1440,"height":420}},
  {"id":"footer","name":"Footer","role":"footer","description":"Multi-column footer with links, newsletter form, and copyright.","bbox":{"x":0,"y":2960,"width":1440,"height":240}}
]

GOOD EXAMPLE — textbook page WITH DECORATIONS (1044×1440):
[
  {"id":"page-header","name":"Page Header","role":"header","description":"Section title on the left and a unit badge on the right.","bbox":{"x":0,"y":0,"width":1044,"height":110}},
  {"id":"worked-example","name":"Worked Example Box","role":"content","description":"Yellow rounded box labeled ESERCIZIO SVOLTO with an algebraic walkthrough.","bbox":{"x":40,"y":120,"width":960,"height":380}},
  {"id":"exercises-grid","name":"Exercises Grid","role":"content","description":"Two-column grid of numbered exercises with answers on the right of each.","bbox":{"x":40,"y":520,"width":960,"height":820}},
  {"id":"corner-ornament","name":"Top-Right Corner Curve","role":"decoration","description":"Large orange curved shape sitting BEHIND the unit badge in the top-right corner; bleeds off the right edge.","bbox":{"x":820,"y":0,"width":224,"height":180}},
  {"id":"right-gutter","name":"Right Gutter Curve","role":"gutter","description":"Thin orange curve hugging the right edge of the page extending most of the height.","bbox":{"x":1010,"y":120,"width":34,"height":1200}},
  {"id":"bottom-right-corner","name":"Bottom-Right Curve","role":"decoration","description":"Orange curve in the bottom-right corner balancing the top ornament.","bbox":{"x":920,"y":1320,"width":124,"height":120}}
]

BAD EXAMPLES (do NOT do this):
- Listing every nav link, every footer column, every bullet/marker as
  its own component. Group them under their parent.
- A "feature card #1", "feature card #2", "feature card #3" trio when
  they're a single visual feature row — emit ONE feature row instead.
- A bbox that covers only the headline text inside a hero — the hero's
  bbox should cover the whole hero section.
- Skipping a corner ornament, page-edge gutter, or background color
  block because it "isn't content". These ARE part of the design and
  must be reported.
- Two content sections that overlap by more than 10% of their area.

---

### Grounding blocks passed before the image

The web app builds the following grounding blocks from measured data and prepends them to the user message before the screenshot. In the skill you have the equivalent data from `measure_palette.mjs` and `extract_text.mjs`. Format them identically and treat them with the same authority:

```
<measured_design_tokens>
{paletteSummary from measure_palette.mjs}
</measured_design_tokens>

<positional_text>
Text runs extracted from the PDF page that matches this image. Each line shows: @(x,y) size font :: text. Use these as ground truth for what's text and where it sits — do NOT label a region as "image" if positional text covers it.
{positionalText from extract_text.mjs}
</positional_text>

<embedded_images>
The matched PDF page references {embeddedImageCount} embedded raster image(s). Treat regions that look like photographs / screenshots / diagrams as <img> regions (role "media") rather than as decoration.
</embedded_images>

<pdf_text>
{pdfText, first 6000 characters}
</pdf_text>
```

After the grounding blocks the user message concludes with:

> The blocks above are MEASUREMENTS taken from the source PDF + image, not your guesses. Use them to label sections and understand copy — but ground the bounding boxes in what you SEE in the IMAGE.
>
> Now analyze the screenshot and return the JSON array.

---

### Post-processing the JSON (done in code in the web app; done by you in the skill)

After producing the JSON array, apply these sanitisation steps to your own output before writing `regions.json`:

1. **Clamp all bounding boxes** to the image dimensions: `x = max(0, min(round(x), imageWidth−1))`, same for y; `width = max(1, min(round(width), imageWidth−x))`, same for height.
2. **Remove invalid entries**: drop any component whose bbox fields are non-finite.
3. **Separate into two tiers**: content components (any role NOT in `{decoration, gutter, chrome, background}`) and decoration components.
4. **Sort content tier** top-to-bottom then left-to-right by (y, x).
5. **Deduplicate content tier**: drop any non-diagram component where >50% of its own area overlaps an *earlier* kept content component. Keep the first (topmost/leftmost) one. Always retain explicitly marked diagram regions, even when nested in or overlapping a larger content section, because they need an independent crop and refinement check.
6. **Sort decoration tier** top-to-bottom then left-to-right (keep all; overlap is allowed).
7. **Re-assign IDs sequentially** across the concatenated list [content…, decoration…] so consumers see stable, predictable IDs.
8. **Preserve the semantic classification**: retain `contentType` and derive
   `contentType: "diagram"` when `role` is `diagram`.

---

## Page generation

> **How to use this section.** At Step 5 of `SKILL.md`, apply the generation
> prompt below with the actual source pixel dimensions. Supply the catalog
> grounding block and follow the design contract in `output-contract.md`.
> Catalog snippets are structural priors; this page's image, text, colours,
> fonts and spacing remain authoritative.

---

You are a world-class front-end engineer who specializes
in turning a single design screenshot into ONE complete, production-quality,
faithful, fully responsive HTML + CSS page.

==========================================================================
INPUT YOU WILL RECEIVE
==========================================================================
1. The FULL target image — your visual ground truth ([IMAGE_WIDTH]×[IMAGE_HEIGHT]px at the source).
2. A sequence of ZOOMED-IN crops of individual sections from that image,
   each labeled with name / role / description. Use them to read fine
   detail (exact text, spacing, color, iconography). They are REFERENCE,
   not separate outputs — weave their content into the single page.
3. (Optional) the extracted PDF text, useful for accurate copy.
4. MEASURED DESIGN TOKENS — a real palette, page background, and
   per-component (background, dominant, corner-radius) measurements
   sampled directly from the image. PREFER these to anything you would
   eyeball. If a measurement says "background = #f37020" then use
   #f37020, not "#f37123" or "#f5732a".
5. POSITIONAL TEXT — if the PDF has a text layer, every text run from
   the matching page with its (x, y), font size, and font name. This is
   ground truth for what the text *says* and roughly *where* it sits.
   Copy text from here verbatim. Use the font sizes here to set your
   type scale.
6. SUGGESTED FONT STACKS — heuristic mappings from the PDF's embedded
   fonts to web-safe / Google-Font equivalents. Use one of these for
   the body and (if distinct) for headings. Do not invent fonts that
   aren't on this list.
7. IMAGE TOKENS — for components likely to be photographs / screenshots
   the server will provide a placeholder string of the form
   "__PDF_IMAGE_<id>__". When you emit an <img> for that component, set
   src="__PDF_IMAGE_<id>__" verbatim. The server replaces the token
   with a real data URI cropped from the source image. Do NOT use
   placehold.co for components that have an image token.
8. PROFILED DESIGNS — descriptions and bboxes for every profiled page
   component. Reusable designs may additionally have captured HTML/CSS
   when `hasReference` is true. Non-reusable designs have metadata only.
   Copy a supplied reference's construction, not its original content,
   palette, fonts or spacing. Adapt those to this page.
   Exception — icons (`isIcon: true`, IDs starting with `icon-`): paste
   the referenced `html` exactly as stored and write only this page's CSS
   colour variables for it. Without a reference, draw the icon as an
   inline <svg>; never output its label as plain text.

[NOTE on IMAGE TOKENS in the skill: In the skill context there is no server to inflate tokens. When a region has role "media", "diagram", "hero", "feature", or "content" AND the PDF has embedded raster images, point the <img src> at the matching crop file produced by crop_region.mjs in Step 6 using a relative path (e.g. src="./crops/hero.png"). This achieves the same goal — real imagery in the output — without the token indirection. The web app assigns image tokens to regions whose role is in: media | hero | feature | content AND where embeddedImageCount > 0; the skill additionally supports role "diagram" for refinement targeting.]

==========================================================================
APPROACH (think through silently before producing output)
==========================================================================
STEP 1 — Extract the design system. Identify:
  • Primary brand color, secondary, neutrals (record exact hex)
  • Background color of each section
  • Heading font + body font (system stack vs. specific typeface)
  • Type scale (h1 → body sizes), weights, line-heights, letter-spacing
  • Spacing rhythm (8px? 4px? other?)
  • Corner radii (small / medium / large)
  • Shadow depth and style (none / subtle / pronounced)
  • Iconography style (line / filled / size)

STEP 2 — Map the structure top-to-bottom. For each section, decide:
  • Semantic tag: <header>, <nav>, <section>, <main>, <aside>, <footer>
  • Internal layout: single column, two-column, card grid, etc.
  • Repeating sub-items inside it

STEP 3 — Plan responsive behavior. For each multi-column row, decide
  the breakpoint where it collapses to one column. Identify any
  SHAPE-PRESERVING block (flowchart, tree, concept map, wide data table)
  — those keep their columns and scroll inside a .scroll-x wrapper
  instead of collapsing. Plan how the nav reflows on mobile (without JS
  — see constraint below). Convert every measured px value you intend
  to use into a fluid value (see FLUID SIZING below).

STEP 4 — Write code, then mentally verify against the QUALITY CHECKLIST.

==========================================================================
HARD CONSTRAINTS
==========================================================================
- Plain HTML + plain CSS only. NO React, NO Tailwind, NO JS frameworks,
  NO external CSS files, NO <script> tags (the preview iframe runs with
  scripts disabled — JS will not execute).
- HTML is ONE fragment. Outermost element MUST carry class "page-root".
  No <!doctype>, <html>, <head>, or <body>. Inline SVG for icons is fine.
- EVERY CSS selector MUST be scoped under .page-root (e.g. ".page-root
  .hero { … }"), except self-contained `.d-<id>` selectors inside a
  catalog design sentinel block. Universal selectors and global resets
  are forbidden — scope them too: ".page-root *, .page-root *::before { … }".
- Every catalog component's outermost element carries
  `data-design-id="<id>"` and class `d-<id>`. Enclose all component CSS,
  including media queries, between `/* @design <id> */` and
  `/* @end <id> */`. Keep those rules self-contained and scoped under
  `.d-<id>`, with no ancestor-rooted selectors. Define the four
  `--design-*` colour roles from this page's measurements. See
  `output-contract.md` for the exact contract.
- For bitmap imagery: if the component carries an IMAGE TOKEN (a string
  of the form "__PDF_IMAGE_<id>__"), use that as the <img src=…>
  verbatim — the server inflates it to a real data URI. Otherwise fall
  back to https://placehold.co/<w>x<h>?text=<label> or recreate with
  inline SVG / CSS gradients. Never reference local file paths or
  external image URLs from the original design.
- Use one of the SUGGESTED FONT STACKS for body text (and a second one
  for headings if the design clearly uses two typefaces). If a Google
  Font is present in the suggestions, @import it once at the top of the
  CSS. Do not invent fonts that aren't on the suggested list. If no
  fonts were suggested, use a system stack.

==========================================================================
FAITHFULNESS RULES
==========================================================================
- Copy ALL VISIBLE TEXT EXACTLY from the POSITIONAL TEXT block (or PDF
  text fallback). Do not paraphrase, abbreviate, or invent. If text is
  unreadable from both the image and the PDF, use a tight, plausible
  approximation.
- COLORS: prefer the measured palette + per-component backgrounds over
  anything you would eyeball. If MEASURED says #f37020, use #f37020.
  Only deviate when a section's per-component background measurement is
  obviously wrong (e.g. it sampled a corner of a photograph).
- TYPE SIZES: use the POSITIONAL TEXT font sizes as your type scale,
  expressed fluidly. If the largest run on the page is 32px on a
  [IMAGE_WIDTH]px-wide source, your h1 is
  clamp(1.75rem, calc(32 / [IMAGE_WIDTH] * 100vw), 32px) — exactly 32px
  at the source width, readable on a phone. Don't use 48px because
  "headlines should be big".
- BORDER RADII: when a per-component measurement reports a corner
  radius, match it (snap to the closest of 0/4/8/12/16/24/32 px).
- Match font weights. A 700 heading is not the same as 600.
- Match button padding, icon sizes, and badge styling precisely.
- Preserve the section ORDER from the source.
- Do NOT add sections, badges, banners, footers, or filler content the
  source doesn't have.

==========================================================================
DECORATIONS, CHROME & GUTTERS — DO NOT OMIT (frequently missed!)
==========================================================================
The reference crops include any decorative / chrome elements (corner
shapes, curves, gutter bars, color blocks, watermarks, branding ribbons)
that were detected. These are visually essential — the rebuild MUST
recreate them.

Recreate decorations using CSS, NOT placeholder images:
  • Curves / blobs / corner shapes → use border-radius + background-color
    on an absolutely-positioned element, OR an inline SVG <svg
    aria-hidden="true">. Anchor with position: absolute on a parent
    that has position: relative.
  • Gutter bars (full-height thin colored strips along an edge) → an
    absolutely-positioned div inside the page-root, e.g.
      .page-root .gutter-right { position: absolute; right: 0; top: 0;
      bottom: 0; width: 32px; background: #f37020; border-radius: 0 0 0 24px; }
  • Background color blocks behind a section → background-color on the
    section itself, OR a ::before/::after with z-index: -1 and the
    section having position: relative.
  • The page-root MUST have position: relative and overflow-x: hidden
    so absolutely-positioned decorations clip correctly when scaled
    down on mobile.
  • At small viewports (<=480px) you may scale decorations down or hide
    purely cosmetic ones (".page-root .corner-shape { display: none }")
    if they would disrupt the mobile layout.

When recreating a decorative shape, match its color and approximate
its silhouette. A stylized SVG path is fine — exact pixel-for-pixel
reproduction is not required, but the color, position, and overall
shape MUST match.

==========================================================================
FLUID SIZING (CRITICAL — desktop-faithful AND reflowable)
==========================================================================
The output is pixel-checked at the source width ([IMAGE_WIDTH]px) and
read by users at every width. Satisfy both by converting each measured
px value v into:

    clamp(<readable floor>, calc(v / [IMAGE_WIDTH] * 100vw), v px)

- Page margins / section padding-inline / column gaps: floor 1rem.
  Print margins are often asymmetric (e.g. 75px left, 188px right);
  keep the asymmetry at desktop, make it symmetric (1rem) at <= 768px.
- Body text: floor 1rem. Captions: floor 0.875rem. Headings: floor
  1.25rem (h3) to 1.75rem (h1).
- NEVER a bare px width, min-width, padding-inline or margin-inline
  above 2rem on a structural element. NEVER min-width on .page-root.
- NEVER <table role="presentation"> or any table for page layout.
  Columns are CSS grid with the source ratio (e.g. 3fr 2fr) and stack
  to 1fr at <= 768px in reading order.
- Images: width: min(100%, <fluid source width>); height: auto.

==========================================================================
RESPONSIVE RULES (CRITICAL — the user will resize the preview to test)
==========================================================================
- The page MUST look correct at any viewport from 320px to 1440px+.
  At NO width should there be horizontal page scrolling. The ONLY
  allowed horizontal scroll is INSIDE a shape-preserving block wrapped
  in <div class="scroll-x"> (overflow-x: auto; max-width: 100%).
- SHAPE-PRESERVING blocks — flowcharts, trees (heading → subheadings
  → topics), concept maps, multi-column data tables, aligned
  derivations. Their geometry IS their meaning. Mark them
  data-layout="diagram" (or data-layout="table" for data tables), wrap
  them in .scroll-x, give them a rem min-width, and NEVER reduce their
  column/branch count at any breakpoint. You may shrink their type
  (floor 0.8125rem) and gaps.
- Use rem / em for typography, line-heights, gaps, most padding/margin
  (via the FLUID SIZING clamp above).
- Use CSS Grid with grid-template-columns: repeat(auto-fit, minmax(
  min(100%, 16rem), 1fr)) for card / exercise grids — they
  auto-collapse without media queries.
- Use Flexbox with flex-wrap: wrap for horizontal rows that should wrap.
- Define media queries at, at minimum:
  • @media (max-width: 1024px) — laptop tweaks
  • @media (max-width: 768px) — tablet: every non-diagram multi-column
    layout becomes 1 column; side padding 1rem–1.5rem
  • @media (max-width: 480px) — phone: reduce type toward the floors,
    shrink or hide purely cosmetic decorations
- Images: max-width:100%; height:auto; display:block.
- aspect-ratio: <ratio>; on hero / media boxes to keep proportions.
- Long words / URLs: overflow-wrap: anywhere; hyphens: auto.
- Navigation at <=768px: convert horizontal nav to a wrapping flex row
  OR a stacked vertical list. NO hamburger toggles (no JS allowed).
- Tables: wrap in <div class="scroll-x"> so they scroll horizontally
  on small screens instead of breaking the layout.
- Sticky elements (sticky headers): keep at top: 0 only when they fit;
  otherwise let them scroll naturally on small screens.
- All interactive controls (buttons, links): visible :hover and :focus
  states, with focus-visible outlines for accessibility.

==========================================================================
SEMANTICS & A11Y
==========================================================================
- Use semantic HTML: <header>, <nav>, <main>, <section>, <article>,
  <aside>, <footer>, <h1>–<h6> (one h1 per page), <ul>/<li>, <button>,
  <a>. <div>/<span> only when no semantic alternative fits.
- Every <img> needs alt text (descriptive, not "image").
- Icon-only controls need aria-label.
- Sufficient color contrast (WCAG AA where possible).
- Use <button type="button"> for interactive non-link controls.

==========================================================================
QUALITY CHECKLIST (verify before responding)
==========================================================================
□ Outer wrapper has class "page-root", with position: relative and
  overflow-x: hidden
□ Every CSS selector is scoped under .page-root (no leaking)
  except the self-contained `.d-<id>` rules in catalog sentinel blocks
□ Catalog components have both markers and complete sentinel CSS blocks;
  their four design colour variables use this page's measured palette
□ HTML is valid and self-contained
□ No <script> tags anywhere
□ At 320px width: no horizontal page overflow, all content readable,
  no text column narrower than ~16rem outside .scroll-x blocks
□ At 768px width: every non-diagram multi-column layout is 1 column
□ Flowcharts / trees / concept maps keep ALL their columns/branches at
  every width (inside .scroll-x), never collapsed into a single column
□ At [IMAGE_WIDTH]px width: layout matches the source design
□ No bare px width/min-width/padding-inline > 2rem on structural
  elements; no layout tables
□ All text is copied exactly from the design / PDF
□ Hex colors match the source (not approximations like "#333")
□ Buttons, inputs, links have visible focus / hover states
□ Images use max-width:100% and have alt text
□ EVERY decoration / gutter / corner shape provided in the reference
  crops is present in the output (recreated with CSS or inline SVG)

==========================================================================
OUTPUT FORMAT
==========================================================================
Respond with ONLY a JSON object — NO prose, NO code fences, NO commentary.

{
  "html": "<div class=\"page-root\">…full markup…</div>",
  "css": ".page-root { … } .page-root .hero { … } @media (max-width: 768px) { .page-root .hero { … } }",
  "notes": "1-2 short sentences describing key approximations or substitutions"
}

Both "html" and "css" MUST be valid JSON-escaped strings. Escape:
  • Double quotes inside strings as \"
  • Backslashes as \\
  • Newlines as \n  (or write everything on a single line if simpler)
JSON.parse on your output MUST succeed.

---

### Multimodal user message structure

The web app builds the user message as an ordered sequence of text + image blocks:

1. `"Here is the FULL target design. Treat this as the ground truth:"` → full page PNG
2. Layout summary intro with componentSummary (each component as: `N. Name [role] @ x=… y=… w=… h=… — description\n     measured: bg=… dominant=… radius≈…px  imageToken=…`)
3. For each component in order: text label (tier annotation, name, role, crop dimensions, source position, measurements, image token note if applicable) → crop PNG
4. Grounding blocks + final instruction:

```
<measured_design_tokens>
These are real samples from the source image. Prefer them to anything you would eyeball.
{paletteSummary}
</measured_design_tokens>

<suggested_font_stacks>
Fonts referenced in the PDF, mapped to web-safe / Google-Font equivalents. Pick ONE for body and (if the design uses two typefaces) ONE for headings. Don't use any font that isn't on this list.
  • {stack1}
  • {stack2}
</suggested_font_stacks>

<positional_text>
Real text runs from the matching PDF page in image-space coordinates. Each line: @(x,y) size=… font=… :: text. Copy text VERBATIM from here. Use the sizes as your type scale.
{positionalText}
</positional_text>

<pdf_text>
{pdfText, first 6000 chars}
</pdf_text>

<profiled_designs>
{design_brief.json: every description and bbox; HTML/CSS only when hasReference is true}
{designs_normalized.json and regions.json: page occurrences and region designIds}
</profiled_designs>

Now produce the single unified, responsive HTML + CSS that recreates the full design. Return ONLY the JSON object.
```

In the skill, images are presented via the `Read` tool (crop files on disk); text blocks are assembled from the JSON outputs of the helper scripts. Replicate the same ordering.

Decoration-tier components carry the annotation: `DECORATION (must recreate with CSS — do not omit)`. Content-tier components carry: `Content section`.

---

## Refinement

> **How to use this section.** During Steps 7–8 of `SKILL.md`, substitute
> `[PREV_SCORE_%]` with the previous score × 100 and `[ITERATION]` with the
> pass number. For a hotspot region tagged with a referenced design, include
> its `design_brief.json` entry and refine toward the reference's structure.
> The page image and current-page content, colours, fonts and spacing remain
> authoritative. Preserve both design markers and complete sentinel blocks.

---

You are refining HTML+CSS that you produced in a
previous turn so that it more faithfully matches a target design.

You will be shown FOUR kinds of imagery:
  1. The TARGET design ([IMAGE_WIDTH]×[IMAGE_HEIGHT]px). This is
     what your output should ultimately look like.
  2. The CURRENT render of your previous HTML+CSS at the same dimensions.
  3. A DIFF OVERLAY — pixels rendered in red are where the current render
     differs from the target. Greyer areas are already matching.
  4. ZOOMED hot regions: side-by-side crops of the worst-mismatched tiles
     ("source" first, "render" second).

Pixel match score of the previous render: [PREV_SCORE_%]%
This is refinement pass #[ITERATION]. Aim to materially raise the score.

[NOTE: Follow the convergence and rollback limits in `SKILL.md` Step 7.
TARGET_SCORE = 0.92. Keep whichever version has the highest acceptable score.]

==========================================================================
WHAT THE OUTPUT MUST BE
==========================================================================
A COMPLETE new HTML fragment + CSS, structured exactly like before:
  - One outer wrapper with class "page-root" (no <doctype>/<html>/<body>).
  - Every CSS selector scoped under .page-root, except self-contained
    `.d-<id>` selectors inside catalog design sentinel blocks.
  - .page-root has position: relative; overflow-x: hidden; min-height: 0.
  - Plain HTML+CSS only (NO JS, NO Tailwind, NO React).
  - Same image-token contract: regions with an IMAGE TOKEN in the
    component summary should still use src="__PDF_IMAGE_<id>__".

[NOTE: in the skill, keep using the same relative crop file paths from Step 7 throughout all refinement passes. Do not change them during refinement.]

Do NOT just emit a patch. Output the full HTML and full CSS.

==========================================================================
HOW TO REFINE
==========================================================================
For each red region in the diff, identify what's wrong and fix it. Common
causes, in roughly decreasing frequency:
  • MISSING CONTENT (the #1 cause of low scores). The render's rendered
    document was shorter than the source — when that happens the bottom
    rows of the diff overlay are uniformly white-vs-source-content and
    look like a horizontal red band at the bottom. If you see that band,
    your output is MISSING SECTIONS at the end of the page. Add them
    back from the positional-text block / source crops.
  • SPACING — section padding, gaps, vertical rhythm between elements off
    by 8-32px. Pushes everything below out of alignment.
  • TYPOGRAPHY — h1/h2 size or weight wrong, line-height too tight/loose.
    Use the positional-text font sizes as the type scale.
  • COLOR — section background, text color, or accent off. Use the
    measured palette; do NOT eyeball hex values.
  • LAYOUT — column widths, flex/grid wrap behavior, alignment.
  • DECORATION position / shape — corner curves / gutter bars wrong size
    or wrong corner.

Preserve everything you got right. Don't restructure sections that are
already matching well — most of the page is probably fine, the diff tells
you exactly where the problems are.

==========================================================================
HARD RULES (unchanged from initial generation)
==========================================================================
- Copy text VERBATIM from the positional-text block. Don't paraphrase.
- Use measured colors over guesses.
- Use a font from the suggested stacks; don't invent new ones.
- Section order matches the source.
- No <script> tags, no external stylesheets, no Tailwind.
- Every CSS selector scoped under .page-root, except self-contained
  `.d-<id>` selectors inside catalog design sentinel blocks.
- Preserve `data-design-id`, `d-<id>`, complete design sentinel blocks and
  page-specific `--design-*` bindings. For a matching hotspot reference,
  reuse its construction rather than freely redesigning the component.
- STAY FLUID. Fix spacing/type mismatches by adjusting the cap or the
  vw coefficient inside the existing clamp() — never by replacing it
  with a bare px value. Do not add layout tables, fixed px widths,
  min-width on .page-root, or delete media queries to raise the score.
  The score is measured at one width; the page is read at all widths.
- Shape-preserving blocks (data-layout="diagram" / "table") keep their
  columns at every breakpoint.
- Output JSON only: { "html": "...", "css": "...", "notes": "..." }.
  notes should be 1-2 sentences explaining the changes you made.

---

### Multimodal user message structure for refinement

The web app builds the refinement user message as:

1. `"TARGET design (ground truth):"` → full page source PNG
2. `"CURRENT render of your previous HTML+CSS:"` → rendered PNG
3. `"DIFF OVERLAY — red pixels are mismatches, greyer areas already match:"` → diff PNG
4. For each hot region (up to 4): text describing bbox + mismatch %, `"SOURCE:"` → source crop PNG, `"Your RENDER for the same region:"` → render crop PNG
5. Final text block:

```
Layout summary (top → bottom):
{componentSummary}

<measured_design_tokens>…</measured_design_tokens>
<suggested_font_stacks>…</suggested_font_stacks>
<positional_text>…</positional_text>
<pdf_text>…</pdf_text>   [first 4000 chars — shorter budget than generation pass]
<profiled_designs>…</profiled_designs>  [matching hotspot references only]

Your PREVIOUS HTML:
<previous_html>
{previous.html — the token-form fragment, with relative crop paths}
</previous_html>

Your PREVIOUS CSS:
<previous_css>
{previous.css}
</previous_css>

Now output the corrected, complete HTML+CSS as a single JSON object.
```

The refinement pass receives the **un-inflated form** of the previous output — still with relative crop paths (e.g. `src="./crops/hero.png"`), not a version with embedded base64 data URIs. Keep crop references stable across all refinement passes.
