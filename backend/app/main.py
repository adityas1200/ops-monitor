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


# ---- Production UI (built frontend) -----------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

if _FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/")
    def spa_root():
        return FileResponse(_FRONTEND_DIST / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = _FRONTEND_DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn
    reload = __import__("os").getenv("OPS_MONITOR_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=reload)
