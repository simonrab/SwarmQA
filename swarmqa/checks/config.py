"""Build `ChecksSettings` from the `[checks]` config table.

```toml
[checks]
functional = true            # or a table of FunctionalSettings fields
layout = { contrast = false } # LayoutSettings fields; platform defaults to app.platform
baseline = false             # on by default only when visual.enabled
judge = { rubrics = ["visual"] }  # JudgeSettings fields; needs [llm] enabled
```

`false` turns a check off, `true` or a missing key keeps its defaults, and a
table overrides fields. Unknown keys raise ValueError. When the model judge
has no provider, `visual.judgment` (the command judge) is the fallback.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from swarmqa.checks import ChecksSettings
from swarmqa.checks.baseline import BaselineSettings
from swarmqa.checks.functional import FunctionalSettings
from swarmqa.checks.judge import JudgeSettings
from swarmqa.checks.layout import LayoutSettings
from swarmqa.models import CampaignConfig

_TABLES = ("functional", "layout", "baseline", "judge")


def checks_settings(config: CampaignConfig) -> ChecksSettings:
    raw = dict(config.checks.settings)
    unknown = sorted(set(raw) - set(_TABLES))
    if unknown:
        raise ValueError(f"unknown checks: {', '.join(unknown)}")
    layout_defaults = {"platform": config.app.platform}
    baseline_defaults = {
        "baseline_dir": config.visual.baseline_dir,
        "threshold": config.visual.threshold,
    }
    judge = raw.get("judge", True)
    if not isinstance(judge, (bool, dict)):
        raise ValueError("checks.judge must be true, false, or a table")
    return ChecksSettings(
        functional=_build(FunctionalSettings, raw.get("functional", True), "functional"),
        layout=_build(LayoutSettings, raw.get("layout", True), "layout", layout_defaults),
        baseline=_build(
            BaselineSettings, raw.get("baseline", config.visual.enabled), "baseline", baseline_defaults
        ),
        judge=judge is not False,
        judge_settings=_build(JudgeSettings, judge if isinstance(judge, dict) else True, "judge")
        or JudgeSettings(),
        command_judge=config.visual.judgment,
    )


def _build(cls, value: Any, name: str, defaults: dict[str, Any] | None = None):
    if value is False:
        return None
    values = dict(defaults or {})
    if isinstance(value, dict):
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown checks.{name} settings: {', '.join(unknown)}")
        values.update(value)
    elif value is not True:
        raise ValueError(f"checks.{name} must be true, false, or a table")
    return cls(**values)
