"""The `[build]` table reaches the config, the CLI and verify."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmqa import cli
from swarmqa.config import load_config
from swarmqa.errors import ConfigError
from swarmqa.verify import build as build_mod
from swarmqa.verify.core import _default_builder


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aqa.config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_build_table_is_loaded_raw(tmp_path: Path):
    config = load_config(_write(tmp_path, '[build]\nproject = "fixtures/App"\n\n[build.ios]\nscheme = "App"\n'))
    assert config.build.settings == {"project": "fixtures/App", "ios": {"scheme": "App"}}


def test_unknown_build_key_is_a_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="build.bogus"):
        load_config(_write(tmp_path, "[build]\nbogus = 1\n"))


def test_verify_uses_the_sha_builder_only_when_build_is_configured(tmp_path: Path):
    plain = load_config(_write(tmp_path, ""))
    assert _default_builder(plain) is build_mod.build_app
    configured = load_config(_write(tmp_path, '[build]\nproject = "fixtures/App"\n'))
    assert _default_builder(configured) is not build_mod.build_app


def test_aqa_build_hands_its_arguments_to_the_build_cli(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr("swarmqa.build.cli.main", fake_main)
    assert cli.main(["build", "--sha", "abc123", "--platform", "ios"]) == 0
    assert seen["argv"] == ["--sha", "abc123", "--platform", "ios"]
