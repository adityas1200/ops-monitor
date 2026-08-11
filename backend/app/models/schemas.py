"""Pydantic models shared across the API."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class AWSCreds(BaseModel):
    region: str = "us-east-1"
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""


class SnowflakeCreds(BaseModel):
    account: str = ""
    user: str = ""
    authenticator: str = "password"  # password | sso | externalbrowser | IdP URL
    password: str = ""
    warehouse: str = ""
    role: str = ""
    database: str = ""
    schema: str = ""
    preprod_account: str = ""


class TaskMonitoringConfig(BaseModel):
    monitor_database: str = "CPH_DB_PROD"
    name_pattern: str = "TASK%"
    historical_months: int = 2
    future_days: int = 7


class DQMonitoringConfig(BaseModel):
    table_fqn: str = "CPH_DB_PRE_PROD.MODEL_V2.DQM_VALIDATION_SUMMARY"
    subject_area: str = "Lynkuet LAAD"


class MonitoringSettings(BaseModel):
    tasks: TaskMonitoringConfig = TaskMonitoringConfig()
    dq: DQMonitoringConfig = DQMonitoringConfig()


class SettingsPayload(BaseModel):
    aws: AWSCreds
    snowflake: SnowflakeCreds
    monitoring: MonitoringSettings = MonitoringSettings()


class Pipeline(BaseModel):
    id: str
    name: str
    platform: str
    status: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_s: Optional[float] = None
    sla_s: Optional[float] = None
    error: Optional[str] = None
    log_ref: Optional[str] = None
    upstream: List[str] = []
    downstream: List[str] = []


class RCARequest(BaseModel):
    pipeline_id: str
    extra_context: Optional[str] = None        # interactive refinement hint
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class FixRequest(BaseModel):
    pipeline_id: str
    incident_id: Optional[str] = None
    user_edit: Optional[str] = None            # interactive fix edit
    rca_context: Optional[Dict[str, Any]] = None  # slim RCA from Workbench (avoid re-run / context loss)


class ValidateRequest(BaseModel):
    pipeline_id: str
    fix_id: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    pipeline_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None


class ActivityErrorRequest(BaseModel):
    tab: str = ""
    action: str = ""
    error: str = ""
    details: Optional[Dict[str, Any]] = None


class AddIssueRequest(BaseModel):
    title: str
    description: str
    platform: Optional[str] = "manual"
    pipeline_id: Optional[str] = None
    suspected_cause: Optional[str] = None
