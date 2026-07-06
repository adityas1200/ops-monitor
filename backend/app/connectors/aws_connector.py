"""AWS connector — Glue, Step Functions, CloudWatch, DynamoDB."""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from app.connectors.status import normalize_run_status
from app.core.config import aws_configured, load_settings


class AWSConnector:
    last_error: Optional[str] = None

    def __init__(self):
        self.settings = load_settings()
        self._client_cache: Dict[str, Any] = {}

    def _configured(self) -> bool:
        return aws_configured(self.settings.get("aws", {}))

    # ---- connectivity -------------------------------------------------
    def health(self) -> List[Dict[str, Any]]:
        if not self._configured():
            return [{"name": "AWS STS", "status": "down", "latency_ms": None,
                     "detail": "Not configured — Access Key ID and Secret Access Key are required."}]
        return self._live_health()

    def _client(self, service: str):
        if service in self._client_cache:
            return self._client_cache[service]
        import boto3
        aws = self.settings["aws"]
        kwargs = {"region_name": (aws.get("region") or "").strip() or "us-east-1"}
        if aws.get("access_key_id"):
            kwargs.update(
                aws_access_key_id=aws["access_key_id"],
                aws_secret_access_key=aws["secret_access_key"],
            )
            if aws.get("session_token"):
                kwargs["aws_session_token"] = aws["session_token"]
        client = boto3.client(service, **kwargs)
        self._client_cache[service] = client
        return client

    def _live_health(self) -> List[Dict[str, Any]]:
        out = []
        try:
            self._client("sts").get_caller_identity()
            out.append({"name": "AWS STS", "status": "connected", "latency_ms": None, "detail": "identity ok"})
        except Exception as e:  # noqa: BLE001
            out.append({"name": "AWS STS", "status": "down", "latency_ms": None, "detail": str(e)[:160]})
        for svc in ("glue", "stepfunctions", "logs", "dynamodb"):
            try:
                self._client(svc)
                out.append({"name": svc, "status": "connected", "latency_ms": None, "detail": "client ok"})
            except Exception as e:  # noqa: BLE001
                out.append({"name": svc, "status": "degraded", "latency_ms": None, "detail": str(e)[:160]})
        return out

    # ---- monitoring reads --------------------------------------------
    def read_telemetry(self) -> List[Dict[str, Any]]:
        self.last_error = None
        if not self._configured():
            return []
        return self._live_telemetry()

    def _live_telemetry(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        try:
            glue = self._client("glue")
            for j in glue.get_jobs().get("Jobs", []):
                name = j["Name"]
                runs = glue.get_job_runs(JobName=name, MaxResults=1).get("JobRuns", [])
                if not runs:
                    continue
                r = runs[0]
                status = normalize_run_status(
                    r.get("JobRunState"),
                    done=r.get("CompletedOn"),
                    error=r.get("ErrorMessage"),
                )
                records.append({
                    "id": f"glue_{name}", "name": f"Glue Job: {name}", "platform": "glue",
                    "status": status, "duration_s": r.get("ExecutionTime"),
                    "error": r.get("ErrorMessage"), "upstream": [], "downstream": [],
                    "started_at": str(r.get("StartedOn")), "ended_at": str(r.get("CompletedOn")),
                    "log_ref": f"/aws-glue/jobs/{name}", "source": "live",
                })
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
        return records

    def get_logs(self, log_ref: str) -> List[str]:
        if not self._configured():
            return ["(AWS not configured)"]
        try:
            logs = self._client("logs")
            group = log_ref
            streams = logs.describe_log_streams(logGroupName=group, limit=1,
                                                orderBy="LastEventTime", descending=True)
            sname = streams["logStreams"][0]["logStreamName"]
            ev = logs.get_log_events(logGroupName=group, logStreamName=sname, limit=50)
            return [e["message"] for e in ev["events"]]
        except Exception as e:  # noqa: BLE001
            return [f"(live log fetch failed: {e})"]
