"""Agent orchestration — single entry point the API uses to coordinate agents.

Flow: Connectivity -> Monitoring -> (per incident) RCA -> Fix -> Test, with Chat
able to drive any step interactively. Mirrors a Claude-Code-style harness where each
skilled agent is invoked as a tool by the orchestrator.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from app.agents.chat_agent import ChatAgent
from app.agents.connectivity_agent import ConnectivityAgent
from app.agents.fix_agent import FixAgent
from app.agents.monitoring_agent import MonitoringAgent
from app.agents.rca_agent import RCAAgent
from app.agents.test_agent import TestAgent
from app.memory.memory_store import incident_log


class Orchestrator:
    def __init__(self):
        self.connectivity = ConnectivityAgent()
        self.monitoring = MonitoringAgent()
        self.rca = RCAAgent()
        self.fix = FixAgent()
        self.test = TestAgent()
        self.chat = ChatAgent()

    # individual agent entrypoints -------------------------------------
    def check_connectivity(self) -> Dict[str, Any]:
        return self.connectivity.check()

    def get_summary(self, status: Optional[str] = None,
                    date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
        return self.monitoring.summary(status, date_from, date_to)

    def get_dq_summary(self, date_from: Optional[str] = None,
                       date_to: Optional[str] = None) -> Dict[str, Any]:
        return self.monitoring.dq_summary(date_from, date_to)

    def run_rca(self, pipeline_id: str, extra_context: Optional[str] = None) -> Dict[str, Any]:
        return self.rca.analyze(pipeline_id, extra_context)

    def suggest_fix(self, pipeline_id: str, incident_id: Optional[str] = None,
                    user_edit: Optional[str] = None) -> Dict[str, Any]:
        return self.fix.suggest(pipeline_id, incident_id, user_edit)

    def validate_fix(self, pipeline_id: str, fix_id: Optional[str] = None) -> Dict[str, Any]:
        return self.test.validate(pipeline_id, fix_id)

    def chat_message(self, message: str, pipeline_id: Optional[str] = None,
                     context: Optional[Dict[str, Any]] = None,
                     history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        return self.chat.handle(message, pipeline_id, context, history)

    def chat_stream(self, message: str, pipeline_id: Optional[str] = None,
                    context: Optional[Dict[str, Any]] = None,
                    history: Optional[List[Dict[str, str]]] = None):
        """Returns (metadata_dict, text_generator) for SSE streaming."""
        return self.chat.handle_stream(message, pipeline_id, context, history)

    # end-to-end auto-remediation pipeline (used by /api/remediate) -----
    def auto_remediate(self, pipeline_id: str) -> Dict[str, Any]:
        rca = self.run_rca(pipeline_id)
        fix = self.suggest_fix(pipeline_id, incident_id=rca.get("incident_id"))
        test = self.validate_fix(pipeline_id, fix.get("fix_id"))
        return {"rca": rca, "fix": fix, "test": test}

    def incidents(self):
        return incident_log.all()


orchestrator = Orchestrator()
