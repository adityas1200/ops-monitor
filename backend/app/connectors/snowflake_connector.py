"""Snowflake connector — task history/graph, query history, guarded SQL execution."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.connectors.sql_guardrail import check_sql, classify_sql, log_sql, submit_for_approval
from app.connectors.status import normalize_task_result
from app.core.config import (get_task_monitoring_config, interpret_snowflake_error, is_keypair_auth,
                             is_sso_auth, load_settings, snowflake_auth_mode, snowflake_configured)


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


def _task_result_case_sql() -> str:
    return """
                        CASE
                            WHEN UPPER(STATE) = 'SUCCEEDED'            THEN 'PASS'
                            WHEN UPPER(STATE) LIKE '%%FAIL%%'           THEN 'FAIL'
                            WHEN UPPER(STATE) IN ('SKIPPED', 'SKIP')   THEN 'SKIP'
                            WHEN UPPER(STATE) IN ('CANCELLED', 'CANCELED') THEN 'SKIP'
                            WHEN UPPER(STATE) IN ('RUNNING', 'EXECUTING') THEN 'RUNNING'
                            WHEN UPPER(STATE) = 'SCHEDULED'            THEN 'SCHEDULED'
                            ELSE 'OTHER'
                        END
    """.strip()


def _is_scheduled_row(state: Any, task_result: Any, record_type: Any) -> bool:
    return (
        str(record_type or "").strip().upper() == "SCHEDULED"
        or str(state or "").strip().upper() == "SCHEDULED"
        or str(task_result or "").strip().upper() == "SCHEDULED"
    )


def _record_from_task_row(row: tuple) -> Dict[str, Any]:
    (task_name, database_name, schema_name, state, task_result, scheduled_time,
     query_start_time, completed_time, query_id, error_code, error_message,
     record_type, duration_s) = row
    sched_key = _ts(scheduled_time) or "unknown"
    status = normalize_task_result(task_result, state, done=completed_time, error=error_message)
    return {
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
    }


_shared_conn = None
_shared_conn_params_key: Optional[str] = None
_shared_conn_warmed_at: Optional[float] = None
_connect_lock = __import__("threading").Lock()
_query_lock = __import__("threading").Lock()  # shared conn is not thread-safe
_warm_lock = __import__("threading").Lock()
_warm_inflight = None  # threading Event + result holder for single-flight warm
_INFO_SCHEMA_LOOKBACK_DAYS = 14
# INFORMATION_SCHEMA.TASK_HISTORY applies RESULT_LIMIT before DATABASE_NAME/NAME
# filters — 1000 account-wide rows routinely exceeded a 60s statement timeout.
_TASK_HISTORY_RESULT_LIMIT = 200
# Prefer ACCOUNT_USAGE for dashboard windows: WHERE DATABASE_NAME/NAME prune the
# scan. INFORMATION_SCHEMA is kept only for near-term SCHEDULED (future) rows.
_USE_ACCOUNT_USAGE_FOR_RECENT = True
# Dashboard TASK_HISTORY must not sit for the full network_timeout (600s), but
# prod ACCOUNT_USAGE windows often need more than 60s under warehouse load.
# Override with env TASK_TELEMETRY_STATEMENT_TIMEOUT_S (seconds).
_TELEMETRY_STATEMENT_TIMEOUT_S = max(
    30,
    int(__import__("os").getenv("TASK_TELEMETRY_STATEMENT_TIMEOUT_S", "180")),
)
# How long we treat an open backend session as "warm" without re-pinging Snowflake.
_SESSION_TTL_S = 50 * 60
# How often a warm session is health-checked with SELECT 1 (not full SSO).
_SESSION_PING_EVERY_S = 10 * 60


def _params_cache_key(params: Dict[str, Any]) -> str:
    """Deterministic key from connection params to detect config changes."""
    import hashlib
    parts: List[str] = []
    for k, v in sorted(params.items()):
        if k == "login_timeout":
            continue
        if k == "private_key" and isinstance(v, (bytes, bytearray)):
            digest = hashlib.sha256(bytes(v)).hexdigest()[:16]
            parts.append(f"{k}=sha256:{digest}")
        else:
            parts.append(f"{k}={v}")
    return "|".join(parts)


def _normalize_pem_text(pem: str) -> str:
    """Turn env-style single-line PEMs (literal \\n) into real multiline PEM text."""
    text = (pem or "").strip().lstrip("\ufeff")
    if not text:
        return ""
    # Common when pasting from .env / Secrets Manager / JSON: "\\n" as two chars.
    if "\\n" in text or "\\r" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def sanitize_snowflake_account(account: str) -> str:
    """Normalize account identifiers pasted from URLs or hostnames."""
    a = (account or "").strip()
    if not a:
        return ""
    lower = a.lower()
    for prefix in ("https://", "http://"):
        if lower.startswith(prefix):
            a = a[len(prefix):]
            lower = a.lower()
            break
    a = a.split("/")[0].strip()
    lower = a.lower()
    for host_suffix in (".snowflakecomputing.com", ".snowflakecomputing.cn"):
        if lower.endswith(host_suffix):
            a = a[: -len(host_suffix)]
            break
    return a.strip()


def _load_private_key_obj(pem: str, passphrase: str = ""):
    """Load a cryptography private key from PEM (handles literal \\n pastes)."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    raw = _normalize_pem_text(pem).encode("utf-8")
    if not raw:
        raise ValueError("Private key PEM is empty")
    pwd = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_pem_private_key(raw, password=pwd, backend=default_backend())
    except TypeError as e:
        raise ValueError(f"Could not load private key (check passphrase): {e}") from e
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Could not deserialize private key PEM: {e}") from e


def _load_private_key_der(pem: str, passphrase: str = "") -> bytes:
    """Convert PKCS#8 / traditional PEM private key to unencrypted DER for the Snowflake driver."""
    from cryptography.hazmat.primitives import serialization

    key = _load_private_key_obj(pem, passphrase)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def snowflake_public_key_fingerprint(pem: str, passphrase: str = "") -> str:
    """Return Snowflake-style SHA256 fingerprint for the public key of this private key."""
    import base64
    import hashlib
    from cryptography.hazmat.primitives import serialization

    key = _load_private_key_obj(pem, passphrase)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii")


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

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings if settings is not None else load_settings()
        self._conn = None

    def _configured(self) -> bool:
        return snowflake_configured(self.settings.get("snowflake", {}))

    # ---- connectivity -------------------------------------------------
    def health(self, ephemeral: bool = False) -> List[Dict[str, Any]]:
        if not self._configured():
            return [{"name": "Snowflake", "status": "down", "latency_ms": None,
                     "detail": "Not configured — account, warehouse, role, and password / SSO / key-pair are required.",
                     "auth_mode": "unknown", "hints": []}]
        sf = self.settings["snowflake"]
        auth_mode = snowflake_auth_mode(sf.get("authenticator", "password"))
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
            if ephemeral:
                v = self._probe_version_ephemeral()
            else:
                cur = self._connect().cursor()
                cur.execute("SELECT CURRENT_VERSION()")
                v = cur.fetchone()[0]
            self.last_error = None
            hint = {
                "sso": "SSO via browser.",
                "keypair": "Key-pair (JWT) auth.",
                "password": "Password auth.",
            }.get(auth_mode, f"{auth_mode} auth.")
            return [{"name": "Snowflake", "status": "connected", "latency_ms": None,
                     "detail": f"version {v}. {hint}", "auth_mode": auth_mode, "hints": []}]
        except Exception as e:  # noqa: BLE001
            err = str(e)
            self.last_error = err
            self._conn = None
            interpreted = interpret_snowflake_error(err, sf)
            hints = list(interpreted["hints"] or [])
            # Help operators match DESCRIBE USER → RSA_PUBLIC_KEY_FP to this private key.
            if auth_mode == "keypair":
                try:
                    fp = snowflake_public_key_fingerprint(
                        str(sf.get("private_key_pem") or ""),
                        str(sf.get("private_key_passphrase") or ""),
                    )
                    hints.append(
                        f"Fingerprint of the private key in Settings: {fp}. "
                        "In Snowflake run DESCRIBE USER \"…\" and confirm RSA_PUBLIC_KEY_FP matches exactly."
                    )
                    hints.append(
                        "If fingerprints differ, run ALTER USER … SET RSA_PUBLIC_KEY with the public key "
                        "derived from this private key (same account you connect to)."
                    )
                except Exception as fp_exc:  # noqa: BLE001
                    hints.append(
                        f"Could not derive public-key fingerprint from the PEM/passphrase: {fp_exc}"
                    )
            return [{"name": "Snowflake", "status": "down", "latency_ms": None,
                     "detail": interpreted["summary"],
                     "raw_error": interpreted["raw_error"],
                     "auth_mode": interpreted["auth_mode"],
                     "hints": hints}]

    def _probe_version_ephemeral(self) -> str:
        """One-off connect for Test — does not touch the shared session pool."""
        import snowflake.connector as sf
        params = self._connect_params()
        params.setdefault("network_timeout", 600)
        auth = str(params.get("authenticator") or "").lower()
        if auth == "externalbrowser" or auth.startswith("http"):
            params.setdefault("client_store_temporary_credential", True)
        conn = None
        try:
            conn = sf.connect(**params)
            cur = conn.cursor()
            cur.execute("SELECT CURRENT_VERSION()")
            return cur.fetchone()[0]
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _connect_params(self) -> Dict[str, Any]:
        sfc = self.settings["snowflake"]
        params: Dict[str, Any] = {
            "account": sanitize_snowflake_account(sfc.get("account", "")),
            "user": (sfc.get("user") or "").strip(),
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
        if is_keypair_auth(auth_lower):
            pem = str(sfc.get("private_key_pem") or "")
            passphrase = str(sfc.get("private_key_passphrase") or "")
            params["private_key"] = _load_private_key_der(pem, passphrase)
            # Explicit JWT auth avoids any leftover password authenticator defaults.
            params["authenticator"] = "SNOWFLAKE_JWT"
        elif auth_lower in ("sso", "externalbrowser"):
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
        elif is_sso_auth(auth_lower):
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
                        with _query_lock:
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
        monitor_wh = str(task_cfg.get("warehouse") or "").strip()
        session_wh = str((self.settings.get("snowflake") or {}).get("warehouse") or "").strip()
        range_from, range_to = _default_task_date_range(future_days)
        if date_from:
            range_from = _iso_date(date_from) or range_from
        if date_to:
            range_to = _iso_date(date_to, end_of_day=True) or range_to

        # Prefer ACCOUNT_USAGE with DATABASE_NAME/NAME pushdown for dashboard windows.
        # INFORMATION_SCHEMA.TASK_HISTORY applies RESULT_LIMIT before those filters and
        # was the dominant /api/summary cost (often 60s+ under warehouse overload).
        today = datetime.now(timezone.utc).date()
        try:
            from_d = datetime.fromisoformat(range_from).date()
            to_d = datetime.fromisoformat(range_to).date()
        except ValueError:
            from_d, to_d = today - timedelta(days=7), today
        info_schema_floor = today - timedelta(days=_INFO_SCHEMA_LOOKBACK_DAYS)
        use_fast_history = (not _USE_ACCOUNT_USAGE_FOR_RECENT) and from_d >= info_schema_floor

        try:
            cur = self._connect().cursor()
            if use_fast_history:
                # INFORMATION_SCHEMA.TASK_HISTORY rejects windows longer than 7 days.
                hist_start = from_d.isoformat()
                hist_end = min(to_d + timedelta(days=1), from_d + timedelta(days=7)).isoformat()
                params = (hist_start, hist_end, monitor_db, name_pattern, range_from, range_to)
                sql = f"""
                    SELECT
                        NAME              AS TASK_NAME,
                        DATABASE_NAME,
                        SCHEMA_NAME,
                        STATE,
                        {_task_result_case_sql()} AS TASK_RESULT,
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
                        DATEDIFF(
                            'second',
                            COALESCE(QUERY_START_TIME, SCHEDULED_TIME),
                            COMPLETED_TIME
                        ) AS DURATION_S
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
                    ORDER BY TASK_NAME, SCHEDULED_TIME DESC
                """
            else:
                params = (range_from, range_to, monitor_db, name_pattern)
                sql = f"""
                SELECT
                    TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE,
                    {_task_result_case_sql()} AS TASK_RESULT,
                    SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                    QUERY_ID, ERROR_CODE, ERROR_MESSAGE, RECORD_TYPE,
                    DATEDIFF(
                        'second',
                        COALESCE(QUERY_START_TIME, SCHEDULED_TIME),
                        COMPLETED_TIME
                    ) AS DURATION_S
                FROM (
                    SELECT
                        NAME AS TASK_NAME, DATABASE_NAME, SCHEMA_NAME, STATE, QUERY_ID,
                        SCHEDULED_TIME, QUERY_START_TIME, COMPLETED_TIME,
                        ERROR_CODE, ERROR_MESSAGE,
                        CASE
                            WHEN UPPER(STATE) = 'SCHEDULED' THEN 'SCHEDULED'
                            ELSE 'HISTORICAL'
                        END AS RECORD_TYPE
                    FROM SNOWFLAKE.ACCOUNT_USAGE.TASK_HISTORY
                    WHERE SCHEDULED_TIME >= GREATEST(
                            %s::TIMESTAMP_LTZ,
                            DATEADD(MONTH, -{historical_months}, CURRENT_TIMESTAMP())
                          )
                      AND SCHEDULED_TIME < DATEADD(DAY, 1, %s::DATE)
                      AND DATABASE_NAME = %s
                      AND NAME ILIKE %s
                )
                ORDER BY TASK_NAME, SCHEDULED_TIME DESC
                """
            # Upcoming SCHEDULED rows live in INFORMATION_SCHEMA (ACCOUNT_USAGE lags / omits them).
            # Cap to 7 days (Snowflake limit) and settings future_days.
            sched_horizon = min(max(1, int(future_days or 1)), 7)
            sched_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            sched_end = (datetime.now(timezone.utc) + timedelta(days=sched_horizon)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            sched_params = (sched_start, sched_end, monitor_db, name_pattern)
            sched_sql = f"""
                SELECT
                    NAME AS TASK_NAME,
                    DATABASE_NAME,
                    SCHEMA_NAME,
                    STATE,
                    {_task_result_case_sql()} AS TASK_RESULT,
                    SCHEDULED_TIME,
                    QUERY_START_TIME,
                    COMPLETED_TIME,
                    QUERY_ID,
                    ERROR_CODE,
                    ERROR_MESSAGE,
                    'SCHEDULED' AS RECORD_TYPE,
                    NULL AS DURATION_S
                FROM TABLE(
                    INFORMATION_SCHEMA.TASK_HISTORY(
                        SCHEDULED_TIME_RANGE_START => %s::TIMESTAMP_LTZ,
                        SCHEDULED_TIME_RANGE_END   => %s::TIMESTAMP_LTZ,
                        RESULT_LIMIT => {_TASK_HISTORY_RESULT_LIMIT}
                    )
                )
                WHERE DATABASE_NAME = %s
                  AND NAME ILIKE %s
                  AND UPPER(STATE) = 'SCHEDULED'
                  AND SCHEDULED_TIME >= CURRENT_TIMESTAMP()
                ORDER BY TASK_NAME, SCHEDULED_TIME ASC
            """
            log_sql(sql[:500], "monitoring:telemetry", "READ", allowed=True)
            log_sql(sched_sql[:500], "monitoring:scheduled", "READ", allowed=True)
            # Serialize: shared connection cannot run overlapping cursors.
            with _query_lock:
                cur.execute(
                    f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_TELEMETRY_STATEMENT_TIMEOUT_S}"
                )
                # Use configured monitor warehouse when it differs from the session WH.
                switched_wh = False
                if (
                    monitor_wh
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_$]*", monitor_wh)
                    and monitor_wh.upper() != session_wh.upper()
                ):
                    cur.execute(f"USE WAREHOUSE {monitor_wh}")
                    switched_wh = True
                try:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    cur.execute(sched_sql, sched_params)
                    sched_rows = cur.fetchall()
                finally:
                    if (
                        switched_wh
                        and session_wh
                        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_$]*", session_wh)
                    ):
                        try:
                            cur.execute(f"USE WAREHOUSE {session_wh}")
                        except Exception:
                            pass
            # All completed/skipped/failed/running runs per task (exclude planned).
            for row in rows:
                rec = _record_from_task_row(row)
                if _is_scheduled_row(row[3], row[4], row[11]):
                    continue
                records.append(rec)
            # Next upcoming planned run per task (ORDER BY SCHEDULED_TIME ASC).
            seen_sched = set()
            for row in sched_rows:
                rec = _record_from_task_row(row)
                rec["status"] = "SCHEDULED"
                rec["record_type"] = "SCHEDULED"
                task_key = (rec.get("database"), rec.get("schema"), row[0])
                if task_key in seen_sched:
                    continue
                seen_sched.add(task_key)
                records.append(rec)
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
        """Databases where DDL/DML is permitted (preprod naming conventions only)."""
        sf = self.settings.get("snowflake", {})
        allowed = []
        preprod = (sf.get("preprod_account") or "").strip()
        if preprod:
            allowed.append(preprod)
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

    def run_query(self, sql: str, source: str = "unknown",
                  allow_write: bool = False) -> Dict[str, Any]:
        """Execute a SQL query with guardrail protection.

        DDL/DML on production is blocked unless allow_write=True.
        All queries are logged to the audit trail.
        """
        return self._execute_with_guardrail(sql, source=source, allow_write=allow_write)
