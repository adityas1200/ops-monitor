"""Snowflake DQ validation summary reader for dashboard monitoring."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.connectors.snowflake_connector import SnowflakeConnector
from app.connectors.sql_guardrail import log_sql
from app.core.config import (get_dq_monitoring_config, load_settings,
                             platforms_configured, snowflake_configured)


def normalize_dq_status(value: Any) -> str:
    v = str(value or "").strip().upper()
    if v in ("PASS", "PASSED", "OK", "SUCCESS", "TRUE", "Y", "YES", "GREEN"):
        return "SUCCESS"
    if v in ("FAIL", "FAILED", "ERROR", "FALSE", "N", "NO", "RED"):
        return "FAILED"
    if v in ("WARN", "WARNING", "AMBER", "YELLOW"):
        return "DELAYED"
    if v in ("SKIP", "SKIPPED", "NA", "N/A", "NOT_APPLICABLE"):
        return "SKIPPED"
    if v in ("RUNNING", "IN_PROGRESS", "PENDING", "EXECUTING"):
        return "RUNNING"
    return v or "RUNNING"


def _format_run_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    s = str(value).strip()
    if len(s) == 15 and s[8] == "_":
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}:{s[13:15]}"
    return s


def _row_status(status: Any, pass_count: Any, fail_count: Any) -> str:
    if status is not None and str(status).strip():
        return normalize_dq_status(status)
    try:
        if fail_count is not None and int(fail_count) > 0:
            return "FAILED"
        if pass_count is not None and int(pass_count) > 0:
            return "SUCCESS"
    except (TypeError, ValueError):
        pass
    return "RUNNING"


def _detail_text(description: Any, pass_count: Any, fail_count: Any) -> Optional[str]:
    parts = []
    if pass_count is not None or fail_count is not None:
        parts.append(f"Pass: {pass_count or 0} / Fail: {fail_count or 0}")
    if description is not None and str(description).strip():
        parts.append(str(description).strip())
    return " · ".join(parts) if parts else None


class DQConnector:
    last_error: Optional[str] = None

    def __init__(self):
        self.sf = SnowflakeConnector()
        self.settings = load_settings()

    def _configured(self) -> bool:
        return snowflake_configured(self.settings.get("snowflake", {}))

    def read_results(self, date_from: Optional[str] = None,
                     date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        self.last_error = None
        if not self._configured():
            return []

        records: List[Dict[str, Any]] = []
        dq_cfg = get_dq_monitoring_config(self.settings)
        table_fqn = dq_cfg["table_fqn"]
        subject_area = dq_cfg["subject_area"]
        try:
            cur = self.sf._connect().cursor()
            sql = f"""
                SELECT
                    RUN_DATE,
                    QC_ID,
                    SUBJECT_AREA,
                    CHECK_TYPE,
                    PASS_COUNT,
                    FAIL_COUNT,
                    STATUS,
                    QC_DESCRIPTION
                FROM {table_fqn}
                WHERE SUBJECT_AREA = %s
            """
            params: List[Any] = [subject_area]
            if date_from:
                sql += " AND TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') >= %s::TIMESTAMP_LTZ"
                params.append(date_from)
            if date_to:
                sql += " AND TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') <= %s::TIMESTAMP_LTZ"
                params.append(date_to)
            sql += """
                ORDER BY TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') DESC NULLS LAST
                LIMIT 500
            """
            log_sql(sql[:300], "dq_connector:read_results", "READ", allowed=True)
            cur.execute(sql, params)
            for i, row in enumerate(cur.fetchall()):
                run_date, qc_id, subject_area, check_type, pass_count, fail_count, status, description = row
                qc_key = str(qc_id or f"check_{i}")
                records.append({
                    "id": f"dq_{qc_key}_{run_date}",
                    "name": qc_key,
                    "platform": "snowflake",
                    "status": _row_status(status, pass_count, fail_count),
                    "table_name": str(subject_area) if subject_area is not None else None,
                    "column_name": str(check_type) if check_type is not None else None,
                    "run_at": _format_run_date(run_date),
                    "error": _detail_text(description, pass_count, fail_count),
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "source": "live",
                    "raw": {
                        "RUN_DATE": run_date,
                        "QC_ID": qc_id,
                        "SUBJECT_AREA": subject_area,
                        "CHECK_TYPE": check_type,
                        "PASS_COUNT": pass_count,
                        "FAIL_COUNT": fail_count,
                        "STATUS": status,
                        "QC_DESCRIPTION": description,
                    },
                })
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.sf._conn = None
        return records

    def fetch_dq_rule_sql(self, qc_id: str, subject_area: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch DQ rule definition from the configured rules table for a given QC ID + Subject Area.

        Returns the row with actual SQL_CODE (longest, starting with SELECT/WITH).
        Multiple rows may exist per QC_ID — we pick the one with valid SQL.
        """
        if not self._configured():
            return None
        dq_cfg = get_dq_monitoring_config(self.settings)
        rules_table = dq_cfg.get("rules_table_fqn", "")
        if not rules_table:
            return None
        try:
            cur = self.sf._connect().cursor()
            if subject_area:
                cur.execute(
                    f"SELECT * FROM {rules_table}"
                    f" WHERE CAST(QC_ID AS VARCHAR) = %s AND SUBJECT_AREA = %s"
                    f" ORDER BY LENGTH(COALESCE(SQL_CODE, '')) DESC"
                    f" LIMIT 5",
                    (str(qc_id), subject_area),
                )
            else:
                cur.execute(
                    f"SELECT * FROM {rules_table}"
                    f" WHERE CAST(QC_ID AS VARCHAR) = %s"
                    f" ORDER BY LENGTH(COALESCE(SQL_CODE, '')) DESC"
                    f" LIMIT 5",
                    (str(qc_id),),
                )
            cols = [desc[0] for desc in cur.description]
            sql_col_idx = next((i for i, c in enumerate(cols) if c == "SQL_CODE"), None)
            best_row = None
            for row in cur.fetchall():
                if sql_col_idx is not None and row[sql_col_idx]:
                    sql_val = str(row[sql_col_idx]).strip()
                    if sql_val.upper().startswith(("SELECT", "WITH")):
                        best_row = row
                        break
                if best_row is None:
                    best_row = row
            if not best_row:
                return None
            return {col: (str(val) if val is not None else None) for col, val in zip(cols, best_row)}
        except Exception:  # noqa: BLE001
            return None

    def execute_dq_sql(self, sql_code: str, database: Optional[str] = None,
                       schema: Optional[str] = None,
                       warehouse: Optional[str] = None) -> Dict[str, Any]:
        """Execute a DQ check SQL and return the results.

        This is read-only — the SQL_CODE from config tables are SELECT statements.
        Uses a read-only role to ensure no write side-effects.
        Sets database/schema/warehouse context before executing so unqualified
        object references resolve correctly.
        Returns columns and rows, or an error message.
        """
        if not self._configured():
            return {"executed": False, "error": "Snowflake not configured", "columns": [], "rows": []}
        if not sql_code or not sql_code.strip():
            return {"executed": False, "error": "No SQL code provided", "columns": [], "rows": []}
        try:
            cur = self.sf._connect().cursor()
            sf_cfg = self.settings.get("snowflake", {})
            read_only_role = sf_cfg.get("read_only_role", "")
            if read_only_role:
                cur.execute(f"USE ROLE {read_only_role}")
            if warehouse:
                cur.execute(f"USE WAREHOUSE {warehouse}")
            if database:
                cur.execute(f"USE DATABASE {database}")
            if schema:
                cur.execute(f"USE SCHEMA {schema}")
            log_sql(sql_code[:300], "dq_connector:execute_dq_sql", "READ", allowed=True)
            cur.execute(sql_code)
            columns = [desc[0] for desc in cur.description]
            rows = []
            for row in cur.fetchmany(100):
                rows.append({col: (str(val) if val is not None else None) for col, val in zip(columns, row)})
            result = {"executed": True, "columns": columns, "rows": rows, "row_count": len(rows)}
            if read_only_role:
                original_role = sf_cfg.get("role", "")
                if original_role:
                    cur.execute(f"USE ROLE {original_role}")
            return result
        except Exception as e:  # noqa: BLE001
            return {"executed": False, "error": str(e), "columns": [], "rows": []}

    def execute_dq_rule(self, qc_id: str, subject_area: Optional[str] = None,
                        limit: int = 20) -> Dict[str, Any]:
        """Fetch and execute a DQ rule SQL to get actual failing rows.

        Returns execution results including sample rows, count, and the SQL used.
        """
        rule = self.fetch_dq_rule_sql(qc_id, subject_area)
        if not rule:
            return {"executed": False, "error": "No DQ rule SQL found", "columns": [], "rows": [],
                    "rule_sql": None}
        sql_code = rule.get("SQL_CODE", "")
        if not sql_code or not sql_code.strip():
            return {"executed": False, "error": "Rule has no SQL_CODE", "columns": [], "rows": [],
                    "rule_sql": None, "rule_meta": rule}

        limited_sql = f"SELECT * FROM ({sql_code.rstrip(';')}) _dq_sub LIMIT {limit}"

        dq_cfg = get_dq_monitoring_config(self.settings)
        fqn_parts = dq_cfg["table_fqn"].split(".")
        ctx_db = fqn_parts[0] if len(fqn_parts) >= 3 else None
        ctx_schema = fqn_parts[1] if len(fqn_parts) >= 3 else None
        sf_cfg = self.settings.get("snowflake", {})
        wh = sf_cfg.get("warehouse")

        result = self.execute_dq_sql(limited_sql, database=ctx_db, schema=ctx_schema, warehouse=wh)
        result["rule_sql"] = sql_code
        result["rule_meta"] = {k: v for k, v in rule.items() if k != "SQL_CODE"}
        return result

    def summary(self, date_from: Optional[str] = None,
                date_to: Optional[str] = None) -> Dict[str, Any]:
        checks = self.read_results(date_from, date_to)
        kpis = {"success": 0, "failed": 0, "delayed": 0, "skipped": 0, "running": 0, "total": len(checks)}
        key = {"SUCCESS": "success", "FAILED": "failed", "DELAYED": "delayed",
               "SKIPPED": "skipped", "RUNNING": "running"}
        for c in checks:
            kpis[key.get(c["status"], "running")] += 1

        errors = []
        if self.last_error:
            errors.append({"platform": "snowflake", "error": self.last_error})

        dq_cfg = get_dq_monitoring_config(self.settings)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "live_only": True,
            "table": dq_cfg["table_fqn"],
            "subject_area": dq_cfg["subject_area"],
            "platforms": platforms_configured(self.settings),
            "kpis": kpis,
            "all_checks": checks,
            "errors": errors,
        }
