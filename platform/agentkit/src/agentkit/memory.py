"""Long-term memory behind one interface: in-process for dev/CI, AgentCore Memory in AWS.

Start with ONE kind of long-term memory and earn the rest with evals. Here that is episodic
"what did we decide last time for this entity", namespaced per tenant + entity so one
customer's history can never be retrieved into another customer's case.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Protocol


class MemoryStore(Protocol):
    def remember(self, namespace: str, event: dict[str, Any]) -> None: ...
    def recall(self, namespace: str, limit: int = 5) -> list[dict[str, Any]]: ...


class InMemoryMemory:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def remember(self, namespace: str, event: dict[str, Any]) -> None:
        if not namespace.count("/") >= 2:
            raise ValueError("namespace must be '<tenant>/<entity_type>/<entity_id>'")
        self._events[namespace].append({**event, "ts": datetime.now(UTC).isoformat()})

    def recall(self, namespace: str, limit: int = 5) -> list[dict[str, Any]]:
        return list(reversed(self._events.get(namespace, [])))[:limit]


class AgentCoreMemory:
    """Adapter for AgentCore Memory (short-term events + extracted long-term records).

    STATUS: DRAFT - method names follow the bedrock-agentcore MemoryClient; verify against the
    installed SDK version before enabling (set MEMORY_BACKEND=agentcore).
    """

    def __init__(self, memory_id: str, region: str):
        from bedrock_agentcore.memory import MemoryClient  # type: ignore[import-not-found]

        self.client, self.memory_id = MemoryClient(region_name=region), memory_id

    def remember(self, namespace: str, event: dict[str, Any]) -> None:
        tenant, _, entity = namespace.partition("/")
        self.client.create_event(memory_id=self.memory_id, actor_id=entity, session_id=tenant,
                                 messages=[(str(event), "ASSISTANT")])

    def recall(self, namespace: str, limit: int = 5) -> list[dict[str, Any]]:
        recs = self.client.retrieve_memories(memory_id=self.memory_id, namespace=namespace, query="decisions",
                                             top_k=limit)
        return [{"text": r.get("content", {}).get("text", "")} for r in recs]
