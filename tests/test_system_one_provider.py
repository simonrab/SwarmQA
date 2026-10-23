"""System One provider: wire format, gating, escalation, triage, and the agent loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.screen_graph import fingerprint
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import HistoryStep, ModelError, ModelRefused, ModelTimeout, StepDecision
from swarmqa.llm.serialize import NO_EFFECT
from swarmqa.llm.settings import LLMSettings, create_provider, step_timeout_default
from swarmqa.llm.system_one import (
    HTTPTransport,
    SystemOneProvider,
    SystemOneSettings,
    apply_triage,
    create_system_one_provider,
    normalized_confidence,
)
from swarmqa.llm.transport import ReplayTransport, TransientError, TransportTimeout
from swarmqa.models import ElementQuery, Finding, Shard, UIElement
from swarmqa.testing import make_app, sample_config


def home() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(role="text", label="Welcome"),
                UIElement(role="button", label="Settings", identifier="settings"),
                UIElement(role="button", label="Profile", identifier="profile"),
                UIElement(role="textfield", label="Search", identifier="search"),
                UIElement(
                    role="cell",
                    frame=(0, 300, 390, 44),
                    children=[UIElement(role="text", label="Finish report")],
                ),
            ],
        )
    ]


def about() -> list[UIElement]:
    return [UIElement(role="window", label="Demo", children=[UIElement(role="text", label="Version 1.0")])]


def obs(tree=None) -> ScreenObservation:
    return ScreenObservation(tree=tree if tree is not None else home(), ts=0.0)


def choice(answer: str, confidence: float = 0.9, question: str = "next_step", **extra) -> dict:
    body = {"choice": answer, "probabilities": {answer: confidence}, "confidence": confidence}
    return {"model": "jev-1.13.0", "answers": {question: body, **extra}, "usage": {"input_tokens": 1000, "output_tokens": 0}}


def provider(*responses, escalate=None, **settings) -> tuple[SystemOneProvider, ReplayTransport]:
    transport = ReplayTransport(list(responses))
    client = SystemOneProvider(SystemOneSettings(**settings), transport, escalate=escalate, max_retries=1)
    client.client._sleep = lambda _s: None
    return client, transport


# Wire format --------------------------------------------------------------


def test_request_is_a_jev_choice_over_the_screen_controls():
    p, transport = provider(choice("o1"))
    p.decide_step(obs(), "open settings", [])
    request = transport.requests[0]
    assert request["model"] == "jev-latest"
    assert request["state"].startswith("Goal: open settings\nScreen shows: Welcome; Finish report")
    question = request["questions"]["next_step"]
    assert question["type"] == "choice"
    assert question["criteria"] == {
        "o1": 'Tap button "Settings" (id settings)',
        "o2": 'Tap button "Profile" (id profile)',
        "o3": 'Type into textfield "Search" (id search)',
        "o4": 'Tap cell "Finish report"',
        "back": "Go back to the previous screen.",
        "done": "The goal is already met on this screen.",
    }


def test_state_formats():
    history = [HistoryStep(action=StepDecision(kind="tap", target=ElementQuery(role="button", label="Profile")), outcome=NO_EFFECT)]
    p, transport = provider(choice("o1"), choice("o1"), state_format="tree")
    p.decide_step(obs(), "g", history)
    assert 'button "Settings" id=settings' in transport.requests[0]["state"]
    p.settings.state_format = "compact"
    p.decide_step(obs(), "g", history)
    assert transport.requests[1]["state"].endswith('Last steps: tap button "Profile" (no effect)')
    with pytest.raises(ValueError):
        SystemOneSettings(state_format="xml")


def test_each_option_maps_to_the_action_the_loop_can_do():
    expected = {
        "o1": StepDecision(kind="tap", target=ElementQuery(role="button", label="Settings", identifier="settings")),
        "o3": StepDecision(
            kind="type", target=ElementQuery(role="textfield", label="Search", identifier="search"), text="test"
        ),
        "o4": StepDecision(kind="tap_point", point=(195.0, 322.0)),
        "back": StepDecision(kind="back"),
    }
    for option, want in expected.items():
        p, _ = provider(choice(option, 0.8))
        got = p.decide_step(obs(), "goal", [])
        assert (got.kind, got.target, got.point, got.text) == (want.kind, want.target, want.point, want.text)
        assert got.confidence == 0.8
        assert got.rationale.startswith(f"system_one {option} confidence=0.80")
        assert got.usage is not None and got.usage.provider == "system_one" and got.usage.model == "jev-1.13.0"


def test_url_and_bearer_header(monkeypatch):
    settings = SystemOneSettings(base_url="https://api.typesafe.ai/")
    assert settings.endpoint() == "https://api.typesafe.ai/v1/systemone"
    assert SystemOneSettings(base_url="http://x:1/v1/systemone").endpoint() == "http://x:1/v1/systemone"
    built = create_system_one_provider({"api_key_env": "K"}, environ={"K": "secret"})
    assert isinstance(built.client.transport, HTTPTransport)
    assert built.client.transport.api_key == "secret"
    assert create_system_one_provider({}, environ={}).client.transport.api_key is None
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(choice("o1")).encode()

    def fake_urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert HTTPTransport("http://h/v1/systemone", api_key="k").send({"a": 1}, timeout_s=1)["answers"]
    assert seen == {"auth": "Bearer k", "url": "http://h/v1/systemone", "body": {"a": 1}}


def test_probabilities_as_a_list_and_confidence_from_entropy():
    response = {
        "answers": {
            "next_step": {
                "probabilities": [{"value": "o2", "probability": 0.97}, {"value": "o1", "probability": 0.03}]
            }
        }
    }
    p, _ = provider(response, min_confidence=0.0)
    decision = p.decide_step(obs(), "profile", [])
    assert decision.target.label == "Profile"
    assert decision.confidence == pytest.approx(normalized_confidence({"o2": 0.97, "o1": 0.03}, 6))
    assert normalized_confidence({"a": 0.5, "b": 0.5}, 2) == pytest.approx(0.0)
    assert normalized_confidence({"a": 1.0}, 4) == pytest.approx(1.0)


def test_cost_uses_the_typesafe_price_only_for_typesafe():
    p, _ = provider(choice("o1"), base_url="https://api.typesafe.ai")
    assert p.decide_step(obs(), "g", []).usage.cost == pytest.approx(1000 * 0.042 / 1e6)
    p, _ = provider(choice("o1"))
    assert p.decide_step(obs(), "g", []).usage.cost == 0.0


# Gating and failures ------------------------------------------------------


def test_low_confidence_without_escalation_refuses_so_the_loop_uses_rules():
    p, _ = provider(choice("o1", 0.3))
    with pytest.raises(ModelRefused, match="below 0.5"):
        p.decide_step(obs(), "g", [])
    assert p.stats.refused == 1


def test_done_needs_a_higher_bar():
    p, _ = provider(choice("done", 0.7), choice("done", 0.85), trust_done=True)
    with pytest.raises(ModelRefused, match="below 0.8"):
        p.decide_step(obs(), "g", [])
    assert p.decide_step(obs(), "g", []).kind == "done"


def test_an_untrusted_done_is_confirmed_by_escalation_or_refused():
    p, _ = provider(choice("done", 0.99))
    with pytest.raises(ModelRefused, match="needs confirming"):
        p.decide_step(obs(), "g", [])
    p, _ = provider(choice("done", 0.99))
    p.escalate = FakeModelProvider(steps=[StepDecision(kind="back")])
    decision = p.decide_step(obs(), "g", [])
    assert decision.kind == "back" and "needs confirming" in decision.rationale
    assert p.stats.escalated == 1


def test_low_confidence_escalates_with_the_escalation_providers_timeout():
    seen = []

    class Spy(FakeModelProvider):
        def decide_step(self, obs, goal, history, *, timeout_s=30.0):
            seen.append(timeout_s)
            return super().decide_step(obs, goal, history, timeout_s=timeout_s)

    fallback = Spy(steps=[StepDecision(kind="tap", target=ElementQuery(role="button", label="Profile"))])
    p, _ = provider(choice("o1", 0.2), escalate=fallback)
    p.escalate_timeout_s = 90.0
    decision = p.decide_step(obs(), "g", [], timeout_s=5.0)
    assert decision.target.label == "Profile"
    assert "escalated to fake" in decision.rationale
    assert seen == [90.0]
    assert p.stats.as_dict()["escalated"] == 1


@pytest.mark.parametrize(
    "response, cause",
    [
        (ModelError("HTTP 400: bad question"), ModelError),
        (TransportTimeout("slow"), ModelTimeout),
        (TransientError("HTTP 503"), ModelError),
        ({"model": "x"}, ModelRefused),
        (choice("o99"), ModelRefused),
        ({"answers": {"other": {}}}, ModelRefused),
    ],
)
def test_failures_refuse_so_the_loop_fails_open(response, cause):
    p, _ = provider(response, response)
    with pytest.raises(ModelRefused, match="system_one failed") as caught:
        p.decide_step(obs(), "g", [])
    assert isinstance(caught.value.__cause__, cause)
    assert p.stats.errors == 1


def test_transient_errors_are_retried():
    p, transport = provider(TransientError("HTTP 503"), choice("o2"))
    assert p.decide_step(obs(), "g", []).target.label == "Profile"
    assert len(transport.requests) == 2


def test_controls_with_no_effect_here_are_not_offered_and_choices_are_capped():
    tree = home()
    dead = HistoryStep(
        action=StepDecision(kind="tap", target=ElementQuery(role="button", label="Settings", identifier="settings")),
        screen=fingerprint(tree),
        outcome=NO_EFFECT,
    )
    p, _ = provider()
    options = p.step_options(obs(tree), [dead])
    assert [text for text, _ in options.values()][0] == 'Tap button "Profile" (id profile)'
    p, _ = provider(max_choices=4)
    assert list(p.step_options(obs(tree), [])) == ["o1", "o2", "back", "done"]


def test_settings_are_validated():
    with pytest.raises(ValueError, match="unknown llm.system_one"):
        SystemOneSettings.from_mapping({"bogus": 1})
    with pytest.raises(ValueError):
        SystemOneSettings(min_confidence=2)
    with pytest.raises(ValueError):
        SystemOneSettings(escalate_to="system_one")
    with pytest.raises(ValueError, match="escalate_to"):
        LLMSettings(provider="system_one", system_one={"escalate_to": "nope"})


def test_create_provider_builds_it_with_an_escalation_provider():
    built = create_provider(
        LLMSettings(provider="system_one", system_one={"escalate_to": "fake", "base_url": "http://h:9"}),
        transport=ReplayTransport([]),
    )
    assert built.name == "system_one"
    assert built.escalate.name == "fake"
    assert built.escalate_timeout_s == step_timeout_default("fake")
    assert step_timeout_default("system_one") == 5.0
    alone = create_provider(LLMSettings(provider="system_one"))
    assert alone.escalate is None
    assert alone.client.transport.url == "http://127.0.0.1:8000/v1/systemone"


def test_judging_without_escalation_fails_open():
    p, _ = provider()
    with pytest.raises(ModelError):
        p.judge_screen(obs(), None)  # type: ignore[arg-type]


# Triage -------------------------------------------------------------------


def finding(title: str) -> Finding:
    return Finding(
        id="f-1", title=title, severity="medium", kind="error_state", steps=["tap Sync"],
        fingerprint="x", worker_id="w1", backend="local",
    )


def test_triage_marks_confident_artifacts_advisory_and_keeps_real_bugs():
    artifact = choice("environment_artifact", 0.8, question="verdict", user_visible={"noul": 0.1})
    real = choice("real_bug", 0.9, question="verdict", user_visible={"noul": 0.95})
    unsure = choice("harness_artifact", 0.2, question="verdict")
    p, transport = provider(artifact, real, unsure, ModelError("down"))
    findings = [finding("Sync failed: offline"), finding("Share does nothing"), finding("x"), finding("y")]
    done = apply_triage(p, findings, min_confidence=0.5)
    assert [t.verdict for _, t in done] == ["environment_artifact", "real_bug", "harness_artifact"]
    assert [f.advisory for f in findings] == [True, False, False, False]
    assert findings[0].confidence == pytest.approx(0.2)
    assert findings[0].environment["system_one_verdict"] == "environment_artifact"
    assert findings[1].environment["system_one_user_visible"] == "0.95"
    assert "marked advisory" in findings[0].details
    assert "system_one_verdict" not in findings[3].environment
    question = transport.requests[0]["questions"]
    assert set(question["verdict"]["criteria"]) == {
        "real_bug", "environment_artifact", "harness_artifact", "expected_behavior"
    }
    assert question["user_visible"]["type"] == "noul"
    assert "Title: Sync failed: offline" in transport.requests[0]["state"]


# Agent loop ---------------------------------------------------------------


class LabelTransport:
    """Answer each step by picking the option whose text names the next label in `plan`."""

    def __init__(self, plan: list[str], confidence: float = 0.9):
        self.plan = list(plan)
        self.confidence = confidence
        self.requests: list[dict] = []

    def send(self, request, *, timeout_s):
        self.requests.append(request)
        criteria = request["questions"]["next_step"]["criteria"]
        want = self.plan.pop(0) if self.plan else "done"
        pick = next(k for k, text in criteria.items() if k == want or f'"{want}"' in text)
        return choice(pick, self.confidence)


def loop_driver(tmp_path: Path) -> FakeDriver:
    driver = FakeDriver(make_app(tmp_path), tmp_path / "campaign" / "workers" / "w1")
    settings = [UIElement(role="window", label="Demo", children=[
        UIElement(role="button", label="Home", identifier="back"),
        UIElement(role="button", label="About", identifier="about"),
    ])]
    driver.set_tree(home())
    driver.transitions = {"Settings": settings, "About": about(), "Home": home()}
    return driver


def run_loop(tmp_path, p, goal="Find the version number"):
    driver = loop_driver(tmp_path)
    result = run_agent_loop(
        Shard(id="s1", kind="exploratory", name="hunt", goal=goal),
        driver,
        sample_config(make_app(tmp_path)),
        provider=p,
        settings=AgentLoopSettings(max_steps=10, heuristic_first=False),
        work_dir=tmp_path / "campaign" / "workers" / "w1",
        worker_id="w1",
    )
    return driver, result


def test_agent_loop_follows_confident_system_one_choices(tmp_path):
    transport = LabelTransport(["Settings", "About", "done"])
    p = SystemOneProvider(SystemOneSettings(trust_done=True), transport)
    driver, result = run_loop(tmp_path, p)
    assert [a for a in driver.actions_log if a.startswith("click:")] == ["click:Settings", "click:About"]
    assert result.status == "passed"
    assert result.steps[-1].message == "done"
    assert p.stats.confident == 3


def test_agent_loop_falls_back_to_rules_when_system_one_is_unsure(tmp_path):
    p = SystemOneProvider(SystemOneSettings(), LabelTransport(["Profile"] * 10, confidence=0.1))
    driver, result = run_loop(tmp_path, p, goal="open settings")
    assert "click:Settings" in driver.actions_log  # the rules' goal-word match
    assert p.stats.refused >= 1
    assert any(s.action == "model" and s.status == "failed" for s in result.steps)


def test_agent_loop_keeps_going_after_an_untrusted_done(tmp_path):
    p = SystemOneProvider(SystemOneSettings(), LabelTransport(["done"] * 10))
    driver, result = run_loop(tmp_path, p, goal="open settings")
    assert "click:Settings" in driver.actions_log  # the rules carried on
    assert p.stats.confident == 0 and p.stats.refused >= 1
