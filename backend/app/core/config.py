"""Central configuration. Reads env + a runtime-mutable settings file."""
from __future__ import annotations
import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_DIR = Path(__file__).resolve().parents[1]          # .../backend/app
BACKEND_DIR = APP_DIR.parent                           # .../backend
PROJECT_ROOT = BACKEND_DIR.parent                      # .../ops-monitor


def _load_env_file(path: Path, force: bool = False) -> None:
    """Load a .env file into os.environ. With force=True, overrides existing vars."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and (force or key not in os.environ):
            os.environ[key] = value


_load_env_file(PROJECT_ROOT / ".env")
SETTINGS_FILE = APP_DIR / "data" / "connection_settings.json"
SKILLS_DIR = APP_DIR / "skills"
MEMORY_DIR = APP_DIR / "memory" / "store"

# Snowflake DQ monitoring (DQM_VALIDATION_SUMMARY) — env defaults; overridden by settings file
DQ_TABLE_FQN = os.getenv(
    "DQ_TABLE_FQN",
    "CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY",
)
DQ_SUBJECT_AREA = os.getenv("DQ_SUBJECT_AREA", "Lynkuet LAAD")
DQ_RULES_TABLE_FQN = os.getenv(
    "DQ_RULES_TABLE_FQN",
    "CPH_DB_PROD.MODEL_V2.CONFIG_LYNKUET",
)

# Snowflake task monitoring (ACCOUNT_USAGE + TASK_HISTORY) — env defaults; overridden by settings file
TASK_MONITOR_DATABASE = os.getenv("TASK_MONITOR_DATABASE", "CPH_DB_PROD")
TASK_NAME_PATTERN = os.getenv("TASK_NAME_PATTERN", "TASK%")
TASK_HISTORICAL_MONTHS = int(os.getenv("TASK_HISTORICAL_MONTHS", "2"))
TASK_FUTURE_DAYS = int(os.getenv("TASK_FUTURE_DAYS", "7"))
# Dashboard TASK_HISTORY warehouse (empty = keep the Snowflake session warehouse).
TASK_MONITOR_WAREHOUSE = os.getenv("TASK_MONITOR_WAREHOUSE", "PROD_ETL_DAYLIGHT_WH_XL")
# Session STATEMENT_TIMEOUT_IN_SECONDS for dashboard TASK_HISTORY queries (default 180).
TASK_TELEMETRY_STATEMENT_TIMEOUT_S = os.getenv("TASK_TELEMETRY_STATEMENT_TIMEOUT_S", "180")

# Lineage traversal — how upstream tracing recognises the source/ingestion (L1) layer.
# Tracing only stops on an explicit match; a table with no discoverable producer and no
# match here is reported as unresolved rather than assumed to be a source.
LINEAGE_SOURCE_SCHEMAS = os.getenv(
    "LINEAGE_SOURCE_SCHEMAS", "L1,RAW,RAW_V2,LANDING,INGEST,INGESTION")
LINEAGE_SOURCE_TABLE_PREFIXES = os.getenv("LINEAGE_SOURCE_TABLE_PREFIXES", "L1_,RAW_,SRC_")
LINEAGE_SOURCE_TAG = os.getenv("LINEAGE_SOURCE_TAG", "DATA_LAYER")
LINEAGE_SOURCE_TAG_VALUES = os.getenv("LINEAGE_SOURCE_TAG_VALUES", "L1,RAW,SOURCE,INGESTION")
LINEAGE_MAX_DEPTH = int(os.getenv("LINEAGE_MAX_DEPTH", "8"))
LINEAGE_MAX_LOOKUPS = int(os.getenv("LINEAGE_MAX_LOOKUPS", "60"))

# Sanitize proxy env vars (some corporate VDIs inject newlines that break httpx)
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    _val = os.environ.get(_proxy_var)
    if _val and _val != _val.strip():
        os.environ[_proxy_var] = _val.strip()

# Anthropic / Claude harness
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4.5")
USE_CLAUDE = bool(ANTHROPIC_API_KEY)
# Prompt caching on system/skill blocks (Anthropic cache_control ephemeral)
LLM_CACHE_CONTROL = os.getenv("LLM_CACHE_CONTROL", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

SF_OPTIONAL_FIELDS = frozenset({
    "database", "schema", "preprod_account", "user", "private_key_passphrase",
})
AWS_OPTIONAL_FIELDS = frozenset({"session_token", "region"})

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "aws": {
        "region": "",
        "access_key_id": "",
        "secret_access_key": "",
        "session_token": "",
    },
    "snowflake": {
        "account": "",
        "user": "",
        "authenticator": "password",
        "password": "",
        "private_key_pem": "",
        "private_key_passphrase": "",
        "warehouse": "",
        "role": "",
        "read_only_role": "",
        "database": "",
        "schema": "",
        "preprod_account": "",
    },
    "monitoring": {
        "tasks": {
            "monitor_database": TASK_MONITOR_DATABASE,
            "name_pattern": TASK_NAME_PATTERN,
            "historical_months": TASK_HISTORICAL_MONTHS,
            "future_days": TASK_FUTURE_DAYS,
            "warehouse": TASK_MONITOR_WAREHOUSE,
        },
        "dq": {
            "table_fqn": DQ_TABLE_FQN,
            "subject_area": DQ_SUBJECT_AREA,
            "rules_table_fqn": DQ_RULES_TABLE_FQN,
        },
        "lineage": {
            "source_schemas": LINEAGE_SOURCE_SCHEMAS,
            "source_table_prefixes": LINEAGE_SOURCE_TABLE_PREFIXES,
            "source_tag": LINEAGE_SOURCE_TAG,
            "source_tag_values": LINEAGE_SOURCE_TAG_VALUES,
            "max_depth": LINEAGE_MAX_DEPTH,
            "max_lookups": LINEAGE_MAX_LOOKUPS,
        },
    },
}

_MONITORING_SECTIONS = ("tasks", "dq", "lineage")


def is_sso_auth(authenticator: str) -> bool:
    a = (authenticator or "password").strip().lower()
    return a in ("sso", "externalbrowser") or a.startswith("http")


def is_keypair_auth(authenticator: str) -> bool:
    a = (authenticator or "password").strip().lower()
    return a in ("keypair", "key_pair", "private_key", "snowflake_jwt")


def snowflake_auth_mode(authenticator: str) -> str:
    if is_sso_auth(authenticator):
        return "sso"
    if is_keypair_auth(authenticator):
        return "keypair"
    return "password"


def snowflake_configured(sf: Dict[str, Any]) -> bool:
    if not all(str(sf.get(k) or "").strip() for k in ("account", "warehouse", "role")):
        return False
    auth = sf.get("authenticator", "password")
    if is_sso_auth(auth):
        return True
    if is_keypair_auth(auth):
        return (bool(str(sf.get("user") or "").strip())
                and bool(str(sf.get("private_key_pem") or "").strip()))
    return (bool(str(sf.get("user") or "").strip())
            and bool(str(sf.get("password") or "").strip()))


def aws_configured(aws: Dict[str, Any]) -> bool:
    return bool(str(aws.get("access_key_id") or "").strip()
                and str(aws.get("secret_access_key") or "").strip())


def platforms_configured(settings: Dict[str, Any] | None = None) -> Dict[str, bool]:
    s = settings or load_settings()
    return {
        "snowflake": snowflake_configured(s.get("snowflake", {})),
        "aws": aws_configured(s.get("aws", {})),
    }


def validate_snowflake_settings(sf: Dict[str, Any]) -> list[str]:
    """Return list of validation errors for partially filled Snowflake config."""
    if not any(str(sf.get(k) or "").strip() for k in (
            "account", "user", "warehouse", "role", "password", "database", "schema",
            "private_key_pem", "private_key_passphrase")):
        return []
    missing = [f for f in ("account", "warehouse", "role")
               if not str(sf.get(f) or "").strip()]
    auth = sf.get("authenticator", "password")
    if is_sso_auth(auth):
        if missing:
            return [f"Snowflake SSO requires: {', '.join(missing)}"]
        return []
    if is_keypair_auth(auth):
        if not str(sf.get("user") or "").strip():
            missing.append("user")
        if missing:
            return [f"Snowflake key-pair auth requires: {', '.join(missing)}"]
        if not str(sf.get("private_key_pem") or "").strip():
            return ["Snowflake private key (PEM) is required for key-pair login."]
        return []
    if not str(sf.get("user") or "").strip():
        missing.append("user")
    if missing:
        return [f"Snowflake requires: {', '.join(missing)}"]
    if not str(sf.get("password") or "").strip():
        return ["Snowflake password is required for password login (or switch to SSO / key-pair)."]
    return []


def _default_aws() -> Dict[str, Any]:
    return deepcopy(_DEFAULT_SETTINGS["aws"])


def _default_snowflake() -> Dict[str, Any]:
    return deepcopy(_DEFAULT_SETTINGS["snowflake"])


def _default_monitoring() -> Dict[str, Any]:
    return deepcopy(_DEFAULT_SETTINGS["monitoring"])


def _new_connection_id() -> str:
    return str(uuid.uuid4())


def _merge_monitoring(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Overlay stored monitoring values on the defaults, section by section."""
    stored = raw or {}
    defaults = _default_monitoring()
    return {
        section: {**defaults[section], **(stored.get(section) or {})}
        for section in _MONITORING_SECTIONS
    }


def _make_profile(
    name: str = "Default",
    *,
    profile_id: Optional[str] = None,
    aws: Optional[Dict[str, Any]] = None,
    snowflake: Optional[Dict[str, Any]] = None,
    monitoring: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": profile_id or _new_connection_id(),
        "name": (name or "Default").strip() or "Default",
        "aws": aws if aws is not None else _default_aws(),
        "snowflake": normalize_snowflake_auth(
            snowflake if snowflake is not None else _default_snowflake()
        ),
        "monitoring": normalize_monitoring_settings(
            monitoring if monitoring is not None else _default_monitoring()
        ),
    }


def _profile_summary(profile: Dict[str, Any], active_id: Optional[str]) -> Dict[str, Any]:
    sf = profile.get("snowflake") or {}
    return {
        "id": profile.get("id"),
        "name": profile.get("name") or "Untitled",
        "account": sf.get("account") or "",
        "user": sf.get("user") or "",
        "warehouse": sf.get("warehouse") or "",
        "role": sf.get("role") or "",
        "database": sf.get("database") or "",
        "auth_mode": snowflake_auth_mode(sf.get("authenticator", "password")),
        "active": profile.get("id") == active_id,
        "platforms": {
            "snowflake": snowflake_configured(sf),
            "aws": aws_configured(profile.get("aws") or {}),
        },
    }


def _find_connection(connections: List[Dict[str, Any]], connection_id: str) -> Optional[Dict[str, Any]]:
    for c in connections:
        if c.get("id") == connection_id:
            return c
    return None


def _mirror_active_into_toplevel(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Copy the active profile's aws/snowflake/monitoring onto top-level keys."""
    connections = list(settings.get("connections") or [])
    active_id = settings.get("active_connection_id")
    active = _find_connection(connections, active_id) if active_id else None
    if active is None and connections:
        active = connections[0]
        settings["active_connection_id"] = active["id"]
    if active is None:
        settings["aws"] = _default_aws()
        settings["snowflake"] = _default_snowflake()
        settings["monitoring"] = _default_monitoring()
        return settings
    settings["aws"] = deepcopy(active.get("aws") or _default_aws())
    settings["snowflake"] = normalize_snowflake_auth(
        deepcopy(active.get("snowflake") or _default_snowflake())
    )
    settings["monitoring"] = normalize_monitoring_settings(
        deepcopy(active.get("monitoring") or _default_monitoring())
    )
    return settings


def _sync_toplevel_into_active(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Write top-level aws/snowflake/monitoring back into the active profile."""
    connections = list(settings.get("connections") or [])
    active_id = settings.get("active_connection_id")
    if not connections:
        profile = _make_profile(
            "Default",
            aws=deepcopy(settings.get("aws") or _default_aws()),
            snowflake=deepcopy(settings.get("snowflake") or _default_snowflake()),
            monitoring=deepcopy(settings.get("monitoring") or _default_monitoring()),
        )
        settings["connections"] = [profile]
        settings["active_connection_id"] = profile["id"]
        return settings
    active = _find_connection(connections, active_id) if active_id else None
    if active is None:
        active = connections[0]
        settings["active_connection_id"] = active["id"]
    active["aws"] = deepcopy(settings.get("aws") or _default_aws())
    active["snowflake"] = normalize_snowflake_auth(
        deepcopy(settings.get("snowflake") or _default_snowflake())
    )
    active["monitoring"] = normalize_monitoring_settings(
        deepcopy(settings.get("monitoring") or _default_monitoring())
    )
    settings["connections"] = connections
    return settings


def _ensure_connections_structure(data: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    """Migrate legacy single-slot settings into connections[] + active_connection_id.

    Returns (settings, migrated) where migrated means the on-disk shape should be rewritten.
    """
    out = deepcopy(data)
    connections = out.get("connections")
    if isinstance(connections, list) and connections:
        # Normalize each profile
        normalized: List[Dict[str, Any]] = []
        changed = False
        for raw in connections:
            if not isinstance(raw, dict):
                changed = True
                continue
            pid = str(raw.get("id") or "").strip() or _new_connection_id()
            if not str(raw.get("id") or "").strip():
                changed = True
            normalized.append(_make_profile(
                str(raw.get("name") or "Untitled"),
                profile_id=pid,
                aws={**_default_aws(), **(raw.get("aws") or {})},
                snowflake={**_default_snowflake(), **(raw.get("snowflake") or {})},
                monitoring=_merge_monitoring(raw.get("monitoring")),
            ))
        out["connections"] = normalized
        active_id = out.get("active_connection_id")
        if not active_id or not _find_connection(normalized, active_id):
            out["active_connection_id"] = normalized[0]["id"]
            changed = True
        return _mirror_active_into_toplevel(out), changed

    # Legacy: promote top-level aws/snowflake/monitoring into Default profile
    profile = _make_profile(
        "Default",
        profile_id="default",
        aws={**_default_aws(), **(out.get("aws") or {})},
        snowflake={**_default_snowflake(), **(out.get("snowflake") or {})},
        monitoring=_merge_monitoring(out.get("monitoring")),
    )
    out["connections"] = [profile]
    out["active_connection_id"] = profile["id"]
    return _mirror_active_into_toplevel(out), True


def load_settings() -> Dict[str, Any]:
    base = json.loads(json.dumps(_DEFAULT_SETTINGS))
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for section in ("aws", "snowflake"):
                if section in data and isinstance(data[section], dict):
                    base[section].update(data[section])
            if "monitoring" in data and isinstance(data["monitoring"], dict):
                for subsection in _MONITORING_SECTIONS:
                    if subsection in data["monitoring"]:
                        for k, v in data["monitoring"][subsection].items():
                            if v is None:
                                continue
                            if isinstance(v, str) and not v.strip():
                                continue
                            base["monitoring"][subsection][k] = v
            if "authenticator" not in data.get("snowflake", {}):
                base["snowflake"]["authenticator"] = "password"
            if "connections" in data:
                base["connections"] = data["connections"]
            if "active_connection_id" in data:
                base["active_connection_id"] = data["active_connection_id"]
            ensured, migrated = _ensure_connections_structure(base)
            if migrated:
                # Persist migration once so ids stay stable across reloads.
                SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
                SETTINGS_FILE.write_text(json.dumps(ensured, indent=2), encoding="utf-8")
            return ensured
        except json.JSONDecodeError:
            pass
    ensured, _ = _ensure_connections_structure(base)
    return ensured


def get_connection_by_id(connection_id: str, settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    s = settings or load_settings()
    return _find_connection(list(s.get("connections") or []), connection_id)


def list_connection_summaries(settings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    s = settings or load_settings()
    active_id = s.get("active_connection_id")
    return [_profile_summary(c, active_id) for c in (s.get("connections") or [])]


def settings_slice_for_connection(connection_id: Optional[str] = None) -> Dict[str, Any]:
    """Return a settings-like dict (aws/sf/monitoring) for merge/test of a profile.

    When connection_id is None, uses the active profile (current load_settings mirror).
    """
    s = load_settings()
    if not connection_id:
        return {
            "aws": deepcopy(s.get("aws") or _default_aws()),
            "snowflake": deepcopy(s.get("snowflake") or _default_snowflake()),
            "monitoring": deepcopy(s.get("monitoring") or _default_monitoring()),
        }
    profile = get_connection_by_id(connection_id, s)
    if not profile:
        return {
            "aws": _default_aws(),
            "snowflake": _default_snowflake(),
            "monitoring": _default_monitoring(),
        }
    return {
        "aws": deepcopy(profile.get("aws") or _default_aws()),
        "snowflake": deepcopy(profile.get("snowflake") or _default_snowflake()),
        "monitoring": deepcopy(profile.get("monitoring") or _default_monitoring()),
    }


def upsert_connection(
    *,
    name: str,
    aws: Dict[str, Any],
    snowflake: Dict[str, Any],
    monitoring: Dict[str, Any],
    connection_id: Optional[str] = None,
    activate: bool = True,
) -> Dict[str, Any]:
    """Create or update a named profile. Optionally make it active. Returns full settings."""
    s = load_settings()
    connections = list(s.get("connections") or [])
    sf = normalize_snowflake_auth(deepcopy(snowflake))
    mon = normalize_monitoring_settings(deepcopy(monitoring))
    aws_cfg = deepcopy(aws)

    existing = _find_connection(connections, connection_id) if connection_id else None
    if existing:
        existing["name"] = (name or existing.get("name") or "Untitled").strip() or "Untitled"
        existing["aws"] = aws_cfg
        existing["snowflake"] = sf
        existing["monitoring"] = mon
        target_id = existing["id"]
    else:
        profile = _make_profile(
            name or "Untitled",
            profile_id=connection_id or None,
            aws=aws_cfg,
            snowflake=sf,
            monitoring=mon,
        )
        connections.append(profile)
        target_id = profile["id"]

    s["connections"] = connections
    if activate or s.get("active_connection_id") == target_id or not s.get("active_connection_id"):
        s["active_connection_id"] = target_id
    s = _mirror_active_into_toplevel(s)
    return save_settings(s)


def set_active_connection(connection_id: str) -> Dict[str, Any]:
    s = load_settings()
    if not _find_connection(list(s.get("connections") or []), connection_id):
        raise KeyError(f"Connection not found: {connection_id}")
    s["active_connection_id"] = connection_id
    s = _mirror_active_into_toplevel(s)
    return save_settings(s)


def delete_connection(connection_id: str) -> Dict[str, Any]:
    s = load_settings()
    connections = list(s.get("connections") or [])
    if len(connections) <= 1:
        raise ValueError("Cannot delete the last saved connection.")
    if not _find_connection(connections, connection_id):
        raise KeyError(f"Connection not found: {connection_id}")
    remaining = [c for c in connections if c.get("id") != connection_id]
    s["connections"] = remaining
    if s.get("active_connection_id") == connection_id:
        s["active_connection_id"] = remaining[0]["id"]
    s = _mirror_active_into_toplevel(s)
    return save_settings(s)


def get_task_monitoring_config(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Effective task monitoring config (settings file overrides env defaults)."""
    s = settings or load_settings()
    tasks = s.get("monitoring", {}).get("tasks", {})
    return {
        "monitor_database": str(tasks.get("monitor_database") or TASK_MONITOR_DATABASE).strip(),
        "name_pattern": str(tasks.get("name_pattern") or TASK_NAME_PATTERN).strip(),
        "historical_months": int(tasks.get("historical_months") or TASK_HISTORICAL_MONTHS),
        "future_days": int(tasks.get("future_days") or TASK_FUTURE_DAYS),
        "warehouse": str(tasks.get("warehouse") or TASK_MONITOR_WAREHOUSE).strip(),
    }


def get_dq_monitoring_config(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Effective DQ monitoring config (settings file overrides env defaults)."""
    s = settings or load_settings()
    dq = s.get("monitoring", {}).get("dq", {})
    return {
        "table_fqn": str(dq.get("table_fqn") or DQ_TABLE_FQN).strip(),
        "subject_area": str(dq.get("subject_area") or DQ_SUBJECT_AREA).strip(),
        "rules_table_fqn": str(dq.get("rules_table_fqn") or DQ_RULES_TABLE_FQN).strip(),
    }


def _as_upper_list(value: Any, default: str) -> List[str]:
    """Accept either a comma-separated string or a list; normalize to upper-case tokens."""
    raw = value if value not in (None, "") else default
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return [str(i).strip().upper() for i in items if str(i).strip()]


def get_lineage_config(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Effective lineage traversal config (rules that terminate upstream tracing)."""
    s = settings or load_settings()
    lin = s.get("monitoring", {}).get("lineage", {}) or {}
    try:
        max_depth = int(lin.get("max_depth") or LINEAGE_MAX_DEPTH)
    except (TypeError, ValueError):
        max_depth = LINEAGE_MAX_DEPTH
    try:
        max_lookups = int(lin.get("max_lookups") or LINEAGE_MAX_LOOKUPS)
    except (TypeError, ValueError):
        max_lookups = LINEAGE_MAX_LOOKUPS
    return {
        "source_schemas": _as_upper_list(
            lin.get("source_schemas"), LINEAGE_SOURCE_SCHEMAS),
        "source_table_prefixes": _as_upper_list(
            lin.get("source_table_prefixes"), LINEAGE_SOURCE_TABLE_PREFIXES),
        "source_tag": str(
            lin.get("source_tag") or LINEAGE_SOURCE_TAG).strip().upper(),
        "source_tag_values": _as_upper_list(
            lin.get("source_tag_values"), LINEAGE_SOURCE_TAG_VALUES),
        "max_depth": max(1, max_depth),
        "max_lookups": max(0, max_lookups),
    }


def validate_monitoring_settings(monitoring: Dict[str, Any]) -> list[str]:
    """Validate effective monitoring config (stored values fall back to defaults)."""
    wrapper = {"monitoring": monitoring or {}}
    tasks = get_task_monitoring_config(wrapper)
    dq = get_dq_monitoring_config(wrapper)
    lineage = get_lineage_config(wrapper)
    errors: list[str] = []
    if not lineage["source_schemas"] and not lineage["source_tag"]:
        errors.append(
            "Lineage tracing needs at least one source schema or a source tag, "
            "otherwise upstream traversal can never reach the ingestion layer.")

    if not tasks["monitor_database"]:
        errors.append("Task monitoring requires a database name.")
    if not tasks["name_pattern"]:
        errors.append("Task monitoring requires a name pattern (e.g. TASK%).")
    if tasks["historical_months"] < 1:
        errors.append("Historical months must be at least 1.")
    if tasks["future_days"] < 1:
        errors.append("Future days must be at least 1.")
    if not dq["table_fqn"]:
        errors.append("DQ monitoring requires a fully qualified table name.")
    return errors


def normalize_monitoring_settings(monitoring: Dict[str, Any]) -> Dict[str, Any]:
    """Persist effective monitoring values (never store blank overrides)."""
    wrapper = {"monitoring": monitoring or {}}
    return {
        "tasks": get_task_monitoring_config(wrapper),
        "dq": get_dq_monitoring_config(wrapper),
        "lineage": get_lineage_config(wrapper),
    }


def normalize_snowflake_auth(sf: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure password / SSO / key-pair secrets are mutually exclusive in stored settings."""
    from app.connectors.snowflake_connector import sanitize_snowflake_account

    sf = dict(sf)
    if sf.get("account"):
        sf["account"] = sanitize_snowflake_account(str(sf.get("account") or ""))
    auth = sf.get("authenticator", "password")
    if is_sso_auth(auth):
        sf["password"] = ""
        sf["private_key_pem"] = ""
        sf["private_key_passphrase"] = ""
        auth_val = (sf.get("authenticator") or "sso").strip()
        sf["authenticator"] = auth_val if auth_val.lower().startswith("http") else "sso"
    elif is_keypair_auth(auth):
        sf["password"] = ""
        sf["authenticator"] = "keypair"
        sf.setdefault("private_key_pem", "")
        sf.setdefault("private_key_passphrase", "")
        # Env/Secrets pastes often use literal \n; store real multiline PEM.
        pem = str(sf.get("private_key_pem") or "")
        if pem and ("\\n" in pem or "\\r" in pem):
            sf["private_key_pem"] = (
                pem.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n").strip()
            )
    else:
        sf["authenticator"] = "password"
        sf["private_key_pem"] = ""
        sf["private_key_passphrase"] = ""
    return sf


def interpret_snowflake_error(error: str, sf: Dict[str, Any]) -> Dict[str, Any]:
    """Turn raw Snowflake driver errors into auth-mode-aware guidance for chat/UI."""
    raw = error or "Connection failed."
    mode = snowflake_auth_mode(sf.get("authenticator", "password"))
    lower = raw.lower()
    hints: list[str] = []

    # Detect missing package before any auth-mode branching — this is never an auth error.
    if "no module named" in lower and "snowflake" in lower:
        return {
            "summary": (
                "snowflake-connector-python is not installed. "
                "Run: pip install snowflake-connector-python"
            ),
            "raw_error": raw,
            "auth_mode": mode,
            "hints": [
                "Install the Snowflake Python driver in your virtual environment:",
                "  cd backend && .venv\\Scripts\\pip.exe install snowflake-connector-python",
                "Then restart the backend and test connectivity again.",
            ],
        }
    if "no module named" in lower and "cryptography" in lower:
        return {
            "summary": "cryptography is required to load Snowflake private keys.",
            "raw_error": raw,
            "auth_mode": mode,
            "hints": [
                "Install cryptography in the backend virtual environment:",
                "  cd backend && .venv\\Scripts\\pip.exe install cryptography",
                "Then restart the backend and test connectivity again.",
            ],
        }

    if mode == "sso":
        summary = (
            "Snowflake SSO login failed. "
            "SSO does not use a password — a browser window must open on the backend machine for IdP sign-in."
        )
        if "incorrect username or password" in lower or "250001" in raw:
            summary = (
                "Snowflake SSO login failed. The 'incorrect username or password' message is misleading for SSO — "
                "it usually means browser SSO did not complete, the stored login method is still password auth, "
                "or the Snowflake login name / account identifier does not match your org."
            )
            hints += [
                "In Settings, set Login method to SSO and click Save & Test again (password is cleared automatically).",
                "Watch for a browser window on the machine running the backend and complete IdP sign-in within 2 minutes.",
                "User field is your Snowflake login name (often your corporate email) — not a password.",
                "Leave Password blank for SSO; do not re-enter a masked password.",
                "Verify Account format with your admin (e.g. org-account or account.region).",
            ]
        elif "timeout" in lower or "timed out" in lower:
            summary = "Snowflake SSO timed out — browser sign-in was not completed in time."
            hints += [
                "Retry Save & Test and complete the browser login within 2 minutes.",
                "Ensure the backend host allows opening a default browser.",
            ]
        elif "browser" in lower or "external" in lower:
            summary = "Snowflake SSO browser authentication could not start or finish."
            hints += [
                "Run the backend on a machine with a desktop/browser (not headless).",
                "Check firewall/proxy rules for Snowflake and your IdP.",
            ]
        else:
            hints += [
                "Confirm Login method is SSO in Settings and save again.",
                "Complete browser IdP sign-in when testing connectivity.",
                f"Technical detail: {raw[:200]}",
            ]
    elif mode == "keypair":
        summary = "Snowflake key-pair login failed."
        if "passphrase" in lower or "bad decrypt" in lower or "incorrect password" in lower:
            summary = "Could not decrypt the private key — check the passphrase."
            hints += [
                "If the PEM is encrypted, enter the correct private key passphrase.",
                "If the key is unencrypted, leave the passphrase blank.",
            ]
        elif "private key" in lower or "could not deserialize" in lower or "pem" in lower:
            summary = "Private key PEM could not be loaded."
            hints += [
                "Paste a full PKCS#8 PEM (BEGIN PRIVATE KEY / BEGIN ENCRYPTED PRIVATE KEY).",
                "Single-line keys with \\n (from .env / Secrets Manager) are supported after a Settings save.",
                "Confirm the matching public key is assigned on the Snowflake user (RSA_PUBLIC_KEY).",
            ]
        elif "jwt" in lower or "390144" in raw or "250001" in raw or "authentication" in lower:
            summary = (
                "Snowflake rejected the key-pair JWT. Usually the public key on the user "
                "does not match this private key, or the user/account is wrong."
            )
            hints += [
                "Confirm ALTER USER … SET RSA_PUBLIC_KEY='…' used the public key for this private key.",
                "User must be the Snowflake login name that owns the registered public key.",
                "Masked private keys (with ***) are not re-sent — paste the full PEM again if needed.",
                "Verify Account identifier format (e.g. org-account).",
            ]
        elif not str(sf.get("private_key_pem") or "").strip():
            hints.append("Private key PEM is empty — paste the key or switch login method.")
        else:
            hints += [
                "Confirm Login method is Key pair in Settings and save again.",
                f"Technical detail: {raw[:200]}",
            ]
    else:
        summary = raw
        if "incorrect username or password" in lower or "250001" in raw:
            hints += [
                "Verify Snowflake User (login name) and Password in Settings.",
                "If you intend to use SSO or key-pair, switch Login method and save — password auth will be disabled.",
                "Masked passwords (with ***) are not re-sent; enter the full password again if needed.",
            ]
        elif not str(sf.get("password") or "").strip():
            hints.append("Password is empty — enter your Snowflake password or switch to SSO / key-pair.")

    if not hints:
        hints.append("Review Account, User, Warehouse, Role, and login method in Settings → Snowflake.")

    return {"summary": summary, "raw_error": raw, "auth_mode": mode, "hints": hints}


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Persist settings. Top-level aws/snowflake/monitoring win over stale connection profiles.

    Callers (e.g. Settings save after SSO switch) update top-level first. We must sync those
    values into the active profile — not re-mirror the old profile over them.
    """
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Capture caller updates before ensure/mirror can clobber them with the stored profile.
    top_aws = deepcopy(settings.get("aws")) if "aws" in settings else None
    top_sf = deepcopy(settings.get("snowflake")) if "snowflake" in settings else None
    top_mon = deepcopy(settings.get("monitoring")) if "monitoring" in settings else None

    settings, _ = _ensure_connections_structure(settings)

    if top_aws is not None:
        settings["aws"] = top_aws
    if top_sf is not None:
        settings["snowflake"] = normalize_snowflake_auth(top_sf)
    if top_mon is not None:
        settings["monitoring"] = normalize_monitoring_settings(top_mon)

    settings = _sync_toplevel_into_active(settings)
    settings = _mirror_active_into_toplevel(settings)
    if "snowflake" in settings:
        settings["snowflake"] = normalize_snowflake_auth(settings["snowflake"])
    settings.pop("mode", None)
    # Persist connections + active + mirrored top-level for connectors / legacy readers
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def _mask_profile_secrets(profile: Dict[str, Any]) -> Dict[str, Any]:
    p = json.loads(json.dumps(profile))

    def mask(v: str) -> str:
        if not v:
            return ""
        return v[:2] + "***" + v[-2:] if len(v) > 6 else "***"

    aws = p.setdefault("aws", {})
    sf = p.setdefault("snowflake", {})
    aws["secret_access_key"] = mask(aws.get("secret_access_key", ""))
    aws["session_token"] = mask(aws.get("session_token", ""))
    sf["password"] = mask(sf.get("password", ""))
    sf["private_key_pem"] = "***" if sf.get("private_key_pem") else ""
    sf["private_key_passphrase"] = mask(sf.get("private_key_passphrase", ""))
    p["monitoring"] = normalize_monitoring_settings(p.get("monitoring") or {})
    return p


def masked_settings() -> Dict[str, Any]:
    """Return settings with secrets masked for the UI."""
    s = load_settings()

    def mask(v: str) -> str:
        if not v:
            return ""
        return v[:2] + "***" + v[-2:] if len(v) > 6 else "***"

    def mask_pem(v: str) -> str:
        if not v:
            return ""
        # Keep enough shape that the UI knows a key is stored; never return raw PEM.
        return "***"

    s = json.loads(json.dumps(s))
    s["aws"]["secret_access_key"] = mask(s["aws"].get("secret_access_key", ""))
    s["aws"]["session_token"] = mask(s["aws"].get("session_token", ""))
    s["snowflake"]["password"] = mask(s["snowflake"].get("password", ""))
    s["snowflake"]["private_key_pem"] = mask_pem(s["snowflake"].get("private_key_pem", ""))
    s["snowflake"]["private_key_passphrase"] = mask(s["snowflake"].get("private_key_passphrase", ""))
    s["platforms"] = platforms_configured(s)
    s["monitoring"] = normalize_monitoring_settings(s.get("monitoring") or {})
    active_id = s.get("active_connection_id")
    connections = s.get("connections") or []
    s["active_connection_id"] = active_id
    s["connections"] = [_mask_profile_secrets(c) for c in connections]
    s["connection_summaries"] = [_profile_summary(c, active_id) for c in connections]
    return s
