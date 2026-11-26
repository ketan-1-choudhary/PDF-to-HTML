#!/usr/bin/env python3
"""
resume_from_checkpoint.py - resume planner for pdf-converter-full.

Reads pipeline_state.json plus existing artifacts in WORK and tells you
whether you can restart from a chosen stage without rerunning earlier stages.

Usage:
  python3 resume_from_checkpoint.py --work-dir <work/page_N> [--start-from step5-generate]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from checkpoint_state import load_state


STAGES = [
    ("step1-extract", ["extracted_text.json"]),
    ("step2-analyze-route", ["meta.json", "source.png", "docling.json", "ocr_text.json", "routing.json", "regions.json", "section_coordinates.json"]),
    ("step3-section-analyze", ["sections"]),
    ("step4-ocr-verify", ["sections"]),
    ("step5-generate", ["content.html", "page.css"]),
    ("step6-verify-hotspots", ["render.png", "diff.png", "section_refine_plan.json"]),
    ("step7-page-converge", ["content.html", "page.css", "verification.json"]),
    ("step8-section-refine", ["content.html", "page.css"]),
    ("step9-final-verify", ["render.png", "verification.json"]),
    ("step10-responsive", ["responsive_dom_report.json"]),
    ("step11-save", ["content.html", "css/page.css"]),
    ("step12-report", ["pipeline_state.json"]),
]


def _exists(work: Path, rel: str) -> bool:
    path = work / rel
    if rel == "sections":
        return path.is_dir() and any(p.is_dir() for p in path.iterdir())
    return path.exists()


def _stage_status(work: Path):
    status = {}
    for stage, reqs in STAGES:
        missing = [r for r in reqs if not _exists(work, r)]
        status[stage] = {"ready": len(missing) == 0, "missing": missing}
    return status


def _guess_stage_from_state(state: dict):
    last = (state or {}).get("lastSuccessfulStage")
    mapping = {
        "extract_text": "step1-extract",
        "analyze_page": "step2-analyze-route",
        "section_analyze": "step3-section-analyze",
        "ocr_verify": "step4-ocr-verify",
        "generate_page": "step5-generate",
        "verify_layered": "step6-verify-hotspots",
        "refine_targeted": "step7-page-converge",
        "section_refine": "step8-section-refine",
        "final_verify": "step9-final-verify",
        "responsive_checks": "step10-responsive",
        "save_page": "step11-save",
        "report": "step12-report",
    }
    return mapping.get(last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--start-from", default=None)
    args = ap.parse_args()

    work = Path(args.work_dir).resolve()
    state = load_state(str(work))
    status = _stage_status(work)

    stage_names = [s for s, _ in STAGES]
    selected = args.start_from or _guess_stage_from_state(state) or "step1-extract"
    if selected not in stage_names:
        print(json.dumps({
            "ok": False,
            "error": f"Unknown stage '{selected}'",
            "validStages": stage_names,
        }, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    idx = stage_names.index(selected)
    blocked = []
    for prior in stage_names[:idx]:
        if not status[prior]["ready"]:
            blocked.append({"stage": prior, "missing": status[prior]["missing"]})

    print(json.dumps({
        "ok": len(blocked) == 0,
        "workDir": str(work),
        "selectedStage": selected,
        "canResume": len(blocked) == 0,
        "blockedBy": blocked,
        "stageStatus": status,
        "state": {
            "lastSuccessfulStage": state.get("lastSuccessfulStage"),
            "currentStage": state.get("currentStage"),
            "lastError": state.get("lastError"),
        },
        "runOrderFromSelected": stage_names[idx:],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
