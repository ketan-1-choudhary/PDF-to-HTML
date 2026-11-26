#!/usr/bin/env python3
"""
assemble_page.py — the DETERMINISTIC half of the merge step (phase D) for
the pdf-converter skill's divide-and-merge pipeline.

The pipeline divides a page into sections, sections into *assets*, builds
each asset as an isolated HTML snippet in clean context, then merges them
back up. Merging has two halves:

  1. Mechanical (this script): collect every asset's HTML snippet in
     reading-order, copy media crops into a single `assets/` folder next to
     the eventual content.html, rewrite each media snippet's <img src> to
     point at ./assets/<file>, and concatenate the snippets into one
     page-root fragment + one concatenated CSS blob.

  2. Semantic (the model, guided by references/merge-reconcile.md): take the
     mechanically-merged fragment this script emits and RECONCILE it —
     unify heading levels across assets, group figures with their captions,
     dedupe repeated running heads, fold the per-asset CSS into one global
     type scale + measured palette. That is NOT done here; this script only
     does the deterministic stapling so the model starts from a consistent,
     ordered draft instead of a pile of snippets.

This separation is the whole point of the merge-sort analogy: merging is
cheap and order-preserving mechanically, but unlike a numeric merge the
elements can *conflict*, so a semantic pass runs on top of the mechanical one.

INPUT — an "assets manifest" JSON file: an ORDERED array (reading order) of

    {
      "id": "sec1-heading",
      "kind": "text" | "media" | "decoration",
      "order": 0,                       // reading-order index (int, ascending)
      "htmlPath": "<path to snippet .html>",   // required for text/media
      "cssPath":  "<path to snippet .css>",    // optional per-asset CSS
      "cropPath": "<path to source crop .png>" // required when kind == media
    }

OUTPUT — writes into <work-dir>:
    merged.fragment.html   the concatenated page-root fragment (draft)
    merged.css             the concatenated per-asset CSS (draft)
    assets/<file>.png      every media crop, copied + de-duplicated by hash
  and copies assets/ next to --out-html-dir too when that is given, so the
  saved content.html can resolve ./assets/... directly.

Exits 0 on success, prints a JSON summary. Never silently drops an asset:
anything skipped is reported in the summary's "skipped" array.
"""

import argparse, json, os, sys, shutil, hashlib, html, traceback


def read_text(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def short_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:10]


IMG_SRC_RE = None  # compiled lazily to keep top import list minimal


def rewrite_img_src(snippet, new_src):
    """Rewrite the FIRST <img ... src="..."> in snippet to point at new_src.

    Media asset snippets are expected to contain exactly one <img>. If none
    is found the snippet is returned unchanged (the summary flags it).
    """
    import re
    global IMG_SRC_RE
    if IMG_SRC_RE is None:
        IMG_SRC_RE = re.compile(r'(<img[^>]*\bsrc=")([^"]*)(")', re.IGNORECASE)
    replaced = {"n": 0}

    def _sub(m):
        if replaced["n"] == 0:
            replaced["n"] += 1
            return m.group(1) + new_src + m.group(3)
        return m.group(0)

    out = IMG_SRC_RE.sub(_sub, snippet, count=1)
    return out, replaced["n"] > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="assets manifest JSON (ordered array)")
    ap.add_argument("--work-dir", required=True, help="where to write merged.* + assets/")
    ap.add_argument("--out-html-dir", default=None,
                    help="optional: also copy assets/ into this dir (where content.html will live)")
    ap.add_argument("--assets-subdir", default="assets", help="asset folder name (default: assets)")
    args = ap.parse_args()

    try:
        manifest = json.loads(read_text(args.manifest))
        if not isinstance(manifest, list):
            raise ValueError("manifest must be a JSON array")

        os.makedirs(args.work_dir, exist_ok=True)
        assets_dir = os.path.join(args.work_dir, args.assets_subdir)
        os.makedirs(assets_dir, exist_ok=True)

        # Stable reading order.
        ordered = sorted(
            [a for a in manifest if isinstance(a, dict)],
            key=lambda a: (a.get("order", 1_000_000), str(a.get("id", "")))
        )

        frag_parts = []
        css_parts = []
        skipped = []
        copied_by_hash = {}   # hash -> asset filename (dedupe identical crops)
        media_count = 0
        text_count = 0
        deco_count = 0

        for a in ordered:
            aid = str(a.get("id", "asset"))
            kind = a.get("kind", "text")
            html_path = a.get("htmlPath")
            css_path = a.get("cssPath")

            # Per-asset CSS (optional, any kind).
            if css_path and os.path.exists(css_path):
                try:
                    css_parts.append(f"/* --- asset: {aid} --- */\n" + read_text(css_path).strip())
                except Exception as e:
                    skipped.append({"id": aid, "reason": f"css read failed: {e}"})

            if kind == "media":
                crop = a.get("cropPath")
                if not crop or not os.path.exists(crop):
                    skipped.append({"id": aid, "reason": "media asset missing cropPath"})
                    continue
                # Copy into assets/, deduped by content hash.
                h = short_hash(crop)
                if h in copied_by_hash:
                    asset_file = copied_by_hash[h]
                else:
                    ext = os.path.splitext(crop)[1].lower() or ".png"
                    asset_file = f"{aid}{ext}"
                    # avoid collision on differing content with same id
                    dest = os.path.join(assets_dir, asset_file)
                    if os.path.exists(dest) and short_hash(dest) != h:
                        asset_file = f"{aid}-{h}{ext}"
                        dest = os.path.join(assets_dir, asset_file)
                    shutil.copyfile(crop, dest)
                    copied_by_hash[h] = asset_file
                rel = f"./{args.assets_subdir}/{asset_file}"

                if html_path and os.path.exists(html_path):
                    snippet = read_text(html_path)
                    snippet, ok = rewrite_img_src(snippet, rel)
                    if not ok:
                        skipped.append({"id": aid, "reason": "media snippet had no <img> to rewrite; used raw snippet"})
                else:
                    # Synthesise a minimal figure if the model didn't supply a snippet.
                    alt = html.escape(a.get("alt", aid.replace("-", " ")))
                    snippet = f'<figure class="asset asset--media" data-asset-id="{html.escape(aid)}"><img src="{rel}" alt="{alt}" /></figure>'
                frag_parts.append(f"<!-- asset:{aid} kind:media -->\n{snippet.strip()}")
                media_count += 1

            elif kind == "decoration":
                # Decorations are CSS/SVG only — HTML snippet optional.
                if html_path and os.path.exists(html_path):
                    frag_parts.append(f"<!-- asset:{aid} kind:decoration -->\n{read_text(html_path).strip()}")
                deco_count += 1

            else:  # text
                if not html_path or not os.path.exists(html_path):
                    skipped.append({"id": aid, "reason": "text asset missing htmlPath"})
                    continue
                frag_parts.append(f"<!-- asset:{aid} kind:text order:{a.get('order')} -->\n{read_text(html_path).strip()}")
                text_count += 1

        merged_fragment = (
            '<div class="page-root">\n'
            + "\n\n".join(frag_parts)
            + "\n</div>\n"
        )
        merged_css = "\n\n".join(css_parts) + "\n"

        frag_out = os.path.join(args.work_dir, "merged.fragment.html")
        css_out = os.path.join(args.work_dir, "merged.css")
        with open(frag_out, "w", encoding="utf-8") as f:
            f.write(merged_fragment)
        with open(css_out, "w", encoding="utf-8") as f:
            f.write(merged_css)

        # Optionally mirror the MEDIA files next to where content.html will be
        # saved. Copy only the media assets this run actually produced (the
        # values of copied_by_hash) — never the whole work assets_dir, which
        # also holds snippet .html files and manifest.json that must NOT ship
        # in the deliverable assets/ folder.
        mirrored_to = None
        if args.out_html_dir:
            dest_assets = os.path.join(args.out_html_dir, args.assets_subdir)
            os.makedirs(dest_assets, exist_ok=True)
            for asset_file in copied_by_hash.values():
                src = os.path.join(assets_dir, asset_file)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(dest_assets, asset_file))
            mirrored_to = dest_assets

        print(json.dumps({
            "ok": True,
            "mergedFragment": frag_out,
            "mergedCss": css_out,
            "assetsDir": assets_dir,
            "assetsMirroredTo": mirrored_to,
            "counts": {"text": text_count, "media": media_count, "decoration": deco_count,
                       "assetsCopied": len(copied_by_hash)},
            "skipped": skipped,
            "note": "This is the MECHANICAL merge. Now run the semantic reconciliation "
                    "pass (references/merge-reconcile.md) on merged.fragment.html + merged.css "
                    "before saving.",
        }, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "traceback": traceback.format_exc()}))
        sys.exit(1)


if __name__ == "__main__":
    main()
