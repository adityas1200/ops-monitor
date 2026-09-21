"""Normalize platform-specific run states to dashboard status codes."""
from __future__ import annotations
from typing import Any

_STATUS_MAP = {
    "SUCCEEDED": "SUCCESS",
    "SUCCESS": "SUCCESS",
    "FAILED": "FAILED",
    "FAILED_AND_AUTO_SUSPENDED": "FAILED",
    "FAILED_AND_SKIPPED": "FAILED",
    "TIMEOUT": "FAILED",
    "SKIPPED": "SKIPPED",
    "SKIP": "SKIPPED",
    "CANCELLED": "SKIPPED",
    "CANCELED": "SKIPPED",
    "EXECUTING": "RUNNING",
    "RUNNING": "RUNNING",
    "SCHEDULED": "SCHEDULED",
    "PASS": "SUCCESS",
    "FAIL": "FAILED",
}

# Snowflake WHEN clause not met — not a failure; skill: SKIPPED = condition false.
_SKIP_ERROR_MARKERS = (
    "conditional expression for task evaluated to false",
)


def _is_condition_skip(error: Any = None) -> bool:
    err = str(error or "").strip().lower()
    return any(m in err for m in _SKIP_ERROR_MARKERS)


def normalize_task_result(task_result: Any, state: Any = None, *, done: Any = None, error: Any = None) -> str:
    """Map TASK_RESULT / STATE from Snowflake task monitoring to dashboard status codes."""
    if _is_condition_skip(error):
        return "SKIPPED"
    # Prefer STATE over coarse TASK_RESULT buckets (OTHER used to hide SKIPPED).
    for raw in (state, task_result):
        state_u = str(raw or "").strip().upper()
        if not state_u or state_u == "OTHER":
            continue
        if state_u in _STATUS_MAP:
            return _STATUS_MAP[state_u]
        if "FAIL" in state_u:
            return "FAILED"
        if state_u in ("PASS", "PASSED", "OK"):
            return "SUCCESS"
        if "SKIP" in state_u:
            return "SKIPPED"
    return normalize_run_status(state, done=done, error=error)


def normalize_run_status(state: Any, *, done: Any = None, error: Any = None) -> str:
    """Map vendor run state (+ optional completion signals) to SUCCESS/FAILED/DELAYED/SKIPPED/RUNNING."""
    if _is_condition_skip(error):
        return "SKIPPED"
    state_u = str(state or "").strip().upper()
    if state_u in _STATUS_MAP:
        return _STATUS_MAP[state_u]
    if "SKIP" in state_u:
        return "SKIPPED"
    if done is not None and not error:
        return "SUCCESS"
    if error:
        return "FAILED"
    return "RUNNING"
