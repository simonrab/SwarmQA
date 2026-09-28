"""Optional observation cache for model decision backends."""

from __future__ import annotations

import hashlib
import json

from swarmqa.decision.protocol import DecisionAction, Observation


class ObservationCache:
    """Skip duplicate System One calls for the same tree fingerprint."""

    def __init__(self) -> None:
        self._store: dict[str, DecisionAction] = {}

    def get(self, key: str) -> DecisionAction | None:
        return self._store.get(key)

    def put(self, key: str, action: DecisionAction) -> None:
        self._store[key] = action

    @staticmethod
    def key_for(obs: Observation) -> str:
        payload = {
            "goal": obs.goal,
            "tree": obs.tree_summary,
            "tried_clicks": sorted(obs.tried_clicks),
            "tried_menus": [list(path) for path in sorted(obs.tried_menus)],
            "expected": list(obs.expected),
        }
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
