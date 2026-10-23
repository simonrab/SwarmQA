"""`aqa try --save`: a starter `aqa.config.toml` and one example intent."""

from __future__ import annotations

import json
from pathlib import Path

from swarmqa.quickstart.resolve import AppInfo

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "aqa.config.toml"
CONFIG_NAME = "aqa.config.toml"
INTENT_PATH = Path("intents") / "example-goal.md"
EXAMPLE_INTENT = """# Example goal

Open the first screen of the app and use its main feature once.
Check that the result appears and that no error is shown.
"""


def render_config(app: AppInfo, simulator: str | None = None, template: str | None = None) -> str:
    """The template config with the app, platform, runner driver and agent engine filled in."""
    text = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    platform_line = f"platform = {_q(app.platform)}"
    if app.source_dir:
        platform_line += f"\nsource_dir = {_q(app.source_dir)}"
    replacements = [
        ('# path = "/path/to/MyApp.app"', f"path = {_q(app.path)}" if app.path else None),
        ('# bundle_id = ""', f"bundle_id = {_q(app.bundle_id)}"),
        ('platform = "macos"', platform_line),
        ('# simulator = "iPhone 17"', f"simulator = {_q(simulator)}" if simulator else None),
        ('kind = "auto" #', 'kind = "runner" #'),
        ('engine = "legacy" #', 'engine = "agent" #'),
    ]
    for old, new in replacements:
        if new is not None:
            text = text.replace(old, new, 1)
    return text


def save_starter(directory: Path, app: AppInfo, simulator: str | None = None) -> list[str]:
    """Write the config and the example intent under `directory`. Never overwrites.
    Returns one line per file saying what happened."""
    lines = []
    for relative, body in ((Path(CONFIG_NAME), lambda: render_config(app, simulator)),
                           (INTENT_PATH, lambda: EXAMPLE_INTENT)):
        path = directory / relative
        if path.exists():
            lines.append(f"kept {relative} (it already exists)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body(), encoding="utf-8")
        lines.append(f"wrote {relative}")
    return lines


def _q(value: str | None) -> str:
    return json.dumps(value or "")
