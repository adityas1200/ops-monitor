"""In-memory conversation session store for multi-turn chat."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Dict, List


class SessionStore:
    """Stores conversation history per session ID for multi-turn LLM calls."""

    def __init__(self, max_messages: int = 40, max_sessions: int = 100):
        self._sessions: OrderedDict[str, List[Dict[str, str]]] = OrderedDict()
        self._max_messages = max_messages
        self._max_sessions = max_sessions
        self._lock = threading.Lock()

    def get_history(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def add_message(self, session_id: str, role: str, content: str):
        with self._lock:
            if session_id not in self._sessions:
                if len(self._sessions) >= self._max_sessions:
                    self._sessions.popitem(last=False)
                self._sessions[session_id] = []
            else:
                self._sessions.move_to_end(session_id)
            self._sessions[session_id].append({"role": role, "content": content})
            if len(self._sessions[session_id]) > self._max_messages:
                self._sessions[session_id] = self._sessions[session_id][-self._max_messages:]

    def clear(self, session_id: str):
        with self._lock:
            self._sessions.pop(session_id, None)


session_store = SessionStore()
