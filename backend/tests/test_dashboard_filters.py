"""Backend Dashboard search / status / platform filters."""
from app.core.dashboard_filters import (
    filter_dq_checks,
    filter_task_pipelines,
    matches_dq_text,
    normalize_page,
    paginate_rows,
)
from app.connectors.dq_connector import DQConnector


def test_dq_numeric_search_is_exact_qc_id():
    checks = [
        {"name": "3", "status": "FAILED", "table_name": "A", "column_name": "X"},
        {"name": "23", "status": "FAILED", "table_name": "A", "column_name": "X"},
        {"name": "31", "status": "SUCCESS", "table_name": "B", "column_name": "Y"},
    ]
    assert [c["name"] for c in filter_dq_checks(checks, q="3")] == ["3"]
    assert matches_dq_text(checks[1], "3") is False


def test_dq_text_search_matches_subject_and_type():
    checks = [
        {"name": "1", "status": "FAILED", "table_name": "SALES", "column_name": "NULL_CHECK"},
        {"name": "2", "status": "SUCCESS", "table_name": "HR", "column_name": "PK"},
    ]
    assert [c["name"] for c in filter_dq_checks(checks, q="sales")] == ["1"]
    assert [c["name"] for c in filter_dq_checks(checks, q="null")] == ["1"]


def test_dq_status_filter_uses_aliases():
    checks = [
        {"name": "1", "status": "SUCCESS"},
        {"name": "2", "status": "PASS"},
        {"name": "3", "status": "FAILED"},
        {"name": "4", "status": "WARNING"},
    ]
    assert len(filter_dq_checks(checks, status="SUCCESS")) == 2
    assert len(filter_dq_checks(checks, status="FAILED")) == 1
    assert len(filter_dq_checks(checks, status="WARNING")) == 1


def test_task_platform_and_search_filter():
    rows = [
        {"name": "TASK_A", "platform": "snowflake", "status": "FAILED"},
        {"name": "job_b", "platform": "glue", "status": "SUCCESS"},
        {"name": "TASK_C", "platform": "snowflake", "status": "SUCCESS"},
    ]
    out = filter_task_pipelines(rows, platform="snowflake", q="task", status="ALL")
    assert [r["name"] for r in out] == ["TASK_A", "TASK_C"]
    out_failed = filter_task_pipelines(rows, platform="snowflake", status="FAILED")
    assert [r["name"] for r in out_failed] == ["TASK_A"]


def test_normalize_page_defaults():
    assert normalize_page(None, None) == (1, 10)
    assert normalize_page(2, 25) == (2, 25)
    assert normalize_page(0, 9999) == (1, 500)


def test_paginate_rows_defaults_to_first_ten():
    rows = [{"id": i} for i in range(1, 36)]
    page = paginate_rows(rows)
    assert page["page"] == 1
    assert page["page_size"] == 10
    assert page["match_count"] == 35
    assert page["page_from"] == 1
    assert page["page_to"] == 10
    assert [r["id"] for r in page["rows"]] == list(range(1, 11))


def test_paginate_rows_second_page():
    rows = [{"id": i} for i in range(1, 36)]
    page = paginate_rows(rows, page=2, page_size=10)
    assert page["page_from"] == 11
    assert page["page_to"] == 20
    assert [r["id"] for r in page["rows"]] == list(range(11, 21))


def test_dq_summary_attaches_paginated_table_checks(monkeypatch):
    connector = DQConnector()
    connector.settings = {
        "snowflake": {
            "account": "a", "user": "u", "warehouse": "w", "role": "r", "password": "p",
        },
        "monitoring": {"dq": {"table_fqn": "DB.SCHEMA.T"}},
    }
    inventory = [
        {"name": str(i), "status": "FAILED" if i % 2 else "SUCCESS",
         "table_name": "A", "column_name": "X"}
        for i in range(1, 26)
    ]
    monkeypatch.setattr(
        "app.connectors.dq_connector.get_cached_summary",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(connector, "read_results", lambda *a, **k: list(inventory))
    monkeypatch.setattr(connector, "count_latest_results", lambda *a, **k: 25)

    summary = connector.summary(revalidate=False, status="ALL", page=1, page_size=10)
    assert summary["kpis"]["total"] == 25
    assert summary["match_count"] == 25
    assert summary["page"] == 1
    assert summary["page_size"] == 10
    assert summary["page_from"] == 1
    assert summary["page_to"] == 10
    assert len(summary["table_checks"]) == 10
    # Wire payload ships failing preview, not the full inventory.
    assert all(c["status"] == "FAILED" for c in summary["all_checks"])
    assert summary["inventory_count"] == 25

    page2 = connector.summary(revalidate=False, status="FAILED", page=2, page_size=5)
    assert page2["match_count"] == 13
    assert page2["page"] == 2
    assert page2["page_from"] == 6
    assert page2["page_to"] == 10
    assert len(page2["table_checks"]) == 5
