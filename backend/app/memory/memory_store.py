"""File-based memory for agents.

Two layers:
  * Per-agent memory  -> store/<agent>_memory.json  (learned patterns, user prefs)
  * Shared incident log -> store/incident_log.json   (every incident + resolution)

Memory is intentionally simple/transparent JSON so it is auditable and portable.
Agents read it to short-circuit known problems and write to it to learn.
"""
from __future__ import annotations
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import MEMORY_DIR

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(name: str) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return MEMORY_DIR / name


def _read(name: str, default: Any) -> Any:
    p = _path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def _write(name: str, data: Any) -> None:
    _path(name).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


class AgentMemory:
    """One instance per agent. Stores a list of learned 'records'."""

    def __init__(self, agent: str):
        self.agent = agent
        self.file = f"{agent}_memory.json"

    def all(self) -> List[Dict[str, Any]]:
        return _read(self.file, [])

    def remember(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with _LOCK:
            data = self.all()
            record = {"id": f"{self.agent}-{len(data)+1}", "ts": _now(), **record}
            data.append(record)
            _write(self.file, data)
        return record

    def recall(self, signature: str) -> List[Dict[str, Any]]:
        """Naive but effective: substring/keyword match on a 'signature' field."""
        sig = (signature or "").lower()
        hits = []
        for r in self.all():
            hay = json.dumps(r).lower()
            if sig and any(tok in hay for tok in sig.split() if len(tok) > 3):
                hits.append(r)
        return hits


class IncidentLog:
    """Shared incident log all agents append to and learn from."""

    file = "incident_log.json"

    def all(self) -> List[Dict[str, Any]]:
        return _read(self.file, [])

    def log(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        with _LOCK:
            data = self.all()
            incident = {
                "incident_id": incident.get("incident_id") or f"INC-{len(data)+1:04d}",
                "logged_at": _now(),
                "source": incident.get("source", "system"),
                **incident,
            }
            # de-dupe by pipeline_id + status within the same batch
            data.append(incident)
            _write(self.file, data)
        return incident

    def update(self, incident_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with _LOCK:
            data = self.all()
            for inc in data:
                if inc.get("incident_id") == incident_id:
                    inc.update(patch)
                    inc["updated_at"] = _now()
                    _write(self.file, data)
                    return inc
        return None

    def find_by_signature(self, signature: str) -> List[Dict[str, Any]]:
        sig = (signature or "").lower()
        return [i for i in self.all() if sig and sig[:40] in json.dumps(i).lower()]


incident_log = IncidentLog()
