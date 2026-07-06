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
    "CANCELLED": "SKIPPED",
    "EXECUTING": "RUNNING",
    "RUNNING": "RUNNING",
    "SCHEDULED": "RUNNING",
    "PASS": "SUCCESS",
    "FAIL": "FAILED",
    "OTHER": "RUNNING",
}


def normalize_task_result(task_result: Any, state: Any = None, *, done: Any = None, error: Any = None) -> str:
    """Map TASK_RESULT / STATE from Snowflake task monitoring to dashboard status codes."""
    raw = task_result if task_result is not None and str(task_result).strip() else state
    state_u = str(raw or "").strip().upper()
    if state_u in _STATUS_MAP:
        return _STATUS_MAP[state_u]
    if "FAIL" in state_u:
        return "FAILED"
    if state_u in ("PASS", "PASSED", "OK"):
        return "SUCCESS"
    return normalize_run_status(state, done=done, error=error)


def normalize_run_status(state: Any, *, done: Any = None, error: Any = None) -> str:
    """Map vendor run state (+ optional completion signals) to SUCCESS/FAILED/DELAYED/SKIPPED/RUNNING."""
    state_u = str(state or "").strip().upper()
    if state_u in _STATUS_MAP:
        return _STATUS_MAP[state_u]
    if done is not None and not error:
        return "SUCCESS"
    if error:
        return "FAILED"
    return "RUNNING"
