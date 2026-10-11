"""WP-F2: findings people can trust — confirmation re-runs and grouping."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmqa.config import load_config
from swarmqa.confirm import trust_findings
from swarmqa.confirm import rerun as rerun_mod
from swarmqa.confirm.settings import FindingsConfigError, FindingsSettings
from swarmqa.devices.fake import FakeDevicePool
from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import ConfigError
from swarmqa.models import Finding, WorkerResult
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.orchestrator.swarm import LeasingDriverFactory
from swarmqa.report.findings_json import load_findings_json
from swarmqa.report.paths import public
from swarmqa.reporter.findings import write_replay
from swarmqa.testing import make_app, sample_config, sample_tree, scripted_shard

SAVE = [{"action": "click", "target": {"role": "button", "label": "Save"}}]


def _finding(fid: str, title: str = "Crash on Save", kind: str = "crash", severity: str = "high", **kw) -> Finding:
    kw.setdefault("worker_id", "w1")
    return Finding(id=fid, title=title, severity=severity, kind=kind, steps=["click Save"],
                   fingerprint=kw.pop("fingerprint", f"fp-{fid}"), backend="local", **kw)


def _replayable(campaign: Path, finding: Finding) -> Finding:
    finding.replay_json = public(campaign, write_replay(finding.id, campaign, SAVE))
    return finding


class Factory:
    """Fake app whose Save crashes on the drivers listed in `crashes` (1-based call numbers)."""

    def __init__(self, crashes=(), *, broken: bool = False, clock: dict | None = None):
        self.crashes = set(crashes)
        self.broken = broken
        self.clock = clock
        self.drivers: list[FakeDriver] = []

    def __call__(self, target, work_dir):
        if self.clock is not None:
            self.clock["t"] += 200
        if self.broken:
            raise RuntimeError("no device")
        driver = FakeDriver(target, work_dir)
        driver.set_tree(sample_tree())
        if len(self.drivers) + 1 in self.crashes:
            driver.crash_labels = {"Save"}
        self.drivers.append(driver)
        return driver


@pytest.fixture
def setup(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    campaign = tmp_path / "reports" / "c1"
    campaign.mkdir(parents=True)
    return config, campaign


def _trust(findings, config, campaign, factory, results=None, **settings):
    config.findings.settings = dict(settings)
    results = results if results is not None else [WorkerResult("w1", "s1", "failed", findings=list(findings))]
    return trust_findings(findings, results, campaign, config, driver_factory=factory), results


# Confirmation


def test_confirmed_flaky_not_reproduced_and_skipped(setup):
    config, campaign = setup
    confirmed = _trust([_replayable(campaign, _finding("f-c"))], config, campaign, Factory({1}))[0][0]
    assert (confirmed.confirmation, confirmed.confirm_attempts, confirmed.confirm_hits) == ("confirmed", 1, 1)
    assert not confirmed.advisory and confirmed.confidence == 1.0
    assert "Confirmed" in confirmed.details
    assert (campaign / "confirm" / "f-c-1").is_dir()

    flaky = _trust([_replayable(campaign, _finding("f-f"))], config, campaign, Factory({1}), confirm_runs=2)[0][0]
    assert (flaky.confirmation, flaky.confirm_attempts, flaky.confirm_hits) == ("flaky", 2, 1)
    assert not flaky.advisory and flaky.confidence < 1.0

    gone = _trust([_replayable(campaign, _finding("f-n"))], config, campaign, Factory())[0][0]
    assert (gone.confirmation, gone.confirm_attempts, gone.confirm_hits) == ("not_reproduced", 1, 0)
    assert gone.advisory and gone.confidence <= 0.3
    assert "Not reproduced" in gone.details

    no_replay = _trust([_finding("f-s")], config, campaign, Factory())[0][0]
    assert no_replay.confirmation == "skipped" and no_replay.confirm_attempts == 0
    broken = _trust([_replayable(campaign, _finding("f-b"))], config, campaign, Factory(broken=True))[0][0]
    assert broken.confirmation == "skipped" and "no device" in broken.details


def test_only_eligible_findings_are_rerun(setup):
    config, campaign = setup
    advisory = _replayable(campaign, _finding("f-a", advisory=True, confidence=0.5))
    visual = _replayable(campaign, _finding("f-v", title="Small tap target: button", kind="visual"))
    factory = Factory()
    _trust([advisory, visual], config, campaign, factory)
    assert advisory.confirmation is None and visual.confirmation is None and factory.drivers == []
    off = _replayable(campaign, _finding("f-o"))
    _trust([off], config, campaign, factory, confirm=False)
    assert off.confirmation is None and factory.drivers == []


def test_most_severe_first_and_max_confirm(setup):
    config, campaign = setup
    low = _replayable(campaign, _finding("f-low", severity="low"))
    critical = _replayable(campaign, _finding("f-crit", severity="critical"))
    _trust([low, critical], config, campaign, Factory({1, 2}), max_confirm=1)
    assert critical.confirmation == "confirmed" and low.confirmation is None


def test_budget_stops_cleanly(setup, monkeypatch):
    config, campaign = setup
    clock = {"t": 0.0}
    monkeypatch.setattr(rerun_mod, "_now", lambda: clock["t"])
    findings = [_replayable(campaign, _finding(f"f-{n}")) for n in range(3)]
    factory = Factory({1, 2, 3}, clock=clock)
    _trust(findings, config, campaign, factory, confirm_budget_s=300)
    assert [item.confirmation for item in findings] == ["confirmed", "confirmed", None]
    assert findings[2].confirm_attempts == 0 and len(factory.drivers) == 2


def test_drop_not_reproduced_removes_it_everywhere(setup):
    config, campaign = setup
    gone = _replayable(campaign, _finding("f-gone"))
    kept, results = _trust([gone], config, campaign, Factory(), drop_not_reproduced=True)
    assert kept == [] and results[0].findings == []
    assert results[0].status == "passed"


def test_drivers_and_leases_are_returned_on_error(setup, monkeypatch):
    config, campaign = setup

    def boom(*args, **kwargs):
        raise RuntimeError("replay blew up")

    monkeypatch.setattr(rerun_mod, "replay_finding", boom)
    plain = Factory()
    finding = _replayable(campaign, _finding("f-e"))
    _trust([finding], config, campaign, plain)
    assert finding.confirmation == "skipped" and "replay blew up" in finding.details
    assert plain.drivers and all(driver.closed for driver in plain.drivers)

    config.driver.kind = "fake"
    pool = FakeDevicePool({"macos": 1})
    leased = _replayable(campaign, _finding("f-l"))
    _trust([leased], config, campaign, LeasingDriverFactory(pool, "macos", config, timeout_s=1))
    assert leased.confirmation == "skipped"
    assert pool._leased == {} and len(pool.released) == 1

    injected = LeasingDriverFactory(FakeDevicePool({"macos": 1}), "macos", config, base=Factory(), timeout_s=1)
    assert rerun_mod._unleased(injected) is injected.base


def test_broken_internals_return_findings_unchanged(setup, monkeypatch):
    config, campaign = setup
    titles = ['Small tap target: button "Share"', 'Small tap target: button "Edit"']
    findings = [_finding(f"f-{n}", title=title, kind="visual", severity="low") for n, title in enumerate(titles)]
    results = [WorkerResult("w1", "s1", "failed", findings=list(findings))]

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(rerun_mod, "confirm_findings", boom)
    returned, _ = _trust(findings, config, campaign, Factory(), results=results)
    assert returned is findings
    assert [item.title for item in findings] == titles
    assert all(item.occurrences == [] for item in findings)
    assert results[0].findings == findings and results[0].status == "failed"


# Grouping


def test_grouping_folds_the_same_problem_and_keeps_results_consistent(setup):
    config, campaign = setup
    share = _finding("f-1", title='Small tap target: button "Share"', kind="visual", severity="low",
                     screenshots=["a.png"])
    edit = _finding("f-2", title='Small tap target: button "Edit"', kind="visual", severity="medium",
                    worker_id="w2", screenshots=["b.png"])
    done = _finding("f-3", title='Small tap target: button "Done"', kind="visual", severity="low",
                    worker_id="w2", screenshots=["c.png"])
    other_role = _finding("f-4", title='Small tap target: switch "Wi-Fi"', kind="visual", severity="low")
    advisory = _finding("f-5", title='Small tap target: button "Help"', kind="visual", severity="low",
                        advisory=True)
    crashes = [_finding("f-6", title='Crash on "A"'), _finding("f-7", title='Crash on "B"')]
    results = [
        WorkerResult("w1", "s1", "failed", findings=[share, other_role, advisory, *crashes]),
        WorkerResult("w2", "s2", "failed", findings=[edit, done]),
    ]
    every = [share, other_role, advisory, *crashes, edit, done]
    kept, results = _trust(every, config, campaign, Factory(), results=results, confirm=False)
    assert kept == [other_role, advisory, *crashes, edit]
    assert edit.title == 'Small tap target: button "Edit" (+2 more)'
    assert edit.severity == "medium"
    assert sorted(edit.occurrences) == ['"Done"', '"Share"']
    assert edit.worker_ids == ["w2", "w1"]
    assert edit.screenshots == ["b.png", "a.png", "c.png"]
    assert "Same problem in 3 places" in edit.details
    assert results[0].findings == [other_role, advisory, *crashes]
    assert results[1].findings == [edit]
    assert all(item.confirmation is None for item in kept)


def test_grouping_rules(setup):
    config, campaign = setup
    pair = [
        _finding("f-1", title='Text "High priority" overlaps text "Finish quarterly report"', kind="visual"),
        _finding("f-2", title='Text "Low priority" overlaps text "Call the bank"', kind="visual"),
    ]
    kept, _ = _trust(list(pair), config, campaign, Factory(), confirm=False, group_min=3)
    assert kept == pair and pair[0].occurrences == []
    kept, _ = _trust(list(pair), config, campaign, Factory(), confirm=False, group=False)
    assert kept == pair
    kept, _ = _trust(list(pair), config, campaign, Factory(), confirm=False)
    assert len(kept) == 1 and kept[0].occurrences in (['"Low priority" / "Call the bank"'],
                                                     ['"High priority" / "Finish quarterly report"'])
    unquoted = [_finding(f"f-c{n}", title=f"Low text contrast: {name}", kind="visual") for n, name in
                enumerate(["Due today", "Overdue"])]
    points = [_finding(f"f-p{n}", title=f"Unlabeled button at ({x}, 40)", kind="visual") for n, x in
              enumerate([20, 360])]
    kept, _ = _trust(unquoted + points, config, campaign, Factory(), confirm=False)
    assert len(kept) == 2
    assert {tuple(item.occurrences) for item in kept} == {("Overdue",), ("at (360, 40)",)}


def test_group_first_then_confirm_the_representative(setup):
    config, campaign = setup
    taps = [_replayable(campaign, _finding(f"f-{n}", title=f'Tapping button "{label}" did nothing',
                                           kind="unresponsive", severity="medium"))
            for n, label in enumerate(["Save", "Help", "About"])]
    factory = Factory()
    kept, _ = _trust(taps, config, campaign, factory)
    assert len(kept) == 1 and len(factory.drivers) == 1
    assert kept[0].confirmation is not None and len(kept[0].occurrences) == 2


# Settings


def test_settings_defaults_and_validation(tmp_path: Path):
    settings = FindingsSettings.from_mapping(None)
    assert settings.confirm and settings.confirm_runs == 1 and settings.max_confirm == 20
    assert "crash" in settings.confirm_kinds and "visual" not in settings.confirm_kinds
    with pytest.raises(FindingsConfigError) as exc:
        FindingsSettings.from_mapping({
            "confirm": "yes", "confirm_runs": 0, "bogus": 1, "confirm_kinds": ["crash", "nope"],
            "confirm_budget_s": 0, "group_min": 1, "max_confirm": -1,
        })
    assert set(exc.value.errors) == {
        "findings.confirm: must be true or false",
        "findings.bogus: unknown key",
        "findings.confirm_runs: must be >= 1",
        "findings.max_confirm: must be >= 0",
        "findings.group_min: must be >= 2",
        "findings.confirm_budget_s: must be a number > 0",
        "findings.confirm_kinds: unknown kinds: nope",
    }
    path = tmp_path / "aqa.config.toml"
    path.write_text("[findings]\nconfirm_runs = 3\n", encoding="utf-8")
    assert FindingsSettings.from_config(load_config(path)).confirm_runs == 3
    path.write_text("[findings]\nconfirm_runs = \"many\"\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="findings.confirm_runs: must be"):
        load_config(path)


# Through the campaign


def test_campaign_finding_that_does_not_reproduce_becomes_advisory(tmp_path: Path):
    def factory(target, work_dir):
        driver = FakeDriver(target, work_dir)
        driver.set_tree(sample_tree())
        if "workers" in Path(work_dir).parts:
            driver.crash_labels = {"Save"}
        return driver

    def run(name: str, **settings):
        config = sample_config(make_app(tmp_path))
        config.report_root = str(tmp_path / name)
        config.workers = 1
        config.findings.settings = settings
        return run_campaign(config, [scripted_shard()], driver_factory=factory)

    unchecked = run("unchecked", confirm=False)
    assert unchecked.exit_code == 1

    result = run("checked")
    crash = next(f for w in result.results for f in w.findings if f.kind == "crash")
    assert crash.confirmation == "not_reproduced" and crash.advisory
    assert result.results[0].status == "passed"
    assert result.exit_code == 0
    stored = load_findings_json(Path(result.report_dir) / "findings.json")
    assert [(f.confirmation, f.advisory) for f in stored] == [("not_reproduced", True)]
    assert (Path(result.report_dir) / "confirm" / f"{crash.id}-1").is_dir()
