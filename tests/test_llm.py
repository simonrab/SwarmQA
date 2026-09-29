"""Shared model-provider pieces: settings, factory, images, parsing, pricing, transports."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm import schemas
from swarmqa.llm.anthropic_provider import AnthropicProvider, AnthropicSDKTransport
from swarmqa.llm.base import load_prompt
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.images import prepare_image
from swarmqa.llm.openai_provider import OpenAISDKTransport
from swarmqa.llm.parse import parse_flows, parse_judgment, parse_step
from swarmqa.llm.pricing import ModelPrice, cost_of, price_for
from swarmqa.llm.protocol import ModelError, ModelRefused, Rubric, StepDecision, Usage
from swarmqa.llm.serialize import files_text, history_text, tree_text
from swarmqa.llm.settings import LLMSettings, MissingExtraError, create_provider
from swarmqa.llm.transport import (
    RecordingTransport,
    ReplayTransport,
    TransientError,
    TransportTimeout,
    classify_sdk_error,
)
from swarmqa.llm.usage import UsageMeter
from swarmqa.models import ElementQuery, UIElement
from swarmqa.spend import SpendMeter

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


# Settings and factory


def test_settings_defaults_and_overrides():
    settings = LLMSettings()
    assert settings.model_for("step") == "claude-haiku-4-5"
    assert settings.model_for("judge") == "claude-opus-5-5"
    assert settings.key_env() == "ANTHROPIC_API_KEY"
    openai = LLMSettings(provider="openai", judge_model="my-model", api_key_env="MY_KEY")
    assert openai.model_for("judge") == "my-model"
    assert openai.model_for("step") == "gpt-5.4-mini"
    assert openai.key_env() == "MY_KEY"
    assert LLMSettings(provider="openai").key_env() == "OPENAI_API_KEY"
    assert LLMSettings(effort={"judge": "high"}).effort_for("judge") == "high"


@pytest.mark.parametrize(
    "bad",
    [{"provider": "gemini"}, {"max_calls": 0}, {"max_cost": -1}, {"effort": {"steps": "low"}}, {"max_retries": -1}],
)
def test_settings_validation(bad):
    with pytest.raises(ValueError):
        LLMSettings(**bad)


def test_settings_from_mapping():
    settings = LLMSettings.from_mapping(
        {"provider": "openai", "max_cost": 2.5, "prices": {"gpt-5.5": {"input": 1, "output": 8}}}
    )
    assert settings.prices["gpt-5.5"] == ModelPrice(1, 8)
    with pytest.raises(ValueError, match="unknown llm settings"):
        LLMSettings.from_mapping({"providr": "openai"})


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_missing_sdk_names_the_extra(name, monkeypatch):
    monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(MissingExtraError) as info:
        create_provider(LLMSettings(provider=name))
    assert isinstance(info.value, ModelError)
    assert f"uv tool install 'swarmqa[{name}]'" in str(info.value)


def test_factory_with_replay_transport_needs_no_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    provider = create_provider(LLMSettings(), transport=ReplayTransport())
    assert isinstance(provider, AnthropicProvider)


def test_factory_fake_provider_behaves_as_before(tmp_path):
    provider = create_provider(LLMSettings(provider="fake"))
    assert isinstance(provider, FakeModelProvider)
    tree = [UIElement(role="button", label="Go", frame=(0, 0, 10, 10))]
    obs = ScreenObservation(tree=tree, ts=0.0)
    decision = provider.decide_step(obs, "g", [])
    assert decision.kind == "tap" and decision.target == ElementQuery(role="button", label="Go")
    assert decision.usage == Usage(provider="fake", model="fake")
    assert provider.computer_use(obs, "x").point == (5.0, 5.0)
    assert provider.propose_flows("d", []).flows == []
    assert provider.calls == ["decide_step", "computer_use", "propose_flows"]


# Prompts and schemas


@pytest.mark.parametrize("name", ["step", "judge_visual", "judge_confusion", "flows", "computer_use"])
def test_prompts_ship_as_package_data(name):
    assert len(load_prompt(name)) > 200


@pytest.mark.parametrize(
    "schema", [schemas.STEP_SCHEMA, schemas.COMPUTER_USE_SCHEMA, schemas.JUDGE_SCHEMA, schemas.FLOWS_SCHEMA]
)
def test_schemas_fit_strict_mode(schema):
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert sorted(node["required"]) == sorted(node["properties"])
            for banned in ("minimum", "maximum", "minLength", "maxLength", "minItems"):
                assert banned not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


# Images


def test_large_screenshot_is_downscaled(tmp_path):
    path = tmp_path / "big.png"
    Image.new("RGB", (1179, 2556), "white").save(path)
    image = prepare_image(path, max_edge=1568, scale=3.0)
    assert (image.width, image.height) == (723, 1568)
    assert (image.original_width, image.original_height) == (1179, 2556)
    assert image.estimated_tokens == 723 * 1568 // 750
    x, y = image.to_points(723, 1568)
    assert (x, y) == pytest.approx((393, 852), abs=0.1)


def test_small_png_is_sent_unchanged(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (400, 300), "white").save(path)
    image = prepare_image(path)
    assert image.data == path.read_bytes()
    assert image.to_points(100, 50) == (100, 50)


def test_non_png_is_reencoded_and_missing_file_is_model_error(tmp_path):
    path = tmp_path / "shot.jpg"
    Image.new("RGB", (300, 200), "white").save(path, format="JPEG")
    image = prepare_image(path)
    assert image.media_type == "image/png" and image.data.startswith(b"\x89PNG")
    with pytest.raises(ModelError):
        prepare_image(tmp_path / "missing.png")
    (tmp_path / "junk.png").write_text("not an image")
    with pytest.raises(ModelError):
        prepare_image(tmp_path / "junk.png")


# Serialisation


def test_tree_text_ids_and_cap():
    tree = [
        UIElement(role="window", label="W", children=[UIElement(role="button", label="Ok", identifier="ok", frame=(1, 2, 3, 4))]),
        UIElement(role="button", label="Off", enabled=False),
    ]
    text, index = tree_text(tree)
    assert text.splitlines() == ['e1 window "W"', '  e2 button "Ok" id=ok frame=1,2,3,4', 'e3 button "Off" disabled']
    assert index["e2"].identifier == "ok"
    capped, index = tree_text(tree, max_elements=1)
    assert "[2 more elements omitted]" in capped and list(index) == ["e1"]


def test_history_and_files_text():
    from swarmqa.llm.protocol import HistoryStep

    steps = [HistoryStep(action=StepDecision(kind="key", keys=["cmd", "n"]), outcome="new window")] * 3
    text = history_text(steps, limit=2)
    assert text.splitlines()[0] == "[1 earlier steps omitted]"
    assert "2. key cmd+n -> new window" in text
    long = files_text("x" * 50, [], max_chars=20)
    assert long.endswith("more characters not shown]")


# Parsing


def _elements():
    return {"e1": UIElement(role="button", label="Save", identifier="save")}


def test_parse_step_kinds():
    base = {"element_id": None, "x": None, "y": None, "end_x": None, "end_y": None, "text": None, "keys": [],
            "rationale": "", "confidence": 0.5}
    tap = parse_step({**base, "kind": "tap", "element_id": "e1"}, _elements())
    assert tap.target == ElementQuery(role="button", label="Save", identifier="save")
    swipe = parse_step({**base, "kind": "swipe", "x": 1, "y": 2, "end_x": 3, "end_y": 4}, {})
    assert swipe.point == (1, 2) and swipe.end == (3, 4)
    typed = parse_step({**base, "kind": "type", "text": "hi"}, {})
    assert typed.text == "hi" and typed.target is None
    assert parse_step({**base, "kind": "key", "keys": ["return"]}, {}).keys == ["return"]
    assert parse_step({**base, "kind": "done", "element_id": "e1"}, _elements()).target is None


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "fly"},
        {"kind": "tap"},
        {"kind": "tap", "element_id": "e9"},
        {"kind": "tap_point", "x": 1},
        {"kind": "type", "text": ""},
        {"kind": "key", "keys": []},
        {"kind": "done", "confidence": 1.5},
        {"kind": "done", "confidence": "high"},
        {"kind": "tap_point", "x": "a", "y": 2},
    ],
)
def test_parse_step_rejects(patch):
    base = {"element_id": None, "x": None, "y": None, "end_x": None, "end_y": None, "text": None, "keys": [],
            "rationale": "", "confidence": 0.5}
    with pytest.raises(ModelRefused):
        parse_step({**base, **patch}, _elements())


def test_parse_judgment_and_flows_reject_bad_shapes():
    with pytest.raises(ModelRefused):
        parse_judgment({"issues": "none"}, {}, min_confidence=0.5)
    with pytest.raises(ModelRefused):
        parse_judgment({"issues": [{"category": "ugly", "title": "x", "confidence": 0.9}]}, {}, min_confidence=0.5)
    with pytest.raises(ModelRefused):
        parse_flows({"flows": [{"name": "", "goal": "g", "priority": 1, "steps": [], "expected": [], "touched_files": []}]},
                    max_flows=3)
    with pytest.raises(ModelRefused):
        parse_flows({"flows": [{"name": "n", "goal": "g", "priority": 1.5, "steps": [], "expected": [], "touched_files": []}]},
                    max_flows=3)
    clamped = parse_flows({"flows": [{"name": "n", "goal": "g", "priority": 9, "steps": [], "expected": [], "touched_files": []}]},
                          max_flows=3)
    assert clamped.flows[0].priority == 5


# Pricing and usage


def test_price_lookup_and_cost():
    assert price_for("claude-haiku-4-5").input == 1.0
    assert price_for("claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5")
    assert price_for("unknown-model") is None
    assert price_for("x", {"x": ModelPrice(1, 2)}) == ModelPrice(1, 2)
    price = ModelPrice(input=3, output=15)
    assert cost_of(price, input_tokens=1000, output_tokens=100, cache_read_tokens=1000, cache_write_tokens=1000) == (
        pytest.approx((1000 * 3 + 100 * 15 + 1000 * 3 + 1000 * 3) / 1e6)
    )
    assert cost_of(None, input_tokens=10) == 0


def test_usage_meter_feeds_spend_meter():
    spend = SpendMeter(cap=None)
    meter = UsageMeter(spend=spend)
    meter.start_call()
    meter.add(Usage(provider="anthropic", model="m", input_tokens=10, output_tokens=5, cost=0.25))
    meter.add(Usage(provider="fake", model="fake"))
    assert spend.spent == 0.25 and meter.cost == 0.25
    summary = meter.summary()
    assert summary["calls"] == 1
    assert summary["models"]["anthropic/m"]["input_tokens"] == 10


# Transports


def test_replay_transport_from_files_and_exhaustion():
    transport = ReplayTransport.from_files(FIXTURES / "anthropic" / "step_tap.json")
    assert transport.send({"a": 1}, timeout_s=3)["id"] == "msg_01StepTap"
    assert transport.requests == [{"a": 1}] and transport.timeouts == [3]
    with pytest.raises(ModelError):
        transport.send({}, timeout_s=1)


def test_recording_transport_saves_response_and_elides_images(tmp_path):
    inner = ReplayTransport([{"ok": True}])
    recorder = RecordingTransport(inner, tmp_path / "rec")
    request = {"messages": [{"source": {"data": "A" * 1000}}]}
    assert recorder.send(request, timeout_s=1) == {"ok": True}
    assert json.loads((tmp_path / "rec" / "001.json").read_text()) == {"ok": True}
    saved = json.loads((tmp_path / "rec" / "001.request.json").read_text())
    assert saved["messages"][0]["source"]["data"] == "<1000 chars elided>"


class _SDKError(Exception):
    pass


def _fake_sdk():
    class APIConnectionError(_SDKError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(_SDKError):
        def __init__(self, status, headers=None):
            super().__init__(f"status {status}")
            self.status_code = status
            self.message = f"status {status}"
            self.response = SimpleNamespace(headers=headers or {})

    return SimpleNamespace(
        APIConnectionError=APIConnectionError, APITimeoutError=APITimeoutError, APIStatusError=APIStatusError
    )


def test_classify_sdk_error():
    sdk = _fake_sdk()
    assert isinstance(classify_sdk_error(sdk.APITimeoutError("t"), sdk), TransportTimeout)
    assert isinstance(classify_sdk_error(sdk.APIConnectionError("c"), sdk), TransientError)
    limited = classify_sdk_error(sdk.APIStatusError(429, {"retry-after": "4"}), sdk)
    assert isinstance(limited, TransientError) and limited.retry_after == 4.0 and limited.status == 429
    assert isinstance(classify_sdk_error(sdk.APIStatusError(529), sdk), TransientError)
    bad = classify_sdk_error(sdk.APIStatusError(400), sdk)
    assert type(bad) is ModelError and "400" in str(bad)


def test_anthropic_sdk_transport_routes_betas_and_maps_errors():
    sdk = _fake_sdk()
    calls = []

    class Response:
        def to_dict(self):
            return {"id": "msg"}

    def create(**kwargs):
        calls.append(kwargs)
        return Response()

    def failing(**kwargs):
        raise sdk.APIStatusError(503)

    client = SimpleNamespace(
        messages=SimpleNamespace(create=create), beta=SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    transport = AnthropicSDKTransport(client, sdk)
    assert transport.send({"model": "m", "betas": ["b"], "fallbacks": "default"}, timeout_s=9) == {"id": "msg"}
    assert calls[0] == {"model": "m", "betas": ["b"], "extra_body": {"fallbacks": "default"}, "timeout": 9}
    transport.send({"model": "m"}, timeout_s=2)
    assert calls[1] == {"model": "m", "timeout": 2}
    client.messages.create = failing
    with pytest.raises(TransientError):
        transport.send({"model": "m"}, timeout_s=2)


def test_openai_sdk_transport_dumps_response():
    sdk = _fake_sdk()

    class Response:
        def model_dump(self, mode):
            return {"id": "resp", "mode": mode}

    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: Response()))
    assert OpenAISDKTransport(client, sdk).send({"model": "m"}, timeout_s=1) == {"id": "resp", "mode": "json"}


def test_judge_rubric_prompt_varies(tmp_path):
    assert load_prompt("judge_visual") != load_prompt("judge_confusion")
    assert Rubric(name="confusion").min_confidence == 0.5


@pytest.mark.parametrize("priority", [float("inf"), float("nan")])
def test_non_finite_flow_priority_is_refused(priority):
    flow = {"name": "n", "goal": "g", "priority": priority, "steps": [], "expected": [], "touched_files": []}
    with pytest.raises(ModelRefused):
        parse_flows({"flows": [flow]}, max_flows=5)


def test_call_cap_holds_across_threads():
    import threading

    from swarmqa.llm.protocol import ModelBudgetExceeded

    meter = UsageMeter(calls=9)
    barrier = threading.Barrier(4)
    refused = []

    def call():
        barrier.wait()
        try:
            meter.start_call(10)
        except ModelBudgetExceeded:
            refused.append(True)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert meter.calls == 10
    assert len(refused) == 3
