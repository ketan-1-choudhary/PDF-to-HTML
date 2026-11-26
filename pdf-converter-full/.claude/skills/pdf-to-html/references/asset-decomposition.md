# Asset decomposition — DIVIDE ② (section → assets)

This is the step that makes `pdf-converter` different from a one-shot page
generator. After the layout pass gives you sections (`regions.json`), you
break **each section** into its atomic **assets** and build each one in
isolation before merging. Think merge-sort: keep dividing until each piece
is trivial to get right, then merge upward.

## What an "asset" is

An asset is the smallest unit that can be authored correctly on its own,
with only local context. One asset = one of:

| Asset kind    | Examples                                                        | Output |
|---------------|-----------------------------------------------------------------|--------|
| `text`        | a heading, a paragraph, a list, a blockquote, a caption, a table, an inline formula run | real reflowable HTML |
| `media`       | a photograph, illustration, chart, labelled diagram, scientific schema, logo | `<img>` → `./assets/<id>.png` |
| `decoration`  | corner curve, gutter bar, background block, watermark, ribbon    | CSS / inline SVG, no `<img>` |

The **reflowability rule from `output-contract.md` still governs**: a
`text` asset is NEVER an image of text. A `media` asset is never a crop that
contains prose. If a crop you're about to call `media` has 30+ words of
readable prose in it, it's really one or more `text` assets — split it.

## How to decompose a section

For each section in `regions.json`, look at its crop (from `crop_region.mjs`)
and the `textBlocks` from `analyze_page.py` (or `extract_text.mjs`) that fall
inside its bbox, and emit an ordered list of assets.

1. **Walk the section top-to-bottom, then left-to-right within a row.** Each
   visually distinct atom becomes one asset. A heading followed by two
   paragraphs and a figure = 4 assets (heading, para, para, figure), in that
   reading order.
2. **Two-column bodies:** emit column 1's assets fully, then column 2's —
   reading order is document order, NOT visual left-to-right interleaving
   (see the design doc §Stage 7). Record that intent in `order`.
3. **Tables stay whole.** A data table is ONE `text` asset (a real `<table>`),
   not one asset per cell. Use the ruling-line / grid structure you see in
   the crop.
4. **Figure + caption are TWO assets** — a `media` asset for the picture and
   an adjacent `text` asset for the caption. Never bake the caption into the
   image. They get grouped back together in the reconcile step.
5. **Media assets prefer the PDF's embedded raster.** When
   `analyze_page.py`'s `embeddedImages[]` has an entry whose bbox matches the
   asset, use that `cropPath` (native resolution) rather than a crop of the
   downscaled page render. Falls back to a `crop_region.mjs` crop otherwise.
6. **Decorations come from the decoration tier** of `regions.json`. One asset
   per distinct shape. No `htmlPath`/`cropPath` needed — you'll author the
   CSS/SVG for them in the build step.

## Assign a reading-order index

Every asset gets an integer `order`, ascending across the WHOLE page (not
per-section), so the mechanical merge (`assemble_page.py`) can concatenate
the page correctly regardless of which section produced each asset. Leave
gaps (0, 10, 20, …) so you can insert without renumbering.

## Output: the assets manifest

Write one JSON array for the whole page to
`<work>/page_<N>/assets/manifest.json` — the ordered list `assemble_page.py`
consumes. Each entry:

```json
{
  "id": "sec2-para1",
  "sectionId": "worked-example",
  "kind": "text",
  "order": 40,
  "cropPath": "<work>/page_N/crops/sec2-para1.png",
  "text": "Verbatim text from the positional-text block for this atom.",
  "classification": "paragraph",
  "neighborHint": "follows the section heading; precedes the figure"
}
```

For `media`: add `"cropPath"` (embedded-image crop preferred) and an `"alt"`
draft. For `decoration`: add `"role"` (decoration|gutter|chrome|background)
and a shape/colour `"description"`; omit `cropPath`.

`classification` vocabulary (drives the build step's tag choice):
`heading | subheading | paragraph | list | table | blockquote | caption |
formula | figure | callout | footnote`.

## Then: build each asset (CONQUER)

Build assets **one at a time, in clean context**, using the per-asset build
brief. Give the builder ONLY: the asset's own crop, its `text` (verbatim),
its `classification`, the measured palette + font stack, and a one-line
`neighborHint`. Do NOT give it the whole page or the other assets' HTML —
that's what causes cross-asset bleed and hallucination. Each `text`/`media`
asset build writes a snippet `.html` (and optional `.css`) whose path you put
back into the manifest as `htmlPath` / `cssPath`.

Copy text VERBATIM. Use MEASURED colours. Use the positional-text font sizes
as the type scale. Same faithfulness rules as `prompts.md` § "Page
generation" — they apply per asset here.
