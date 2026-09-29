"""Shared fixtures for the whole suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_host_locks(tmp_path_factory, monkeypatch):
    """Keep host-wide device locks out of the real ~/.aqa/locks."""
    monkeypatch.setenv("AQA_LOCK_DIR", str(tmp_path_factory.mktemp("locks")))
