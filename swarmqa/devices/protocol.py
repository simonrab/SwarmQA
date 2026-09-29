"""DevicePool contract.

A pool leases devices to workers. `acquire` returns a booted device with the
build installed, or raises `DeviceUnavailable` when none frees up within
`timeout_s`. `release` hands the device back; with `erase=True` the pool
wipes app state so the next lease starts clean. Leases are exclusive across
every campaign on the host, not just within one campaign.

Use `lease(pool, ...)` in a `with` block so a crashed worker still releases.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Literal, Protocol, runtime_checkable

from swarmqa.models import BuildArtifact, TargetPlatform

DeviceKind = Literal["simulator", "vm", "host"]


class DeviceUnavailable(Exception):
    """No device of that platform could be leased in time."""


class DeviceSetupError(DeviceUnavailable):
    """A device was free but could not be prepared: clone, boot, or install failed.

    Subclasses DeviceUnavailable so callers that only catch that still work.
    Report it as a setup or build problem, not as a capacity shortage.
    """


@dataclass
class Device:
    """One leased device.

    `id` is unique on the host (a simulator UDID, a VM name, or `host`).
    `address` is how drivers reach it: empty for a local simulator or the
    local Mac, an IP or hostname for a VM. `work_dir` is a host path the
    worker may write to for this lease.
    """

    id: str
    platform: TargetPlatform
    kind: DeviceKind
    name: str = ""
    address: str = ""
    work_dir: str = ""
    build: BuildArtifact | None = None
    meta: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class DevicePool(Protocol):
    def acquire(
        self,
        platform: TargetPlatform,
        build: BuildArtifact | None,
        *,
        timeout_s: float = 600.0,
    ) -> Device:
        """Lease a booted device with `build` installed (none when `build` is None)."""

    def release(self, device: Device, *, erase: bool = True) -> None:
        """Return a lease. Safe to call twice for the same device."""

    def capacity(self, platform: TargetPlatform) -> int:
        """How many devices of `platform` this host can run at once."""

    def close(self) -> None:
        """Release every lease and shut down devices the pool started."""


@contextmanager
def lease(
    pool: DevicePool,
    platform: TargetPlatform,
    build: BuildArtifact | None,
    *,
    timeout_s: float = 600.0,
    erase: bool = True,
) -> Iterator[Device]:
    device = pool.acquire(platform, build, timeout_s=timeout_s)
    try:
        yield device
    finally:
        pool.release(device, erase=erase)
