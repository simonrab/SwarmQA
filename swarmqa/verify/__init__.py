"""verify_fix: rebuild and replay a finding to confirm a fix.

Contract (signatures are fixed; WP-C2 implements the bodies, WP-C1's MCP
tools call them):

- `run_verify(...)` does the work in-process and returns a finished
  `VerifyResult` (state `passed`, `failed` or `error`).
- `start_verify(...)` launches `run_verify` in a detached process and returns
  at once with state `running` and a `verify_id`.
- `verify_status(verify_id)` reads the result the detached run writes.

`passed` means the finding did not reproduce on any device. Each verify run
writes to `<report_root>/<campaign_id>/verify/<verify_id>/` (a `status.json`
plus replay artifacts per device).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from swarmqa.mcp.tools import VerifyResult
from swarmqa.models import CampaignConfig


def run_verify(
    finding_id: str,
    *,
    config: CampaignConfig,
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
    verify_id: str | None = None,
    driver_factory: Callable[[Any, Path], Any] | None = None,
) -> VerifyResult:
    """Build (unless `build` is False), replay the finding on `devices` devices, and return the verdict."""
    raise NotImplementedError("run_verify lands with WP-C2")


def start_verify(
    finding_id: str,
    *,
    config_path: str | Path = "aqa.config.toml",
    campaign_id: str | None = None,
    devices: int = 2,
    build: bool = True,
) -> VerifyResult:
    """Start `run_verify` in a detached process; return state `running` with its `verify_id`."""
    raise NotImplementedError("start_verify lands with WP-C2")


def verify_status(verify_id: str, *, report_root: str | Path | None = None) -> VerifyResult:
    """Current state of a verify run started with `start_verify`."""
    raise NotImplementedError("verify_status lands with WP-C2")
