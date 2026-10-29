"""DQ dashboard fetch bound and source KPIs (no editable Maximum rows)."""
import pytest
from pydantic import ValidationError

from app.connectors.dq_connector import DQConnector, _DQ_FETCH_HARD_CAP, kpis_from_checks
from app.core.config import get_dq_monitoring_config, validate_monitoring_settings
from app.models.schemas import DQMonitoringConfig


def test_dq_result_limit_defaults_to_500():
    # Legacy setting still loads for compatibility; dashboard fetch ignores it.
    cfg = get_dq_monitoring_config({"monitoring": {"dq": {}}})
    assert cfg["result_limit"] == 500


def test_dq_query_uses_date_range_count_not_settings_limit(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, sql, params):
            executed.append(sql)

        def fetchall(self):
            return []

        def fetchone(self):
            return (1684,)

    class Connection:
        def cursor(self):
            return Cursor()

    connector = DQConnector()
    connector.settings = {
        "snowflake": {
            "account": "account",
            "user": "user",
            "warehouse": "warehouse",
            "role": "role",
            "password": "password",
        },
        "monitoring": {
            "dq": {
                "table_fqn": "DB.SCHEMA.RESULTS",
                "result_limit": 600,
            },
        },
    }
    connector.sf._connect = lambda: Connection()
    monkeypatch.setattr(connector, "count_latest_results", lambda *a, **k: 1684)

    assert connector.read_results(revalidate=False, latest_only=True) == []
    assert any("LIMIT 1684" in sql for sql in executed)
    assert not any("LIMIT 600" in sql for sql in executed)


def test_dq_query_hard_caps_fetch(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, sql, params):
            executed.append(sql)

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

    connector = DQConnector()
    connector.settings = {
        "snowflake": {
            "account": "account",
            "user": "user",
            "warehouse": "warehouse",
            "role": "role",
            "password": "password",
        },
        "monitoring": {"dq": {"table_fqn": "DB.SCHEMA.RESULTS"}},
    }
    connector.sf._connect = lambda: Connection()
    monkeypatch.setattr(connector, "count_latest_results", lambda *a, **k: 50_000)

    connector.read_results(revalidate=False, latest_only=True)
    assert any(f"LIMIT {_DQ_FETCH_HARD_CAP}" in sql for sql in executed)


@pytest.mark.parametrize("value", [0, 10_001])
def test_dq_result_limit_rejects_out_of_range_values_when_provided(value):
    with pytest.raises(ValidationError):
        DQMonitoringConfig(result_limit=value)

    errors = validate_monitoring_settings({
        "dq": {"result_limit": value},
    })
    assert "Maximum DQ rows must be between 1 and 10000." in errors


def test_kpis_from_checks_counts_all_statuses():
    checks = [
        {"status": "SUCCESS"},
        {"status": "SUCCESS"},
        {"status": "FAILED"},
        {"status": "WARNING"},
        {"status": "SKIPPED"},
        {"status": "RUNNING"},
        {"status": "PASS"},
        {"status": "FAIL"},
    ]
    kpis = kpis_from_checks(checks)
    assert kpis["total"] == 8
    assert kpis["success"] == 3
    assert kpis["failed"] == 2
    assert kpis["warning"] == 1
    assert kpis["skipped"] == 1
    assert kpis["running"] == 1


def test_dq_summary_kpis_use_full_check_set(monkeypatch):
    connector = DQConnector()
    connector.settings = {
        "snowflake": {
            "account": "account",
            "user": "user",
            "warehouse": "warehouse",
            "role": "role",
            "password": "password",
        },
        "monitoring": {
            "dq": {
                "table_fqn": "DB.SCHEMA.RESULTS",
                "result_limit": 600,
            },
        },
    }
    monkeypatch.setattr(
        "app.connectors.dq_connector.get_cached_summary",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        connector,
        "read_results",
        lambda *a, **k: (
            [{"status": "SUCCESS"}] * 1500
            + [{"status": "FAILED"}] * 180
            + [{"status": "WARNING"}] * 4
        ),
    )
    monkeypatch.setattr(connector, "count_latest_results", lambda *a, **k: 1684)

    summary = connector.summary(revalidate=False, page=1, page_size=10)
    assert summary["inventory_count"] == 1684
    assert summary["match_count"] == 1684
    assert len(summary["table_checks"]) == 10
    assert summary["page"] == 1
    assert summary["page_size"] == 10
    assert summary["page_from"] == 1
    assert summary["page_to"] == 10
    assert summary["kpis"]["total"] == 1684
    assert summary["kpis"]["success"] == 1500
    assert summary["kpis"]["failed"] == 180
    assert summary["kpis"]["warning"] == 4
    assert summary["result_limit"] == 1684
    # Wire payload only includes a failing-row preview, not the full inventory.
    assert len(summary["all_checks"]) <= 30
    assert all(c["status"] == "FAILED" for c in summary["all_checks"])


def test_dq_cached_summary_reloads_when_rows_are_capped(monkeypatch):
    connector = DQConnector()
    connector.settings = {
        "monitoring": {"dq": {"table_fqn": "DB.SCHEMA.RESULTS", "result_limit": 600}},
    }
    monkeypatch.setattr(
        "app.connectors.dq_connector.get_cached_summary",
        lambda *_a, **_k: {
            "all_checks": [{"status": "SUCCESS"}] * 600,
            "kpis": {"total": 600, "success": 600},
            "result_limit": 600,
        },
    )
    monkeypatch.setattr(connector, "count_latest_results", lambda *a, **k: 1684)
    monkeypatch.setattr(
        connector,
        "read_results",
        lambda *a, **k: [{"status": "SUCCESS"}] * 1600 + [{"status": "FAILED"}] * 84,
    )

    summary = connector.summary(revalidate=False, page=1, page_size=10)
    assert summary["inventory_count"] == 1684
    assert summary["match_count"] == 1684
    assert len(summary["table_checks"]) == 10
    assert summary["kpis"]["total"] == 1684
    assert summary["kpis"]["failed"] == 84
    assert summary["kpis"]["success"] == 1600


def test_count_latest_results_has_no_limit():
    executed = []

    class Cursor:
        def execute(self, sql, params):
            executed.append(sql)

        def fetchone(self):
            return (1842,)

    class Connection:
        def cursor(self):
            return Cursor()

    connector = DQConnector()
    connector.settings = {
        "snowflake": {
            "account": "account",
            "user": "user",
            "warehouse": "warehouse",
            "role": "role",
            "password": "password",
        },
        "monitoring": {
            "dq": {"table_fqn": "CPH_DB_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY"},
        },
    }
    connector.sf._connect = lambda: Connection()

    assert connector.count_latest_results("2026-10-01", "2026-10-29") == 1842
    assert executed
    assert "LIMIT" not in executed[0].upper()
    assert "row_marker" in executed[0]
