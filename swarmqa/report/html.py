"""The HTML campaign report (WP-F4).

Contract (lead-owned signature; WP-F4 owns the body):

`write_html_report(campaign_dir)` reads what the campaign wrote
(`summary.json`, `findings.json`, `findings/`, `media/`, `screen_graph.json`,
`workers/*/result.json`) and writes `<campaign_dir>/report.html`, returning
its path. It is called by the orchestrator after `summary.md`, and by
`aqa try` and `aqa report --html`. It must never raise: on any error it
returns None and the campaign still succeeds.
"""

from __future__ import annotations

from pathlib import Path


def write_html_report(campaign_dir: Path) -> Path | None:
    return None
