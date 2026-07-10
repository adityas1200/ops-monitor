"""Central configuration. Reads env + a runtime-mutable settings file."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict

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

# Sanitize proxy env vars (some corporate VDIs inject newlines that break httpx)
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    _val = os.environ.get(_proxy_var)
    if _val and _val != _val.strip():
        os.environ[_proxy_var] = _val.strip()

# Anthropic / Claude harness
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4.5")
USE_CLAUDE = bool(ANTHROPIC_API_KEY)

SF_OPTIONAL_FIELDS = frozenset({"database", "schema", "preprod_account", "user"})
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
        },
        "dq": {
            "table_fqn": DQ_TABLE_FQN,
            "subject_area": DQ_SUBJECT_AREA,
            "rules_table_fqn": DQ_RULES_TABLE_FQN,
        },
    },
}


def is_sso_auth(authenticator: str) -> bool:
    a = (authenticator or "password").strip().lower()
    return a in ("sso", "externalbrowser") or a.startswith("http")


def snowflake_configured(sf: Dict[str, Any]) -> bool:
    if not all(str(sf.get(k) or "").strip() for k in ("account", "warehouse", "role")):
        return False
    if is_sso_auth(sf.get("authenticator", "password")):
        return True
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
            "account", "user", "warehouse", "role", "password", "database", "schema")):
        return []
    missing = [f for f in ("account", "warehouse", "role")
               if not str(sf.get(f) or "").strip()]
    if is_sso_auth(sf.get("authenticator", "password")):
        if missing:
            return [f"Snowflake SSO requires: {', '.join(missing)}"]
        return []
    if not str(sf.get("user") or "").strip():
        missing.append("user")
    if missing:
        return [f"Snowflake requires: {', '.join(missing)}"]
    if not str(sf.get("password") or "").strip():
        return ["Snowflake password is required for non-SSO login (or switch to SSO)."]
    return []


def load_settings() -> Dict[str, Any]:
    base = json.loads(json.dumps(_DEFAULT_SETTINGS))
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for section in ("aws", "snowflake"):
                if section in data:
                    base[section].update(data[section])
            if "monitoring" in data:
                for subsection in ("tasks", "dq"):
                    if subsection in data["monitoring"]:
                        for k, v in data["monitoring"][subsection].items():
                            if v is None:
                                continue
                            if isinstance(v, str) and not v.strip():
                                continue
                            base["monitoring"][subsection][k] = v
            if "authenticator" not in data.get("snowflake", {}):
                base["snowflake"]["authenticator"] = "password"
            return base
        except json.JSONDecodeError:
            pass
    return base


def get_task_monitoring_config(settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Effective task monitoring config (settings file overrides env defaults)."""
    s = settings or load_settings()
    tasks = s.get("monitoring", {}).get("tasks", {})
    return {
        "monitor_database": str(tasks.get("monitor_database") or TASK_MONITOR_DATABASE).strip(),
        "name_pattern": str(tasks.get("name_pattern") or TASK_NAME_PATTERN).strip(),
        "historical_months": int(tasks.get("historical_months") or TASK_HISTORICAL_MONTHS),
        "future_days": int(tasks.get("future_days") or TASK_FUTURE_DAYS),
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


def validate_monitoring_settings(monitoring: Dict[str, Any]) -> list[str]:
    """Validate effective monitoring config (stored values fall back to defaults)."""
    wrapper = {"monitoring": monitoring or {}}
    tasks = get_task_monitoring_config(wrapper)
    dq = get_dq_monitoring_config(wrapper)
    errors: list[str] = []

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
    if not dq["subject_area"]:
        errors.append("DQ monitoring requires a subject area filter.")
    return errors


def normalize_monitoring_settings(monitoring: Dict[str, Any]) -> Dict[str, Any]:
    """Persist effective monitoring values (never store blank overrides)."""
    wrapper = {"monitoring": monitoring or {}}
    return {
        "tasks": get_task_monitoring_config(wrapper),
        "dq": get_dq_monitoring_config(wrapper),
    }


def normalize_snowflake_auth(sf: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure SSO and password auth are mutually exclusive in stored settings."""
    sf = dict(sf)
    if is_sso_auth(sf.get("authenticator", "password")):
        sf["password"] = ""
        auth = (sf.get("authenticator") or "sso").strip()
        sf["authenticator"] = auth if auth.lower().startswith("http") else "sso"
    else:
        sf["authenticator"] = "password"
    return sf


def interpret_snowflake_error(error: str, sf: Dict[str, Any]) -> Dict[str, Any]:
    """Turn raw Snowflake driver errors into auth-mode-aware guidance for chat/UI."""
    raw = error or "Connection failed."
    sso = is_sso_auth(sf.get("authenticator", "password"))
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
            "auth_mode": "sso" if sso else "password",
            "hints": [
                "Install the Snowflake Python driver in your virtual environment:",
                "  cd backend && .venv\\Scripts\\pip.exe install snowflake-connector-python",
                "Then restart the backend and test connectivity again.",
            ],
        }

    if sso:
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
    else:
        summary = raw
        if "incorrect username or password" in lower or "250001" in raw:
            hints += [
                "Verify Snowflake User (login name) and Password in Settings.",
                "If you intend to use SSO, switch Login method to SSO and save — password auth will be disabled.",
                "Masked passwords (with ***) are not re-sent; enter the full password again if needed.",
            ]
        elif not str(sf.get("password") or "").strip():
            hints.append("Password is empty — enter your Snowflake password or switch to SSO.")

    if not hints:
        hints.append("Review Account, User, Warehouse, Role, and login method in Settings → Snowflake.")

    return {"summary": summary, "raw_error": raw, "auth_mode": "sso" if sso else "password", "hints": hints}


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if "snowflake" in settings:
        settings["snowflake"] = normalize_snowflake_auth(settings["snowflake"])
    settings.pop("mode", None)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def masked_settings() -> Dict[str, Any]:
    """Return settings with secrets masked for the UI."""
    s = load_settings()

    def mask(v: str) -> str:
        if not v:
            return ""
        return v[:2] + "***" + v[-2:] if len(v) > 6 else "***"

    s = json.loads(json.dumps(s))
    s["aws"]["secret_access_key"] = mask(s["aws"].get("secret_access_key", ""))
    s["aws"]["session_token"] = mask(s["aws"].get("session_token", ""))
    s["snowflake"]["password"] = mask(s["snowflake"].get("password", ""))
    s["platforms"] = platforms_configured(s)
    s["monitoring"] = {
        "tasks": get_task_monitoring_config(s),
        "dq": get_dq_monitoring_config(s),
    }
    return s
