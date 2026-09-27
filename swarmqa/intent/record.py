"""C3 — write the owned JSON recorded-flow document.

See docs/CONTRACTS.md section C3.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady


def record_flow(
    *,
    app_path: str,
    out_path: Path,
    name: str = "recorded-flow",
    interactive: bool = False,
    events: list[dict] | None = None,
) -> Path:
    """Write a version-1 JSON flow to `out_path` and return that path.

    `events` lets tests supply actions without a display. Interactive mode
    reads line-oriented commands from stdin. On macOS, also poll the front
    app when events is None and interactive is false.
    """
    raise ChunkNotReady("C3", "swarmqa.intent.record.record_flow")
