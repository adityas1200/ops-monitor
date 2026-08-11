"""Snowflake connector — task history/graph, query history, zero-copy clone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.connectors.sql_guardrail import check_sql, classify_sql, log_sql, submit_for_approval
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
_shared_conn_warmed_at: Optional[float] = None
_connect_lock = __import__("threading").Lock()
_warm_lock = __import__("threading").Lock()
_warm_inflight = None  # threading Event + result holder for single-flight warm
_INFO_SCHEMA_LOOKBACK_DAYS = 14
_TASK_HISTORY_RESULT_LIMIT = 1000
# How long we treat an open backend session as "warm" without re-pinging Snowflake.
_SESSION_TTL_S = 50 * 60
# How often a warm session is health-checked with SELECT 1 (not full SSO).
_SESSION_PING_EVERY_S = 10 * 60


def _params_cache_key(params: Dict[str, Any]) -> str:
    """Deterministic key from connection params to detect config changes."""
    return "|".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "login_timeout")


def reset_shared_connection():
    """Invalidate cached connection (called when settings change)."""
    global _shared_conn, _shared_conn_params_key, _shared_conn_warmed_at
    if _shared_conn:
        try:
            _shared_conn.close()
        except Exception:
            pass
    _shared_conn = None
    _shared_conn_params_key = None
    _shared_conn_warmed_at = None


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
        global _shared_conn, _shared_conn_params_key, _shared_conn_warmed_at
        if self._conn:
            return self._conn
        params = self._connect_params()
        # Keep session alive so the next /api/summary does not re-auth / wake warehouse cold.
        params.setdefault("client_session_keep_alive", True)
        # DQ rules (e.g. QC 17 COUNT DISTINCT over large LAAD tables) often need several
        # minutes. A 60s client network_timeout cancels them with 000604 even when the
        # same SQL succeeds in the Snowflake UI and returns matching SOURCE/TARGET counts.
        params.setdefault("network_timeout", 600)
        # Cache IdP tokens in OS secure storage so SSO browser is not required every restart.
        # Requires: pip install "snowflake-connector-python[secure-local-storage]"
        auth = str(params.get("authenticator") or "").lower()
        if auth == "externalbrowser" or auth.startswith("http"):
            params.setdefault("client_store_temporary_credential", True)
        key = _params_cache_key(params)
        with _connect_lock:
            if self._conn:
                return self._conn
            if _shared_conn and _shared_conn_params_key == key:
                try:
                    if getattr(_shared_conn, "is_closed", lambda: False)():
                        raise RuntimeError("shared connection closed")
                    self._conn = _shared_conn
                    return self._conn
                except Exception:
                    _shared_conn = None
                    _shared_conn_params_key = None
                    _shared_conn_warmed_at = None
            import snowflake.connector as sf  # lazy import
            try:
                self._conn = sf.connect(**params)
                _shared_conn = self._conn
                _shared_conn_params_key = key
                import time as _time
                _shared_conn_warmed_at = _time.time()
                return self._conn
            except Exception:
                self._conn = None
                raise

    def session_status(self) -> Dict[str, Any]:
        """Cheap status of the shared in-process Snowflake session (no network)."""
        import time as _time
        now = _time.time()
        alive = False
        if _shared_conn is not None:
            try:
                alive = not getattr(_shared_conn, "is_closed", lambda: False)()
            except Exception:
                alive = False
        warmed_at = _shared_conn_warmed_at
        expires_at = (warmed_at + _SESSION_TTL_S) if warmed_at else None
        remaining = max(0, int(expires_at - now)) if expires_at else 0
        return {
            "warmed": bool(alive and remaining > 0),
            "alive": alive,
            "warmed_at": (
                datetime.fromtimestamp(warmed_at, tz=timezone.utc).isoformat()
                if warmed_at else None
            ),
            "expires_at": (
                datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
                if expires_at else None
            ),
            "ttl_s": remaining,
            "detail": (
                "Snowflake session ready (reused)" if alive and remaining > 0
                else "Snowflake session not ready"
            ),
        }

    def warm(self, *, force_ping: bool = False) -> Dict[str, Any]:
        """Establish (or reuse) the shared Snowflake session. Single-flight SSO.

        - If an open session exists within TTL → return immediately (no SELECT 1).
        - If open but older than ping interval → cheap SELECT 1 health check.
        - Concurrent callers share one connect/SSO attempt.
        """
        import time as _time
        global _warm_inflight, _shared_conn_warmed_at

        if not self._configured():
            return {"warmed": False, "detail": "Snowflake not configured", "ttl_s": 0}

        status = self.session_status()
        if status["warmed"] and not force_ping:
            # Optional periodic ping without re-SSO.
            age = (_time.time() - (_shared_conn_warmed_at or 0))
            if age < _SESSION_PING_EVERY_S:
                return {**status, "reused": True}

        with _warm_lock:
            status = self.session_status()
            if status["warmed"] and not force_ping:
                age = (_time.time() - (_shared_conn_warmed_at or 0))
                if age < _SESSION_PING_EVERY_S:
                    return {**status, "reused": True}
            # Single-flight: if another thread is warming, wait for it.
            if _warm_inflight is not None:
                evt, box = _warm_inflight
            else:
                evt = __import__("threading").Event()
                box: Dict[str, Any] = {}
                _warm_inflight = (evt, box)

                def _do_warm() -> None:
                    global _shared_conn_warmed_at, _warm_inflight
                    try:
                        cur = self._connect().cursor()
                        cur.execute("SELECT 1")
                        cur.fetchone()
                        _shared_conn_warmed_at = _time.time()
                        box["result"] = {
                            **self.session_status(),
                            "reused": False,
                            "detail": "Snowflake session ready",
                        }
                    except Exception as e:  # noqa: BLE001
                        self.last_error = str(e)
                        box["result"] = {
                            "warmed": False,
                            "detail": str(e)[:300],
                            "ttl_s": 0,
                            "reused": False,
                        }
                    finally:
                        with _warm_lock:
                            _warm_inflight = None
                        evt.set()

                __import__("threading").Thread(
                    target=_do_warm, daemon=True, name="sf-warm-once"
                ).start()

        evt.wait(timeout=130)
        return box.get("result") or {"warmed": False, "detail": "Warmup timed out", "ttl_s": 0}

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

        # INFORMATION_SCHEMA.TASK_HISTORY only covers ~7 days and is far faster than
        # ACCOUNT_USAGE. Use it for recent dashboard ranges; fall back to ACCOUNT_USAGE
        # only when the requested window is older. Always push the date filter into the
        # source scan — never scan historical_months then filter afterward.
        today = datetime.now(timezone.utc).date()
        try:
            from_d = datetime.fromisoformat(range_from).date()
            to_d = datetime.fromisoformat(range_to).date()
        except ValueError:
            from_d, to_d = today - timedelta(days=7), today
        info_schema_floor = today - timedelta(days=_INFO_SCHEMA_LOOKBACK_DAYS)
        use_fast_history = from_d >= info_schema_floor
        include_future = to_d >= today

        try:
            cur = self._connect().cursor()
            if use_fast_history:
                # INFORMATION_SCHEMA.TASK_HISTORY rejects windows longer than 7 days.
                hist_start = from_d.isoformat()
                hist_end = min(to_d + timedelta(days=1), from_d + timedelta(days=7)).isoformat()
                future_cte = ""
                future_union = ""
                params: list = [hist_start, hist_end, monitor_db, name_pattern, range_from, range_to]
                if include_future:
                    future_cte = f""",
                FUTURE_TASKS AS (
                    SELECT
                        NAME AS TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE,
                        CASE
                            WHEN UPPER(STATE) = 'SUCCEEDED'            THEN 'PASS'
                            WHEN UPPER(STATE) LIKE '%%FAIL%%'           THEN 'FAIL'
                            WHEN UPPER(STATE) IN ('RUNNING', 'EXECUTING') THEN 'RUNNING'
                            WHEN UPPER(STATE) = 'SCHEDULED'            THEN 'SCHEDULED'
                            ELSE 'OTHER'
                        END AS TASK_RESULT,
                        SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                        QUERY_ID, ERROR_CODE, ERROR_MESSAGE,
                        'SCHEDULED' AS RECORD_TYPE,
                        DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS DURATION_S
                    FROM TABLE(
                        INFORMATION_SCHEMA.TASK_HISTORY(
                            SCHEDULED_TIME_RANGE_START => CURRENT_TIMESTAMP(),
                            SCHEDULED_TIME_RANGE_END   => DATEADD(DAY, {int(future_days)}, CURRENT_TIMESTAMP()),
                            RESULT_LIMIT => 200
                        )
                    )
                    WHERE STATE = 'SCHEDULED'
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                )"""
                    future_union = "\n                    UNION ALL\n                    SELECT * FROM FUTURE_TASKS"
                    params.extend([monitor_db, name_pattern])
                sql = f"""
                    WITH RECENT_TASKS AS (
                    SELECT
                        NAME              AS TASK_NAME,
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
                        CASE
                            WHEN UPPER(STATE) = 'SCHEDULED' THEN 'SCHEDULED'
                            ELSE 'HISTORICAL'
                        END AS RECORD_TYPE,
                        DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS DURATION_S
                    FROM TABLE(
                        INFORMATION_SCHEMA.TASK_HISTORY(
                            SCHEDULED_TIME_RANGE_START => %s::TIMESTAMP_LTZ,
                            SCHEDULED_TIME_RANGE_END   => %s::TIMESTAMP_LTZ,
                            RESULT_LIMIT => {_TASK_HISTORY_RESULT_LIMIT}
                        )
                    )
                    WHERE DATABASE_NAME = %s
                      AND NAME ILIKE %s
                      AND SCHEDULED_TIME >= %s::TIMESTAMP_LTZ
                      AND SCHEDULED_TIME < DATEADD(DAY, 1, %s::DATE)
                    ){future_cte}
                    SELECT * FROM RECENT_TASKS{future_union}
                    ORDER BY TASK_NAME, SCHEDULED_TIME DESC
                """
                params = tuple(params)
            else:
                future_cte = ""
                future_union = ""
                params = [range_from, range_to, monitor_db, name_pattern]
                if include_future:
                    future_cte = f""",
                FUTURE_TASKS AS (
                    SELECT
                        NAME AS TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE, QUERY_ID,
                        SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                        ERROR_CODE, ERROR_MESSAGE, 'SCHEDULED' AS RECORD_TYPE
                    FROM TABLE(
                        INFORMATION_SCHEMA.TASK_HISTORY(
                            SCHEDULED_TIME_RANGE_START => CURRENT_TIMESTAMP(),
                            SCHEDULED_TIME_RANGE_END   => DATEADD(DAY, {future_days}, CURRENT_TIMESTAMP()),
                            RESULT_LIMIT => 200
                        )
                    )
                    WHERE STATE = 'SCHEDULED'
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                      AND SCHEDULED_TIME < DATEADD(DAY, 1, %s::DATE)
                )"""
                    future_union = "\n                    UNION ALL\n                    SELECT * FROM FUTURE_TASKS"
                    params.extend([monitor_db, name_pattern, range_to])
                sql = f"""
                WITH HISTORICAL_TASKS AS (
                    SELECT
                        NAME AS TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE, QUERY_ID,
                        SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                        ERROR_CODE, ERROR_MESSAGE, 'HISTORICAL' AS RECORD_TYPE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE SCHEDULED_TIME >= GREATEST(
                            %s::TIMESTAMP_LTZ,
                            DATEADD(MONTH, -{historical_months}, CURRENT_TIMESTAMP())
                          )
                      AND SCHEDULED_TIME < DATEADD(DAY, 1, %s::DATE)
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                ){future_cte}
                SELECT
                    TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE,
                    CASE
                        WHEN UPPER(STATE) = 'SUCCEEDED'            THEN 'PASS'
                        WHEN UPPER(STATE) LIKE '%%FAIL%%'           THEN 'FAIL'
                        WHEN UPPER(STATE) IN ('RUNNING', 'EXECUTING') THEN 'RUNNING'
                        WHEN UPPER(STATE) = 'SCHEDULED'            THEN 'SCHEDULED'
                        ELSE 'OTHER'
                    END AS TASK_RESULT,
                    SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                    QUERY_ID, ERROR_CODE, ERROR_MESSAGE, RECORD_TYPE,
                    DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS DURATION_S
                FROM (
                    SELECT * FROM HISTORICAL_TASKS{future_union}
                )
                ORDER BY TASK_NAME, SCHEDULED_TIME DESC
                """
                params = tuple(params)
            log_sql(sql[:500], "monitoring:telemetry", "READ", allowed=True)
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

    # ---- SQL execution with guardrails ---------------------------------
    def _allowed_write_databases(self) -> List[str]:
        """Databases where DDL/DML is permitted (clones and preprod only)."""
        sf = self.settings.get("snowflake", {})
        allowed = []
        preprod = (sf.get("preprod_account") or "").strip()
        if preprod:
            allowed.append(preprod)
        # Clone databases created by the tool follow a naming convention
        allowed.append("CLONE_")
        allowed.append("_CLONE")
        allowed.append("_PREPROD")
        allowed.append("PREPROD_")
        return allowed

    def _production_databases(self) -> List[str]:
        """Known production database names."""
        sf = self.settings.get("snowflake", {})
        db = (sf.get("database") or "").strip()
        return [db] if db else []

    def _execute_with_guardrail(self, sql: str, source: str = "unknown",
                                allow_write: bool = False) -> Dict[str, Any]:
        """Execute SQL with guardrail checks. Returns result or blocks with approval request."""
        if not self._configured():
            return {"executed": False, "sql": sql, "error": "Snowflake not configured"}

        classification = classify_sql(sql)

        # Log all SQL (READ and WRITE)
        if classification == "WRITE" and not allow_write:
            guard = check_sql(sql, self._production_databases(), self._allowed_write_databases(), source)
            if not guard["allowed"]:
                log_sql(sql, source, classification, allowed=False, result="BLOCKED")
                approval = submit_for_approval(sql, source, {"reason": guard["reason"]})
                return {
                    "executed": False,
                    "sql": sql,
                    "error": f"BLOCKED: {guard['reason']}",
                    "approval_id": approval["id"],
                    "requires_approval": True,
                }

        # Execute
        try:
            cur = self._connect().cursor()
            cur.execute(sql)
            rows = cur.fetchall() if classification == "READ" else []
            log_sql(sql, source, classification, allowed=True, result="SUCCESS")
            return {"executed": True, "sql": sql, "rows": rows}
        except Exception as e:  # noqa: BLE001
            log_sql(sql, source, classification, allowed=True, result=f"ERROR: {str(e)[:200]}")
            return {"executed": False, "sql": sql, "error": str(e)}

    # ---- zero-copy clone (Test agent) --------------------------------
    def create_zero_copy_clone(self, database: str, clone_name: str,
                               environment: str = "PREPROD") -> Dict[str, Any]:
        ddl = f"CREATE OR REPLACE DATABASE {clone_name} CLONE {database};"
        if not self._configured():
            return {"executed": False, "error": "Snowflake not configured", "ddl": ddl, "clone_name": clone_name}
        # Clone creation is allowed on preprod targets
        result = self._execute_with_guardrail(ddl, source="test_agent:clone", allow_write=True)
        if result["executed"]:
            target = self.settings["snowflake"].get("preprod_account") or "current"
            return {"executed": True, "ddl": ddl, "clone_name": clone_name,
                    "environment": environment, "target_account": target}
        return {"executed": False, "error": result.get("error", "Unknown"), "ddl": ddl, "clone_name": clone_name}

    def run_query(self, sql: str, source: str = "unknown",
                  allow_write: bool = False) -> Dict[str, Any]:
        """Execute a SQL query with guardrail protection.

        DDL/DML on production is blocked unless allow_write=True.
        All queries are logged to the audit trail.
        """
        return self._execute_with_guardrail(sql, source=source, allow_write=allow_write)

    def drop_clone(self, clone_name: str) -> Dict[str, Any]:
        # Clone drops are allowed (they only affect clone databases)
        return self._execute_with_guardrail(
            f"DROP DATABASE IF EXISTS {clone_name};",
            source="test_agent:drop_clone",
            allow_write=True,
        )
