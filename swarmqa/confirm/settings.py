"""Settings for findings people can trust: the `[findings]` config table.

    [findings]
    confirm = true              # re-run each finding before reporting it
    confirm_runs = 1            # replays per finding, each on a fresh launch
    confirm_kinds = ["crash", "unresponsive", "timeout", "error_state",
                     "assertion", "missing_control", "launch"]
    max_confirm = 20            # most findings to re-run, most severe first
    confirm_budget_s = 300      # total time for all re-runs
    skip_advisory = true        # do not re-run model-only findings
    drop_not_reproduced = false # remove findings that never showed again
    group = true                # fold the same problem in many places into one finding
    group_min = 2               # fold only when at least this many match

Unknown keys and bad values raise `FindingsConfigError`. See docs/trust.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swarmqa.build.settings import _fill
from swarmqa.errors import ConfigError

CONFIRM_KINDS = ["crash", "unresponsive", "timeout", "error_state", "assertion", "missing_control", "launch"]
KNOWN_KINDS = set(CONFIRM_KINDS) | {"visual", "visual_judgment", "suite_failure", "friction_path"}


class FindingsConfigError(ConfigError):
    """The `[findings]` table has a bad key or value."""


@dataclass
class FindingsSettings:
    confirm: bool = True
    confirm_runs: int = 1
    confirm_kinds: list[str] = field(default_factory=lambda: list(CONFIRM_KINDS))
    max_confirm: int = 20
    confirm_budget_s: float = 300.0
    skip_advisory: bool = True
    drop_not_reproduced: bool = False
    group: bool = True
    group_min: int = 2

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "FindingsSettings":
        """Build settings from the raw `[findings]` table; raise listing every problem."""
        errors: list[str] = []
        settings = _fill(cls(), dict(data or {}), "findings", errors)
        for name, least in (("confirm_runs", 1), ("max_confirm", 0), ("group_min", 2)):
            value = getattr(settings, name)
            if isinstance(value, int) and value < least:
                errors.append(f"findings.{name}: must be >= {least}")
        budget = settings.confirm_budget_s
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget <= 0:
            errors.append("findings.confirm_budget_s: must be a number > 0")
        unknown = [kind for kind in settings.confirm_kinds if kind not in KNOWN_KINDS]
        if unknown:
            errors.append(f"findings.confirm_kinds: unknown kinds: {', '.join(unknown)}")
        if errors:
            raise FindingsConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "FindingsSettings":
        """Read `config.findings.settings` (the raw table) when the config has one."""
        return cls.from_mapping(getattr(getattr(config, "findings", None), "settings", None))
