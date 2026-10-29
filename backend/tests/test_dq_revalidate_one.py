"""Single-check DQ revalidate API helper."""
from app.connectors.dq_connector import DQConnector


def test_revalidate_one_requires_qc_id():
    connector = DQConnector()
    out = connector.revalidate_one("")
    assert out["ok"] is False
    assert "qc_id" in (out.get("error") or "").lower()


def test_revalidate_one_applies_live_success():
    connector = DQConnector()
    connector.revalidate_check = lambda check: {
        "live_status": "SUCCESS",
        "failing_count": 0,
        "row_count": 4,
        "executed": True,
        "error": None,
    }
    out = connector.revalidate_one(
        "42",
        "Lynkuet LAAD",
        recorded_status="FAILED",
        check_id="dq-42",
        description="row count",
    )
    assert out["ok"] is True
    assert out["qc_id"] == "42"
    assert out["subject_area"] == "Lynkuet LAAD"
    assert out["status"] == "SUCCESS"
    assert out["stale"] is True
    assert out["fail_count"] == 0
