"""Shared fixtures for the whole suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_host_locks(tmp_path_factory, monkeypatch):
    """Keep host-wide device locks out of the real ~/.aqa/locks."""
    monkeypatch.setenv("AQA_LOCK_DIR", str(tmp_path_factory.mktemp("locks")))
    # Keep verify_fix's run index out of the real ~/.aqa/verify.
    monkeypatch.setenv("AQA_VERIFY_INDEX", str(tmp_path_factory.mktemp("verify-index")))


@pytest.fixture(autouse=True)
def _no_implicit_simulator_pool(request, monkeypatch):
    """Never let a test lease real simulators through the default iOS pool.

    A config with more iOS workers than listed simulators gets `[swarm] pool =
    "auto"`, which clones and boots real devices. Tests that check that
    default ask for it with `@pytest.mark.real_pool_default`.
    """
    if request.node.get_closest_marker("real_pool_default") is None:
        monkeypatch.setattr("swarmqa.orchestrator.swarm._wants_ios_pool", lambda config: False)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_pool_default: use the real default-pool rule")


@pytest.fixture(autouse=True)
def _children_import_this_checkout(monkeypatch):
    """Child `python -m swarmqa...` processes import this checkout, even if the install's `.pth` is hidden."""
    from swarmqa.util import child_env

    monkeypatch.setenv("PYTHONPATH", child_env()["PYTHONPATH"])
