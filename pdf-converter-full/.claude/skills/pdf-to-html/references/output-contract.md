# Output contract

Every page this skill produces must conform to the contract below. The `save_page.mjs` helper assumes it.

## The fragment (`fragment.html`)

A single tree rooted at one element with `class="page-root"`. No `<!doctype>`, no `<html>`, no `<head>`, no `<body>`. Inline SVG for icons is fine. No `<script>` tags anywhere — the preview iframe runs with scripts disabled and they would not execute.

```html
<div class="page-root">
  <header class="page-root__header">…</header>
  <main>…</main>
  <footer>…</footer>
</div>
```

## The CSS (`page.css`)

- Every selector scoped under `.page-root` (e.g. `.page-root .hero { … }`),
  except the self-contained `.d-<id>` selectors inside catalog design blocks
  described below. Those selectors are scoped to the design class instead.
- Universal selectors and global resets are scoped too:
  `.page-root *, .page-root *::before { … }`.
- `.page-root` MUST set `position: relative;` and `overflow-x: hidden;` so absolutely-positioned decorations clip correctly when the viewport scales down on mobile.
- **`.page-root` MUST fill the canvas width.** Use `width: 100%; max-width: 100%; margin: 0;`. Never set a pixel `max-width` (e.g. `1240px`) or `min-width` — the verification renders at the source PDF width and a narrower `.page-root` produces large lateral whitespace, while a `min-width` breaks every phone. See `renderer-pitfalls.md` § 1.
- **Fluid, not fixed.** All structural sizes follow the "Fluid layout contract" below: source measurements become `clamp()`-capped fluid values so the page is pixel-faithful at the source width and re-flows below it.
- `@import` Google Fonts only if you reference them; one import line at the top of the CSS.
- No Tailwind. No external stylesheets beyond a single optional Google Fonts `@import`.
- **Emit `@font-face` metric proxies when the source uses non-embeddable fonts.** When `extract_text` returns `fontName` values for proprietary fonts (Caecilia LT Pro, Hypatia Sans Pro, ITC Garamond Std, etc.), wrap the substitute font in a `@font-face` with `size-adjust` / `ascent-override` / `descent-override` / `line-gap-override` that matches the source's metrics, then point `font-family` at the proxy name. This is the highest-leverage fix for layout-block IoU because it makes substitute fonts wrap at the same word positions as the source. WeasyPrint silently ignores these descriptors on `local()` sources — that's fine, the proxies are no-ops there and active under Chromium. Example:

  ```css
  @font-face {
    font-family: 'CaeciliaProxy';
    src: local('Source Serif 4'), local('Georgia');
    size-adjust: 106%; ascent-override: 78%;
    descent-override: 22%; line-gap-override: 0%;
  }
  .page-root { font-family: 'CaeciliaProxy', 'Source Serif 4', Georgia, serif; }
  ```

## Profiled design components

Use the page's normalized profiler assignments and `design_brief.json`. For
every component corresponding to a catalog design, including a non-reusable
one, put both `data-design-id="<id>"` and `class="d-<id>"` on its outermost
element. The ID is the hyphen-only normalized ID. A component may carry other
classes; nested designs each need their own markers. Do not use these design
classes outside `.page-root`.

For each design, wrap **all** its component CSS, including media queries and
any keyframes used by that design, in exact matching sentinel comments:

```html
<aside class="d-box-notice" data-design-id="box-notice">This page's content</aside>
```

```css
/* @design box-notice */
.d-box-notice {
  --design-accent: #d26020;
  --design-bg: #fff4e8;
  --design-border: #b94b18;
  --design-text: #222222;
  background: var(--design-bg, transparent);
  border: 1px solid var(--design-border, currentColor);
  color: var(--design-text, currentColor);
}
.d-box-notice .icon { color: var(--design-accent, currentColor); }
@media (max-width: 768px) {
  .d-box-notice { padding-inline: 1rem; }
}
/* @end box-notice */
```

Component rules must be self-contained and scoped under `.d-<id>`: do not use
ancestor-rooted selectors such as `.page-root .d-<id>`, depend on page-level
`:root` variables without a local fallback, or use keyframes referenced from
outside the block. The `.d-<id>` rules are the narrow exception to the usual
`.page-root` selector prefix so the captured CSS can be reused independently.
Other page CSS remains scoped under `.page-root`.

When `hasReference` is true, use its HTML/CSS as a **structural prior**, not a
template and not the source of the new page's text, colours, fonts or spacing.
Rebuild the current page's content and styles from its own image and analysis.
Define the four role variables above in this page's CSS using its own measured
colours; per-instance inline bindings may override them when needed. The
harvester removes these bindings and replaces colour literals in captured CSS
and inline styles with role variables, using `transparent` for background and
`currentColor` for accent, border and text fallbacks. Do not copy those
fallbacks as the current page's intended palette.

Non-reusable entries supply descriptions and bboxes for identification only:
never inject their HTML/CSS as references or harvest them.

### Icon designs

Designs whose ID starts with `icon-` (`isIcon: true` in the brief) are graphic
symbols. Draw a new icon as an inline `<svg>` inside its marked element; never
replace an icon with its label text or leave the element empty. Text drawn
inside the symbol belongs to the SVG, not to the page text.

When an icon has a reference, paste its `html` **exactly as stored** at each
instance: do not redraw, edit or restyle the markup. Generate only this page's
CSS for it, in its sentinel block, defining the colour variables it uses, for
example `.d-icon-media-video { --design-bg: #d6337a; --design-text: #ffffff; }`.

## Layout primitives — what to use when

The verification renderer is headless Chromium (`render_chromium.py`), so modern CSS layout is safe. Build every structural layout with grid/flex that can re-flow; reserve tables for real tabular data.

| Layout need                            | Use                                                                 | Don't use                                   |
|----------------------------------------|---------------------------------------------------------------------|---------------------------------------------|
| 2-column body (text + figures)         | `display: grid; grid-template-columns: <a>fr <b>fr` (source ratio), stacks to 1 column at ≤ 768px | `<table role="presentation">`, fixed px column widths |
| Side-by-side cards / exercise boxes    | `grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 1fr))` | layout tables, `width: 50%` floats |
| Multi-column data table                | Real `<table>` (`<thead>`/`<tbody>`) inside a `.scroll-x` wrapper  | CSS Grid table-mimicry                      |
| Flowchart / tree / concept map         | Shape-preserving block (see below): grid/flex with fixed branch geometry inside `.scroll-x` | collapsing branches into one column, layout tables |
| Stack of figures inside a column       | Block flow, `<figure>` with margin                                  | absolute positioning                         |
| Inline math / formula row              | `display: flex; flex-wrap: wrap; justify-content: center` or inline-block runs | fixed widths                    |

**Layout tables are forbidden.** `<table role="presentation">` cannot re-flow: its cells stay side by side at every width, which produces 60px-wide text columns on a phone. Use `<table>` only when the content is genuinely tabular (rows × columns of data).

## Fluid layout contract (desktop-faithful, fluid everywhere)

The page must match the source at the source width (`meta.imageWidth`, the verification canvas) AND re-flow cleanly down to 320px. Achieve both with one rule: **express every source measurement relative to the source width, then cap it at the source value.**

Let `W` = source image width in px. For a measured value `v` px:

```
fluid(v) = clamp(<floor>, calc(v / W * 100vw), v px)
```

At viewport `W` this resolves to exactly `v` px (pixel fidelity preserved). Below `W` it shrinks proportionally, but never below a readable floor.

### Spacing (page margins, section padding, column gaps)

- Measure the source's print margins (`renderer-pitfalls.md` § 1) and express them as fluid values, e.g. source left 75px / right 188px on `W = 1633`:
  ```css
  .page-root { --pad-l: clamp(1rem, 4.6vw, 75px); --pad-r: clamp(1rem, 11.5vw, 188px); }
  .page-root .section { padding-inline: var(--pad-l) var(--pad-r); }
  @media (max-width: 768px) { .page-root { --pad-l: 1rem; --pad-r: 1rem; } }
  ```
- Asymmetric print margins are a print artifact; on tablet/phone, make them symmetric.
- Gaps: `gap: clamp(0.75rem, calc(v / W * 100vw), v px)`.
- Vertical rhythm: `rem`/`em`, optionally clamped the same way.
- **Never** a bare px `width`, `min-width`, `padding-inline` or `margin-inline` above `2rem` on a structural element. Small px values (borders, 1–8px radii, hairlines) are fine.

### Typography

- Body copy: `font-size: clamp(1rem, calc(v / W * 100vw), v px)`. The floor of `1rem` keeps text readable on phones.
- Captions/footnotes: floor `0.875rem`. Headings: floor ≈ `1.25rem` (h3) to `1.75rem` (h1).
- Line-height unitless (e.g. `1.4`). `overflow-wrap: anywhere; hyphens: auto;` on text containers.
- When the floor is hit, text naturally wraps more — that is correct reflow, not a fidelity bug.

### Columns

- Multi-column bodies use grid with the source's column ratio (`grid-template-columns: 3fr 2fr`), and collapse to a single column at `@media (max-width: 768px)` in reading order (column 1 fully, then column 2).
- Card/exercise rows use `repeat(auto-fit, minmax(min(100%, 16rem), 1fr))` so they wrap without a media query.
- Images: `max-width: 100%; height: auto; display: block;` sized by their column, never by a fixed px width.

### Shape-preserving blocks (flowcharts, trees, concept maps, wide tables)

Some content has **meaning in its geometry**: a flowchart with branches, a tree (1 heading → 3 subheadings → 2 topics each), a concept map with connectors, a multi-column data table, an aligned derivation. Collapsing these into one column destroys the meaning. For these blocks only:

- Mark the block with `data-layout="diagram"` (flowcharts/trees/maps) or `data-layout="table"` (data tables).
- Wrap it in a scroll container: `<div class="scroll-x">…</div>` with
  ```css
  .page-root .scroll-x { max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  ```
- Inside, keep the desktop geometry: same number of columns/branches, same connectors. Give the block a sensible `min-width` (in `rem`) below which it scrolls instead of crushing.
- You MAY scale its type down with clamp (floor `0.8125rem`) and tighten gaps on small screens, but NEVER reduce its column/branch count.
- This is the ONLY place horizontal scrolling is allowed. Everything else must fit the viewport.

### Decorations

- Absolutely positioned, `aria-hidden="true"`, sized in `%`/`vw`/fluid clamps — never a px size that exceeds the viewport.
- They must never create horizontal overflow. At `≤ 480px` shrink them or `display: none` the purely cosmetic ones.

## Responsive contract

- Must look correct from 320 px to 1440 px+ AND match the source at width `W`.
- No horizontal page scroll at any width. Only `.scroll-x` shape-preserving blocks may scroll, inside themselves.
- Base styles target the source width; adapt with media queries at `1024px`, `768px`, and `480px` at minimum.
- At `≤ 768px`: every non-diagram multi-column layout is a single column; page side padding is `1rem`–`1.5rem`.
- No text block narrower than ~16rem on a phone (outside shape-preserving blocks) — that means a layout failed to stack.
- Images: `max-width: 100%; height: auto; display: block;` and a meaningful `alt`.

## Images

### Reflowability is non-negotiable

**This is the most important rule in the contract — read it before you write any `<img>` tag.**

The output MUST be reflowable HTML. Text content — paragraphs, headings, lists, captions, table cells, math expressions, exercise prompts, conclusions, footnotes — MUST be reconstructed as real HTML elements with the text embedded as text. The model is forbidden from cropping the page body (or any large prose-bearing section) as one image and wrapping it in `<img>`. That shortcut produces a screenshot in an HTML envelope, not reflowable HTML, and defeats the entire purpose of the skill.

### When `<img>` is allowed — media-tier gating

`<img src="./crops/...">` is permitted ONLY for a component whose detected role is one of:

  `media` | `diagram` | `hero` | `feature` | `content`

AND which is a true visual asset — a photograph, illustration, chart, labelled diagram, scientific schematic, or hand-drawn schema with colored overlay annotations (e.g. a Ruffini-rule schema with red arrows, a circuit diagram with current-flow arrows). This is the same `MEDIA_ROLES` gating the web app's `__PDF_IMAGE_<id>__` mechanism uses (`agent-pdf-html-main/app/api/analyze/route.ts` line 71).

Components with role `decoration`, `gutter`, `chrome`, or `background` MUST be recreated with CSS or inline SVG — NEVER with `<img>`.

When you do embed a media asset, crop it **at the finest granularity possible** — one `<img>` per individual figure/schema, never one per page or one per body. The crop must be tight to the asset; caption text goes in adjacent real HTML, never inside the image.

### Disallowed image patterns

An `<img>` MUST NOT contain:

- Body paragraphs, prose, or any text content that could be reproduced as a `<p>` / `<h1>` / `<li>`.
- More than one logical section. If a single image contains both a figure and its caption text, split them and put the caption in real HTML.
- An entire page section. There is no legitimate `<img src="./crops/body.png">`, `<img src="./crops/content.png">`, or `<img src="./crops/whole-page.png">`.
- A region whose detected role is `decoration`, `gutter`, `chrome`, or `background`.

### Self-check before you save

Before calling `save_page.mjs`, count `<p>` tags and `<img>` tags in your fragment. A healthy reflowable page has many more `<p>` than `<img>` (typically 8–20 `<p>` to 0–4 `<img>` for a textbook page). If `<img>` count ≥ `<p>` count, or any `<img src>` references a file named `body.png` / `content.png` / `page.png` / similar, you've taken the disallowed shortcut — re-do the section as real HTML. The fidelity score will be lower than the all-image shortcut; that's expected (the all-image shortcut games the colour/pixel layers by embedding source pixels). Take the lower score; ship reflowable HTML.

### Pointing at the crops

For media assets that pass the gating above, point `<img src="…">` at the matching crop file produced by `crop_region.mjs` in Step 6, using a relative path that resolves from the saved `content.html`. The default save layout is:

```
<out-root>/<run-slug>/page_<N>/
  content.html
  css/page.css
  crops/<region-id>.png    ← src="./crops/<region-id>.png"
```

If the user wants a single portable file, run `save_page.mjs --inline-images` to base64-embed each crop into a `data:` URI so `content.html` is self-contained.

For purely decorative shapes (corner curves, gutter bars, watermarks), recreate with CSS or inline SVG. Do not use `<img>` for those.

## Saved layout (what `save_page.mjs` writes)

```
<out-root>/<run-slug>/page_<N>/
  ├── content.html   ← <link rel="stylesheet" href="./css/page.css">
  └── css/
      └── page.css   ← boilerplate reset + your scoped CSS + anti-whitespace overrides
```

`<run-slug>` is `slugify(pdfFilename) + "-" + first10HexOfSHA256(pdfBytes)`, so two runs of the same file don't overwrite each other.

## Why this shape

- A standalone `content.html` opens in any browser with no build step, which is the whole point.
- A `page-root` wrapper + scoped CSS means the page can be safely embedded inside the Reader Workbench or any host page without leaking styles.
- The anti-whitespace overrides at the end of `page.css` neutralise three common failures that cause iframes to grow past their content: `.page-root { min-height: 100vh }` pegged to the viewport, `html, body { height: 100% }` doing the same at document level, and trailing `margin-bottom` on the last section.
