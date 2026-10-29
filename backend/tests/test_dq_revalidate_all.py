"""Live revalidate: Default = all failures; Custom = capped (1…200)."""
from app.connectors.dq_connector import DQConnector
from app.core.config import DQ_REVALIDATE_CUSTOM_MAX, validate_monitoring_settings


def _fake_rv(check):
    return {
        "live_status": "FAILED",
        "failing_count": 1,
        "row_count": 1,
        "executed": True,
        "error": None,
    }


def test_revalidate_failures_runs_all_failing_checks_when_unlimited():
    connector = DQConnector()
    records = [
        {"name": f"qc_{i}", "status": "FAILED", "table_name": "SA", "raw": {}}
        for i in range(12)
    ]
    records.append({"name": "ok", "status": "SUCCESS", "table_name": "SA", "raw": {}})
    called = []
    connector.revalidate_check = lambda c: (called.append(c["name"]) or _fake_rv(c))
    connector._revalidate_failures(records, limit=None)

    assert called == [f"qc_{i}" for i in range(12)]
    assert records[-1].get("live_checked") is None


def test_revalidate_failures_respects_custom_limit():
    connector = DQConnector()
    records = [
        {"name": f"qc_{i}", "status": "FAILED", "table_name": "SA", "raw": {}}
        for i in range(10)
    ]
    called = []
    connector.revalidate_check = lambda c: (called.append(c["name"]) or _fake_rv(c))
    connector._revalidate_failures(records, limit=3)
    assert called == ["qc_0", "qc_1", "qc_2"]


def test_configured_revalidate_limit_from_settings():
    connector = DQConnector()
    connector.settings = {
        "monitoring": {
            "dq": {
                "table_fqn": "DB.S.T",
                "revalidate_mode": "custom",
                "revalidate_limit": 7,
            },
        },
    }
    assert connector._configured_revalidate_limit() == 7
    connector.settings["monitoring"]["dq"]["revalidate_mode"] = "default"
    assert connector._configured_revalidate_limit() is None


def test_custom_revalidate_limit_validation():
    base = {"dq": {"table_fqn": "DB.S.T", "revalidate_mode": "custom", "revalidate_limit": 50}}
    assert validate_monitoring_settings(base) == []
    bad = {"dq": {"table_fqn": "DB.S.T", "revalidate_mode": "custom", "revalidate_limit": 201}}
    errs = validate_monitoring_settings(bad)
    assert any("1 and 200" in e or str(DQ_REVALIDATE_CUSTOM_MAX) in e for e in errs)
    zero = {"dq": {"table_fqn": "DB.S.T", "revalidate_mode": "custom", "revalidate_limit": 0}}
    assert validate_monitoring_settings(zero)
