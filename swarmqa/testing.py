"""Helpers for chunk tests. Safe to import from the package."""

from __future__ import annotations

from pathlib import Path

from swarmqa.models import (
    Action,
    AppTarget,
    CampaignConfig,
    ElementQuery,
    Shard,
    UIElement,
)


def make_app(directory: Path, name: str = "Sample.app") -> AppTarget:
    app = directory / name
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    plist = app / "Contents" / "Info.plist"
    plist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.swarmqa.sample</string>
<key>CFBundleShortVersionString</key><string>0.0.1</string>
<key>CFBundleExecutable</key><string>Sample</string>
</dict></plist>
""",
        encoding="utf-8",
    )
    return AppTarget(path=str(app), bundle_id="dev.swarmqa.sample", maturity="shipped")


def sample_tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Save"),
                UIElement(role="textfield", label="Email", value=""),
                UIElement(role="button", label="Dark Mode"),
            ],
        )
    ]


def sample_config(app: AppTarget | None = None) -> CampaignConfig:
    config = CampaignConfig()
    if app is not None:
        config.app = app
    return config


def scripted_shard(actions: list[Action] | None = None) -> Shard:
    steps = actions or [
        Action(action="click", target=ElementQuery(role="button", label="Save")),
    ]
    return Shard(id="s-1-save", kind="scripted", name="save", actions=steps, goal="Save")
