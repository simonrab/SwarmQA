"""Phase 1 contracts: Driver v2, runner wire protocol, ModelProvider,
DevicePool, Findings v2, and MCP tool signatures."""

from __future__ import annotations

import json
import threading
import typing
from importlib import resources
from pathlib import Path

import pytest

from swarmqa.devices.fake import FakeDevicePool
from swarmqa.devices.protocol import DevicePool, DeviceUnavailable, lease
from swarmqa.driver import runner_schema as rs
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import AppDriverV2, UnsupportedAction, supports_v2
from swarmqa.driver.v1_adapter import V1Adapter, as_v2, element_at
from swarmqa.errors import AppCrashedError
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import (
    HistoryStep,
    JudgedIssue,
    ModelError,
    ModelProvider,
    Rubric,
    ScreenJudgment,
    StepDecision,
)
from swarmqa.mcp import tools as mcp_tools
from swarmqa.models import (
    AppTarget,
    BuildArtifact,
    ElementQuery,
    Evidence,
    Finding,
    UIElement,
    category_for_kind,
)
from swarmqa.report.dedup import dedup_findings
from swarmqa.serialize import to_plain

ROOT = Path(__file__).resolve().parents[1]


def _tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Main",
            frame=(0, 0, 390, 844),
            children=[
                UIElement(role="button", label="Settings", identifier="home.settings", frame=(10, 100, 100, 44)),
                UIElement(role="button", label="Boom", frame=(10, 200, 100, 44)),
                UIElement(role="text", label="Welcome", frame=(10, 20, 200, 30)),
            ],
        )
    ]


def _driver(tmp_path: Path) -> FakeDriver:
    driver = FakeDriver(AppTarget(path="/Fake.app", bundle_id="com.example.fake"), tmp_path, require_path=False)
    driver.set_tree(_tree())
    driver.launch()
    return driver


def _finding(**overrides) -> Finding:
    values = dict(
        id="f1",
        title="Save does nothing",
        severity="high",
        kind="unresponsive",
        steps=["tap Save"],
        fingerprint="fp1",
        worker_id="w1",
        backend="local",
    )
    values.update(overrides)
    return Finding(**values)


# Findings v2


def test_finding_v2_defaults_keep_v1_constructors_working():
    finding = _finding()
    assert finding.category == "broken"
    assert finding.confidence == 1.0
    assert finding.advisory is False
    assert finding.repro is None
    assert finding.suspected_sources == []
    assert finding.evidence == Evidence()


@pytest.mark.parametrize(
    ("kind", "category"),
    [("crash", "crash"), ("launch", "crash"), ("visual", "visual"), ("visual_judgment", "visual"),
     ("friction_path", "confusing"), ("assertion", "broken"), ("timeout", "broken")],
)
def test_category_defaults_from_kind(kind, category):
    assert category_for_kind(kind) == category
    assert _finding(kind=kind).category == category


def test_finding_round_trips_through_json():
    finding = _finding(
        advisory=True,
        confidence=0.6,
        repro="findings/f1.replay.json",
        suspected_sources=["App/SettingsView.swift:42"],
        evidence=Evidence(video_clip="media/f1.mp4", frames=["media/a.png"]),
    )
    plain = json.loads(json.dumps(to_plain(finding)))
    assert Finding.from_dict(plain) == finding


def test_v1_finding_json_still_loads():
    plain = to_plain(_finding(kind="crash"))
    for key in ("category", "confidence", "advisory", "repro", "suspected_sources", "evidence"):
        plain.pop(key)
    finding = Finding.from_dict(plain)
    assert finding.category == "crash"
    assert finding.evidence.frames == []


def test_schema_matches_finding_fields():
    schema = json.loads(resources.files("swarmqa").joinpath("schemas/findings.v2.json").read_text())
    finding_schema = schema["$defs"]["finding"]
    keys = set(to_plain(_finding()))
    assert keys == set(finding_schema["properties"])
    assert set(finding_schema["required"]) <= keys
    kinds = set(typing.get_args(typing.get_type_hints(Finding)["kind"]))
    assert kinds == set(finding_schema["properties"]["kind"]["enum"])
    assert schema["properties"]["schema_version"]["const"] == 2


def test_dedup_merges_v2_evidence_and_firms_up_advisory():
    first = _finding(advisory=True, confidence=0.5, evidence=Evidence(frames=["a.png"]))
    second = _finding(
        id="f2",
        worker_id="w2",
        advisory=False,
        confidence=0.9,
        repro="r.json",
        suspected_sources=["A.swift"],
        evidence=Evidence(video_clip="c.mp4", frames=["b.png"]),
    )
    [merged] = dedup_findings([first, second])
    assert merged.evidence == Evidence(video_clip="c.mp4", frames=["a.png", "b.png"])
    assert merged.advisory is False
    assert merged.confidence == 0.9
    assert merged.repro == "r.json"
    assert merged.suspected_sources == ["A.swift"]
    assert first.evidence.frames == ["a.png"], "dedup must not mutate its input"


# Driver v2


def test_fake_driver_speaks_v2(tmp_path: Path):
    driver = _driver(tmp_path)
    assert supports_v2(driver)
    assert isinstance(driver, AppDriverV2)
    obs = driver.observe()
    assert obs.tree is driver.tree
    assert obs.screenshot is not None and obs.screenshot.is_file()
    assert obs.size == driver.screen_size
    assert driver.observe(screenshot=False).screenshot is None


def test_fake_tap_point_hits_the_smallest_element_and_transitions(tmp_path: Path):
    driver = _driver(tmp_path)
    settings = [UIElement(role="button", label="Back", frame=(0, 0, 44, 44))]
    driver.transitions["Settings"] = settings
    driver.tap_point(50, 120)
    assert driver.tree == settings
    driver.crash_labels.add("Back")
    with pytest.raises(AppCrashedError):
        driver.tap_point(20, 20)


def test_fake_logs_and_crashes_filter_by_time(tmp_path: Path):
    driver = _driver(tmp_path)
    driver.add_log("old", ts=100.0)
    driver.add_log("boom", level="error", ts=200.0)
    driver.add_crash("EXC_BAD_ACCESS", ts=150.0)
    assert [entry.message for entry in driver.logs_since(150.0)] == ["boom"]
    assert [report.summary for report in driver.crash_reports_since(150.0)] == ["EXC_BAD_ACCESS"]
    assert driver.crash_reports_since(151.0) == []


class _V1Only:
    """Exposes only the v1 surface of a FakeDriver."""

    def __init__(self, inner: FakeDriver):
        self._inner = inner

    def __getattr__(self, name):
        if name in ("observe", "tap_point", "swipe", "logs_since", "crash_reports_since"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_v1_adapter_serves_v2_from_a_v1_driver(tmp_path: Path):
    inner = _driver(tmp_path)
    v1 = _V1Only(inner)
    assert not supports_v2(v1)
    adapted = as_v2(v1)
    assert isinstance(adapted, V1Adapter)
    assert as_v2(inner) is inner
    obs = adapted.observe()
    assert obs.screenshot is not None and obs.tree == inner.tree
    adapted.tap_point(50, 120)
    assert inner.actions_log[-1] == "click:Settings"
    adapted.swipe((200, 600), (200, 200))
    assert inner.actions_log[-1] == "scroll:3"
    with pytest.raises(UnsupportedAction):
        adapted.swipe((0, 10), (100, 10))
    with pytest.raises(UnsupportedAction):
        adapted.tap_point(1000, 1000)
    assert adapted.logs_since(0) == [] and adapted.crash_reports_since(0) == []
    assert adapted.metadata().bundle_id == "com.example.fake"


def test_v1_adapter_refuses_taps_a_query_cannot_pin_down(tmp_path: Path):
    inner = _driver(tmp_path)
    inner.set_tree([
        UIElement(role="window", frame=(0, 0, 390, 844), children=[
            UIElement(role="button", label="Save As…", frame=(10, 10, 100, 44)),
            UIElement(role="button", label="Save", frame=(10, 100, 100, 44)),
            UIElement(role="button", frame=(10, 200, 44, 44)),
            UIElement(role="button", frame=(10, 300, 44, 44)),
        ])
    ])
    adapted = V1Adapter(_V1Only(inner))
    with pytest.raises(UnsupportedAction):
        adapted.tap_point(20, 120)
    with pytest.raises(UnsupportedAction):
        adapted.tap_point(20, 310)
    adapted.tap_point(20, 20)
    assert inner.actions_log[-1] == "click:Save As…"


def test_element_at_skips_disabled_and_frameless():
    tree = [
        UIElement(role="window", frame=(0, 0, 100, 100), children=[
            UIElement(role="button", label="Off", enabled=False, frame=(0, 0, 10, 10)),
            UIElement(role="button", label="NoFrame"),
        ])
    ]
    assert element_at(tree, 5, 5).role == "window"
    assert element_at(tree, 500, 5) is None


# Runner wire protocol


def test_element_and_observe_round_trip():
    png = b"\x89PNG fake bytes"
    response = rs.ObserveResponse(
        ts=1700000000.5,
        elements=_tree(),
        size=(390.0, 844.0),
        scale=3.0,
        screenshot=rs.Screenshot(format="png", data=png),
    )
    wire = json.loads(json.dumps(rs.encode_observe_response(response)))
    decoded = rs.decode_observe_response(wire)
    assert decoded == response


def test_decoders_reject_bad_shapes():
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_element({"label": "no role"})
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_element({"role": "button", "frame": [1, 2, 3]})
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_observe_response({"ts": "soon", "elements": []})
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_observe_response({"ts": 1, "elements": [], "screenshot": {"format": "png", "data": "***"}})
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_health({"protocol_version": 1, "platform": "android"})
    with pytest.raises(rs.RunnerProtocolError):
        rs.decode_query({})


def test_request_encoders():
    assert rs.encode_tap(rs.TapRequest(point=(1, 2))) == {"x": 1.0, "y": 2.0}
    assert rs.encode_tap(rs.TapRequest(query=ElementQuery(label="Save")))["query"]["label"] == "Save"
    with pytest.raises(rs.RunnerProtocolError):
        rs.encode_tap(rs.TapRequest())
    with pytest.raises(rs.RunnerProtocolError):
        rs.encode_tap(rs.TapRequest(point=(1, 2), query=ElementQuery(label="Save")))
    assert rs.encode_swipe(rs.SwipeRequest(start=(0, 10), end=(0, 5))) == {
        "from": [0.0, 10.0], "to": [0.0, 5.0], "duration_s": 0.3,
    }
    assert rs.encode_type(rs.TypeRequest(text="hi"))["query"] is None
    with pytest.raises(rs.RunnerProtocolError):
        rs.encode_key(rs.KeyRequest(keys=[]))
    assert rs.encode_launch(rs.LaunchRequest(bundle_id="com.x"))["terminate_existing"] is True


def test_errors_round_trip_and_unknown_codes_become_internal():
    error = rs.RunnerError(code="not_found", message="no Save")
    assert rs.decode_error(rs.encode_error(error)).code == "not_found"
    assert rs.decode_error({"error": {"code": "weird"}}).code == "internal"
    assert rs.decode_error("<html>").code == "internal"
    assert set(rs.ERROR_STATUS) == set(typing.get_args(rs.ErrorCode))


def test_protocol_doc_lists_every_endpoint_and_error_code():
    doc = (ROOT / "agents" / "swarm-runner" / "PROTOCOL.md").read_text()
    for endpoint in rs.ENDPOINTS.values():
        assert f"`{endpoint}`" in doc, endpoint
    for code in rs.ERROR_STATUS:
        assert f"`{code}`" in doc, code
    assert f"version {rs.PROTOCOL_VERSION}" in doc


# ModelProvider


def test_fake_model_provider_walks_untried_controls_then_finishes(tmp_path: Path):
    provider = FakeModelProvider()
    assert isinstance(provider, ModelProvider)
    obs = _driver(tmp_path).observe()
    history: list[HistoryStep] = []
    labels = []
    while True:
        decision = provider.decide_step(obs, "explore", history)
        assert decision.usage is not None and decision.usage.provider == "fake"
        if decision.kind == "done":
            break
        labels.append(decision.target.label)
        history.append(HistoryStep(action=decision))
    assert labels == ["Settings", "Boom"]


def test_fake_model_provider_scripts_and_errors(tmp_path: Path):
    obs = _driver(tmp_path).observe()
    issue = JudgedIssue(category="visual", title="Clipped title", confidence=0.8)
    provider = FakeModelProvider(
        judgments=[ScreenJudgment(issues=[issue])],
        steps=[StepDecision(kind="give_up")],
    )
    assert provider.judge_screen(obs, Rubric(name="visual")).issues == [issue]
    assert provider.judge_screen(obs, Rubric(name="visual")).issues == []
    assert provider.decide_step(obs, "g", []).kind == "give_up"
    assert provider.computer_use(obs, "tap anything").kind == "tap_point"
    assert provider.propose_flows("diff", []).flows == []
    with pytest.raises(ModelError):
        provider.judge_screen(_driver(tmp_path / "b").observe(screenshot=False), Rubric(name="visual"))
    provider.error = ModelError("down")
    with pytest.raises(ModelError):
        provider.decide_step(obs, "g", [])


# DevicePool


def test_fake_pool_leases_up_to_capacity():
    pool = FakeDevicePool({"ios": 1})
    assert isinstance(pool, DevicePool)
    build = BuildArtifact(platform="ios", app_path="/A.app", bundle_id="com.a")
    first = pool.acquire("ios", build)
    assert first.build is build
    with pytest.raises(DeviceUnavailable):
        pool.acquire("ios", build, timeout_s=0.01)
    with pytest.raises(DeviceUnavailable):
        pool.acquire("macos", None, timeout_s=0.01)
    pool.release(first)
    pool.release(first)
    assert pool.released == [(first.id, True)]
    assert pool.acquire("ios", build).id != first.id


def test_lease_releases_on_error_and_wakes_waiters():
    pool = FakeDevicePool({"ios": 1})
    got = []
    with pytest.raises(RuntimeError):
        with lease(pool, "ios", None, erase=False):
            waiter = threading.Thread(target=lambda: got.append(pool.acquire("ios", None, timeout_s=5)))
            waiter.start()
            raise RuntimeError("worker crashed")
    waiter.join(5)
    assert got and pool.released[0][1] is False


# MCP tools


def test_mcp_tool_stubs_have_resolvable_json_friendly_signatures():
    names = [tool.__name__ for tool in mcp_tools.TOOLS]
    assert names == [
        "start_campaign", "campaign_status", "list_findings", "get_finding",
        "verify_fix", "verify_status", "cancel_campaign",
    ]
    for tool in mcp_tools.TOOLS:
        hints = typing.get_type_hints(tool)
        assert "return" in hints
        assert tool.__doc__
        required = tool.__code__.co_argcount - len(tool.__defaults__ or ())
        with pytest.raises(NotImplementedError):
            tool(*["x"] * required)
