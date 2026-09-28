"""Optional observation cache stub (filled when model backends land)."""

from __future__ import annotations

from swarmqa.decision.protocol import DecisionAction, Observation


class ObservationCache:
    """No-op cache used when ``cache_observations`` is enabled."""

    def __init__(self) -> None:
        self._store: dict[str, DecisionAction] = {}

    def get(self, key: str) -> DecisionAction | None:
        return self._store.get(key)

    def put(self, key: str, action: DecisionAction) -> None:
        self._store[key] = action

    @staticmethod
    def key_for(obs: Observation) -> str:
        return f"{obs.goal}|{obs.stall_count}|{len(obs.elements)}"
