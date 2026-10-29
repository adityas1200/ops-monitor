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
                                DQRevalidateRequest, FixRequest, RCARequest, SettingsPayload)

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
def summary(status: Optional[str] = Query(default="ALL"),
            date_from: Optional[str] = None, date_to: Optional[str] = None,
            q: Optional[str] = None, platform: Optional[str] = None):
    return orchestrator.get_summary(
        status, date_from, date_to, q=q, platform=platform,
    )


@app.get("/api/dq/summary")
def dq_summary(date_from: Optional[str] = None, date_to: Optional[str] = None,
               revalidate: bool = True, status: Optional[str] = "ALL",
               q: Optional[str] = None,
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=10, ge=1, le=500)):
    return orchestrator.get_dq_summary(
        date_from, date_to, revalidate=revalidate, status=status, q=q,
        page=page, page_size=page_size,
    )


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


@app.post("/api/dq/revalidate")
def dq_revalidate_one(payload: DQRevalidateRequest):
    """Live-revalidate one DQ check (QC ID + subject area)."""
    from app.connectors.dq_connector import DQConnector
    qc = (payload.qc_id or "").strip()
    if not qc:
        raise HTTPException(status_code=400, detail="qc_id is required")
    dq = DQConnector()
    result = dq.revalidate_one(
        qc,
        payload.subject_area,
        recorded_status=payload.status,
        check_id=payload.id,
        description=payload.error,
        pass_count=payload.pass_count,
        fail_count=payload.fail_count,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Revalidate failed")
    return result


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
def _rca_http_error(result: Dict[str, Any]) -> None:
    """Raise HTTP 404 only for explicit RCA lookup misses (not analysis failures)."""
    if not isinstance(result, dict) or not result.get("error"):
        return
    code = str(result.get("error_code") or "")
    if code not in ("DQ_CHECK_NOT_FOUND", "PIPELINE_NOT_FOUND"):
        return
    raise HTTPException(
        status_code=404,
        detail={
            "error": str(result["error"]),
            "error_code": code,
            "pipeline_id": result.get("pipeline_id"),
            "date_from": result.get("date_from"),
            "date_to": result.get("date_to"),
        },
    )


@app.post("/api/rca")
def rca(req: RCARequest):
    # #region agent log
    import time as _dbg_time
    from pathlib import Path as _P
    _dbg_t0 = _dbg_time.perf_counter()
    # #endregion
    try:
        result = orchestrator.run_rca(
            req.pipeline_id, req.extra_context, date_from=req.date_from, date_to=req.date_to,
            include_lineage=req.include_lineage,
        )
    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "budget" in err_str.lower() or "exceeded" in err_str.lower() or "quota" in err_str.lower():
            raise HTTPException(status_code=402, detail=err_str)
        raise
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
    _rca_http_error(result or {})
    return result


# ---- RCA Knowledge Base -----------------------------------------------
@app.get("/api/rca/knowledge")
def get_rca_knowledge():
    """Return all user-contributed RCA knowledge rules."""
    from app.core.config import SKILLS_DIR
    from app.core.rca_knowledge_store import read_rules
    return {"rules": read_rules(SKILLS_DIR / "rca_knowledge.md")}


@app.post("/api/rca/knowledge")
def add_rca_knowledge(payload: dict):
    """Append a new RCA knowledge rule to the skill file."""
    from app.core.config import SKILLS_DIR
    from app.core.rca_knowledge_store import append_rule
    try:
        rule = append_rule(SKILLS_DIR / "rca_knowledge.md", payload or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "ok", "pattern": rule["PATTERN"], "rule": rule}


@app.put("/api/rca/knowledge/{index}")
def update_rca_knowledge(index: int, payload: dict):
    """Update an existing RCA knowledge rule by list index."""
    from app.core.config import SKILLS_DIR
    from app.core.rca_knowledge_store import update_rule
    try:
        rule = update_rule(SKILLS_DIR / "rca_knowledge.md", index, payload or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except IndexError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": "ok", "index": index, "pattern": rule["PATTERN"], "rule": rule}


@app.delete("/api/rca/knowledge/{index}")
def delete_rca_knowledge(index: int):
    """Delete an RCA knowledge rule by list index."""
    from app.core.config import SKILLS_DIR
    from app.core.rca_knowledge_store import delete_rule
    try:
        rule = delete_rule(SKILLS_DIR / "rca_knowledge.md", index)
    except IndexError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"status": "ok", "index": index, "pattern": rule.get("PATTERN"), "rule": rule}


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
    try:
        result = orchestrator.chat_message(req.message, req.pipeline_id, req.context, history)
    except Exception as e:
        err_str = str(e)
        if "402" in err_str or "budget" in err_str.lower() or "exceeded" in err_str.lower():
            raise HTTPException(status_code=402, detail=err_str)
        raise
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
