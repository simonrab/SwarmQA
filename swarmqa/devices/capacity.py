"""How many simulators and VMs this host can run at once.

iOS simulators are bounded by RAM and CPU cores (by default one per 2.5 GB
and per 2 cores, after a host reserve). Tart is additionally capped at two
macOS VMs per host: Apple's macOS licence allows at most two virtual macOS
instances per Mac, and Virtualization.framework enforces it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

from swarmqa.devices.commands import Runner, default_runner, run

TART_VM_LIMIT = 2
_GB = 1024**3


@dataclass
class HostResources:
    memory_bytes: int
    cpu_cores: int

    @property
    def memory_gb(self) -> float:
        return self.memory_bytes / _GB


@dataclass
class CapacityPolicy:
    """Per-device costs. `max_*` caps the result when set."""

    gb_per_simulator: float = 2.5
    cores_per_simulator: float = 2.0
    gb_per_vm: float = 8.0
    cores_per_vm: float = 4.0
    reserve_gb: float = 4.0
    max_simulators: int | None = None
    max_vms: int | None = None


def host_resources(
    runner: Runner | None = None,
    *,
    platform: str | None = None,
    sysconf: Callable[[str], int] | None = None,
    cpu_count: Callable[[], int | None] | None = None,
) -> HostResources:
    """Read RAM and cores: `sysctl` on macOS, `os.sysconf` elsewhere."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        runner = runner or default_runner
        memory = _sysctl_int(runner, "hw.memsize")
        cores = _sysctl_int(runner, "hw.ncpu")
        if memory and cores:
            return HostResources(memory, cores)
    sysconf = sysconf or os.sysconf
    cpu_count = cpu_count or os.cpu_count
    try:
        memory = int(sysconf("SC_PAGE_SIZE")) * int(sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        memory = 0
    return HostResources(max(0, memory), int(cpu_count() or 1))


def _sysctl_int(runner: Runner, key: str) -> int:
    result = run(runner, ["sysctl", "-n", key], timeout=10)
    if not result.ok:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def simulator_capacity(resources: HostResources, policy: CapacityPolicy | None = None) -> int:
    """Simulators this host can run at once. Always at least 1."""
    policy = policy or CapacityPolicy()
    by_memory = int(max(0.0, resources.memory_gb - policy.reserve_gb) // policy.gb_per_simulator)
    by_cores = int(resources.cpu_cores // policy.cores_per_simulator)
    count = max(1, min(by_memory, by_cores))
    if policy.max_simulators is not None:
        count = min(count, max(0, policy.max_simulators))
    return count


def vm_capacity(resources: HostResources, policy: CapacityPolicy | None = None) -> int:
    """Tart VMs this host can run at once: at least 1, never more than 2."""
    policy = policy or CapacityPolicy()
    by_memory = int(max(0.0, resources.memory_gb - policy.reserve_gb) // policy.gb_per_vm)
    by_cores = int(resources.cpu_cores // policy.cores_per_vm)
    count = max(1, min(by_memory, by_cores, TART_VM_LIMIT))
    if policy.max_vms is not None:
        count = min(count, max(0, policy.max_vms))
    return min(count, TART_VM_LIMIT)
