"""FastAPI backend for the agentic data-ops monitoring system."""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agents.orchestrator import orchestrator
from app.connectors.aws_connector import AWSConnector
from app.connectors.snowflake_connector import SnowflakeConnector, reset_shared_connection
from app.core.config import (AWS_OPTIONAL_FIELDS, SF_OPTIONAL_FIELDS, load_settings,
                             masked_settings, normalize_monitoring_settings,
                             normalize_snowflake_auth, platforms_configured,
                             save_settings, validate_monitoring_settings,
                             validate_snowflake_settings)
from app.models.schemas import (ActivityErrorRequest, AddIssueRequest, ChatRequest,
                                FixRequest, RCARequest, SettingsPayload, ValidateRequest)

app = FastAPI(title="ops-monitor", version="1.0.0",
              description="Agentic data-ops monitoring for Snowflake + AWS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)


@app.get("/api/health")
def health():
    s = load_settings()
    return {"status": "ok", "live_only": True, "platforms": platforms_configured(s)}


# ---- Connectivity -----------------------------------------------------
@app.get("/api/connectivity")
def connectivity():
    return orchestrator.check_connectivity()


# ---- Settings ---------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return masked_settings()


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    current = load_settings()
    new = payload.model_dump()
    optional = {"aws": AWS_OPTIONAL_FIELDS, "snowflake": SF_OPTIONAL_FIELDS}

    for section in ("aws", "snowflake"):
        for k, v in new[section].items():
            if v is not None and "***" in str(v):
                continue
            if v == "" and k in optional[section]:
                current[section][k] = ""
                continue
            if v == "" and k == "password" and section == "snowflake":
                auth = (new[section].get("authenticator") or "password").lower()
                if auth in ("sso", "externalbrowser") or auth.startswith("http"):
                    current[section][k] = ""
                continue
            if v is not None:
                current[section][k] = v

    current["snowflake"] = normalize_snowflake_auth(current.get("snowflake", {}))

    if new.get("monitoring"):
        current["monitoring"] = normalize_monitoring_settings({
            "tasks": {
                **current.get("monitoring", {}).get("tasks", {}),
                **(new["monitoring"].get("tasks") or {}),
            },
            "dq": {
                **current.get("monitoring", {}).get("dq", {}),
                **(new["monitoring"].get("dq") or {}),
            },
        })

    sf_errors = validate_snowflake_settings(current.get("snowflake", {}))
    if sf_errors:
        raise HTTPException(status_code=400, detail="; ".join(sf_errors))

    mon_errors = validate_monitoring_settings(current.get("monitoring", {}))
    if mon_errors:
        raise HTTPException(status_code=400, detail="; ".join(mon_errors))

    saved = save_settings(current)
    reset_shared_connection()
    return {"saved": True, "platforms": platforms_configured(saved), "settings": masked_settings()}


# ---- Monitoring / Dashboard ------------------------------------------
@app.get("/api/summary")
def summary(status: Optional[str] = Query(default="FAILED"),
            date_from: Optional[str] = None, date_to: Optional[str] = None):
    return orchestrator.get_summary(status, date_from, date_to)


@app.get("/api/dq/summary")
def dq_summary(date_from: Optional[str] = None, date_to: Optional[str] = None):
    return orchestrator.get_dq_summary(date_from, date_to)


@app.get("/api/dq/details/{qc_id}")
def dq_details(qc_id: str, subject_area: Optional[str] = None):
    """Fetch DQ rule definition, execute the SQL_CODE, and return results."""
    from app.connectors.dq_connector import DQConnector
    dq = DQConnector()
    rule = dq.fetch_dq_rule_sql(qc_id, subject_area)
    if not rule:
        return {"qc_id": qc_id, "subject_area": subject_area, "found": False, "rule": None, "sql_results": None}
    sql_code = rule.get("SQL_CODE") or ""
    sql_results = None
    if sql_code.strip() and sql_code.strip().upper().startswith(("SELECT", "WITH")):
        warehouse = rule.get("WAREHOUSE") or None
        # Derive database context from the rules table FQN (SQL typically runs against that DB)
        from app.core.config import get_dq_monitoring_config
        dq_cfg = get_dq_monitoring_config()
        rules_fqn = dq_cfg.get("rules_table_fqn", "")
        database = rule.get("DATABASE") or (rules_fqn.split(".")[0] if "." in rules_fqn else None)
        sql_results = dq.execute_dq_sql(sql_code, database=database, warehouse=warehouse)
    return {"qc_id": qc_id, "subject_area": subject_area, "found": True, "rule": rule, "sql_results": sql_results}


@app.get("/api/pipelines/{pipeline_id}/logs")
def pipeline_logs(pipeline_id: str):
    pipes = {p["id"]: p for p in orchestrator.monitoring.collect()}
    p = pipes.get(pipeline_id)
    if not p:
        return {"error": "not found"}
    getter = SnowflakeConnector() if p["platform"] == "snowflake" else AWSConnector()
    return {"pipeline_id": pipeline_id, "log_ref": p.get("log_ref"),
            "lines": getter.get_logs(p.get("log_ref") or "")}


# ---- RCA --------------------------------------------------------------
@app.post("/api/rca")
def rca(req: RCARequest):
    return orchestrator.run_rca(req.pipeline_id, req.extra_context)


# ---- RCA Knowledge Base -----------------------------------------------
@app.get("/api/rca/knowledge")
def get_rca_knowledge():
    """Return all user-contributed RCA knowledge rules."""
    from app.core.config import SKILLS_DIR
    p = SKILLS_DIR / "rca_knowledge.md"
    if not p.exists():
        return {"rules": []}
    content = p.read_text(encoding="utf-8")
    rules = []
    current: dict = {}
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if current.get("PATTERN"):
                rules.append(current)
            current = {}
            continue
        if ":" in stripped and stripped.split(":")[0].strip().upper() in (
            "PATTERN", "CATEGORY", "ROOT_CAUSE", "FIX", "ADDED_BY", "ADDED_ON", "NOTES"
        ):
            key, _, val = stripped.partition(":")
            current[key.strip().upper()] = val.strip()
    if current.get("PATTERN"):
        rules.append(current)
    return {"rules": rules}


@app.post("/api/rca/knowledge")
def add_rca_knowledge(payload: dict):
    """Append a new RCA knowledge rule to the skill file."""
    from datetime import datetime, timezone
    from app.core.config import SKILLS_DIR
    p = SKILLS_DIR / "rca_knowledge.md"
    pattern = payload.get("pattern", "").strip()
    if not pattern:
        return {"error": "pattern is required"}
    entry = (
        f"\n---\n"
        f"PATTERN: {pattern}\n"
        f"CATEGORY: {payload.get('category', 'Unknown Failure')}\n"
        f"ROOT_CAUSE: {payload.get('root_cause', '')}\n"
        f"FIX: {payload.get('fix', '')}\n"
        f"ADDED_BY: {payload.get('added_by', 'user')}\n"
        f"ADDED_ON: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
        f"NOTES: {payload.get('notes', '')}\n"
        f"---\n"
    )
    with open(p, "a", encoding="utf-8") as f:
        f.write(entry)
    return {"status": "ok", "pattern": pattern}


# ---- Fix --------------------------------------------------------------
@app.post("/api/fix")
def fix(req: FixRequest):
    return orchestrator.suggest_fix(req.pipeline_id, req.incident_id, req.user_edit)


# ---- Validate (zero-copy clone + tests) ------------------------------
@app.post("/api/validate")
def validate(req: ValidateRequest):
    return orchestrator.validate_fix(req.pipeline_id, req.fix_id)


# ---- Auto remediate (end-to-end) -------------------------------------
@app.post("/api/remediate/{pipeline_id}")
def remediate(pipeline_id: str):
    return orchestrator.auto_remediate(pipeline_id)


# ---- Chat -------------------------------------------------------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    return orchestrator.chat_message(req.message, req.pipeline_id, req.context)


@app.post("/api/chat/activity-error")
def activity_error(req: ActivityErrorRequest):
    return orchestrator.chat.report_activity_error(req.model_dump())


# ---- Memory / Incidents ----------------------------------------------
@app.get("/api/incidents")
def incidents():
    return {"incidents": orchestrator.incidents()}


@app.post("/api/issues")
def add_issue(req: AddIssueRequest):
    from app.memory.memory_store import incident_log
    inc = incident_log.log({
        "pipeline_id": req.pipeline_id, "name": req.title, "platform": req.platform,
        "status": "USER_REPORTED", "error": req.description, "source": "user",
        "suspected_cause": req.suspected_cause, "signature": f"user {req.title}",
    })
    orchestrator.chat.learn({"signature": f"user-issue {req.title}",
                             "incident_id": inc["incident_id"], "description": req.description})
    return {"logged": True, "incident": inc}


@app.get("/api/memory/{agent}")
def agent_memory(agent: str):
    from app.memory.memory_store import AgentMemory
    return {"agent": agent, "records": AgentMemory(agent).all()}


# ---- SQL Guardrail (audit log + approvals) ----------------------------
@app.get("/api/guardrail/audit")
def sql_audit_log(limit: int = 50):
    """View the SQL audit log — every query sent to Snowflake."""
    from app.connectors.sql_guardrail import get_audit_log
    return {"entries": get_audit_log(limit)}


@app.get("/api/guardrail/pending")
def sql_pending():
    """View blocked DDL/DML statements awaiting human approval."""
    from app.connectors.sql_guardrail import get_pending_approvals
    return {"pending": get_pending_approvals()}


@app.post("/api/guardrail/approve/{approval_id}")
def sql_approve(approval_id: str):
    """Approve a blocked DDL/DML statement for execution."""
    from app.connectors.sql_guardrail import approve_sql
    result = approve_sql(approval_id)
    if not result:
        return {"error": f"Approval {approval_id} not found or already processed"}
    return {"status": "approved", "approval": result}


@app.post("/api/guardrail/reject/{approval_id}")
def sql_reject(approval_id: str):
    """Reject a blocked DDL/DML statement."""
    from app.connectors.sql_guardrail import reject_sql
    result = reject_sql(approval_id)
    if not result:
        return {"error": f"Approval {approval_id} not found or already processed"}
    return {"status": "rejected", "approval": result}


# ---- Production UI (built frontend) -----------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response as StarletteResponse

    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/")
    def spa_root():
        return FileResponse(_FRONTEND_DIST / "index.html")

    class SPAFallbackMiddleware(BaseHTTPMiddleware):
        """Serve index.html for non-API GET requests that don't match a file."""

        async def dispatch(self, request, call_next):
            response = await call_next(request)
            if (
                response.status_code == 404
                and request.method == "GET"
                and not request.url.path.startswith("/api/")
                and not request.url.path.startswith("/assets/")
            ):
                return FileResponse(_FRONTEND_DIST / "index.html")
            return response

    app.add_middleware(SPAFallbackMiddleware)


if __name__ == "__main__":
    import uvicorn
    reload = __import__("os").getenv("OPS_MONITOR_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=reload)
