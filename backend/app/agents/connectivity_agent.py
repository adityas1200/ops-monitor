from __future__ import annotations
from typing import Any, Dict, List, Optional

from app.agents.base import BaseAgent
from app.connectors.aws_connector import AWSConnector
from app.connectors.snowflake_connector import SnowflakeConnector
from app.core.config import (
    aws_configured,
    load_settings,
    platforms_configured,
    snowflake_auth_mode,
    snowflake_configured,
)


def can_save_connections(settings: Dict[str, Any], services: List[Dict[str, Any]]) -> bool:
    """True when every configured platform's primary probe is connected.

    Ignores optional AWS sub-services (glue, etc.) so Snowflake-only setups can save.
    """
    sf_ok = snowflake_configured(settings.get("snowflake", {}))
    aws_ok = aws_configured(settings.get("aws", {}))
    if not sf_ok and not aws_ok:
        return False
    by_name = {s.get("name"): s for s in services}
    if sf_ok and by_name.get("Snowflake", {}).get("status") != "connected":
        return False
    if aws_ok and by_name.get("AWS STS", {}).get("status") != "connected":
        return False
    return True


class ConnectivityAgent(BaseAgent):
    name = "connectivity"
    skill_file = "connectivity.md"

    def check(self, settings: Optional[Dict[str, Any]] = None,
              ephemeral: bool = False) -> Dict[str, Any]:
        cfg = settings if settings is not None else load_settings()
        sf_cfg = cfg.get("snowflake", {})
        services: List[Dict[str, Any]] = []
        services += AWSConnector(settings=cfg).health()
        services += SnowflakeConnector(settings=cfg).health(ephemeral=ephemeral)

        for i, svc in enumerate(services):
            if svc.get("name") == "Snowflake":
                services[i] = self._annotate_snowflake(svc, sf_cfg)

        ok = all(s["status"] == "connected" for s in services)
        configured = platforms_configured(cfg)
        report = {
            "platforms": configured,
            "services": services,
            "ok": ok,
            "can_save": can_save_connections(cfg, services),
            "snowflake_auth_mode": snowflake_auth_mode(sf_cfg.get("authenticator", "password")),
        }

        for s in services:
            if s["status"] != "connected":
                self.learn({"signature": f"connectivity {s['name']} {s['status']}",
                            "service": s["name"], "detail": s.get("detail"),
                            "auth_mode": s.get("auth_mode"), "hints": s.get("hints")})
        return report

    def _annotate_snowflake(self, svc: Dict[str, Any], sf_cfg: Dict[str, Any]) -> Dict[str, Any]:
        auth_mode = svc.get("auth_mode") or snowflake_auth_mode(
            sf_cfg.get("authenticator", "password")
        )
        svc["auth_mode"] = auth_mode
        if svc["status"] != "connected" and not svc.get("hints"):
            svc["hints"] = [
                "Review Snowflake Settings: Account, Warehouse, Role, and login method "
                "(Password, SSO, or Key pair).",
            ]
        return svc
