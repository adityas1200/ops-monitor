"""SQL Guardrail — protects production data from accidental DDL/DML.

Three layers of protection:
  1. Classification: every SQL is classified as READ or WRITE (DDL/DML).
  2. Blocking: WRITE statements against production databases are blocked.
     Only clone/preprod databases are allowed for writes.
  3. Human-in-the-loop: blocked WRITE statements are logged as pending
     approval requests. Users must explicitly approve before execution.

All SQL sent to Snowflake is logged to backend/app/data/sql_audit_log.json.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import APP_DIR

_LOCK = threading.Lock()
_AUDIT_FILE = APP_DIR / "data" / "sql_audit_log.json"
_PENDING_FILE = APP_DIR / "data" / "sql_pending_approvals.json"

# DDL/DML keywords that indicate a WRITE operation
_WRITE_PATTERNS = re.compile(
    r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|MERGE|TRUNCATE|REPLACE|"
    r"COPY\s+INTO|PUT|REMOVE|GRANT|REVOKE|CALL)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Safe read-only patterns
_READ_PATTERNS = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN|LIST|WITH)\b",
    re.IGNORECASE,
)


def classify_sql(sql: str) -> str:
    """Classify SQL as 'READ' or 'WRITE'."""
    stripped = sql.strip()
    if _WRITE_PATTERNS.search(stripped):
        return "WRITE"
    if _READ_PATTERNS.match(stripped):
        return "READ"
    return "WRITE"


def is_safe_target(sql: str, allowed_databases: List[str]) -> bool:
    """Check if a WRITE SQL targets only allowed (non-production) databases.

    allowed_databases should contain clone/preprod database names (case-insensitive).
    """
    if not allowed_databases:
        return False
    sql_upper = sql.upper()
    allowed_upper = [db.upper() for db in allowed_databases if db]
    for db in allowed_upper:
        if db in sql_upper:
            return True
    return False


def check_sql(
    sql: str,
    production_databases: List[str],
    allowed_write_databases: List[str],
    source: str = "unknown",
) -> Dict[str, Any]:
    """Check if a SQL statement is safe to execute.

    Returns:
        {
            "allowed": True/False,
            "classification": "READ" | "WRITE",
            "reason": str,
            "requires_approval": True/False,
        }
    """
    classification = classify_sql(sql)

    if classification == "READ":
        return {
            "allowed": True,
            "classification": "READ",
            "reason": "Read-only query",
            "requires_approval": False,
        }

    # WRITE statement — check if it targets a safe database
    if is_safe_target(sql, allowed_write_databases):
        return {
            "allowed": True,
            "classification": "WRITE",
            "reason": f"Write allowed on clone/preprod database",
            "requires_approval": False,
        }

    # WRITE statement targeting production — BLOCK
    return {
        "allowed": False,
        "classification": "WRITE",
        "reason": "DDL/DML statement blocked — targets production data. Requires human approval.",
        "requires_approval": True,
    }


def log_sql(
    sql: str,
    source: str,
    classification: str,
    allowed: bool,
    result: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a SQL statement to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "classification": classification,
        "allowed": allowed,
        "sql": sql[:2000],
        "result": result,
    }
    with _LOCK:
        data = _read_json(_AUDIT_FILE, [])
        data.append(entry)
        # Keep last 500 entries
        if len(data) > 500:
            data = data[-500:]
        _write_json(_AUDIT_FILE, data)
    return entry


def submit_for_approval(
    sql: str,
    source: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Submit a blocked SQL for human approval."""
    entry = {
        "id": f"APPROVAL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "sql": sql[:4000],
        "status": "pending",
        "context": context or {},
    }
    with _LOCK:
        data = _read_json(_PENDING_FILE, [])
        data.append(entry)
        _write_json(_PENDING_FILE, data)
    return entry


def get_pending_approvals() -> List[Dict[str, Any]]:
    """Get all pending SQL approval requests."""
    return [e for e in _read_json(_PENDING_FILE, []) if e.get("status") == "pending"]


def approve_sql(approval_id: str, approved_by: str = "user") -> Optional[Dict[str, Any]]:
    """Approve a pending SQL statement."""
    with _LOCK:
        data = _read_json(_PENDING_FILE, [])
        for entry in data:
            if entry.get("id") == approval_id and entry.get("status") == "pending":
                entry["status"] = "approved"
                entry["approved_by"] = approved_by
                entry["approved_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(_PENDING_FILE, data)
                return entry
    return None


def reject_sql(approval_id: str, rejected_by: str = "user") -> Optional[Dict[str, Any]]:
    """Reject a pending SQL statement."""
    with _LOCK:
        data = _read_json(_PENDING_FILE, [])
        for entry in data:
            if entry.get("id") == approval_id and entry.get("status") == "pending":
                entry["status"] = "rejected"
                entry["rejected_by"] = rejected_by
                entry["rejected_at"] = datetime.now(timezone.utc).isoformat()
                _write_json(_PENDING_FILE, data)
                return entry
    return None


def get_audit_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent SQL audit log entries."""
    entries = _read_json(_AUDIT_FILE, [])
    return entries[-limit:]


def _read_json(path: Path, default: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
