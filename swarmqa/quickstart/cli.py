"""`aqa try` entry point (WP-F1 owns the body).

Contract: `main(argv) -> int`, called by `swarmqa.cli` with every argument
after `try`. Exit 0 when the campaign ran (findings or not), 2 when it could
not run.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("aqa try is not built yet")
