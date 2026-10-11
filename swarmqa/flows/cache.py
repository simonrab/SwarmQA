"""WP-D2: the replay cache.

When the agent loop reaches a goal (`done`), the path that got there is saved
as a cached flow: each step's action plus the fingerprints of the screen it
started on and the one it led to. The next run of the same intent on the
same platform replays that path first, with no model calls, while the
checks run as usual on every screen.

Self-heal: a cached step whose screen does not match, whose target is gone
or that leads somewhere else is re-explored alone (heuristic, then model).
As soon as the loop reaches a screen a later cached step starts on, the
replay picks up from there. When the goal is reached again the new path
replaces the old one and `heals` goes up. A flow that fails
`flows.max_failures` runs in a row is dropped.

Files: `<cache root>/<platform>/<intent slug>-<goal hash>.json` (see
`FlowsSettings.cache_root`: the app repo's `.aqa/flows/` or `flows.cache_dir`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarmqa.flows.settings import FlowsSettings
from swarmqa.models import Shard
from swarmqa.serialize import dump_json, load_json
from swarmqa.util import slug

CACHE_VERSION = 1


@dataclass
class CachedStep:
    """`action` is the agent loop's decision JSON (`decision_to_json`)."""

    action: dict[str, Any]
    screen: str
    after: str


@dataclass
class CachedFlow:
    name: str
    goal: str
    platform: str
    steps: list[CachedStep] = field(default_factory=list)
    end_screen: str = ""
    runs: int = 0
    heals: int = 0
    failures: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "name": self.name,
            "goal": self.goal,
            "platform": self.platform,
            "steps": [{"action": s.action, "screen": s.screen, "after": s.after} for s in self.steps],
            "end_screen": self.end_screen,
            "runs": self.runs,
            "heals": self.heals,
            "failures": self.failures,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedFlow":
        if data.get("version") != CACHE_VERSION:
            raise ValueError(f"cached flow version must be {CACHE_VERSION}")
        return cls(
            name=str(data["name"]),
            goal=str(data["goal"]),
            platform=str(data["platform"]),
            steps=[CachedStep(dict(s["action"]), str(s["screen"]), str(s["after"])) for s in data["steps"]],
            end_screen=str(data.get("end_screen") or ""),
            runs=int(data.get("runs", 0)),
            heals=int(data.get("heals", 0)),
            failures=int(data.get("failures", 0)),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FlowCache:
    def __init__(self, root: str | Path, *, max_failures: int = 3):
        self.root = Path(root)
        self.max_failures = max_failures

    @classmethod
    def from_config(cls, config, *, force: bool = False) -> "FlowCache | None":
        """The cache for `config.app`, or None when `flows.cache` is off (unless
        `force`) or there is nowhere to keep it (see `FlowsSettings.cache_root`)."""
        settings = FlowsSettings.from_config(config)
        root = settings.cache_root(config)
        if root is None or (not settings.cache and not force):
            return None
        return cls(root, max_failures=settings.max_failures)

    def path_for(self, shard: Shard, platform: str) -> Path:
        digest = hashlib.sha256(shard.goal.strip().encode("utf-8")).hexdigest()[:8]
        return self.root / slug(platform or "macos") / f"{slug(shard.name)}-{digest}.json"

    def load(self, shard: Shard, platform: str) -> CachedFlow | None:
        """The cached flow for `shard`, or None (missing, unreadable, or a different goal)."""
        path = self.path_for(shard, platform)
        if not path.is_file():
            return None
        try:
            flow = CachedFlow.from_dict(load_json(path))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return flow if flow.goal == shard.goal.strip() and flow.steps else None

    def save(
        self,
        shard: Shard,
        platform: str,
        steps: list[CachedStep],
        end_screen: str,
        *,
        previous: CachedFlow | None = None,
    ) -> Path:
        """Store the path that reached the goal. A changed path counts as a heal."""
        flow = CachedFlow(
            name=shard.name,
            goal=shard.goal.strip(),
            platform=platform,
            steps=list(steps),
            end_screen=end_screen,
            created_at=_now(),
        )
        if previous is not None:
            flow.created_at = previous.created_at or flow.created_at
            flow.runs = previous.runs
            changed = [s.action for s in previous.steps] != [s.action for s in steps]
            flow.heals = previous.heals + int(changed)
        flow.runs += 1
        flow.updated_at = _now()
        path = self.path_for(shard, platform)
        dump_json(flow.to_dict(), path)
        return path

    def record_failure(self, shard: Shard, platform: str, flow: CachedFlow) -> bool:
        """Count a run that did not reach the goal. Return True when the flow was dropped."""
        path = self.path_for(shard, platform)
        flow.failures += 1
        if flow.failures >= self.max_failures:
            path.unlink(missing_ok=True)
            return True
        flow.updated_at = _now()
        dump_json(flow.to_dict(), path)
        return False

    def entries(self) -> list[CachedFlow]:
        found: list[CachedFlow] = []
        if not self.root.is_dir():
            return found
        for path in sorted(self.root.glob("*/*.json")):
            try:
                found.append(CachedFlow.from_dict(load_json(path)))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return found
