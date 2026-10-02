"""Settings for the GitHub trigger: the `[github]` config table.

    [github]
    repo = "acme/App"            # default: issues.github_repo, then `gh repo view`
    clone_url = "git@github.com:acme/App.git"   # default: build.repo, app.source_dir, then https
    platforms = ["ios", "macos"]  # one campaign per platform; default: app.platform
    events = ["pr", "push"]       # what `aqa watch` reacts to
    branch = "main"               # the branch whose merges trigger a run; default: the repo default
    poll_interval_s = 60
    report = "none"               # "check", "comment" or "both"; nothing is posted by default
    handoff = "none"              # "claude" or "codex": ask that agent to fix in the PR comment
    check_name = "SwarmQA"
    fail_on_advisory = false      # model-only findings never fail the check unless set
    max_findings = 20             # rows in the check summary and the comment
    evidence_command = "./scripts/upload.sh"    # optional: uploads $AQA_FILE, prints its URL

Unknown keys raise `GitHubConfigError`, so a typo never silently turns
reporting off or on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarmqa.build.settings import DEFAULT_ROOT, ROOT_ENV, _fill
from swarmqa.errors import ConfigError

REPORTS = ("none", "check", "comment", "both")
HANDOFFS = ("none", "claude", "codex")
EVENTS = ("pr", "push")
PLATFORMS = ("ios", "macos")


class GitHubConfigError(ConfigError):
    """The `[github]` table has a bad key or value."""


@dataclass
class GitHubSettings:
    repo: str | None = None
    clone_url: str | None = None
    platforms: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=lambda: list(EVENTS))
    branch: str | None = None
    poll_interval_s: float = 60.0
    report: str = "none"
    handoff: str = "none"
    check_name: str = "SwarmQA"
    fail_on_advisory: bool = False
    max_findings: int = 20
    evidence_command: str | None = None
    state_root: str | None = None

    @property
    def posts_check(self) -> bool:
        return self.report in ("check", "both")

    @property
    def posts_comment(self) -> bool:
        return self.report in ("comment", "both")

    def state_dir(self) -> Path:
        if self.state_root:
            return Path(self.state_root).expanduser()
        return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser() / "state"

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "GitHubSettings":
        errors: list[str] = []
        settings = _fill(cls(), dict(data or {}), "github", errors)
        for name, choices in (("report", REPORTS), ("handoff", HANDOFFS)):
            if getattr(settings, name) not in choices:
                errors.append(f"github.{name}: must be one of {', '.join(choices)}")
        for name, choices in (("platforms", PLATFORMS), ("events", EVENTS)):
            bad = [item for item in getattr(settings, name) if item not in choices]
            if bad:
                errors.append(f"github.{name}: unknown {', '.join(map(str, bad))} (expected {', '.join(choices)})")
        if isinstance(settings.poll_interval_s, (int, float)) and settings.poll_interval_s < 5:
            errors.append("github.poll_interval_s: must be >= 5")
        if isinstance(settings.max_findings, int) and settings.max_findings < 1:
            errors.append("github.max_findings: must be >= 1")
        if settings.repo is not None and settings.repo.count("/") != 1:
            errors.append("github.repo: must be owner/name")
        if errors:
            raise GitHubConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "GitHubSettings":
        github = getattr(config, "github", None)
        settings = cls.from_mapping(getattr(github, "settings", None))
        issues = getattr(config, "issues", None)
        if settings.repo is None and issues is not None and getattr(issues, "github_repo", None):
            settings.repo = issues.github_repo
        if not settings.platforms:
            app = getattr(config, "app", None)
            settings.platforms = [getattr(app, "platform", None) or "macos"]
        return settings
