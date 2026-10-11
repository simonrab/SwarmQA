"""Settings for the `[findings]` config table (WP-F2 fills in the fields).

Contract: `FindingsSettings.from_mapping(data)` raises a `ConfigError`
subclass listing every bad key or value; `FindingsSettings.from_config(config)`
reads `config.findings.settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FindingsSettings:
    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "FindingsSettings":
        return cls()

    @classmethod
    def from_config(cls, config: Any) -> "FindingsSettings":
        return cls.from_mapping(getattr(getattr(config, "findings", None), "settings", None))
