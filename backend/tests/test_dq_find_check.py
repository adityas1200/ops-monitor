"""DQ check id normalization and lookup."""
from datetime import datetime

from app.connectors.dq_connector import find_dq_check, make_dq_check_id, parse_dq_check_id


def test_parse_dq_check_id():
    assert parse_dq_check_id("dq_80_2026-09-18 00:18:45") == ("80", "2026-09-18 00:18:45")
    assert parse_dq_check_id("dq_80_2026-09-18T00:18:45") == ("80", "2026-09-18 00:18:45")
    assert parse_dq_check_id("dq_ab_c_2026-09-18 00:18:45") == ("ab_c", "2026-09-18 00:18:45")
    assert parse_dq_check_id("not-a-dq") is None


def test_make_dq_check_id_normalizes_space_and_t():
    assert make_dq_check_id("80", "2026-09-18 00:18:45") == "dq_80_2026-09-18 00:18:45"
    assert make_dq_check_id("80", "2026-09-18T00:18:45") == "dq_80_2026-09-18 00:18:45"
    assert make_dq_check_id("80", "2026-09-18 00:18:45.123456") == "dq_80_2026-09-18 00:18:45"
    assert make_dq_check_id("80", datetime(2026, 9, 18, 0, 18, 45)) == "dq_80_2026-09-18 00:18:45"


def test_find_dq_check_exact_and_normalized_run():
    checks = [{
        "id": "dq_80_2026-09-18 00:18:45",
        "name": "80",
        "run_at": "2026-09-18T00:18:45",
        "table_name": "MODEL_N_L1_CHECKS",
    }]
    assert find_dq_check(checks, "dq_80_2026-09-18 00:18:45")["name"] == "80"
    assert find_dq_check(checks, "dq_80_2026-09-18T00:18:45")["name"] == "80"


def test_find_dq_check_does_not_guess_by_qc_alone():
    """Same QC, different run — must not bind the wrong row."""
    checks = [{
        "id": "dq_80_2026-09-17 01:00:00",
        "name": "80",
        "run_at": "2026-09-17 01:00:00",
    }]
    assert find_dq_check(checks, "dq_80_2026-09-18 00:18:45") is None


def test_find_dq_check_missing():
    assert find_dq_check([], "dq_80_2026-09-18 00:18:45") is None
    assert find_dq_check(
        [{"id": "dq_81_2026-09-18 00:18:45", "name": "81", "run_at": "2026-09-18 00:18:45"}],
        "dq_80_2026-09-18 00:18:45",
    ) is None
