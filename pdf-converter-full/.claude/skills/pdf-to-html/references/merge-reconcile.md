# Merge reconciliation — the semantic half of MERGE (phase D)

`assemble_page.py` has already done the **mechanical** merge: it copied every
media crop into `assets/`, rewrote each `<img src>` to `./assets/<file>`,
concatenated the asset snippets in reading order into `merged.fragment.html`,
and concatenated the per-asset CSS into `merged.css`. That draft is ordered
and complete but NOT coherent — because, unlike a numeric merge-sort merge,
independently-built assets can **conflict**. This step resolves those
conflicts.

> **Why a semantic pass exists at all.** In merge-sort the merge is trivial:
> two sorted lists interleave by comparison. Here the "elements" are HTML
> fragments authored in isolation, so identical-looking elements can be
> tagged differently, a caption can float away from its figure, and every
> asset can invent its own type scale. Mechanical concatenation preserves
> order but not consistency. Reconciliation is what buys the consistency
> back. It is the highest-leverage quality step in the pipeline.

## Inputs

- `merged.fragment.html` and `merged.css` from `assemble_page.py`.
- The full source PNG (visual ground truth).
- The measured palette + `pageBackgroundHex` from `meta.json`.
- The positional-text block (for any verbatim text re-checks).

## What to reconcile — the checklist

Work through every item. Each is a real failure mode of isolated per-asset
generation.

1. **Heading hierarchy.** Assets were classified independently, so two
   visually identical headings may have become `<h2>` in one section and
   `<h3>` in another, or the page may have no single `<h1>`. Re-derive one
   consistent hierarchy for the whole page from the font-size clustering
   (largest run → h1, next cluster → h2, …). Exactly one `<h1>`. No level is
   skipped downward (`<h1>` then `<h3>` with no `<h2>`) unless the source
   genuinely does that.

2. **One global type scale + palette.** Each asset's CSS may have hard-coded
   its own font sizes/colours. Collapse them into ONE type scale and ONE set
   of measured colours applied `.page-root`-wide, so headings and body look
   consistent across the whole page rather than only within each asset. Delete
   per-asset duplicated declarations; keep only genuinely asset-specific rules.

3. **Figure ↔ caption grouping.** The decomposition step split each figure
   from its caption into two assets. Re-pair them: wrap the `media` asset and
   its caption `text` asset in a single `<figure>` + `<figcaption>` so they
   move together and read correctly to a screen reader.

4. **Running head / footer dedupe.** If a running header, footer, page
   number, or chapter badge was captured as an asset, make sure it appears
   once, in the right structural place (`<header>` / `<footer>`), and did not
   leak into the body flow. (These repeat across pages; they're pagination
   artifacts, not content.)

5. **List continuity.** A list split across an asset or section boundary must
   be ONE `<ul>`/`<ol>` with continuous numbering — not two lists that restart
   at 1. Merge adjacent list fragments of the same kind.

6. **Cross-asset paragraph continuation.** A paragraph split by a column or
   section break (often hyphenated at the break) must be rejoined into one
   `<p>`. Undo the hyphenation at the join.

7. **Scoping + contract.** Ensure the final fragment still obeys
   `output-contract.md`: single `.page-root` wrapper, every selector scoped
   under `.page-root`, `.page-root` fills the width (`width:100%;
   max-width:100%; margin:0; position:relative; overflow-x:hidden`), no
   `<script>`, no Tailwind. Fold the per-asset CSS + your global scale into a
   single well-ordered stylesheet.

8. **Decorations present.** Every decoration asset from the manifest must be
   recreated with CSS/inline SVG and correctly positioned. Decorations are not
   optional (they carry most of the colour-layer fidelity score).

## Output

Write the reconciled result to:

- `<work>/page_<N>/fragment.html` — the final page-root fragment.
- `<work>/page_<N>/page.css` — the single reconciled stylesheet.

Then save through `save_page.mjs` as usual. Because the media `<img src>`
already point at `./assets/<file>` and `assemble_page.py` mirrored the
`assets/` folder next to the save location, `content.html` resolves its
imagery from `./assets/` with no further path fixups. (Do NOT pass
`--inline-images` unless the user explicitly wants a single portable file —
the whole point of the `assets/` folder is that media lives there as real
files.)

## Do NOT

- Do NOT re-author correct assets from scratch. Reconciliation edits
  relationships and consistency; it does not rebuild content that is already
  faithful. Most of the draft is fine — touch only what the checklist flags.
- Do NOT paraphrase text. Verbatim rule from `prompts.md` still holds.
- Do NOT introduce a centred `max-width` column, a `min-width` on
  `.page-root`, or layout tables. Structure is fluid grid/flex per
  `output-contract.md` § "Fluid layout contract"; flowcharts/trees keep
  their shape inside `.scroll-x`.
