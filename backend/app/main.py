"""FastAPI backend for the agentic data-ops monitoring system."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional
import threading

import json as _json

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agents.orchestrator import orchestrator
from app.connectors.aws_connector import AWSConnector
from app.connectors.snowflake_connector import SnowflakeConnector, reset_shared_connection
from app.core.config import (AWS_OPTIONAL_FIELDS, SF_OPTIONAL_FIELDS, is_keypair_auth,
                             is_sso_auth, load_settings,
                             masked_settings, normalize_monitoring_settings,
                             normalize_snowflake_auth, platforms_configured,
                             save_settings, validate_monitoring_settings,
                             validate_snowflake_settings)
from app.memory.session_store import session_store
from app.models.schemas import (ActivityErrorRequest, AddIssueRequest, ChatRequest,
                                FixRequest, RCARequest, SettingsPayload)

app = FastAPI(title="ops-monitor", version="1.0.0",
              description="Agentic data-ops monitoring for Snowflake + AWS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)


def _warm_snowflake_background() -> None:
    """Open one shared Snowflake session at startup so the first /api/summary is not blocked on SSO."""
    try:
        SnowflakeConnector().warm()
    except Exception:
        pass


@app.on_event("startup")
def on_startup() -> None:
    threading.Thread(target=_warm_snowflake_background, daemon=True, name="sf-warmup").start()


@app.get("/api/health")
def health():
    s = load_settings()
    from app.core.config import USE_CLAUDE, CLAUDE_MODEL
    return {
        "status": "ok",
        "live_only": True,
        "platforms": platforms_configured(s),
        "llm": {"available": USE_CLAUDE, "model": CLAUDE_MODEL if USE_CLAUDE else None,
                "last_error": orchestrator.chat.harness.last_error},
    }


@app.get("/api/snowflake/warmup")
def snowflake_warmup(force: bool = Query(default=False)):
    """Warm (or reuse) the shared Snowflake session.

    First call may open SSO; later calls return immediately while the
    in-process session is still within TTL. Token reuse across process
    restarts uses the Snowflake driver's secure local storage / keyring.
    """
    return SnowflakeConnector().warm(force_ping=bool(force))


@app.get("/api/snowflake/session")
def snowflake_session():
    """Non-blocking session status (no SSO / no network ping)."""
    return SnowflakeConnector().session_status()


def merge_settings_payload(current: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a SettingsPayload dump into current settings (keeps masked *** secrets)."""
    merged = deepcopy(current)
    optional = {"aws": AWS_OPTIONAL_FIELDS, "snowflake": SF_OPTIONAL_FIELDS}

    for section in ("aws", "snowflake"):
        for k, v in new[section].items():
            if v is not None and "***" in str(v):
                continue
            if v == "" and k in optional[section]:
                merged[section][k] = ""
                continue
            if v == "" and k == "password" and section == "snowflake":
                auth = (new[section].get("authenticator") or "password").lower()
                if (auth in ("sso", "externalbrowser", "keypair", "key_pair", "private_key")
                        or auth.startswith("http")):
                    merged[section][k] = ""
                continue
            if v == "" and k in ("private_key_pem", "private_key_passphrase") and section == "snowflake":
                auth = (new[section].get("authenticator") or "password").lower()
                # Switching away from key-pair: clear stored key material.
                if is_sso_auth(auth) or auth in ("password", "snowflake", ""):
                    merged[section][k] = ""
                    continue
                # Blank PEM while still on key-pair means "keep existing key".
                if k == "private_key_pem" and is_keypair_auth(auth):
                    continue
                # Blank passphrase:
                # - new real PEM pasted → treat as intentional blank (unencrypted key)
                # - PEM masked/empty → keep existing passphrase (avoid wiping on unrelated saves)
                if k == "private_key_passphrase" and is_keypair_auth(auth):
                    new_pem = str(new[section].get("private_key_pem") or "")
                    if new_pem and "***" not in new_pem:
                        merged[section][k] = ""
                    continue
                continue
            if v is not None:
                merged[section][k] = v

    # If login method changed, force secret exclusivity before normalize.
    new_auth = (new.get("snowflake") or {}).get("authenticator")
    if new_auth is not None and "***" not in str(new_auth):
        merged.setdefault("snowflake", {})["authenticator"] = new_auth

    merged["snowflake"] = normalize_snowflake_auth(merged.get("snowflake", {}))

    if new.get("monitoring"):
        merged["monitoring"] = normalize_monitoring_settings({
            section: {
                **merged.get("monitoring", {}).get(section, {}),
                **(new["monitoring"].get(section) or {}),
            }
            for section in ("tasks", "dq", "lineage")
        })
    return merged


# ---- Connectivity -----------------------------------------------------
@app.get("/api/connectivity")
def connectivity():
    """Probe already-saved settings (page-load / status)."""
    return orchestrator.check_connectivity()


@app.post("/api/connectivity")
def test_connectivity(payload: SettingsPayload):
    """Probe form values without saving. Returns can_save for optional Save UI."""
    merged = merge_settings_payload(load_settings(), payload.model_dump(by_alias=True))
    sf_errors = validate_snowflake_settings(merged.get("snowflake", {}))
    if sf_errors:
        raise HTTPException(status_code=400, detail="; ".join(sf_errors))
    return orchestrator.check_connectivity(settings=merged, ephemeral=True)


# ---- Settings ---------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return masked_settings()


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    current = merge_settings_payload(load_settings(), payload.model_dump(by_alias=True))

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
def dq_summary(date_from: Optional[str] = None, date_to: Optional[str] = None,
               revalidate: bool = True):
    return orchestrator.get_dq_summary(date_from, date_to, revalidate=revalidate)


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
        # Same context resolution the dashboard/RCA verdict uses, so this panel and
        # the dashboard status always agree.
        database, schema, warehouse = dq.resolve_rule_context(rule, sql_code)
        sql_results = dq.execute_dq_sql(sql_code, database=database, schema=schema, warehouse=warehouse)
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
    # #region agent log
    import time as _dbg_time
    from pathlib import Path as _P
    _dbg_t0 = _dbg_time.perf_counter()
    # #endregion
    result = orchestrator.run_rca(
        req.pipeline_id, req.extra_context, date_from=req.date_from, date_to=req.date_to,
        include_lineage=req.include_lineage,
    )
    # #region agent log
    try:
        _p = _P(__file__).resolve().parents[2] / "debug-938378.log"
        with _p.open("a", encoding="utf-8") as _f:
            _f.write(_json.dumps({
                "sessionId": "938378", "hypothesisId": "E",
                "location": "main.py:rca", "message": "rca_endpoint_total",
                "data": {
                    "elapsed_ms": round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                    "pipeline_id": req.pipeline_id,
                    "is_dq": str(req.pipeline_id or "").startswith("dq_"),
                    "has_error": bool((result or {}).get("error")),
                    "category": (result or {}).get("category"),
                },
                "timestamp": int(_dbg_time.time() * 1000),
            }) + "\n")
    except Exception:
        pass
    # #endregion
    return result


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
    return orchestrator.suggest_fix(
        req.pipeline_id, req.incident_id, req.user_edit, req.rca_context,
    )


# ---- Auto remediate (end-to-end) -------------------------------------
@app.post("/api/remediate/{pipeline_id}")
def remediate(pipeline_id: str):
    return orchestrator.auto_remediate(pipeline_id)


# ---- Chat -------------------------------------------------------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    sid = req.session_id or "default"
    history = session_store.get_history(sid)
    result = orchestrator.chat_message(req.message, req.pipeline_id, req.context, history)
    session_store.add_message(sid, "user", req.message)
    session_store.add_message(sid, "assistant", result["reply"])
    return result


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """SSE streaming endpoint for chat — returns text chunks as they generate."""
    sid = req.session_id or "default"
    history = session_store.get_history(sid)
    meta, generator = orchestrator.chat_stream(req.message, req.pipeline_id, req.context, history)
    session_store.add_message(sid, "user", req.message)

    def event_stream():
        yield f"event: meta\ndata: {_json.dumps({'intent': meta['intent'], 'agent': meta['agent']})}\n\n"
        full_reply = []
        for chunk in generator:
            full_reply.append(chunk)
            yield f"event: chunk\ndata: {_json.dumps({'text': chunk})}\n\n"
        reply_text = "".join(full_reply)
        session_store.add_message(sid, "assistant", reply_text)
        payload_data = meta.get("payload")
        yield f"event: done\ndata: {_json.dumps({'payload': payload_data}, default=str)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
