"""Base agent + Claude harness.

Every agent is SKILL-DRIVEN: it loads its skill markdown (the contract/procedure)
and is wrapped by a Claude harness. If ANTHROPIC_API_KEY is present the harness
asks Claude to reason over the skill + inputs; otherwise it degrades gracefully to
the agent's deterministic `_reason()` implementation so the platform always works.

Each agent owns an AgentMemory and can write to the shared incident log.
"""
from __future__ import annotations
import json
from typing import Any, Dict, Optional

from app.core.config import SKILLS_DIR, USE_CLAUDE, CLAUDE_MODEL, ANTHROPIC_API_KEY
from app.memory.memory_store import AgentMemory, incident_log


class ClaudeHarness:
    """Thin wrapper around the Anthropic SDK. No-ops cleanly when unavailable."""

    def __init__(self, model: str = CLAUDE_MODEL):
        self.model = model
        self._client = None
        if USE_CLAUDE:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            except Exception:  # noqa: BLE001
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def reason(self, system: str, user: str, max_tokens: int = 1500) -> Optional[str]:
        if not self._client:
            return None
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                system=system, messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        except Exception:  # noqa: BLE001
            return None


class BaseAgent:
    name: str = "base"
    skill_file: str = ""

    def __init__(self):
        self.memory = AgentMemory(self.name)
        self.incident_log = incident_log
        self.harness = ClaudeHarness()
        self.skill = self._load_skill()

    def _load_skill(self) -> str:
        p = SKILLS_DIR / self.skill_file
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def think(self, user_payload: Dict[str, Any], max_tokens: int = 1500) -> Optional[Dict[str, Any]]:
        """Run the skill through Claude and parse a JSON answer if it returns one."""
        if not self.harness.available:
            return None
        system = (
            f"You are the {self.name} agent in an agentic data-ops monitoring system.\n"
            f"Follow this skill exactly and respond ONLY with valid JSON matching its "
            f"Output Contract.\n\n=== SKILL ===\n{self.skill}"
        )
        txt = self.harness.reason(system, json.dumps(user_payload, default=str), max_tokens)
        if not txt:
            return None
        try:
            start, end = txt.find("{"), txt.rfind("}")
            return json.loads(txt[start:end + 1])
        except Exception:  # noqa: BLE001
            return None

    def learn(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return self.memory.remember(record)

    def recall(self, signature: str):
        return self.memory.recall(signature)
