from __future__ import annotations
from typing import Any, Dict, List

from app.agents.base import BaseAgent
from app.connectors.aws_connector import AWSConnector
from app.connectors.snowflake_connector import SnowflakeConnector
from app.core.config import is_sso_auth, load_settings, platforms_configured


class ConnectivityAgent(BaseAgent):
    name = "connectivity"
    skill_file = "connectivity.md"

    def check(self) -> Dict[str, Any]:
        settings = load_settings()
        sf_cfg = settings.get("snowflake", {})
        services: List[Dict[str, Any]] = []
        services += AWSConnector().health()
        services += SnowflakeConnector().health()

        for i, svc in enumerate(services):
            if svc.get("name") == "Snowflake":
                services[i] = self._annotate_snowflake(svc, sf_cfg)

        ok = all(s["status"] == "connected" for s in services)
        configured = platforms_configured(settings)
        report = {
            "platforms": configured,
            "services": services,
            "ok": ok,
            "snowflake_auth_mode": "sso" if is_sso_auth(sf_cfg.get("authenticator", "password")) else "password",
        }

        for s in services:
            if s["status"] != "connected":
                self.learn({"signature": f"connectivity {s['name']} {s['status']}",
                            "service": s["name"], "detail": s.get("detail"),
                            "auth_mode": s.get("auth_mode"), "hints": s.get("hints")})
        return report

    def _annotate_snowflake(self, svc: Dict[str, Any], sf_cfg: Dict[str, Any]) -> Dict[str, Any]:
        auth_mode = svc.get("auth_mode") or (
            "sso" if is_sso_auth(sf_cfg.get("authenticator", "password")) else "password"
        )
        svc["auth_mode"] = auth_mode
        if svc["status"] != "connected" and not svc.get("hints"):
            svc["hints"] = [
                "Review Snowflake Settings: Account, Warehouse, Role, and login method (SSO vs Password).",
            ]
        return svc
