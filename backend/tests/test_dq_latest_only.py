"""Latest-per-check dedupe for DQ dashboard."""
from app.connectors.dq_connector import keep_latest_per_check


def test_keep_latest_per_check_one_per_qc_and_subject():
    rows = [
        {"name": "17", "table_name": "Lynkuet LAAD", "run_at": "2026-07-31 07:39:44", "id": "a"},
        {"name": "17", "table_name": "Lynkuet LAAD", "run_at": "2026-07-24 07:30:44", "id": "b"},
        {"name": "17", "table_name": "Kerendia Validations", "run_at": "2026-07-19 23:31:46", "id": "c"},
        {"name": "3", "table_name": "Lynkuet LAAD", "run_at": "2026-07-24 06:14:52", "id": "d"},
        {"name": "3", "table_name": "Lynkuet LAAD", "run_at": "2026-07-20 02:00:49", "id": "e"},
    ]
    out = keep_latest_per_check(rows)
    assert [r["id"] for r in out] == ["a", "c", "d"]


def test_keep_latest_per_check_empty():
    assert keep_latest_per_check([]) == []
