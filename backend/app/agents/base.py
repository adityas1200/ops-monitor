"""Base agent + Claude harness.

Every agent is SKILL-DRIVEN: it loads its skill markdown (the contract/procedure)
and is wrapped by a Claude harness. If ANTHROPIC_API_KEY is present the harness
asks Claude to reason over the skill + inputs; otherwise it degrades gracefully to
the agent's deterministic `_reason()` implementation so the platform always works.

Each agent owns an AgentMemory and can write to the shared incident log.
"""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional

from app.core.config import SKILLS_DIR, USE_CLAUDE, CLAUDE_MODEL, ANTHROPIC_API_KEY
from app.memory.memory_store import AgentMemory, incident_log

logger = logging.getLogger(__name__)


class ClaudeHarness:
    """Thin wrapper around the Anthropic SDK. No-ops cleanly when unavailable."""

    def __init__(self, model: str = CLAUDE_MODEL):
        self.model = model
        self._client = None
        self._last_error: Optional[str] = None
        self._init_client()

    def _init_client(self) -> None:
        """(Re-)initialize the Anthropic client with the current API key from environment."""
        from app.core.config import _load_env_file, PROJECT_ROOT
        _load_env_file(PROJECT_ROOT / ".env")
        import os
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.warning("ClaudeHarness disabled — ANTHROPIC_API_KEY not set")
            self._client = None
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
            logger.info("ClaudeHarness initialized with model=%s", self.model)
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)
            logger.error("ClaudeHarness init failed: %s", e)
            self._client = None

    def _refresh_and_retry(self) -> None:
        """Re-read API key from .env and reinitialize client (handles token rotation)."""
        import os
        old_key = os.getenv("ANTHROPIC_API_KEY", "")
        from app.core.config import _load_env_file, PROJECT_ROOT
        _load_env_file(PROJECT_ROOT / ".env", force=True)
        new_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if new_key and new_key != old_key:
            logger.info("ClaudeHarness: API key changed, reinitializing client")
            self._init_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _is_auth_error(self, e: Exception) -> bool:
        return "401" in str(e) or "authentication" in str(e).lower() or "x-api-key" in str(e).lower()

    def reason(self, system: str, user: str, max_tokens: int = 1500) -> Optional[str]:
        """Single-turn structured reasoning (JSON extraction). Returns raw text."""
        if not self._client:
            self._init_client()
        if not self._client:
            return None
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                system=system, messages=[{"role": "user", "content": user}],
            )
            self._last_error = None
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        except Exception as e:  # noqa: BLE001
            if self._is_auth_error(e):
                logger.warning("ClaudeHarness.reason() auth error, refreshing key...")
                self._refresh_and_retry()
                if self._client:
                    try:
                        msg = self._client.messages.create(
                            model=self.model, max_tokens=max_tokens,
                            system=system, messages=[{"role": "user", "content": user}],
                        )
                        self._last_error = None
                        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                    except Exception as e2:  # noqa: BLE001
                        self._last_error = str(e2)
                        logger.error("ClaudeHarness.reason() retry failed: %s", e2)
                        return None
            self._last_error = str(e)
            logger.error("ClaudeHarness.reason() failed: %s", e)
            return None

    def speak(self, system: str, messages: list, max_tokens: int = 1500) -> Optional[str]:
        """Multi-turn conversational generation. No JSON constraint."""
        if not self._client:
            self._init_client()
        if not self._client:
            return None
        try:
            msg = self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                system=system, messages=messages,
            )
            self._last_error = None
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        except Exception as e:  # noqa: BLE001
            if self._is_auth_error(e):
                logger.warning("ClaudeHarness.speak() auth error, refreshing key...")
                self._refresh_and_retry()
                if self._client:
                    try:
                        msg = self._client.messages.create(
                            model=self.model, max_tokens=max_tokens,
                            system=system, messages=messages,
                        )
                        self._last_error = None
                        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                    except Exception as e2:  # noqa: BLE001
                        self._last_error = str(e2)
                        logger.error("ClaudeHarness.speak() retry failed: %s", e2)
                        return None
            self._last_error = str(e)
            logger.error("ClaudeHarness.speak() failed: %s", e)
            return None

    def speak_stream(self, system: str, messages: list, max_tokens: int = 1500):
        """Streaming multi-turn generation. Yields text chunks."""
        if not self._client:
            self._init_client()
        if not self._client:
            return
        try:
            with self._client.messages.stream(
                model=self.model, max_tokens=max_tokens,
                system=system, messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    self._last_error = None
                    yield text
        except Exception as e:  # noqa: BLE001
            if self._is_auth_error(e):
                logger.warning("ClaudeHarness.speak_stream() auth error, refreshing key...")
                self._refresh_and_retry()
                if self._client:
                    try:
                        with self._client.messages.stream(
                            model=self.model, max_tokens=max_tokens,
                            system=system, messages=messages,
                        ) as stream:
                            for text in stream.text_stream:
                                self._last_error = None
                                yield text
                        return
                    except Exception as e2:  # noqa: BLE001
                        self._last_error = str(e2)
                        logger.error("ClaudeHarness.speak_stream() retry failed: %s", e2)
                        return
            self._last_error = str(e)
            logger.error("ClaudeHarness.speak_stream() failed: %s", e)
            return


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
            f"Follow this skill exactly and respond with valid JSON matching its Output Contract.\n"
            f"CRITICAL RULES:\n"
            f"- Output raw JSON only — NO markdown code fences, NO ```json wrapper\n"
            f"- Start your response with {{ and end with }}\n"
            f"- Use the EXACT key names from the Output Contract\n\n"
            f"=== SKILL ===\n{self.skill}"
        )
        txt = self.harness.reason(system, json.dumps(user_payload, default=str), max_tokens)
        if not txt:
            return None
        return self._extract_json(txt)

    @staticmethod
    def _extract_json(txt: str) -> Optional[Dict[str, Any]]:
        """Robustly extract JSON from LLM response, handling code fences and truncation."""
        # Strip markdown code fences if present
        cleaned = txt.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        # Try direct parse first
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            pass

        # Find outermost braces
        start = cleaned.find("{")
        if start == -1:
            return None
        end = cleaned.rfind("}")
        if end == -1 or end <= start:
            # Truncated response — try to close it
            # Count open braces and close them
            fragment = cleaned[start:]
            open_braces = fragment.count("{") - fragment.count("}")
            fragment += "}" * open_braces
            try:
                return json.loads(fragment)
            except (json.JSONDecodeError, ValueError):
                return None

        try:
            return json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None

    def learn(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return self.memory.remember(record)

    def recall(self, signature: str):
        return self.memory.recall(signature)
