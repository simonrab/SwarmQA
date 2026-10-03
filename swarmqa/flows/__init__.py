"""Flows contract (Wave D). Read-only for WP-D1 and WP-D2; the lead changes it.

Two sources of shards that `intent/ingest.build_queue` and the GitHub trigger
put in front of the configured intents:

- `swarmqa.flows.from_diff` (WP-D1) turns a code change into goal-mode
  exploratory shards, most important first:

      collect_change(repo_dir, base, head, *, max_chars) -> Change
      flows_from_diff(config, change, *, provider=None, max_flows=None) -> FlowPlan

  With no provider, or when the provider raises `ModelError`, it falls back
  to heuristics over the changed files (accessibility identifiers, view
  names), so a run never fails because the model did.

- `swarmqa.flows.cache` (WP-D2) stores the replay of every exploration that
  reached its goal in `<repo>/.aqa/flows/` and serves it back as a scripted
  shard. A cached step that no longer matches the app is re-explored alone
  and the script rewritten:

      FlowCache(root).load(shard_key) -> CachedFlow | None
      FlowCache(root).save(CachedFlow) -> Path
      cached_shards(config, shards) -> list[Shard]   # replaces hits, keeps misses

  `shard_key(shard)` is the stable cache key for a goal shard (its name and
  goal, normalised), so a flow proposed again from a later diff still hits.

Settings come from the `[flows]` table through `FlowsSettings.from_mapping`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarmqa.llm.protocol import ChangedFile, Usage
from swarmqa.models import Shard

FLOW_TAG = "diff"
CACHED_TAG = "cached"


@dataclass
class Change:
    """A code change: the unified diff (trimmed to a size cap) and the touched files."""

    base: str
    head: str
    diff: str = ""
    files: list[ChangedFile] = field(default_factory=list)
    truncated: bool = False


@dataclass
class FlowPlan:
    """Shards proposed for a change, most important first.

    Each shard is `kind="exploratory"` with `goal` set and tags including
    `FLOW_TAG` and `priority:<n>`. `source` is `"model"`, `"heuristic"` or
    `"none"`; `notes` says why a fallback was used.
    """

    shards: list[Shard] = field(default_factory=list)
    source: str = "none"
    notes: list[str] = field(default_factory=list)
    usage: Usage | None = None
