"""WP-D2: the replay cache. Explorations that reached their goal, served back as scripts.

Every agent-loop session that reached its goal leaves its replay in
`session.json`; `FlowCache.record_session` keeps it as `<root>/<key>.json`,
a version-1 JSON flow that `aqa replay` and the intent loader read as is.
The flow format rejects extra fields, so the cache's own bookkeeping
(goal, timestamps, hits, heals, source SHA) sits beside it in
`<root>/<key>.meta.json`. `cached_shards` swaps each goal shard that has an
entry for a scripted shard running the cached steps; `swarmqa.flows.heal`
repairs a step that no longer matches the app.

See docs/flows.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from swarmqa.errors import IntentError
from swarmqa.flows import CACHED_TAG
from swarmqa.flows.settings import FlowsSettings
from swarmqa.intent.ingest import action_from_dict, action_to_dict
from swarmqa.models import Action, CampaignConfig, Shard
from swarmqa.serialize import dump_json
from swarmqa.util import slug

FLOW_VERSION = 1
_META_SUFFIX = ".meta.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def shard_key(shard: Shard) -> str:
    """The cache key for a goal shard: a slug of its name and a hash of name and goal.

    Case and runs of whitespace do not change the key, so a flow proposed
    again from a later diff still hits.
    """
    name = _normalise(shard.name)
    goal = _normalise(shard.goal)
    digest = hashlib.sha256(f"{name}\n{goal}".encode("utf-8")).hexdigest()[:12]
    return f"{slug(name, 40)}-{digest}"


@dataclass
class CachedFlow:
    """One cached flow: the version-1 steps that reached `goal`, and their history.

    `steps` never starts with the session's own `launch`; the scripted
    runner launches the app before the first step. `hits` counts replays
    that passed, `heals` the times a step was re-explored and the steps
    rewritten.
    """

    key: str
    name: str
    goal: str
    steps: list[dict] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    hits: int = 0
    heals: int = 0
    sha: str | None = None

    def actions(self) -> list[Action]:
        return [action_from_dict(step, origin=f"cached flow {self.key}") for step in self.steps]

    def flow_document(self) -> dict[str, Any]:
        return {"version": FLOW_VERSION, "name": self.name, "steps": [dict(step) for step in self.steps]}

    def meta_document(self) -> dict[str, Any]:
        return {
            "version": FLOW_VERSION,
            "key": self.key,
            "goal": self.goal,
            "created": self.created,
            "updated": self.updated,
            "hits": self.hits,
            "heals": self.heals,
            "sha": self.sha,
        }


def clean_steps(replay: Iterable[dict]) -> list[dict]:
    """Version-1 steps from an agent-loop replay, without its leading `launch`.

    Each step goes through `action_from_dict` and back, so a step the flow
    format rejects raises `IntentError` here, never at replay time.
    """
    steps = [dict(step) for step in replay]
    if steps and steps[0].get("action") == "launch":
        steps = steps[1:]
    return [action_to_dict(action_from_dict(step, origin="session replay")) for step in steps]


class FlowCache:
    """Cached flows under `root`: `<key>.json` (the flow) and `<key>.meta.json`."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def flow_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def meta_path(self, key: str) -> Path:
        return self.root / f"{key}{_META_SUFFIX}"

    def load(self, key: str) -> CachedFlow | None:
        """The cached flow for `key`, or None when it is missing or unreadable."""
        try:
            document = json.loads(self.flow_path(key).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or document.get("version") != FLOW_VERSION:
            return None
        name = document.get("name")
        steps = document.get("steps")
        if not isinstance(name, str) or not isinstance(steps, list):
            return None
        meta = self._load_meta(key)
        flow = CachedFlow(
            key=key,
            name=name,
            goal=str(meta.get("goal") or ""),
            steps=[dict(step) if isinstance(step, dict) else step for step in steps],
            created=str(meta.get("created") or ""),
            updated=str(meta.get("updated") or ""),
            hits=_count(meta.get("hits")),
            heals=_count(meta.get("heals")),
            sha=meta.get("sha") if isinstance(meta.get("sha"), str) else None,
        )
        try:
            flow.actions()
        except IntentError:
            return None
        return flow

    def save(self, flow: CachedFlow) -> Path:
        """Write the flow and its sidecar atomically and return the flow's path."""
        now = _now()
        flow.created = flow.created or now
        flow.updated = now
        path = self.flow_path(flow.key)
        dump_json(flow.flow_document(), path)
        dump_json(flow.meta_document(), self.meta_path(flow.key))
        return path

    def drop(self, key: str) -> bool:
        """Remove the entry for `key`. True when there was one."""
        found = False
        for path in (self.flow_path(key), self.meta_path(key)):
            try:
                path.unlink()
                found = True
            except FileNotFoundError:
                continue
        return found

    def keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            path.name[: -len(".json")]
            for path in self.root.glob("*.json")
            if not path.name.endswith(_META_SUFFIX)
        )

    def record_session(
        self,
        shard: Shard,
        session_json_path: Path | str,
        *,
        sha: str | None = None,
    ) -> CachedFlow | None:
        """Cache the replay in `session.json` when the session reached its goal.

        Returns the saved flow, or None when the session did not reach its
        goal, is missing or unreadable, or holds a step the flow format
        cannot express. An existing entry keeps its creation time and counts.
        """
        try:
            session = json.loads(Path(session_json_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(session, dict) or session.get("goal_reached") is not True:
            return None
        replay = session.get("replay")
        if not isinstance(replay, list) or not all(isinstance(step, dict) for step in replay):
            return None
        try:
            steps = clean_steps(replay)
        except IntentError:
            return None
        key = shard_key(shard)
        flow = self.load(key) or CachedFlow(key=key, name=shard.name, goal=shard.goal)
        flow.name = shard.name
        flow.goal = shard.goal
        flow.steps = steps
        flow.sha = sha or flow.sha
        self.save(flow)
        return flow

    def record_hit(self, key: str) -> CachedFlow | None:
        """Count one passing replay of `key`."""
        flow = self.load(key)
        if flow is None:
            return None
        flow.hits += 1
        self.save(flow)
        return flow

    def _load_meta(self, key: str) -> dict[str, Any]:
        try:
            meta = json.loads(self.meta_path(key).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return meta if isinstance(meta, dict) else {}


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def cache_root(
    config: CampaignConfig,
    settings: FlowsSettings | None = None,
    *,
    config_path: Path | str | None = None,
) -> Path:
    """Where the cache lives: `flows.cache_dir` under the app repo.

    An absolute `cache_dir` is used as is. Otherwise it is relative to
    `app.source_dir` when set, else to the directory of `config_path` (the
    config file, which `CampaignConfig` does not record), else to the
    working directory.
    """
    settings = settings or FlowsSettings.from_config(config)
    directory = Path(settings.cache_dir).expanduser()
    if directory.is_absolute():
        return directory
    if config.app.source_dir:
        base = Path(config.app.source_dir).expanduser()
    elif config_path is not None:
        base = Path(config_path).expanduser().resolve().parent
    else:
        base = Path.cwd()
    return base / directory


def is_goal_shard(shard: Shard) -> bool:
    """An exploratory shard with a goal: what the cache stores and serves."""
    return shard.kind == "exploratory" and bool(shard.goal.strip())


def is_cached_shard(shard: Shard) -> bool:
    """A scripted shard `cached_shards` made from a cache entry."""
    return shard.kind == "scripted" and CACHED_TAG in shard.tags and bool(shard.goal.strip())


def cached_shards(
    config: CampaignConfig,
    shards: list[Shard],
    *,
    settings: FlowsSettings | None = None,
    cache: FlowCache | None = None,
) -> list[Shard]:
    """Replace every goal shard that has a cache entry with a scripted shard.

    The scripted shard keeps the name, id, goal (so a heal can re-explore)
    and tags, plus `CACHED_TAG`; its `source_path` is the cached flow.
    Misses and other shards come back unchanged, in order. With
    `flows.cache = false` the list is returned as is.
    """
    settings = settings or FlowsSettings.from_config(config)
    if not settings.cache:
        return list(shards)
    cache = cache or FlowCache(cache_root(config, settings))
    result: list[Shard] = []
    for shard in shards:
        flow = cache.load(shard_key(shard)) if is_goal_shard(shard) else None
        if flow is None:
            result.append(shard)
            continue
        tags = list(shard.tags)
        if CACHED_TAG not in tags:
            tags.append(CACHED_TAG)
        result.append(
            replace(
                shard,
                kind="scripted",
                source_path=str(cache.flow_path(flow.key)),
                actions=flow.actions(),
                tags=tags,
            )
        )
    return result
