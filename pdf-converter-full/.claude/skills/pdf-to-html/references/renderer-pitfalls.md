# Renderer pitfalls — print-CSS quirks that bite

The verification loop renders your HTML with **headless Chromium** (`render_chromium.py`) at the source page's pixel width. WeasyPrint is only a last-resort fallback when no Chromium binary exists. Read this whole file before writing CSS for a page; it lists *concrete failures* observed in the wild.

**Fidelity never justifies a fixed layout.** Every fix below must keep the page fluid per `output-contract.md` § "Fluid layout contract". The pixel score is measured at ONE width; the user reads the page at ALL widths.

## 1. The page-root MUST fill the canvas width, with content-area paddings matched to the source

**The single biggest correctness bug — and it has two layers.**

### Layer A — `.page-root` fills the canvas

The verification loop renders the HTML at the source PDF's pixel width (e.g. `@page { size: 1633px 2200px }`). If your CSS says `.page-root { max-width: 1240px; margin: 0 auto; }`, the content sits in a 1240-wide column centred in a 1633-wide canvas, with ~196 px of dead whitespace on each side. Every pixel of your render is now laterally offset from the source — and the pixel-diff score craters even when the content itself is correct.

```css
/* WRONG — creates lateral offset in the verification render */
.page-root { max-width: 1240px; margin: 0 auto; }

/* WRONG — breaks every viewport narrower than 860px */
.page-root { min-width: 860px; }

/* RIGHT */
.page-root { width: 100%; max-width: 100%; margin: 0; }
```

### Layer B — the content area inside `.page-root` matches the source's print margins

Filling the canvas is not enough. Real printed pages have asymmetric outer margins (typically wider on the binding side). A textbook spread might have `185 px` of margin on the inner edge of one page and `186 px` on the outer edge of the facing page, with only `~73 px` on the other side. If your section paddings are a uniform `48 px`, content lands `~140 px` off where the source has it on the wide side and `~25 px` off on the narrow side — your render fills the canvas but everything is still misaligned.

**Measure the source's print margins first.** Use a one-line script to find the leftmost and rightmost non-white columns of `source.png`:

```bash
python3 -c "
from PIL import Image; import numpy as np
a = np.asarray(Image.open('<work>/page_<N>/source.png').convert('L'))
c = (a < 240).any(axis=0)
print('left:', int(c.argmax()), 'right:', int(len(c)-1-c[::-1].argmax()))
"
```

Take those numbers into your CSS **as fluid values** — `clamp(1rem, calc(px / W * 100vw), px)` where `W` is the source image width — for the page-header padding *and* every content section's padding. At the verification width they resolve to exactly the measured px; on smaller screens they shrink instead of eating the viewport.

```css
/* Source W = 1633, left margin 75 px, right margin 188 px: */
.page-root {
  --pad-l: clamp(1rem, 4.6vw, 75px);    /* 75 / 1633 = 4.6% */
  --pad-r: clamp(1rem, 11.5vw, 188px);  /* 188 / 1633 = 11.5% */
}
.page-root .page-header,
.page-root .body,
.page-root .exercises { padding-inline: var(--pad-l) var(--pad-r); }

@media (max-width: 768px) {
  .page-root { --pad-l: 1rem; --pad-r: 1rem; }  /* print asymmetry is meaningless on a phone */
}
```

```css
/* WRONG — 188px of right padding leaves ~127px of text on a 390px phone */
.page-root .body { padding: 14px 188px 6px 75px; }
```

Matching the source margins at the source width still matters for the pixel score; doing it with bare px is what makes the output desktop-only.

## 2. Never use layout tables — they cannot re-flow

Earlier versions of this skill told you to build 2-column bodies and card rows from `<table role="presentation">` to dodge WeasyPrint grid bugs. That made the output desktop-only: table cells stay side by side at every width, so on a 390px phone each column is ~150px of crushed text. The verifier now runs on Chromium, where grid and flex are reliable.

**Rule.** Side-by-side structure uses grid with the source's column ratio and stacks on small screens:

```html
<div class="body">
  <div class="text-col">…</div>
  <div class="figures">…</div>
</div>
```

```css
.page-root .body {
  display: grid;
  grid-template-columns: 3fr 2fr;            /* source column ratio, e.g. 60 / 40 */
  gap: clamp(1rem, 2.4vw, 40px);
  align-items: start;
}
@media (max-width: 768px) {
  .page-root .body { grid-template-columns: 1fr; }   /* reading order: text, then figures */
}
```

`<table>` is for genuinely tabular data only, always inside a `.scroll-x` wrapper (see `output-contract.md`).

## 3. Flowcharts, trees and concept maps keep their shape

A flowchart or tree (e.g. 1 heading → 3 subheadings → 2 topics each) carries meaning in its branches. Flattening it into one column on a phone turns a diagram into a confusing list. This is the one exception to "stack on small screens":

- Build it with grid/flex using explicit columns (`grid-template-columns: repeat(3, minmax(10rem, 1fr))`) and mark the block `data-layout="diagram"`.
- Wrap it in `.scroll-x` (`max-width: 100%; overflow-x: auto`) and give it a `min-width` in `rem` below which it scrolls rather than crushes.
- Media queries may shrink its type (floor `0.8125rem`) and gaps, but must NOT reduce its column/branch count or reorder nodes.
- Connectors (lines/arrows) are inline SVG or borders that scale with the nodes — never px-positioned absolute elements that detach when the block resizes.

## 4. If the fallback engine is WeasyPrint

When `render_chromium.py` reports `"engine": "weasyprint"` (no Chromium available), grid/flex children can be clipped, `display: contents` is ignored, and long `<ol>` items inside tight boxes may truncate. **Do not switch to layout tables or fixed px widths to appease it.** Accept a lower plateau (0.78–0.85) for that run; the shipped page is viewed in a real browser, where the fluid layout renders correctly.

## 5. `.page-root { overflow-x: hidden }` hides overflow — it does not fix it

The contract requires `overflow-x: hidden` on `.page-root` so decorations clip. Side effect: any fixed-width content wider than the viewport is silently **cut off** on phones instead of scrolling. If something is wider than the viewport, it is either a shape-preserving block (wrap it in `.scroll-x`) or a layout bug (make it fluid). `responsive_dom_checks.mjs` reports both as `clippedHorizontally` / `overflowOffenders`.

## 6. The page must fit in the source page's height

`@page { size: WxH; margin: 0 }` clips content past `H`. If your render is taller than the source, WeasyPrint paginates and the verification loop only rasterises **page 1** of the PDF — the rest is silently discarded. Symptom: the bottom section of the page is missing entirely from `render.png`, and the diff overlay shows a uniform horizontal red band at the bottom.

Check `pdfinfo render.pdf | grep Pages`. If it's > 1, your HTML is too tall.

**Calibration:** the source PDF page is typically `WIDTHpx × HEIGHTpx` (e.g. 1633×2200). Your total content height must be ≤ that. Tactics to shrink height:
- Drop `line-height` from 1.45 → 1.35.
- Drop section paddings vertically (they are fluid `clamp()` values — lower the cap, not the unit).
- Make sure the type-scale matches the source — a body capped at `18px` that should be `15px` produces 20% more lines.

## 7. Images: cap their CSS width to match what's in the source

A naive `<img src="…" style="width: 100%; height: auto">` will scale the image to fill its whole column, even when the source shows it much smaller. Cap it fluidly at its source size instead:

```css
/* Source figure is 420px wide on W = 1633 */
.page-root .figure img { width: min(100%, clamp(12rem, 25.7vw, 420px)); height: auto; display: block; }
```

On a phone the image takes the column width (never overflows); at the source width it is exactly 420px.