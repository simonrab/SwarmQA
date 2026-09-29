"""In-memory DevicePool for tests. Leases are exclusive within one instance."""

from __future__ import annotations

import threading
import time

from swarmqa.devices.protocol import Device, DeviceUnavailable
from swarmqa.models import BuildArtifact, TargetPlatform


class FakeDevicePool:
    def __init__(self, capacity: dict[str, int] | None = None):
        self._capacity = dict(capacity or {"ios": 2, "macos": 1})
        self._leased: dict[str, Device] = {}
        self._condition = threading.Condition()
        self._serial = 0
        self.released: list[tuple[str, bool]] = []

    def acquire(
        self,
        platform: TargetPlatform,
        build: BuildArtifact | None,
        *,
        timeout_s: float = 600.0,
    ) -> Device:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while self._in_use(platform) >= self.capacity(platform):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceUnavailable(f"no {platform} device free after {timeout_s:g}s")
                self._condition.wait(remaining)
            self._serial += 1
            device = Device(
                id=f"fake-{platform}-{self._serial}",
                platform=platform,
                kind="simulator" if platform == "ios" else "host",
                name=f"Fake {platform} {self._serial}",
                build=build,
            )
            self._leased[device.id] = device
            return device

    def release(self, device: Device, *, erase: bool = True) -> None:
        with self._condition:
            if self._leased.pop(device.id, None) is not None:
                self.released.append((device.id, erase))
                self._condition.notify_all()

    def capacity(self, platform: TargetPlatform) -> int:
        return self._capacity.get(platform, 0)

    def close(self) -> None:
        for device in list(self._leased.values()):
            self.release(device)

    def _in_use(self, platform: str) -> int:
        return sum(1 for device in self._leased.values() if device.platform == platform)
