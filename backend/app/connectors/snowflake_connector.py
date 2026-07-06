"""Snowflake connector — task history/graph, query history, zero-copy clone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.connectors.status import normalize_task_result
from app.core.config import (get_task_monitoring_config, interpret_snowflake_error, is_sso_auth,
                             load_settings, snowflake_configured)


def _iso_date(value: Optional[str], *, end_of_day: bool = False) -> Optional[str]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else value


def _default_task_date_range(future_days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=60)).isoformat(), (today + timedelta(days=future_days)).isoformat()


def _ts(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


_shared_conn = None
_shared_conn_params_key: Optional[str] = None


def _params_cache_key(params: Dict[str, Any]) -> str:
    """Deterministic key from connection params to detect config changes."""
    return "|".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "login_timeout")


def reset_shared_connection():
    """Invalidate cached connection (called when settings change)."""
    global _shared_conn, _shared_conn_params_key
    if _shared_conn:
        try:
            _shared_conn.close()
        except Exception:
            pass
    _shared_conn = None
    _shared_conn_params_key = None


class SnowflakeConnector:
    last_error: Optional[str] = None

    def __init__(self):
        self.settings = load_settings()
        self._conn = None

    def _configured(self) -> bool:
        return snowflake_configured(self.settings.get("snowflake", {}))

    # ---- connectivity -------------------------------------------------
    def health(self) -> List[Dict[str, Any]]:
        if not self._configured():
            return [{"name": "Snowflake", "status": "down", "latency_ms": None,
                     "detail": "Not configured — account, warehouse, role, and password or SSO are required.",
                     "auth_mode": "unknown", "hints": []}]
        sf = self.settings["snowflake"]
        auth_mode = "sso" if is_sso_auth(sf.get("authenticator", "password")) else "password"
        # Detect missing driver before attempting a connection so the error is never
        # misclassified as an auth/SSO failure.
        try:
            import snowflake.connector  # noqa: F401
        except ImportError as exc:
            detail = (
                "snowflake-connector-python is not installed. "
                "Run: pip install snowflake-connector-python"
            )
            return [{"name": "Snowflake", "status": "down", "latency_ms": None,
                     "detail": detail,
                     "raw_error": str(exc),
                     "auth_mode": auth_mode,
                     "hints": [
                         "Install the Snowflake Python driver in the backend virtual environment:",
                         "  cd backend && .venv\\Scripts\\pip.exe install snowflake-connector-python",
                         "Then restart the backend and test connectivity again.",
                     ]}]
        try:
            cur = self._connect().cursor()
            cur.execute("SELECT CURRENT_VERSION()")
            v = cur.fetchone()[0]
            self.last_error = None
            hint = ("SSO via browser." if auth_mode == "sso" else "Password auth.")
            return [{"name": "Snowflake", "status": "connected", "latency_ms": None,
                     "detail": f"version {v}. {hint}", "auth_mode": auth_mode, "hints": []}]
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.last_error = err
            self._conn = None
            interpreted = interpret_snowflake_error(err, sf)
            return [{"name": "Snowflake", "status": "down", "latency_ms": None,
                     "detail": interpreted["summary"],
                     "raw_error": interpreted["raw_error"],
                     "auth_mode": interpreted["auth_mode"],
                     "hints": interpreted["hints"]}]

    def _connect_params(self) -> Dict[str, Any]:
        sfc = self.settings["snowflake"]
        params: Dict[str, Any] = {
            "account": sfc["account"].strip(),
            "user": sfc["user"].strip(),
            "warehouse": (sfc.get("warehouse") or "").strip() or None,
            "role": (sfc.get("role") or "").strip() or None,
        }
        db = (sfc.get("database") or "").strip()
        schema = (sfc.get("schema") or "").strip()
        if db:
            params["database"] = db
        if schema:
            params["schema"] = schema

        auth = (sfc.get("authenticator") or "password").strip()
        auth_lower = auth.lower()
        if auth_lower in ("sso", "externalbrowser"):
            params["authenticator"] = "externalbrowser"
            params["login_timeout"] = 120
        elif auth_lower not in ("password", "snowflake", ""):
            params["authenticator"] = auth
            params["login_timeout"] = 120
        else:
            params["password"] = sfc.get("password", "")
        user = (sfc.get("user") or "").strip()
        if user:
            params["user"] = user
        elif auth_lower in ("sso", "externalbrowser") or auth_lower.startswith("http"):
            params.pop("user", None)
        return {k: v for k, v in params.items() if v is not None}

    def _connect(self):
        global _shared_conn, _shared_conn_params_key
        if self._conn:
            return self._conn
        params = self._connect_params()
        key = _params_cache_key(params)
        if _shared_conn and _shared_conn_params_key == key:
            try:
                _shared_conn.cursor().execute("SELECT 1")
                self._conn = _shared_conn
                return self._conn
            except Exception:
                _shared_conn = None
                _shared_conn_params_key = None
        import snowflake.connector as sf  # lazy import
        try:
            self._conn = sf.connect(**params)
            _shared_conn = self._conn
            _shared_conn_params_key = key
            return self._conn
        except Exception:
            self._conn = None
            raise

    # ---- monitoring reads --------------------------------------------
    def read_telemetry(self, date_from: Optional[str] = None,
                       date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        self.last_error = None
        if not self._configured():
            return []
        return self._live_telemetry(date_from, date_to)

    def _live_telemetry(self, date_from: Optional[str] = None,
                        date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        task_cfg = get_task_monitoring_config(self.settings)
        monitor_db = task_cfg["monitor_database"]
        name_pattern = task_cfg["name_pattern"]
        historical_months = task_cfg["historical_months"]
        future_days = task_cfg["future_days"]
        range_from, range_to = _default_task_date_range(future_days)
        if date_from:
            range_from = _iso_date(date_from) or range_from
        if date_to:
            range_to = _iso_date(date_to, end_of_day=True) or range_to

        try:
            cur = self._connect().cursor()
            sql = f"""
                WITH HISTORICAL_TASKS AS (
                    SELECT
                        NAME              AS TASK_NAME,
                        DATABASE_NAME,
                        SCHEMA_NAME,
                        STATE,
                        QUERY_ID,
                        SCHEDULED_TIME,
                        QUERY_START_TIME,
                        COMPLETED_TIME,
                        ERROR_CODE,
                        ERROR_MESSAGE,
                        'HISTORICAL'      AS RECORD_TYPE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE SCHEDULED_TIME >= DATEADD(MONTH, -{historical_months}, CURRENT_TIMESTAMP())
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                ),
                FUTURE_TASKS AS (
                    SELECT
                        NAME              AS TASK_NAME,
                        DATABASE_NAME,
                        SCHEMA_NAME,
                        STATE,
                        QUERY_ID,
                        SCHEDULED_TIME,
                        QUERY_START_TIME,
                        COMPLETED_TIME,
                        ERROR_CODE,
                        ERROR_MESSAGE,
                        'SCHEDULED'       AS RECORD_TYPE
                    FROM TABLE(
                        INFORMATION_SCHEMA.TASK_HISTORY(
                            SCHEDULED_TIME_RANGE_START => CURRENT_TIMESTAMP(),
                            SCHEDULED_TIME_RANGE_END   => DATEADD(DAY, {future_days}, CURRENT_TIMESTAMP()),
                            RESULT_LIMIT => 10000
                        )
                    )
                    WHERE STATE = 'SCHEDULED'
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                ),
                ALL_TASKS AS (
                    SELECT * FROM HISTORICAL_TASKS
                    UNION ALL
                    SELECT * FROM FUTURE_TASKS
                )
                SELECT
                    TASK_NAME,
                    DATABASE_NAME,
                    SCHEMA_NAME,
                    STATE,
                    CASE
                        WHEN UPPER(STATE) = 'SUCCEEDED'            THEN 'PASS'
                        WHEN UPPER(STATE) LIKE '%%FAIL%%'           THEN 'FAIL'
                        WHEN UPPER(STATE) IN ('RUNNING', 'EXECUTING') THEN 'RUNNING'
                        WHEN UPPER(STATE) = 'SCHEDULED'            THEN 'SCHEDULED'
                        ELSE 'OTHER'
                    END AS TASK_RESULT,
                    SCHEDULED_TIME,
                    QUERY_START_TIME,
                    COMPLETED_TIME,
                    QUERY_ID,
                    ERROR_CODE,
                    ERROR_MESSAGE,
                    RECORD_TYPE,
                    DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS DURATION_S
                FROM ALL_TASKS
                WHERE CAST(SCHEDULED_TIME AS DATE) BETWEEN %s::DATE AND %s::DATE
                ORDER BY TASK_NAME, SCHEDULED_TIME DESC
            """
            params = (
                monitor_db,
                name_pattern,
                monitor_db,
                name_pattern,
                range_from,
                range_to,
            )
            cur.execute(sql, params)
            for row in cur.fetchall():
                (task_name, database_name, schema_name, state, task_result, scheduled_time,
                 query_start_time, completed_time, query_id, error_code, error_message,
                 record_type, duration_s) = row
                sched_key = _ts(scheduled_time) or "unknown"
                status = normalize_task_result(task_result, state, done=completed_time, error=error_message)
                records.append({
                    "id": f"sf_{task_name}_{sched_key}",
                    "name": f"SF Task: {task_name}",
                    "platform": "snowflake",
                    "status": status,
                    "database": database_name,
                    "schema": schema_name,
                    "record_type": record_type,
                    "task_result": task_result,
                    "started_at": _ts(scheduled_time),
                    "query_start_at": _ts(query_start_time),
                    "ended_at": _ts(completed_time),
                    "duration_s": duration_s,
                    "error": error_message,
                    "error_code": error_code,
                    "query_id": query_id,
                    "upstream": [],
                    "downstream": [],
                    "log_ref": f"snowflake://task/{database_name}.{schema_name}.{task_name}",
                    "source": "live",
                })
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self._conn = None
        return records

    def get_logs(self, log_ref: str) -> List[str]:
        if not self._configured():
            return ["(Snowflake not configured)"]
        return [f"(fetch logs from Snowflake query/task history for {log_ref})"]

    # ---- zero-copy clone (Test agent) --------------------------------
    def create_zero_copy_clone(self, database: str, clone_name: str,
                               environment: str = "PREPROD") -> Dict[str, Any]:
        ddl = f"CREATE OR REPLACE DATABASE {clone_name} CLONE {database};"
        if not self._configured():
            return {"executed": False, "error": "Snowflake not configured", "ddl": ddl, "clone_name": clone_name}
        try:
            target = self.settings["snowflake"].get("preprod_account") or "current"
            cur = self._connect().cursor()
            cur.execute(ddl)
            return {"executed": True, "ddl": ddl, "clone_name": clone_name,
                    "environment": environment, "target_account": target}
        except Exception as e:  # noqa: BLE001
            return {"executed": False, "error": str(e), "ddl": ddl, "clone_name": clone_name}

    def run_query(self, sql: str) -> Dict[str, Any]:
        if not self._configured():
            return {"executed": False, "sql": sql, "error": "Snowflake not configured"}
        try:
            cur = self._connect().cursor()
            cur.execute(sql)
            return {"executed": True, "sql": sql, "rows": cur.fetchall()}
        except Exception as e:  # noqa: BLE001
            return {"executed": False, "sql": sql, "error": str(e)}

    def drop_clone(self, clone_name: str) -> Dict[str, Any]:
        return self.run_query(f"DROP DATABASE IF EXISTS {clone_name};")
