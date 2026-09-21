"""Unit tests for task/run status normalization."""
from app.connectors.status import normalize_run_status, normalize_task_result


def test_condition_false_is_skipped_not_running():
    status = normalize_task_result(
        "OTHER",
        "SKIPPED",
        done="2026-09-20T13:07:32",
        error="Conditional expression for task evaluated to false.",
    )
    assert status == "SKIPPED"


def test_condition_false_error_alone_is_skipped():
    status = normalize_task_result(
        "OTHER",
        None,
        done="2026-09-20T13:07:32",
        error="Conditional expression for task evaluated to false.",
    )
    assert status == "SKIPPED"


def test_skipped_state_preferred_over_other_result():
    assert normalize_task_result("OTHER", "SKIPPED") == "SKIPPED"


def test_real_failure_still_failed():
    assert normalize_task_result("FAIL", "FAILED", error="Division by zero") == "FAILED"
    assert normalize_run_status("FAILED", error="boom") == "FAILED"


def test_success_and_running():
    assert normalize_task_result("PASS", "SUCCEEDED") == "SUCCESS"
    assert normalize_task_result("RUNNING", "EXECUTING") == "RUNNING"


def test_scheduled_is_scheduled_not_running():
    assert normalize_task_result("SCHEDULED", "SCHEDULED") == "SCHEDULED"
    assert normalize_run_status("SCHEDULED") == "SCHEDULED"
