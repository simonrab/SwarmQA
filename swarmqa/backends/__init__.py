"""Runner backends. The orchestrator asks this factory for the selected one."""

from __future__ import annotations

from swarmqa.models import CampaignConfig


def create_backend(config: CampaignConfig):
    if config.backend == "local":
        from swarmqa.backends.local import LocalBackend

        return LocalBackend(config)
    if config.backend == "vm":
        from swarmqa.backends.tart import TartBackend

        return TartBackend(config)
    if config.backend == "cloud":
        from swarmqa.backends.cloud import create_cloud_backend

        return create_cloud_backend(config)
    raise ValueError(f"unknown backend: {config.backend}")
