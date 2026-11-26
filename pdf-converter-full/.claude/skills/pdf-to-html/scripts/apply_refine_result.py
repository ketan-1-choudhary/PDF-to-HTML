#!/usr/bin/env python3
"""
apply_refine_result.py — second half of the refinement loop.

Called AFTER the refinement step has written its new fragment.html + page.css
to <work>/refine-pass-<N>/. This script:
    1. Reads the new fragment + CSS the refinement step wrote.
  2. Runs them through save_page.mjs (so the canonical content.html gets
     the new content) — backs up the previous canonical output first.
  3. Re-renders via render_chromium.py.
  4. Re-scores via verify_layered.py.
  5. Compares the new score to the previous best.
  6. COMMITS the change (replaces work/fragment.html + work/page.css with
     the new versions, updates the canonical save) iff:
        new.score >= prev.score + 0.005      (real improvement, not noise)
        AND new.layers.colour >= 0.85         (don't tank colour for layout)
  7. Otherwise REVERTS — restores the previous fragment + CSS from backup,
     re-runs save_page.mjs to put the previous version back canonical.
  8. Returns JSON with the decision + new score so the caller (SKILL.md
     Step 9 loop) can decide whether to do another pass or stop.

This mirrors the web app's bestScore + rollback logic in route.ts L402-414.

Usage:
  python3 apply_refine_result.py \\
      --pass <N> \\
      --work-dir <work>/page_<N> \\
      --previous-score <0..1> \\
      --pdf <pdf-path> --page <N> \\
      --out-root <out-root> --run-id "<pdf-filename>" \\
      [--scripts-dir <path-to-skill-scripts>]
"""
import argparse, json, os, shutil, subprocess, sys, time
from pathlib import Path
from checkpoint_state import checkpoint_update


def run_json(cmd, timeout=120):
    """Run a script that prints a single JSON object on success; return (ok, parsed)."""
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, {"ok": False, "error": "timeout"}
    stdout = (res.stdout or b"").decode("utf-8", errors="replace")
    stderr = (res.stderr or b"").decode("utf-8", errors="replace")
    if res.returncode != 0:
        return False, {"ok": False, "error": stderr.strip()[:500] or stdout.strip()[:500]}
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        return False, {"ok": False, "error": "could not locate JSON in script output", "raw": stdout[:500]}
    try:
        return True, json.loads(stdout[start:end + 1])
    except Exception:
        return False, {"ok": False, "error": "could not parse JSON from script", "raw": stdout[:500]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="pass_n", type=int, required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--previous-score", type=float, required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--scripts-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--min-improvement", type=float, default=0.005)
    ap.add_argument("--min-colour", type=float, default=0.85)
    args = ap.parse_args()

    scripts = Path(args.scripts_dir)
    work = Path(args.work_dir).resolve()
    checkpoint_update(
        str(work),
        "apply_refine_result",
        "running",
        {
            "page": args.page,
            "pass": args.pass_n,
            "previousScore": args.previous_score,
        },
        next_action="Commit if improved else revert and stop/refine again",
    )
    pass_dir = work / f"refine-pass-{args.pass_n}"
    new_frag = pass_dir / "fragment.html"
    new_css  = pass_dir / "page.css"

    # Sanity: refinement step must have written both files.
    if not new_frag.exists() or not new_css.exists():
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "refinement outputs missing", "pass": args.pass_n},
            next_action="Regenerate refine pass outputs before applying result",
        )
        print(json.dumps({
            "ok": False,
            "pass": args.pass_n,
            "decision": "errored",
            "error": "refinement step did not write both fragment.html and page.css",
            "expected_fragment": str(new_frag),
            "expected_css": str(new_css),
        }, indent=2))
        sys.exit(2)

    # Validate the new fragment looks well-formed.
    frag_txt = new_frag.read_text().strip()
    if not frag_txt.startswith("<div") or "page-root" not in frag_txt[:200]:
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "new fragment missing page-root wrapper", "pass": args.pass_n},
            next_action="Fix generated fragment and rerun apply_refine_result",
        )
        print(json.dumps({
            "ok": False, "pass": args.pass_n, "decision": "errored",
            "error": "new fragment missing page-root wrapper",
        }))
        sys.exit(2)
    if not frag_txt.endswith("</div>"):
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "new fragment appears truncated", "pass": args.pass_n},
            next_action="Regenerate full fragment and rerun apply_refine_result",
        )
        print(json.dumps({
            "ok": False, "pass": args.pass_n, "decision": "errored",
            "error": "new fragment does not end with </div> (likely truncated)",
        }))
        sys.exit(2)

    # Back up the previous canonical fragment + CSS so we can roll back.
    backup_dir = pass_dir / "backup-of-previous"
    backup_dir.mkdir(exist_ok=True)
    prev_frag = work / "fragment.html"
    prev_css  = work / "page.css"
    if prev_frag.exists(): shutil.copy(prev_frag, backup_dir / "fragment.html")
    if prev_css.exists():  shutil.copy(prev_css,  backup_dir / "page.css")

    # Stage the new fragment + CSS as the new canonical working files.
    shutil.copy(new_frag, prev_frag)
    shutil.copy(new_css,  prev_css)

    # Re-save through save_page.mjs (writes content.html + css/page.css).
    ok, res = run_json(
        ["node", str(scripts / "save_page.mjs"),
         "--html", str(prev_frag), "--css", str(prev_css),
         "--out-root", args.out_root, "--run-id", args.run_id,
         "--pdf-for-hash", args.pdf, "--page", str(args.page)]
    )
    if not ok:
        # Roll back and bail.
        shutil.copy(backup_dir / "fragment.html", prev_frag)
        shutil.copy(backup_dir / "page.css",      prev_css)
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "save_page failed", "detail": res, "pass": args.pass_n},
            next_action="Fix save failure and rerun apply_refine_result",
        )
        print(json.dumps({"ok": False, "decision": "errored",
                          "stage": "save", "detail": res}, indent=2))
        sys.exit(2)
    content_path = res.get("contentPath")

    # Re-render via render_chromium.py.
    render_png = work / "render.png"
    # Best-effort: we don't have width/height args here, so derive from source.png.
    src_png = work / "source.png"
    try:
        from PIL import Image
        sw, sh = Image.open(src_png).size
    except Exception:
        sw, sh = 1308, 1761
    ok, res = run_json(
        ["python3", str(scripts / "render_chromium.py"),
         "--html", content_path, "--width", str(sw), "--height", str(sh),
         "--out", str(render_png)],
        timeout=60,
    )
    if not ok:
        shutil.copy(backup_dir / "fragment.html", prev_frag)
        shutil.copy(backup_dir / "page.css",      prev_css)
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "render failed", "detail": res, "pass": args.pass_n},
            next_action="Fix render failure and rerun apply_refine_result",
        )
        print(json.dumps({"ok": False, "decision": "errored",
                          "stage": "render", "detail": res}, indent=2))
        sys.exit(2)

    # Re-score.
    ok, res = run_json(
        ["python3", str(scripts / "verify_layered.py"),
         "--source", str(src_png), "--render", str(render_png),
         "--html", content_path,
         "--pdf", args.pdf, "--page", str(args.page),
         "--work-dir", str(work)],
        timeout=120,
    )
    if not ok:
        shutil.copy(backup_dir / "fragment.html", prev_frag)
        shutil.copy(backup_dir / "page.css",      prev_css)
        checkpoint_update(
            str(work),
            "apply_refine_result",
            "failed",
            {"error": "verify_layered failed", "detail": res, "pass": args.pass_n},
            next_action="Fix scoring failure and rerun apply_refine_result",
        )
        print(json.dumps({"ok": False, "decision": "errored",
                          "stage": "score", "detail": res}, indent=2))
        sys.exit(2)

    new_score = float(res.get("score", 0))
    layers = res.get("layers", {})
    new_colour = float(layers.get("colour", {}).get("score", 0))

    improved   = new_score >= args.previous_score + args.min_improvement
    colour_ok  = new_colour >= args.min_colour

    if improved and colour_ok:
        decision = "committed"
        # Files are already in place; nothing to do.
    else:
        decision = "reverted"
        # Roll back canonical files.
        shutil.copy(backup_dir / "fragment.html", prev_frag)
        shutil.copy(backup_dir / "page.css",      prev_css)
        # Re-run save_page.mjs to restore the canonical content.html.
        run_json(
            ["node", str(scripts / "save_page.mjs"),
             "--html", str(prev_frag), "--css", str(prev_css),
             "--out-root", args.out_root, "--run-id", args.run_id,
             "--pdf-for-hash", args.pdf, "--page", str(args.page)]
        )

    checkpoint_update(
        str(work),
        "apply_refine_result",
        "completed",
        {
            "pass": args.pass_n,
            "decision": decision,
            "previousScore": args.previous_score,
            "newScore": new_score,
            "delta": round(new_score - args.previous_score, 4),
        },
        next_action=(
            "Continue next refine pass if below target and committed"
            if decision == "committed"
            else "Stop: reverted to previous best"
        ),
    )

    print(json.dumps({
        "ok": True,
        "pass": args.pass_n,
        "decision": decision,
        "previousScore": args.previous_score,
        "newScore": new_score,
        "delta": round(new_score - args.previous_score, 4),
        "newLayers": {k: v.get("score") for k, v in layers.items()},
        "improvementThreshold": args.min_improvement,
        "minColour": args.min_colour,
        "weakestLayer": res.get("weakestLayer"),
        "contentPath": content_path,
        "guidance": (
            "Continue to the next refinement pass if newScore < 0.92 AND "
            "(committed AND pass < max_passes)."
            if decision == "committed" else
            "Stop the refinement loop. This pass regressed; "
            "the previous version has been restored. Ship the previous best."
        ),
    }, indent=2))


if __name__ == "__main__":
    main()
