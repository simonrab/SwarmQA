"""Anthropic and OpenAI providers against recorded responses (no network, no SDK)."""

from __future__ import annotations

import base64
import io
import json
import warnings
from pathlib import Path

import pytest
from PIL import Image

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from swarmqa.llm.openai_provider import OpenAIProvider
from swarmqa.llm.pricing import ModelPrice
from swarmqa.llm.protocol import (
    ChangedFile,
    HistoryStep,
    ModelBudgetExceeded,
    ModelError,
    ModelProvider,
    ModelRefused,
    ModelTimeout,
    Rubric,
    StepDecision,
)
from swarmqa.llm.settings import LLMSettings
from swarmqa.llm.transport import ReplayTransport, TransientError, TransportTimeout
from swarmqa.llm.usage import UsageMeter
from swarmqa.models import ElementQuery, UIElement
from swarmqa.spend import SpendMeter

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
OPENAI_PRICES = {
    "gpt-5.5": ModelPrice(1.00, 8.00, 0.10),
    "gpt-5.4-mini": ModelPrice(0.25, 2.00, 0.025),
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            frame=(0, 0, 393, 852),
            children=[
                UIElement(
                    role="navbar",
                    label="Settings",
                    children=[UIElement(role="button", label="Back", identifier="back", frame=(8, 50, 60, 44))],
                ),
                UIElement(role="statictext", label="Account", frame=(20, 110, 200, 20)),
                UIElement(role="textfield", label="Email", identifier="email_field", frame=(20, 140, 353, 44)),
                UIElement(role="button", label="Save", identifier="save_btn", frame=(20, 700, 353, 44)),
                UIElement(role="button", label="Delete", enabled=False, frame=(20, 760, 353, 44)),
            ],
        )
    ]


def _screenshot(path: Path, size: tuple[int, int] = (1179, 2556)) -> Path:
    Image.new("RGB", size, (240, 240, 245)).save(path, format="PNG")
    return path


def _obs(tmp_path: Path, *, screenshot: bool = True) -> ScreenObservation:
    shot = _screenshot(tmp_path / "screen.png") if screenshot else None
    return ScreenObservation(tree=_tree(), ts=1.0, screenshot=shot, size=(393, 852), scale=3.0)


def _fixture(provider: str, name: str) -> dict:
    return json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))


def _provider(kind: str, responses: list, clock: FakeClock | None = None, **settings):
    clock = clock or FakeClock()
    if kind == "openai":
        settings.setdefault("prices", OPENAI_PRICES)
    config = LLMSettings(provider=kind, **settings)
    transport = ReplayTransport(list(responses))
    cls = AnthropicProvider if kind == "anthropic" else OpenAIProvider
    return cls(config, transport, sleep=clock.sleep, clock=clock), transport


PROVIDERS = ["anthropic", "openai"]


# The four methods, both providers


@pytest.mark.parametrize("kind", PROVIDERS)
def test_decide_step_maps_element_id_to_query(kind, tmp_path):
    provider, transport = _provider(kind, [_fixture(kind, "step_tap")])
    assert isinstance(provider, ModelProvider)
    history = [HistoryStep(action=StepDecision(kind="tap", target=ElementQuery(role="textfield", label="Email")))]
    decision = provider.decide_step(_obs(tmp_path, screenshot=False), "Change the email", history)
    assert decision.kind == "tap"
    assert decision.target == ElementQuery(role="button", label="Save", identifier="save_btn")
    assert decision.confidence == pytest.approx(0.86)
    assert decision.usage is not None and decision.usage.provider == kind
    assert decision.usage.cost > 0
    request = json.dumps(transport.requests[0])
    assert "e6 button" in request and "Change the email" in request and 'tap textfield' in request


@pytest.mark.parametrize("kind", PROVIDERS)
def test_judge_screen_converts_bbox_and_drops_low_confidence(kind, tmp_path):
    provider, _ = _provider(kind, [_fixture(kind, "judge_visual")])
    judgment = provider.judge_screen(_obs(tmp_path), Rubric(name="visual", min_confidence=0.5))
    assert [issue.title for issue in judgment.issues] == ["Save button label is clipped"]
    issue = judgment.issues[0]
    assert issue.category == "visual" and issue.severity == "medium"
    assert issue.element == ElementQuery(role="button", label="Save", identifier="save_btn")
    # 1179x2556 is sent as 723x1568; bbox pixels map back to points at scale 3.
    x, y, w, h = issue.bbox
    assert x == pytest.approx(30 * 1179 / 723 / 3, abs=0.2)
    assert y == pytest.approx(1101 * 2556 / 1568 / 3, abs=0.2)
    assert w == pytest.approx(663 * 1179 / 723 / 3, abs=0.3)
    assert h == pytest.approx(90 * 2556 / 1568 / 3, abs=0.3)
    assert judgment.summary.startswith("Settings screen")
    assert judgment.usage is not None and judgment.usage.cache_read_tokens > 0


@pytest.mark.parametrize("kind", PROVIDERS)
def test_judge_keeps_everything_with_zero_threshold(kind, tmp_path):
    provider, _ = _provider(kind, [_fixture(kind, "judge_visual")])
    judgment = provider.judge_screen(_obs(tmp_path), Rubric(name="visual", min_confidence=0.0))
    assert len(judgment.issues) == 2
    assert judgment.issues[1].bbox is None and judgment.issues[1].element is None


@pytest.mark.parametrize("kind", PROVIDERS)
def test_judge_needs_screenshot_and_makes_no_call(kind, tmp_path):
    provider, transport = _provider(kind, [])
    with pytest.raises(ModelError):
        provider.judge_screen(_obs(tmp_path, screenshot=False), Rubric(name="visual"))
    assert transport.requests == []


@pytest.mark.parametrize("kind", PROVIDERS)
def test_propose_flows_sorts_by_priority_and_caps(kind):
    provider, transport = _provider(kind, [_fixture(kind, "flows"), _fixture(kind, "flows")])
    files = [ChangedFile("App/Settings/EmailValidator.swift", "struct EmailValidator {}")]
    proposal = provider.propose_flows("diff --git a/x b/x", files)
    assert [flow.name for flow in proposal.flows] == ["Edit account email", "Validate email on save"]
    assert proposal.flows[0].priority == 1
    assert proposal.flows[0].touched_files == ["App/Settings/SettingsView.swift", "App/Settings/EmailValidator.swift"]
    assert "EmailValidator.swift" in json.dumps(transport.requests[0])
    assert len(provider.propose_flows("d", [], max_flows=1).flows) == 1


@pytest.mark.parametrize("kind", PROVIDERS)
def test_computer_use_maps_pixels_to_points(kind, tmp_path):
    provider, _ = _provider(kind, [_fixture(kind, "computer_use")])
    decision = provider.computer_use(_obs(tmp_path), "Tap Save")
    assert decision.kind == "tap_point"
    assert decision.point == pytest.approx((361 * 1179 / 723 / 3, 784 * 2556 / 1568 / 3), abs=0.2)
    assert decision.target is None
    assert decision.usage is not None


# Request shapes


def test_anthropic_request_shape(tmp_path):
    provider, transport = _provider(
        "anthropic", [_fixture("anthropic", "step_tap"), _fixture("anthropic", "judge_visual")]
    )
    provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    provider.judge_screen(_obs(tmp_path), Rubric(name="visual"))
    step, judge = transport.requests
    assert step["model"] == "claude-haiku-4-5"
    assert "effort" not in step["output_config"] and "betas" not in step
    assert step["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert step["output_config"]["format"]["type"] == "json_schema"
    assert all(block["type"] == "text" for block in step["messages"][0]["content"])
    assert judge["model"] == "claude-opus-5-5"
    assert judge["output_config"]["effort"] == "medium"
    assert judge["betas"] == [FALLBACK_BETA] and judge["fallbacks"] == "default"
    image = judge["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    decoded = Image.open(io.BytesIO(base64.b64decode(image["source"]["data"])))
    assert decoded.size == (723, 1568)
    assert "tool_choice" not in judge


def test_anthropic_fallbacks_can_be_turned_off(tmp_path):
    provider, transport = _provider("anthropic", [_fixture("anthropic", "flows")], anthropic_fallbacks=False)
    provider.propose_flows("d", [])
    assert "betas" not in transport.requests[0] and "fallbacks" not in transport.requests[0]


def test_openai_request_shape(tmp_path):
    provider, transport = _provider("openai", [_fixture("openai", "computer_use")])
    provider.computer_use(_obs(tmp_path), "Tap Save")
    request = transport.requests[0]
    assert request["model"] == "gpt-5.5"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["name"] == "computer_action"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    image = request["input"][0]["content"][0]
    assert image["type"] == "input_image" and image["image_url"].startswith("data:image/png;base64,")


def test_rubric_instructions_override_prompt(tmp_path):
    provider, transport = _provider("anthropic", [_fixture("anthropic", "judge_visual")])
    provider.judge_screen(_obs(tmp_path), Rubric(name="confusion", instructions="Only check the Save button."))
    assert transport.requests[0]["system"][0]["text"] == "Only check the Save button."


def test_step_screenshot_setting_attaches_image(tmp_path):
    provider, transport = _provider("anthropic", [_fixture("anthropic", "step_tap")], step_screenshot=True)
    provider.decide_step(_obs(tmp_path), "g", [])
    assert transport.requests[0]["messages"][0]["content"][0]["type"] == "image"


# Bad output


@pytest.mark.parametrize(
    "kind,name",
    [
        ("anthropic", "invalid_schema"),
        ("anthropic", "not_json"),
        ("anthropic", "refusal"),
        ("openai", "invalid_schema"),
        ("openai", "refusal"),
        ("openai", "incomplete"),
    ],
)
def test_unusable_output_is_refused_but_still_metered(kind, name, tmp_path):
    provider, _ = _provider(kind, [_fixture(kind, name)])
    with pytest.raises(ModelRefused):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert provider.meter.calls == 1
    assert sum(t.input_tokens for t in provider.meter.by_model.values()) > 0


def test_anthropic_max_tokens_stop_is_refused(tmp_path):
    raw = _fixture("anthropic", "step_tap")
    raw["stop_reason"] = "max_tokens"
    provider, _ = _provider("anthropic", [raw])
    with pytest.raises(ModelRefused, match="max_tokens"):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])


# Timeouts, retries, budgets


class SlowTransport:
    """Every attempt uses up its whole timeout."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.attempts = 0

    def send(self, request, *, timeout_s):
        self.attempts += 1
        self.clock.now += timeout_s
        raise TransportTimeout("slow")


def test_timeout_raises_model_timeout(tmp_path):
    clock = FakeClock()
    transport = SlowTransport(clock)
    provider = AnthropicProvider(LLMSettings(), transport, sleep=clock.sleep, clock=clock)
    with pytest.raises(ModelTimeout):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [], timeout_s=5)
    assert transport.attempts == 1
    assert clock.now - 1000.0 == pytest.approx(5.0)


def test_timeout_retries_when_time_remains(tmp_path):
    clock = FakeClock()
    provider, transport = _provider(
        "anthropic", [TransportTimeout("t"), TransportTimeout("t"), TransportTimeout("t")], clock=clock
    )
    with pytest.raises(ModelTimeout):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [], timeout_s=60)
    assert len(transport.requests) == 3  # first try plus max_retries=2
    assert len(clock.sleeps) == 2


def test_settings_timeout_caps_method_timeout(tmp_path):
    provider, transport = _provider("anthropic", [_fixture("anthropic", "step_tap")], timeout_s=7)
    provider.decide_step(_obs(tmp_path, screenshot=False), "g", [], timeout_s=30)
    assert transport.timeouts == [7]


@pytest.mark.parametrize("kind", PROVIDERS)
def test_transient_errors_retry_with_backoff_then_succeed(kind, tmp_path):
    clock = FakeClock()
    responses = [
        TransientError("overloaded", status=529),
        TransientError("rate limited", status=429, retry_after=3),
        _fixture(kind, "step_tap"),
    ]
    provider, transport = _provider(kind, responses, clock=clock)
    decision = provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert decision.kind == "tap"
    assert len(transport.requests) == 3
    assert len(clock.sleeps) == 2
    assert 0.5 <= clock.sleeps[0] < 1.0
    assert clock.sleeps[1] >= 3
    assert provider.meter.calls == 1


def test_transient_errors_give_up_after_max_retries(tmp_path):
    provider, transport = _provider("anthropic", [TransientError("down", status=500)] * 5, max_retries=1)
    with pytest.raises(ModelError, match="after 2 attempts") as info:
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert not isinstance(info.value, ModelTimeout)
    assert len(transport.requests) == 2


def test_non_retryable_error_is_not_retried(tmp_path):
    provider, transport = _provider("anthropic", [ModelError("HTTP 400: bad"), _fixture("anthropic", "step_tap")])
    with pytest.raises(ModelError, match="400"):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert len(transport.requests) == 1


def test_call_cap(tmp_path):
    provider, transport = _provider(
        "anthropic", [_fixture("anthropic", "step_tap"), _fixture("anthropic", "step_tap")], max_calls=1
    )
    provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    with pytest.raises(ModelBudgetExceeded):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert len(transport.requests) == 1


def test_cost_cap_refuses_a_call_that_would_pass_it(tmp_path):
    provider, transport = _provider(
        "anthropic", [_fixture("anthropic", "step_tap"), _fixture("anthropic", "step_tap")], max_cost=0.0025
    )
    provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert provider.meter.cost == pytest.approx(0.00233)
    with pytest.raises(ModelBudgetExceeded, match="spend cap"):
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert len(transport.requests) == 1


def test_shared_meter_counts_across_providers_and_feeds_spend(tmp_path):
    spend = SpendMeter(cap=1.0)
    meter = UsageMeter(spend=spend)
    clock = FakeClock()
    anthropic = AnthropicProvider(
        LLMSettings(), ReplayTransport([_fixture("anthropic", "step_tap")]), meter=meter, clock=clock
    )
    openai = OpenAIProvider(
        LLMSettings(provider="openai", prices=OPENAI_PRICES),
        ReplayTransport([_fixture("openai", "step_tap")]),
        meter=meter,
        clock=clock,
    )
    anthropic.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    openai.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert meter.calls == 2
    assert spend.spent == pytest.approx(meter.cost)
    assert set(meter.summary()["models"]) == {"anthropic/claude-haiku-4-5", "openai/gpt-5.4-mini"}


# Cost computation


def test_anthropic_cost_counts_cache_reads_and_writes(tmp_path):
    provider, _ = _provider("anthropic", [_fixture("anthropic", "judge_visual"), _fixture("anthropic", "flows")])
    judged = provider.judge_screen(_obs(tmp_path), Rubric(name="visual")).usage
    # claude-opus-5-5: $4 in, $20 out, $0.20 cache read, $5 cache write per MTok.
    assert judged.input_tokens == 412 and judged.cache_read_tokens == 1210 and judged.output_tokens == 388
    assert judged.cost == pytest.approx((412 * 4 + 1210 * 0.20 + 388 * 20) / 1e6)
    assert judged.currency == "USD" and judged.model == "claude-opus-5-5"
    flows = provider.propose_flows("d", []).usage
    assert flows.input_tokens == 2640 + 950
    assert flows.cost == pytest.approx((2640 * 4 + 950 * 5 + 612 * 20) / 1e6)


def test_openai_cost_splits_cached_input(tmp_path):
    provider, _ = _provider("openai", [_fixture("openai", "judge_visual")])
    usage = provider.judge_screen(_obs(tmp_path), Rubric(name="visual")).usage
    assert usage.input_tokens == 2400 - 1280 and usage.cache_read_tokens == 1280
    assert usage.cost == pytest.approx(((2400 - 1280) * 1.0 + 1280 * 0.10 + 520 * 8.0) / 1e6)


def test_unpriced_model_costs_zero_and_warns_once(tmp_path):
    provider, _ = _provider("openai", [_fixture("openai", "step_tap")] * 2, prices={})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
        provider.decide_step(_obs(tmp_path, screenshot=False), "g", [])
    assert first.usage.cost == 0
    assert len([w for w in caught if "no price" in str(w.message)]) == 1


def test_price_override_and_currency(tmp_path):
    provider, _ = _provider(
        "anthropic",
        [_fixture("anthropic", "step_tap")],
        prices={"claude-haiku-4-5": ModelPrice(2.0, 10.0)},
        currency="EUR",
    )
    usage = provider.decide_step(_obs(tmp_path, screenshot=False), "g", []).usage
    assert usage.cost == pytest.approx((1850 * 2 + 96 * 10) / 1e6)
    assert usage.currency == "EUR"
