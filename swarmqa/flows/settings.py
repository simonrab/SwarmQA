"""Settings for flows: the `[flows]` table.

    [flows]
    from_diff = true          # propose goal shards from the PR diff (needs a base SHA)
    max_flows = 10            # cap on proposed flows per change
    max_diff_chars = 60000    # diff text sent to the model; the rest is dropped
    max_file_chars = 20000    # per touched file
    file_globs = ["*.swift"]  # touched files whose content goes to the model
    cache = false             # replay cached flows instead of exploring (skips the agent-loop checks)
    cache_dir = ".aqa/flows"  # relative to the app repo (app.source_dir, else the config's dir)
    heal = true               # re-explore a failing cached step and rewrite the script
    heal_max_steps = 8        # agent-loop steps allowed to repair one step

Unknown keys raise `FlowsConfigError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from swarmqa.errors import ConfigError


class FlowsConfigError(ConfigError):
    """The `[flows]` table has a bad key or value."""


@dataclass
class FlowsSettings:
    from_diff: bool = True
    max_flows: int = 10
    max_diff_chars: int = 60_000
    max_file_chars: int = 20_000
    file_globs: list[str] = field(default_factory=lambda: ["*.swift"])
    # Off by default: a cached flow replays as a scripted shard, which runs none
    # of the agent-loop checks, so a layout or dead-tap regression on that path
    # would go unreported. Turn it on for speed when those checks matter less.
    cache: bool = False
    cache_dir: str = ".aqa/flows"
    heal: bool = True
    heal_max_steps: int = 8

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "FlowsSettings":
        data = dict(data or {})
        known = {item.name for item in fields(cls)}
        errors = [f"flows.{key}: unknown key" for key in sorted(set(data) - known)]
        settings = cls()
        for key, value in data.items():
            if key not in known:
                continue
            default = getattr(settings, key)
            name = f"flows.{key}"
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    errors.append(f"{name}: must be true or false")
                    continue
            elif isinstance(default, int):
                if type(value) is not int or value < 1:
                    errors.append(f"{name}: must be an integer of at least 1")
                    continue
            elif isinstance(default, list):
                if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                    errors.append(f"{name}: must be a list of non-empty strings")
                    continue
                value = list(value)
            elif not isinstance(value, str) or not value.strip():
                errors.append(f"{name}: must be a non-empty string")
                continue
            setattr(settings, key, value)
        if errors:
            raise FlowsConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "FlowsSettings":
        flows = getattr(config, "flows", None)
        return cls.from_mapping(getattr(flows, "settings", None))
