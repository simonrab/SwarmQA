from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from swarmqa.budgets import CampaignClock
from swarmqa.cli import main
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.query import find_element
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import (
    CampaignConfig,
    ElementQuery,
    Finding,
    UIElement,
)
from swarmqa.report.dedup import dedup_findings
from swarmqa.report.layout import LAYOUT_DIRS, ensure_campaign_layout
from swarmqa.report.summary import empty_result, write_summary
from swarmqa.spend import SpendMeter
from swarmqa.testing import make_app, sample_tree
from swarmqa.util import parse_duration
from swarmqa.video_policy import keep_video, should_start_video


def test_locked_defaults():
    config = CampaignConfig()
    assert config.workers == 2
    assert config.backend == "local"
    assert config.pr.mode == "off"
    assert config.video.mode == "always"
    assert config.spend.currency == "USD"
    assert config.cloud.cost_per_worker_minute > 0


def test_parse_duration():
    assert parse_duration("90s") == 90
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1h30m") == 5400
    assert parse_duration("1h2m3s") == 3723
    with pytest.raises(ValueError):
        parse_duration("30")
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_spend_meter_blocks_at_cap():
    meter = SpendMeter(10, "USD")
    assert meter.can_start(6).allowed
    meter.add(6)
    assert meter.can_start(4).allowed
    meter.add(4)
    decision = meter.can_start(0.01)
    assert not decision.allowed
    assert decision.stop_reason == "spend_cap"
    assert meter.remaining == 0


def test_spend_meter_unmetered_when_cap_missing():
    meter = SpendMeter(None, "USD")
    assert meter.can_start(1000).allowed
    assert "not enforced" in (meter.can_start(1).note or "")


def test_spend_meter_rejects_non_positive_cap():
    with pytest.raises(ValueError):
        SpendMeter(0)


def test_budget_clock_stops_scheduling():
    clock = CampaignClock(max_wall_time_s=10, max_worker_minutes=5)
    assert clock.can_schedule(9).allowed
    assert not clock.can_schedule(10).allowed
    clock.stop_reason = None
    clock.add_worker_minutes(5)
    assert not clock.can_schedule(1).allowed
    assert clock.stop_reason == "budget"


def test_video_policy():
    assert should_start_video("always", "scripted")
    assert should_start_video("on_failure", "scripted")
    assert not should_start_video("exploratory_only", "scripted")
    assert should_start_video("exploratory_only", "exploratory")
    assert keep_video("always", "scripted", "passed")
    assert not keep_video("on_failure", "scripted", "passed")
    assert keep_video("on_failure", "scripted", "failed")
    assert keep_video("exploratory_only", "exploratory", "passed")


def test_layout_and_empty_summary(tmp_path: Path):
    root = tmp_path / "reports" / "camp"
    paths = ensure_campaign_layout(root)
    for name in LAYOUT_DIRS:
        assert paths[name].is_dir()
    result = empty_result("camp", root, backend="local")
    summary = write_summary(result, root)
    text = summary.read_text(encoding="utf-8")
    assert "# Campaign camp" in text
    assert "## Coverage" in text
    assert "## Spend" in text
    assert "## Scripted matrix" in text
    payload = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert payload["campaign_id"] == "camp"
    assert payload["exit_code"] == 0


def test_dedup_unions_evidence():
    first = Finding(
        id="f1",
        title="Save did nothing",
        severity="high",
        kind="assertion",
        steps=["click Save"],
        fingerprint="abc123",
        worker_id="w1",
        backend="local",
        screenshots=["media/w1.png"],
        video="media/w1.mp4",
    )
    second = Finding(
        id="f2",
        title="Save did nothing",
        severity="high",
        kind="assertion",
        steps=["click Save"],
        fingerprint="abc123",
        worker_id="w2",
        backend="vm",
        screenshots=["media/w2.png"],
        video="media/w2.mp4",
    )
    merged = dedup_findings([first, second])
    assert len(merged) == 1
    assert merged[0].worker_ids == ["w1", "w2"]
    assert merged[0].screenshots == ["media/w1.png", "media/w2.png"]
    assert "media/w2.mp4" in merged[0].environment["extra_videos"]


def test_fake_driver_click_type_and_screenshot(tmp_path: Path):
    app = make_app(tmp_path)
    driver = FakeDriver(app, tmp_path / "work")
    driver.set_tree(sample_tree())
    driver.launch()
    driver.click(ElementQuery(role="button", label="Save"))
    driver.type_text(ElementQuery(role="textfield", label="email"), "a@b.c")
    shot = driver.screenshot("after")
    assert shot.exists()
    assert shot.read_bytes().startswith(b"\x89PNG")
    meta = driver.metadata()
    assert meta.bundle_id == "dev.swarmqa.sample"
    assert meta.version
    driver.relaunch()
    assert driver.launched


def test_fake_driver_missing_app_and_controls(tmp_path: Path):
    driver = FakeDriver(
        make_app(tmp_path),
        tmp_path / "work",
    )
    driver.target.path = str(tmp_path / "Missing.app")
    with pytest.raises(AppMissingError):
        driver.launch()

    app = make_app(tmp_path, "Other.app")
    driver = FakeDriver(app, tmp_path / "work2")
    driver.set_tree(sample_tree())
    driver.launch()
    driver.crash_labels.add("Save")
    with pytest.raises(AppCrashedError):
        driver.click(ElementQuery(role="button", label="Save"))
    driver.timeout_labels.add("Email")
    with pytest.raises(UITimeoutError):
        driver.wait_for(ElementQuery(role="textfield", label="Email"), 1)
    with pytest.raises(ElementNotFoundError):
        find_element(driver.accessibility_tree(), ElementQuery(role="button", label="Missing"))


def test_element_match_is_case_insensitive_substring():
    elements = [UIElement(role="Button", label="Save Draft", identifier="save")]
    found = find_element(elements, ElementQuery(role="button", label="save"))
    assert found.identifier == "save"
    with pytest.raises(ElementNotFoundError):
        find_element(elements, ElementQuery())


def test_cli_init_and_help(tmp_path: Path):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert main(["init", "--dir", str(tmp_path)]) == 0
    text = (tmp_path / "aqa.config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["intents"] == ["intents/"]
    assert parsed["campaign"]["workers"] == 2
    assert 'backend = "local"' in text
    assert "workers = 2" in text
    assert 'mode = "off"' in text
    assert 'mode = "always"' in text
    assert (tmp_path / "templates" / "issue.md").exists()
    assert (tmp_path / "intents").is_dir()
    assert (tmp_path / "reports").is_dir()
    code = main(["run", "--config", str(tmp_path / "missing.toml")])
    assert code == 2


def test_cli_init_does_not_clobber(tmp_path: Path):
    config = tmp_path / "aqa.config.toml"
    config.write_text("custom = true\n", encoding="utf-8")
    assert main(["init", "--dir", str(tmp_path)]) == 0
    assert config.read_text(encoding="utf-8") == "custom = true\n"
