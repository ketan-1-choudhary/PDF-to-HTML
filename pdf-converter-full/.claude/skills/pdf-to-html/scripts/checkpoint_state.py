#!/usr/bin/env python3
"""checkpoint_state.py

Lightweight JSON checkpoint state manager for page-level pipeline progress.
All scripts in this skill can update <work-dir>/pipeline_state.json so runs can
resume from the last successful stage.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(work_dir: str) -> str:
    return os.path.join(work_dir, "pipeline_state.json")


def _safe_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not details:
        return {}
    out: Dict[str, Any] = {}
    for k, v in details.items():
        try:
            json.dumps(v)
            out[k] = v
        except Exception:
            out[k] = str(v)
    return out


def load_state(work_dir: str) -> Dict[str, Any]:
    path = _state_path(work_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    now = _ts()
    return {
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
        "currentStage": None,
        "lastSuccessfulStage": None,
        "lastError": None,
        "stages": {},
    }


def save_state(work_dir: str, state: Dict[str, Any]) -> str:
    os.makedirs(work_dir, exist_ok=True)
    path = _state_path(work_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return path


def checkpoint_update(
    work_dir: str,
    stage: str,
    status: str,
    details: Optional[Dict[str, Any]] = None,
    next_action: Optional[str] = None,
) -> str:
    state = load_state(work_dir)
    now = _ts()

    stages = state.setdefault("stages", {})
    stage_rec = stages.get(stage, {})
    if "startedAt" not in stage_rec:
        stage_rec["startedAt"] = now

    stage_rec.update(
        {
            "status": status,
            "updatedAt": now,
        }
    )
    safe = _safe_details(details)
    if safe:
        stage_rec["details"] = safe
    if next_action:
        stage_rec["nextAction"] = next_action

    stages[stage] = stage_rec
    state["stages"] = stages
    state["currentStage"] = stage
    state["updatedAt"] = now

    if status == "completed":
        state["lastSuccessfulStage"] = stage
        state["lastError"] = None
    elif status == "failed":
        state["lastError"] = {
            "stage": stage,
            "at": now,
            "details": safe,
        }

    if next_action:
        state["nextAction"] = next_action

    return save_state(work_dir, state)
