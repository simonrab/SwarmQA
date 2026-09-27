"""C1 — load, override, and validate campaign configuration.

Replace the stub body. Keep these signatures. See docs/CONTRACTS.md section C1.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, CliOverrides


def load_config(path: Path | None, overrides: CliOverrides | None = None) -> CampaignConfig:
    """Read TOML, apply CLI overrides, validate, and return CampaignConfig.

    Raise ConfigError with field-level messages. A missing file is
    `config: not found`. `backend = cloud` with missing or non-positive
    max_spend fails fast. Local backends that set max_spend still load, and
    the returned config keeps the value so the orchestrator can attach the
    ignore note.
    """
    raise ChunkNotReady("C1", "swarmqa.config.load_config")


def validate_config(config: CampaignConfig) -> list[str]:
    """Return field-level errors. An empty list means the config is usable."""
    raise ChunkNotReady("C1", "swarmqa.config.validate_config")
