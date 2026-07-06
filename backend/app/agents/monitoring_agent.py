from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.agents.base import BaseAgent
from app.connectors.aws_connector import AWSConnector
from app.connectors.dq_connector import DQConnector
from app.connectors.snowflake_connector import SnowflakeConnector
from app.core.config import load_settings, platforms_configured

_COLLECT_CACHE: Dict[str, Any] = {"key": None, "ts": 0.0, "records": []}
_COLLECT_CACHE_TTL_S = 90


class MonitoringAgent(BaseAgent):
    name = "monitoring"
    skill_file = "monitoring.md"

    def collect(self, date_from: Optional[str] = None,
                 date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        cache_key = f"{date_from}|{date_to}"
        now = time.time()
        if (_COLLECT_CACHE["key"] == cache_key
                and _COLLECT_CACHE["records"]
                and now - _COLLECT_CACHE["ts"] < _COLLECT_CACHE_TTL_S):
            return list(_COLLECT_CACHE["records"])

        sf = SnowflakeConnector()
        aws = AWSConnector()
        records: List[Dict[str, Any]] = []
        records += sf.read_telemetry(date_from, date_to)
        records += aws.read_telemetry()
        self._connector_errors = []
        if sf.last_error:
            self._connector_errors.append({"platform": "snowflake", "error": sf.last_error})
        if aws.last_error:
            self._connector_errors.append({"platform": "aws", "error": aws.last_error})
        # de-dup by id, preserve order
        seen, merged = set(), []
        for r in records:
            if r["id"] in seen:
                continue
            if r.get("source") != "live":
                continue
            seen.add(r["id"])
            merged.append(self._flag_delay(r))
        _COLLECT_CACHE.update({"key": cache_key, "ts": now, "records": merged})
        return merged

    def _flag_delay(self, r: Dict[str, Any]) -> Dict[str, Any]:
        """Mark DELAYED if duration breaches SLA and not already a failure/skip."""
        if r["status"] in ("SUCCESS", "RUNNING") and r.get("duration_s") and r.get("sla_s"):
            if r["duration_s"] > r["sla_s"]:
                r = {**r, "status": "DELAYED"}
        return r

    def summary(self, status_filter: Optional[str] = None,
                date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
        pipelines = self.collect(date_from, date_to)

        kpis = {"success": 0, "failed": 0, "delayed": 0, "skipped": 0, "running": 0, "total": len(pipelines)}
        key = {"SUCCESS": "success", "FAILED": "failed", "DELAYED": "delayed",
               "SKIPPED": "skipped", "RUNNING": "running"}
        for p in pipelines:
            kpis[key.get(p["status"], "running")] += 1

        filtered = pipelines
        if status_filter and status_filter.upper() != "ALL":
            filtered = [p for p in pipelines if p["status"] == status_filter.upper()]

        # log new incidents
        for p in pipelines:
            if p["status"] in ("FAILED", "DELAYED"):
                self._log_incident(p)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "live_only": True,
            "platforms": platforms_configured(load_settings()),
            "kpis": kpis,
            "pipelines": filtered,
            "all_pipelines": pipelines,
            "errors": getattr(self, "_connector_errors", []),
        }

    def dq_summary(self, date_from: Optional[str] = None,
                   date_to: Optional[str] = None) -> Dict[str, Any]:
        return DQConnector().summary(date_from, date_to)

    def _log_incident(self, p: Dict[str, Any]) -> None:
        sig = f"{p['platform']} {p['status']} {(p.get('error') or '')[:60]}"
        existing = [i for i in self.incident_log.all() if i.get("pipeline_id") == p["id"]
                    and i.get("status") == p["status"]]
        if existing:
            return
        prior = self.incident_log.find_by_signature(sig)
        self.incident_log.log({
            "pipeline_id": p["id"], "name": p["name"], "platform": p["platform"],
            "status": p["status"], "error": p.get("error"), "signature": sig,
            "source": "monitoring",
            "seen_before": bool(prior),
        })
