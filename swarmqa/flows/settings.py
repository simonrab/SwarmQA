"""Settings for flows from the diff and the replay cache: the `[flows]` config table.

    [flows]
    from_diff = false        # on GitHub runs, add intents proposed from the change (needs [llm])
    max_flows = 10           # most intents one change may add
    include = ["*.swift", "*.strings", "*.xcstrings", "*.storyboard", "*.xib"]
    exclude = ["*Tests/*", "*Tests.swift"]
    max_files = 20           # changed files whose content goes to the model
    max_file_bytes = 20000   # per file, after which it is cut
    max_diff_bytes = 60000
    cache = true             # replay saved goal paths first (explorer.engine = "agent")
    cache_dir = ""           # default: <app.source_dir>/.aqa/flows; no cache without either
    max_failures = 3         # drop a cached flow after this many failed runs in a row

Unknown keys raise `FlowsConfigError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarmqa.build.settings import _fill
from swarmqa.errors import ConfigError

_INCLUDE = ["*.swift", "*.strings", "*.xcstrings", "*.storyboard", "*.xib"]
_EXCLUDE = ["*Tests/*", "*Tests.swift"]


class FlowsConfigError(ConfigError):
    """The `[flows]` table has a bad key or value."""


@dataclass
class FlowsSettings:
    from_diff: bool = False
    max_flows: int = 10
    include: list[str] = field(default_factory=lambda: list(_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(_EXCLUDE))
    max_files: int = 20
    max_file_bytes: int = 20000
    max_diff_bytes: int = 60000
    cache: bool = True
    cache_dir: str = ""
    max_failures: int = 3

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "FlowsSettings":
        errors: list[str] = []
        settings = _fill(cls(), dict(data or {}), "flows", errors)
        for name in ("max_flows", "max_files", "max_file_bytes", "max_diff_bytes", "max_failures"):
            value = getattr(settings, name)
            if isinstance(value, int) and value < 1:
                errors.append(f"flows.{name}: must be >= 1")
        if errors:
            raise FlowsConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "FlowsSettings":
        flows = getattr(config, "flows", None)
        return cls.from_mapping(getattr(flows, "settings", None))

    def cache_root(self, config: Any) -> Path | None:
        """Where cached flows live for this app: `cache_dir` (relative to
        `app.source_dir` when set), else `<app.source_dir>/.aqa/flows`, else
        None, so a run with no app repo never writes a cache by surprise."""
        source = getattr(config.app, "source_dir", None)
        if self.cache_dir:
            path = Path(self.cache_dir).expanduser()
            if not path.is_absolute() and source:
                path = Path(source).expanduser() / path
            return path
        if source:
            return Path(source).expanduser() / ".aqa" / "flows"
        return None
