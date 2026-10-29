"""RCA knowledge markdown parse / append / update / delete."""
from pathlib import Path

import pytest

from app.core.rca_knowledge_store import (
    append_rule,
    delete_rule,
    parse_rules,
    read_rules,
    update_rule,
)

SAMPLE = """# RCA Knowledge Base (User-contributed)

## Format

---
PATTERN: example template pattern
CATEGORY: Code Failure
ROOT_CAUSE: template only
FIX: do not treat as a real rule
ADDED_BY: docs
ADDED_ON: 2026-01
NOTES: format example
---

## Rules

---
PATTERN: timeout warehouse
CATEGORY: Infrastructure Failure
ROOT_CAUSE: statement timeout
FIX: enlarge warehouse
ADDED_BY: ops
ADDED_ON: 2026-06
NOTES: seen on Kerendia
---

---
PATTERN: null join key
CATEGORY: Data Quality Failure
ROOT_CAUSE: missing xref
FIX: waterfall join
ADDED_BY: user
ADDED_ON: 2026-07
NOTES:
---
"""


def test_parse_rules_skips_format_example():
    rules = parse_rules(SAMPLE)
    assert len(rules) == 2
    assert rules[0]["PATTERN"] == "timeout warehouse"
    assert rules[1]["PATTERN"] == "null join key"
    assert all(r["PATTERN"] != "example template pattern" for r in rules)


def test_append_and_update_rule(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    path.write_text(SAMPLE, encoding="utf-8")

    appended = append_rule(path, {
        "pattern": "new spill pattern",
        "category": "Infrastructure Failure",
        "root_cause": "remote spill",
        "fix": "cluster keys",
        "notes": "from UI",
        "added_by": "user",
    })
    assert appended["PATTERN"] == "new spill pattern"
    rules = read_rules(path)
    assert len(rules) == 3
    assert rules[-1]["FIX"] == "cluster keys"
    # Format example still present in file, not in rules
    assert "example template pattern" in path.read_text(encoding="utf-8")
    assert rules[0]["PATTERN"] == "timeout warehouse"

    updated = update_rule(path, 0, {
        "pattern": "timeout warehouse",
        "category": "Infrastructure Failure",
        "root_cause": "updated root cause",
        "fix": "retry off-peak",
        "notes": "edited",
        "added_by": "user",
    })
    assert updated["ROOT_CAUSE"] == "updated root cause"
    assert updated["FIX"] == "retry off-peak"
    assert updated["ADDED_ON"] == "2026-06"  # preserved
    rules = read_rules(path)
    assert rules[0]["ROOT_CAUSE"] == "updated root cause"
    assert rules[1]["PATTERN"] == "null join key"


def test_sql_code_multiline_round_trip(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    path.write_text(SAMPLE, encoding="utf-8")
    sql = "SELECT id\nFROM t\nWHERE status = 'fixed'"
    appended = append_rule(path, {
        "pattern": "sql fix pattern",
        "category": "Code Failure",
        "root_cause": "bad filter",
        "fix": "correct WHERE clause",
        "sql_code": sql,
        "added_by": "user",
    })
    assert appended["SQL_CODE"] == sql
    raw = path.read_text(encoding="utf-8")
    assert "\\n" in raw
    assert "SELECT id\\nFROM t\\nWHERE status = 'fixed'" in raw
    rules = read_rules(path)
    assert rules[-1]["SQL_CODE"] == sql


def test_delete_rule(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    path.write_text(SAMPLE, encoding="utf-8")
    removed = delete_rule(path, 0)
    assert removed["PATTERN"] == "timeout warehouse"
    rules = read_rules(path)
    assert len(rules) == 1
    assert rules[0]["PATTERN"] == "null join key"
    assert "timeout warehouse" not in path.read_text(encoding="utf-8")


def test_delete_out_of_range(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    path.write_text(SAMPLE, encoding="utf-8")
    with pytest.raises(IndexError):
        delete_rule(path, 99)


def test_update_out_of_range(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    path.write_text(SAMPLE, encoding="utf-8")
    with pytest.raises(IndexError):
        update_rule(path, 99, {"pattern": "x"})


def test_append_requires_pattern(tmp_path: Path):
    path = tmp_path / "rca_knowledge.md"
    with pytest.raises(ValueError, match="pattern"):
        append_rule(path, {"root_cause": "no pattern"})
