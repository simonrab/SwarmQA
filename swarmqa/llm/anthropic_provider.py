"""Anthropic (Claude) adapter over the Messages API.

Structured output uses `output_config.format` with a JSON schema (forced
tool use is rejected by Opus 5.5-class models). The static system prompt
is one text block with `cache_control`, so repeated calls of the same kind
read it from the prompt cache once it is long enough to cache (the minimum
is model-dependent). Screenshots go in as base64 PNG image blocks before
the text. `output_config.effort` is sent for models that take it (not
Haiku). For Opus 5.5 / Opus 5 / Fable 5.x / Sonnet 5.5 the request opts
into server-side refusal fallback (`fallbacks: "default"`) unless
`anthropic_fallbacks` is off.

Computer use returns one structured coordinate action per call instead of
running the computer-use toolset: the toolset is a multi-turn loop that
expects the client to answer each tool call with a new screenshot, while
the agent loop here already re-observes the screen between steps.
"""

from __future__ import annotations

import os
from typing import Any

from swarmqa.llm.base import BaseProvider, BilledError, Completion, ModelCall, int_field
from swarmqa.llm.protocol import ModelError, ModelRefused
from swarmqa.llm.settings import LLMSettings, MissingExtraError
from swarmqa.llm.transport import classify_sdk_error

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")
_NO_EFFORT_MODELS = ("claude-haiku",)


def supports_effort(model: str) -> bool:
    return not model.startswith(_NO_EFFORT_MODELS)


def supports_fallbacks(model: str) -> bool:
    return model.startswith(_FALLBACK_MODELS)


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def build_request(self, call: ModelCall) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if call.image is not None:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": call.image.media_type, "data": call.image.b64},
                }
            )
        content.append({"type": "text", "text": call.text})
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": call.schema}}
        if call.effort and supports_effort(call.model):
            output_config["effort"] = call.effort
        request: dict[str, Any] = {
            "model": call.model,
            "max_tokens": call.max_tokens,
            "system": [{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }
        if self.settings.anthropic_fallbacks and supports_fallbacks(call.model):
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def parse_response(self, raw: dict[str, Any], call: ModelCall) -> Completion:
        if not isinstance(raw, dict):
            raise ModelRefused("response is not an object")
        usage = raw.get("usage") or {}
        completion = Completion(
            text="",
            model=str(raw.get("model") or call.model),
            input_tokens=int_field(usage, "input_tokens"),
            output_tokens=int_field(usage, "output_tokens"),
            cache_read_tokens=int_field(usage, "cache_read_input_tokens"),
            cache_write_tokens=int_field(usage, "cache_creation_input_tokens"),
        )
        stop = raw.get("stop_reason")
        if stop == "refusal":
            details = raw.get("stop_details") or {}
            category = details.get("category") if isinstance(details, dict) else None
            raise BilledError(completion, ModelRefused(f"model refused ({category or 'no category'})"))
        if stop == "max_tokens":
            raise BilledError(completion, ModelRefused("output was cut off at max_tokens"))
        texts = [
            block.get("text", "")
            for block in raw.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not texts or not texts[-1].strip():
            raise BilledError(completion, ModelRefused("response has no text output"))
        completion.text = texts[-1]
        return completion


class AnthropicSDKTransport:
    """Send requests with the `anthropic` SDK. SDK retries are off; the provider retries."""

    def __init__(self, client: Any, sdk: Any):
        self.client = client
        self.sdk = sdk

    @classmethod
    def from_settings(cls, settings: LLMSettings) -> "AnthropicSDKTransport":
        try:
            import anthropic
        except ImportError as exc:
            raise MissingExtraError("anthropic") from exc
        kwargs: dict[str, Any] = {"max_retries": 0}
        key = os.environ.get(settings.key_env(), "").strip()
        if key:
            kwargs["api_key"] = key
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        try:
            client = anthropic.Anthropic(**kwargs)
        except Exception as exc:  # missing credentials and similar
            raise ModelError(f"could not create the Anthropic client: {exc}") from exc
        return cls(client, anthropic)

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        body = dict(request)
        betas = body.pop("betas", None)
        fallbacks = body.pop("fallbacks", None)
        try:
            if betas:
                extra = {"fallbacks": fallbacks} if fallbacks is not None else None
                response = self.client.beta.messages.create(
                    **body, betas=betas, extra_body=extra, timeout=timeout_s
                )
            else:
                response = self.client.messages.create(**body, timeout=timeout_s)
        except Exception as exc:
            raise classify_sdk_error(exc, self.sdk) from exc
        return _as_dict(response)


def _as_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    for method in ("to_dict", "model_dump"):
        fn = getattr(response, method, None)
        if callable(fn):
            return fn()
    raise ModelError("unexpected response type from SDK")
