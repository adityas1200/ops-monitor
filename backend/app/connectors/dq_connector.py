"""Snowflake DQ validation summary reader for dashboard monitoring."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Cap live revalidation on dashboard so a backlog of failures can't hang the UI.
# Full live truth for a single check still happens in View Details / RCA.
_MAX_LIVE_REVALIDATE = 8
_CACHE_FILE = "dq_revalidate_cache.json"
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes

# Column-name hints used to read a Pass/Fail verdict out of a DQ rule's result set.
_RESULT_COLS = ("RESULT", "QC_RESULT", "PASS_FAIL", "CHECK_RESULT", "DQ_RESULT")
_COUNT_COLS = ("RESULT_COUNT", "RESULT_CNT", "FAIL_COUNT", "FAILED_COUNT",
               "MISMATCH_COUNT", "VIOLATION_COUNT", "EXCEPTION_COUNT")
_PASS_VALUES = ("PASS", "PASSED", "SUCCESS", "OK", "TRUE", "Y", "YES", "0")
_FAIL_VALUES = ("FAIL", "FAILED", "ERROR", "FALSE", "N", "NO")

# Write/DDL keywords blocked in ad-hoc diagnostic queries (defence in depth on top
# of the read-only role) so an LLM-derived drill-down query can only ever read.
_WRITE_KEYWORD_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"CALL|COPY|PUT|REMOVE|UNLOAD|EXECUTE|USE)\b",
    re.IGNORECASE,
)


def _has_write_keyword(sql: str) -> bool:
    return bool(_WRITE_KEYWORD_RE.search(sql or ""))

from app.connectors.snowflake_connector import SnowflakeConnector
from app.connectors.sql_guardrail import log_sql
from app.core.config import (MEMORY_DIR, get_dq_monitoring_config, load_settings,
                             platforms_configured, snowflake_configured)


def _cache_path() -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return MEMORY_DIR / _CACHE_FILE


def _cache_key(date_from: Optional[str], date_to: Optional[str]) -> str:
    # Suffix invalidates older caches that stored every historical run in the window.
    return f"{date_from or ''}|{date_to or ''}|latest1"


def keep_latest_per_check(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the newest run per (QC_ID, subject area). Input must be newest-first."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for rec in records:
        key = (str(rec.get("name") or ""), str(rec.get("table_name") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def _load_revalidate_cache() -> Dict[str, Any]:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_revalidate_cache(data: Dict[str, Any]) -> None:
    """Atomically write cache JSON so partial writes cannot leave an empty file."""
    p = _cache_path()
    tmp = p.with_suffix(".json.tmp")
    text = json.dumps(data, indent=2, default=str)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def write_full_summary_cache(
    date_from: Optional[str],
    date_to: Optional[str],
    response: Dict[str, Any],
) -> str:
    """Persist the full live DQ summary response for this date window. Returns updated_at ISO."""
    import logging
    log = logging.getLogger(__name__)
    updated_at = datetime.now(timezone.utc).isoformat()
    key = _cache_key(date_from, date_to)
    payload = dict(response)
    payload["status_mode"] = "cached_live"
    payload["revalidated"] = False
    payload["cache_updated_at"] = updated_at
    payload["date_from"] = date_from
    payload["date_to"] = date_to
    entry = {
        "date_from": date_from,
        "date_to": date_to,
        "updated_at": updated_at,
        "response": payload,
    }
    try:
        with _CACHE_LOCK:
            store = _load_revalidate_cache()
            store[key] = entry
            _save_revalidate_cache(store)
        log.info("DQ revalidate cache saved key=%s path=%s checks=%s",
                 key, _cache_path(), len(payload.get("all_checks") or []))
    except Exception:
        log.exception("Failed to write DQ revalidate cache to %s", _cache_path())
        raise
    return updated_at


def _parse_cache_ts(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_cached_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return cached live summary only when date range matches and TTL (30m) is valid."""
    key = _cache_key(date_from, date_to)
    with _CACHE_LOCK:
        entry = _load_revalidate_cache().get(key)
    if not isinstance(entry, dict):
        return None
    # Strict date-range match (also encoded in the key; re-check stored fields).
    if (entry.get("date_from") or None) != (date_from or None):
        return None
    if (entry.get("date_to") or None) != (date_to or None):
        return None
    updated = _parse_cache_ts(entry.get("updated_at"))
    if updated is None:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - updated).total_seconds()
    if age_s < 0 or age_s > _CACHE_TTL_SECONDS:
        return None
    response = entry.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("all_checks"), list):
        return None
    out = dict(response)
    out["status_mode"] = "cached_live"
    out["revalidated"] = False
    out["cache_updated_at"] = entry.get("updated_at") or out.get("cache_updated_at")
    out["date_from"] = date_from
    out["date_to"] = date_to
    out["from_cache"] = True
    out["cache_ttl_seconds"] = _CACHE_TTL_SECONDS
    return out


def _infer_schema_from_sql(sql: str) -> tuple:
    """Extract database.schema from fully-qualified references in SQL.

    Uses majority-vote: finds the most common DB.SCHEMA pair among all
    three-part references, excluding known data-share databases (contain 'SHARE').
    Returns (database, schema) or (None, None) if not found.
    """
    matches = re.findall(r'(\w+)\.(\w+)\.\w+', sql.upper())
    if not matches:
        return None, None
    # Filter out data share references and count occurrences
    counts: dict = {}
    for db, schema in matches:
        if "SHARE" in db:
            continue
        key = (db, schema)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None, None
    # Return the most common pair
    best = max(counts, key=counts.get)
    return best[0], best[1]


def normalize_dq_status(value: Any) -> str:
    v = str(value or "").strip().upper()
    # Composite statuses from DQ engines (e.g. "PASS (WARNING)") → WARNING
    if "WARN" in v or v in ("AMBER", "YELLOW"):
        return "WARNING"
    if v in ("PASS", "PASSED", "OK", "SUCCESS", "TRUE", "Y", "YES", "GREEN"):
        return "SUCCESS"
    if v in ("FAIL", "FAILED", "ERROR", "FALSE", "N", "NO", "RED"):
        return "FAILED"
    if v in ("SKIP", "SKIPPED", "NA", "N/A", "NOT_APPLICABLE"):
        return "SKIPPED"
    if v in ("RUNNING", "IN_PROGRESS", "PENDING", "EXECUTING"):
        return "RUNNING"
    if v == "DELAYED":
        # Legacy mapping — DQ "delayed" meant warning, not SLA delay
        return "WARNING"
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
    """Map recorded DQ engine status + pass/fail counts.

    Partial failures (pass > 0 and fail > 0) are WARNING — same idea as
    Snowflake's "Pass (Warning)" — even when the engine stored a bare "Fail".
    """
    try:
        pc = int(pass_count) if pass_count is not None else None
        fc = int(fail_count) if fail_count is not None else None
    except (TypeError, ValueError):
        pc, fc = None, None

    if pc is not None and fc is not None and pc > 0 and fc > 0:
        return "WARNING"

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


# Cache of discovered candidate rules tables, keyed by "CATALOG.SCHEMA|require_sa".
# Schemas rarely change, so a process-lifetime cache avoids repeat INFORMATION_SCHEMA scans.
_RULES_TABLE_CACHE: Dict[str, List[str]] = {}


class DQConnector:
    last_error: Optional[str] = None

    def __init__(self):
        self.sf = SnowflakeConnector()
        self.settings = load_settings()

    def _configured(self) -> bool:
        return snowflake_configured(self.settings.get("snowflake", {}))

    def read_results(self, date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     revalidate: bool = True,
                     latest_only: bool = False) -> List[Dict[str, Any]]:
        self.last_error = None
        if not self._configured():
            return []

        records: List[Dict[str, Any]] = []
        dq_cfg = get_dq_monitoring_config(self.settings)
        table_fqn = dq_cfg["table_fqn"]
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
                WHERE 1=1
            """
            params: List[Any] = []
            if date_from:
                sql += " AND TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') >= %s::TIMESTAMP_LTZ"
                params.append(date_from)
            if date_to:
                sql += " AND TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') <= %s::TIMESTAMP_LTZ"
                params.append(date_to)
            if latest_only:
                # One row per QC + subject area (newest RUN_DATE wins).
                sql += """
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY QC_ID, COALESCE(TO_VARCHAR(SUBJECT_AREA), '')
                    ORDER BY TRY_TO_TIMESTAMP(RUN_DATE, 'YYYYMMDD_HH24MISS') DESC NULLS LAST
                ) = 1
                """
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

        if latest_only and records:
            records = keep_latest_per_check(records)

        if revalidate and records:
            self._revalidate_failures(records)
        return records

    def fetch_dq_rule_sql(self, qc_id: str, subject_area: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch a DQ rule definition for a QC ID (+ Subject Area).

        Resolution order:
          1. The configured rules table (fast path — preserves prior behaviour).
          2. Every other config table in the same database/schema that exposes
             QC_ID / SQL_CODE columns. Each subject area keeps its rules in its own
             CONFIG_* table (e.g. CONFIG_LYNKUET, CONFIG_ALL_L2, ...), so this makes
             the lookup work for any subject area without hand-editing settings.

        Returns the best matching row (longest SQL_CODE that looks like a query),
        annotated with SOURCE_CONFIG_TABLE, or None if nothing matches anywhere.
        """
        if not self._configured():
            return None
        dq_cfg = get_dq_monitoring_config(self.settings)
        rules_table = dq_cfg.get("rules_table_fqn", "")

        # 1. Try the configured table first.
        if rules_table:
            rule = self._fetch_rule_from_table(rules_table, qc_id, subject_area)
            if rule:
                return rule

        # 2. Fall back to searching sibling config tables in the same DB.SCHEMA.
        catalog, schema = self._rules_namespace(rules_table)
        if not (catalog and schema):
            return None

        # Prefer a subject-area-scoped match (a QC_ID can be reused across brands);
        # only if that finds nothing do we fall back to matching QC_ID alone.
        search_modes = (True, False) if subject_area else (False,)
        for require_sa in search_modes:
            candidates = self._candidate_rules_tables(catalog, schema, require_sa)
            candidates = [t for t in candidates if t.upper() != (rules_table or "").upper()]
            if not candidates:
                continue
            found = self._locate_rule_table(
                qc_id, subject_area if require_sa else None, candidates)
            if found:
                rule = self._fetch_rule_from_table(
                    found, qc_id, subject_area if require_sa else None)
                if rule:
                    return rule
        return None

    @staticmethod
    def _rules_namespace(rules_table_fqn: str) -> "tuple[Optional[str], Optional[str]]":
        """Extract (CATALOG, SCHEMA) from a fully-qualified rules table name."""
        parts = [p for p in (rules_table_fqn or "").split(".") if p]
        if len(parts) >= 3:
            return parts[0].strip().upper(), parts[1].strip().upper()
        return None, None

    def _candidate_rules_tables(self, catalog: str, schema: str,
                                require_subject_area: bool) -> List[str]:
        """Discover config tables in {catalog}.{schema} that can hold DQ rules."""
        cache_key = f"{catalog}.{schema}|{require_subject_area}"
        if cache_key in _RULES_TABLE_CACHE:
            return _RULES_TABLE_CACHE[cache_key]
        needed = {"QC_ID", "SQL_CODE"}
        if require_subject_area:
            needed.add("SUBJECT_AREA")
        tables: List[str] = []
        try:
            cur = self.sf._connect().cursor()
            cur.execute(
                f"SELECT TABLE_NAME, COLUMN_NAME"
                f" FROM {catalog}.INFORMATION_SCHEMA.COLUMNS"
                f" WHERE TABLE_SCHEMA = %s"
                f"   AND COLUMN_NAME IN ('QC_ID', 'SUBJECT_AREA', 'SQL_CODE')",
                (schema,),
            )
            by_table: Dict[str, set] = {}
            for tname, cname in cur.fetchall():
                by_table.setdefault(tname, set()).add(cname)
            tables = [f"{catalog}.{schema}.{t}" for t, cols in by_table.items()
                      if needed <= cols]
        except Exception:  # noqa: BLE001
            tables = []
        _RULES_TABLE_CACHE[cache_key] = tables
        return tables

    def _locate_rule_table(self, qc_id: str, subject_area: Optional[str],
                           candidates: List[str]) -> Optional[str]:
        """Find which candidate table holds the QC using a single UNION ALL probe.

        Returns the table with the longest matching SQL_CODE (most complete rule).
        """
        if not candidates:
            return None
        parts: List[str] = []
        params: List[Any] = []
        for fqn in candidates:
            safe = fqn.replace("'", "''")
            if subject_area:
                parts.append(
                    f"SELECT '{safe}' AS SRC, LENGTH(COALESCE(SQL_CODE, '')) AS SQL_LEN"
                    f" FROM {fqn}"
                    f" WHERE CAST(QC_ID AS VARCHAR) = %s AND UPPER(SUBJECT_AREA) = UPPER(%s)"
                )
                params.extend([str(qc_id), subject_area])
            else:
                parts.append(
                    f"SELECT '{safe}' AS SRC, LENGTH(COALESCE(SQL_CODE, '')) AS SQL_LEN"
                    f" FROM {fqn}"
                    f" WHERE CAST(QC_ID AS VARCHAR) = %s"
                )
                params.append(str(qc_id))
        union = " UNION ALL ".join(parts)
        sql = (f"SELECT SRC, MAX(SQL_LEN) AS SQL_LEN FROM ({union})"
               f" GROUP BY SRC ORDER BY SQL_LEN DESC NULLS LAST LIMIT 1")
        try:
            cur = self.sf._connect().cursor()
            log_sql(f"DQ rule-table search across {len(candidates)} config table(s)",
                    "dq_connector:locate_rule_table", "READ", allowed=True)
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001
            return None

    def _fetch_rule_from_table(self, table_fqn: str, qc_id: str,
                               subject_area: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch the best QC rule row from a specific table.

        Picks the row with the longest SQL_CODE that looks like a query
        (SELECT/WITH). Subject-area matching is case-insensitive.
        """
        try:
            cur = self.sf._connect().cursor()
            if subject_area:
                cur.execute(
                    f"SELECT * FROM {table_fqn}"
                    f" WHERE CAST(QC_ID AS VARCHAR) = %s AND UPPER(SUBJECT_AREA) = UPPER(%s)"
                    f" ORDER BY LENGTH(COALESCE(SQL_CODE, '')) DESC"
                    f" LIMIT 5",
                    (str(qc_id), subject_area),
                )
            else:
                cur.execute(
                    f"SELECT * FROM {table_fqn}"
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
            rule = {col: (str(val) if val is not None else None)
                    for col, val in zip(cols, best_row)}
            rule.setdefault("SOURCE_CONFIG_TABLE", table_fqn)
            return rule
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

        def _run(db: Optional[str], sch: Optional[str]) -> Dict[str, Any]:
            # #region agent log
            _t0 = time.time()
            _nt = None
            try:
                _p = self.sf._connect_params() or {}
                _nt = _p.get("network_timeout")
                if _nt is None:
                    # Matches snowflake_connector._connect setdefault
                    _nt = 600
            except Exception:
                _nt = "unknown"
            # #endregion
            try:
                cur = self.sf._connect().cursor()
                sf_cfg = self.settings.get("snowflake", {})
                read_only_role = sf_cfg.get("read_only_role", "")
                if read_only_role:
                    cur.execute(f"USE ROLE {read_only_role}")
                if warehouse:
                    cur.execute(f"USE WAREHOUSE {warehouse}")
                if db:
                    cur.execute(f"USE DATABASE {db}")
                if sch:
                    cur.execute(f"USE SCHEMA {sch}")
                log_sql(sql_code[:300], "dq_connector:execute_dq_sql", "READ", allowed=True)
                cur.execute(sql_code)
                columns = [desc[0] for desc in cur.description]
                rows = []
                for row in cur.fetchmany(500):
                    rows.append({
                        col: (str(val) if val is not None else None)
                        for col, val in zip(columns, row)
                    })
                result = {
                    "executed": True, "columns": columns, "rows": rows,
                    "row_count": len(rows), "database": db, "schema": sch,
                }
                if read_only_role:
                    original_role = sf_cfg.get("role", "")
                    if original_role:
                        cur.execute(f"USE ROLE {original_role}")
                # #region agent log
                try:
                    _pair = []
                    for _r in rows[:4]:
                        _u = {str(k).upper(): v for k, v in (_r or {}).items()}
                        if "SOURCE_CNT" in _u or "TARGET_CNT" in _u:
                            _pair.append({"SOURCE_CNT": _u.get("SOURCE_CNT"),
                                          "TARGET_CNT": _u.get("TARGET_CNT")})
                    open(r"C:\Users\EKGAH\Documents\project\ops-monitor\debug-605d47.log", "a", encoding="utf-8").write(
                    json.dumps({"sessionId": "605d47", "hypothesisId": "A,C,D", "runId": "post-fix",
                                    "location": "dq_connector.py:execute_dq_sql",
                                    "message": "DQ SQL executed OK",
                                    "data": {"elapsed_s": round(time.time() - _t0, 2),
                                             "network_timeout": _nt, "warehouse": warehouse,
                                             "database": db, "schema": sch,
                                             "row_count": len(rows), "pair_samples": _pair,
                                             "sql_head": (sql_code or "")[:120]},
                                    "timestamp": int(time.time() * 1000)}) + "\n")
                except Exception:
                    pass
                # #endregion
                return result
            except Exception as e:  # noqa: BLE001
                # #region agent log
                try:
                    open(r"C:\Users\EKGAH\Documents\project\ops-monitor\debug-605d47.log", "a", encoding="utf-8").write(
                        json.dumps({"sessionId": "605d47", "hypothesisId": "A,D", "runId": "post-fix",
                                    "location": "dq_connector.py:execute_dq_sql",
                                    "message": "DQ SQL execute FAILED",
                                    "data": {"elapsed_s": round(time.time() - _t0, 2),
                                             "network_timeout": _nt, "warehouse": warehouse,
                                             "database": db, "schema": sch,
                                             "error": str(e)[:300],
                                             "is_timeout": "000604" in str(e) or "timeout" in str(e).lower(),
                                             "sql_head": (sql_code or "")[:120]},
                                    "timestamp": int(time.time() * 1000)}) + "\n")
                except Exception:
                    pass
                # #endregion
                return {
                    "executed": False, "error": str(e), "columns": [], "rows": [],
                    "database": db, "schema": sch,
                }

        result = _run(database, schema)
        err = (result.get("error") or "").upper()
        # QC 3-style rules: unqualified VW_* with CPH_DB_PROD.ENRICH_V2.* inside.
        # If first context was CONFIG/MODEL_V2, retry with schema inferred from SQL.
        if (not result.get("executed")
                and ("DOES NOT EXIST" in err or "NOT AUTHORIZED" in err or "002003" in err)):
            inferred_db, inferred_schema = _infer_schema_from_sql(sql_code)
            if inferred_schema and (
                inferred_schema != (schema or "") or (inferred_db or "") != (database or "")
            ):
                retry = _run(inferred_db or database, inferred_schema)
                if retry.get("executed") or retry.get("error"):
                    return retry
        return result

    def resolve_rule_context(self, rule: Dict[str, Any], sql_code: str) -> Tuple[
            Optional[str], Optional[str], Optional[str]]:
        """Resolve (database, schema, warehouse) for executing a rule's SQL.

        Shared by the dashboard/RCA verdict path AND the "View Details" endpoint so
        the rule always runs in the SAME session context and yields the SAME verdict.

        Priority for database/schema:
          1) Inferred from fully-qualified refs in SQL (e.g. CPH_DB_PROD.ENRICH_V2.*)
          2) Rule metadata DATABASE/SCHEMA (often CONFIG/MODEL_V2 — wrong for data views)
          3) Rules-table FQN fallback
        """
        def _clean(v: Any) -> Optional[str]:
            return v.strip() if isinstance(v, str) and v.strip() else None

        sf_cfg = self.settings.get("snowflake", {})
        warehouse = _clean(rule.get("WAREHOUSE")) or sf_cfg.get("warehouse")
        inferred_db, inferred_schema = _infer_schema_from_sql(sql_code)
        dq_cfg = get_dq_monitoring_config(self.settings)
        fqn_parts = (dq_cfg.get("rules_table_fqn", "") or "").split(".")
        database = (inferred_db or _clean(rule.get("DATABASE")) or _clean(rule.get("DB"))
                    or (fqn_parts[0] if len(fqn_parts) >= 1 and fqn_parts[0] else None))
        schema = (inferred_schema or _clean(rule.get("SCHEMA"))
                  or (fqn_parts[1] if len(fqn_parts) >= 2 else None))
        return database, schema, warehouse

    def execute_dq_rule(self, qc_id: str, subject_area: Optional[str] = None,
                        limit: int = 500) -> Dict[str, Any]:
        """Fetch and execute a DQ rule SQL to get actual failing rows.

        Runs the RAW SQL_CODE in the resolved context (identical to the View Details
        endpoint) so the verdict here matches what a user sees in the detail panel.
        Row volume is bounded by execute_dq_sql's fetch cap, so no LIMIT wrapper is
        added (wrapping a WITH/CTE rule can change its behavior).
        """
        rule = self.fetch_dq_rule_sql(qc_id, subject_area)
        if not rule:
            return {"executed": False, "error": "No DQ rule SQL found", "columns": [], "rows": [],
                    "rule_sql": None}
        sql_code = rule.get("SQL_CODE", "")
        if not sql_code or not sql_code.strip():
            return {"executed": False, "error": "Rule has no SQL_CODE", "columns": [], "rows": [],
                    "rule_sql": None, "rule_meta": rule}

        database, schema, warehouse = self.resolve_rule_context(rule, sql_code)
        result = self.execute_dq_sql(
            sql_code.strip().rstrip(";"), database=database, schema=schema, warehouse=warehouse)
        result["rule_sql"] = sql_code
        result["rule_meta"] = {k: v for k, v in rule.items() if k != "SQL_CODE"}
        return result

    def run_diagnostic_sql(self, diagnostic_sql: str, rule: Dict[str, Any],
                           context_sql: Optional[str] = None) -> Dict[str, Any]:
        """Execute an ad-hoc READ-ONLY diagnostic query in a rule's session context.

        Used by RCA drill-down: the query is derived from a check's SQL to surface
        the actual rows driving a failure. It is strictly guarded to SELECT/WITH so
        it can never mutate data, and runs under the read-only role when configured.
        """
        if not self._configured():
            return {"executed": False, "error": "Snowflake not configured", "columns": [], "rows": []}
        s = (diagnostic_sql or "").strip().rstrip(";")
        if not s:
            return {"executed": False, "error": "No diagnostic SQL", "columns": [], "rows": []}
        if ";" in s:
            return {"executed": False, "error": "Multi-statement SQL is not allowed",
                    "columns": [], "rows": []}
        if not s.upper().lstrip("(").startswith(("SELECT", "WITH")):
            return {"executed": False, "error": "Diagnostic SQL must be read-only (SELECT/WITH)",
                    "columns": [], "rows": []}
        if _has_write_keyword(s):
            return {"executed": False, "error": "Diagnostic SQL contains a write/DDL keyword; blocked",
                    "columns": [], "rows": []}
        database, schema, warehouse = self.resolve_rule_context(rule, context_sql or s)
        result = self.execute_dq_sql(s, database=database, schema=schema, warehouse=warehouse)
        result["diagnostic_sql"] = s
        return result

    @staticmethod
    def interpret_execution(exec_result: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
        """Read a Pass/Fail/Warning verdict + violating-row count out of a live rule execution.

        Aggregates across ALL returned rows — a rule may return one summary row OR
        one row per table (e.g. a record-count deviation check). Handles:
        - RESULT column ('Pass'/'Fail') per row.
        - a *_COUNT column per row (non-zero => violation).
        - Layer pairs (SOURCE_CNT vs TARGET_CNT).
        - Detail rules that return the violating rows directly (>0 rows => fail).

        Returns (status, failing_count) where status is
        'SUCCESS' | 'FAILED' | 'WARNING' | None.
        WARNING = partial failure (some rows pass, some fail).
        """
        if not exec_result or not exec_result.get("executed"):
            return None, None
        rows = exec_result.get("rows") or []

        saw_result_col = False
        saw_count_col = False
        saw_pair_col = False
        any_fail = False
        fail_rows = 0          # rows whose RESULT verdict is a failure
        total_count = 0        # sum of any *_COUNT column across rows
        pair_fail_rows = 0     # rows where SOURCE_CNT != TARGET_CNT (layer checks)
        pair_rows = 0

        for row in rows:
            if not isinstance(row, dict):
                continue
            upper = {str(k).strip().upper(): v for k, v in row.items()}
            for k, v in upper.items():
                if v is None:
                    continue
                if k in _RESULT_COLS:
                    saw_result_col = True
                    verdict = str(v).strip().upper()
                    if verdict in _FAIL_VALUES:
                        any_fail = True
                        fail_rows += 1
                elif k in _COUNT_COLS:
                    saw_count_col = True
                    try:
                        c = int(float(v))
                        total_count += c
                        if c > 0:
                            any_fail = True
                    except (TypeError, ValueError):
                        pass
            # Layer / reconciliation checks often return SOURCE_CNT vs TARGET_CNT
            # without a RESULT column — count only the mismatched rows as failures.
            sc = upper.get("SOURCE_CNT")
            tc = upper.get("TARGET_CNT")
            if sc is not None and tc is not None:
                saw_pair_col = True
                pair_rows += 1
                try:
                    if abs(float(sc) - float(tc)) > 1e-9:
                        any_fail = True
                        pair_fail_rows += 1
                except (TypeError, ValueError):
                    pass

        if saw_result_col:
            if any_fail:
                pass_like = max(len(rows) - fail_rows, 0)
                if pass_like > 0 and fail_rows > 0:
                    return "WARNING", (fail_rows or (total_count if saw_count_col else None))
                return "FAILED", (fail_rows or (total_count if saw_count_col else None))
            return "SUCCESS", 0
        if saw_pair_col:
            if pair_fail_rows <= 0:
                return "SUCCESS", 0
            if pair_fail_rows < pair_rows:
                return "WARNING", pair_fail_rows
            return "FAILED", pair_fail_rows
        if saw_count_col:
            return ("FAILED" if total_count > 0 else "SUCCESS"), total_count

        # No verdict/count column: the rule returns violating rows directly.
        rc = exec_result.get("row_count", 0) or 0
        return ("FAILED" if rc > 0 else "SUCCESS"), rc

    def revalidate_check(self, check: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a check's rule SQL live and report its current pass/fail state."""
        # Use a generous row limit so a failing per-table row can't be cut off and
        # cause a false SUCCESS verdict on multi-row (record-count) checks.
        exec_result = self.execute_dq_rule(
            check.get("name") or "", subject_area=check.get("table_name"), limit=500)
        live_status, failing_count = self.interpret_execution(exec_result)
        row_count = 0
        if exec_result and exec_result.get("executed"):
            row_count = int(exec_result.get("row_count") or len(exec_result.get("rows") or []) or 0)
        # #region agent log
        try:
            _nm = str(check.get("name") or "")
            if _nm in ("17", "dq_17") or "17" in _nm:
                open(r"C:\Users\EKGAH\Documents\project\ops-monitor\debug-605d47.log", "a", encoding="utf-8").write(
                    json.dumps({"sessionId": "605d47", "hypothesisId": "A,B,C", "runId": "post-fix",
                                "location": "dq_connector.py:revalidate_check",
                                "message": "DQ17 revalidate verdict",
                                "data": {"name": _nm, "recorded_status": check.get("status"),
                                         "live_status": live_status, "failing_count": failing_count,
                                         "executed": bool(exec_result and exec_result.get("executed")),
                                         "row_count": row_count,
                                         "error": (exec_result.get("error") if exec_result else None),
                                         "error_is_timeout": (
                                             "000604" in str((exec_result or {}).get("error") or "")
                                             or "timeout" in str((exec_result or {}).get("error") or "").lower())},
                                "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        return {
            "live_status": live_status,
            "failing_count": failing_count,
            "row_count": row_count,
            "executed": bool(exec_result and exec_result.get("executed")),
            "error": exec_result.get("error") if exec_result else None,
        }

    def _revalidate_failures(self, records: List[Dict[str, Any]],
                             limit: int = _MAX_LIVE_REVALIDATE) -> None:
        """Re-run recorded FAILED/WARNING checks live and reconcile their status in place.

        A check recorded as failed that now passes is flipped to SUCCESS and flagged
        as stale; a check that still fails keeps FAILED with a live violating-row count.
        """
        checked = 0
        for rec in records:
            if rec.get("status") not in ("FAILED", "DELAYED", "WARNING"):
                continue
            if checked >= limit:
                break
            checked += 1
            rec["recorded_status"] = rec["status"]
            try:
                rv = self.revalidate_check(rec)
            except Exception:  # noqa: BLE001 — never let a bad check break the dashboard
                rec["live_checked"] = False
                continue
            rec["live_checked"] = rv["executed"]
            rec["live_failing_count"] = rv["failing_count"]
            if rv["live_status"]:
                rec["status"] = rv["live_status"]
                rec["stale"] = (rv["live_status"] == "SUCCESS")
                # Keep the displayed detail text consistent with the live verdict, so a
                # reconciled row never shows "SUCCESS" next to a stale "Fail: 1".
                # Use the same "Pass: X / Fail: Y · description" shape as recorded checks.
                fc = rv["failing_count"]
                total = int(rv.get("row_count") or 0)
                desc = (rec.get("raw") or {}).get("QC_DESCRIPTION") or ""
                if rv["live_status"] == "SUCCESS":
                    # Prefer total rows as pass count when the rule returned detail rows.
                    pass_n = total if total > 0 else 1
                    rec["fail_count"] = 0
                    rec["pass_count"] = pass_n
                    note = " (recorded failure is stale / resolved)" if rec["recorded_status"] in (
                        "FAILED", "DELAYED", "WARNING") else ""
                    rec["error"] = _detail_text(f"{desc}{note}".strip(), pass_n, 0)
                else:
                    fail_n = int(fc) if fc is not None else 1
                    # Layer checks: many rows succeed, few mismatch — don't force Pass: 0.
                    pass_n = max(total - fail_n, 0) if total > fail_n else 0
                    rec["fail_count"] = fail_n
                    rec["pass_count"] = pass_n
                    # Soft-fail: keep WARNING when live also reports a partial breach.
                    if rv["live_status"] == "WARNING" or (pass_n > 0 and fail_n > 0):
                        rec["status"] = "WARNING"
                    rec["error"] = _detail_text(desc, pass_n, fail_n)
            elif rv.get("error"):
                # Live SQL did not complete. Distinguish client timeout (000604) from a
                # real rule error (e.g. division by zero): timeout is not proof the data
                # fails — keep the recorded status and annotate that live verify timed out.
                rec["live_checked"] = True
                err = str(rv["error"])[:180]
                desc = (rec.get("raw") or {}).get("QC_DESCRIPTION") or ""
                is_timeout = ("000604" in err) or ("timeout" in err.lower())
                if is_timeout:
                    rec["live_timeout"] = True
                    # Do not invent a stronger FAIL from our client timeout alone.
                    rec["status"] = rec.get("recorded_status") or rec.get("status") or "FAILED"
                    rec["error"] = _detail_text(
                        f"{desc} · Live revalidate timed out (client); recorded status kept"
                        .strip(),
                        rec.get("pass_count"),
                        rec.get("fail_count"),
                    )
                else:
                    rec["status"] = "FAILED"
                    rec["fail_count"] = rec.get("fail_count") or 1
                    rec["pass_count"] = 0
                    rec["error"] = _detail_text(f"{desc} · SQL error: {err}".strip(), 0, 1)

    def summary(self, date_from: Optional[str] = None,
                date_to: Optional[str] = None,
                revalidate: bool = True) -> Dict[str, Any]:
        # Any call: if a live-revalidated payload exists for this exact date range
        # and is within the 30-minute TTL, return it (skip Snowflake live rules).
        # Cache miss + revalidate=False → recorded DQM_VALIDATION_SUMMARY read.
        # Cache miss + revalidate=True  → live rules, then write full response to cache.
        cached = get_cached_summary(date_from, date_to)
        if cached is not None:
            return cached

        # Dashboard shows latest run per QC + subject area within the date window.
        checks = self.read_results(
            date_from, date_to, revalidate=bool(revalidate), latest_only=True)

        kpis = {"success": 0, "failed": 0, "warning": 0, "delayed": 0, "skipped": 0, "running": 0, "total": len(checks)}
        key = {"SUCCESS": "success", "FAILED": "failed", "WARNING": "warning", "DELAYED": "warning",
               "SKIPPED": "skipped", "RUNNING": "running"}
        live_n = 0
        for c in checks:
            c["status"] = normalize_dq_status(c.get("status"))
            kpis[key.get(c["status"], "running")] += 1
            if c.get("live_checked"):
                live_n += 1

        errors = []
        if self.last_error:
            errors.append({"platform": "snowflake", "error": self.last_error})

        dq_cfg = get_dq_monitoring_config(self.settings)
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "live_only": True,
            "status_mode": "live" if revalidate else "recorded",
            "revalidated": bool(revalidate),
            "live_checked_count": live_n,
            "cache_updated_at": None,
            "date_from": date_from,
            "date_to": date_to,
            "table": dq_cfg["table_fqn"],
            "platforms": platforms_configured(self.settings),
            "kpis": kpis,
            "all_checks": checks,
            "errors": errors,
        }
        if revalidate:
            updated_at = write_full_summary_cache(date_from, date_to, result)
            result["cache_updated_at"] = updated_at
            result["status_mode"] = "live"
        return result
