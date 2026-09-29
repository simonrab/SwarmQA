"""OpenAI adapter over the Responses API.

Structured output uses `text.format` with a strict JSON schema. The system
prompt goes in `instructions`; OpenAI caches long shared prefixes on its
own. Screenshots go in as `input_image` data URLs. Reasoning models
(`gpt-5*`, `o*`) get `reasoning.effort`. Responses are not stored.

Default model names are in `settings.DEFAULT_MODELS["openai"]`; check them
against OpenAI's current list. No default prices ship for OpenAI: set
`LLMSettings.prices` so costs and spend caps work.
"""

from __future__ import annotations

import os
from typing import Any

from swarmqa.llm.base import BaseProvider, BilledError, Completion, ModelCall, int_field
from swarmqa.llm.protocol import ModelError, ModelRefused
from swarmqa.llm.settings import LLMSettings, MissingExtraError
from swarmqa.llm.transport import classify_sdk_error

_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high", "minimal": "minimal"}


def is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


class OpenAIProvider(BaseProvider):
    name = "openai"

    def build_request(self, call: ModelCall) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if call.image is not None:
            content.append({"type": "input_image", "image_url": call.image.data_url, "detail": "high"})
        content.append({"type": "input_text", "text": call.text})
        request: dict[str, Any] = {
            "model": call.model,
            "instructions": call.system,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {"type": "json_schema", "name": call.schema_name, "schema": call.schema, "strict": True}
            },
            "max_output_tokens": call.max_tokens,
            "store": False,
        }
        if call.effort and is_reasoning_model(call.model):
            request["reasoning"] = {"effort": _EFFORT.get(call.effort, "medium")}
        return request

    def parse_response(self, raw: dict[str, Any], call: ModelCall) -> Completion:
        if not isinstance(raw, dict):
            raise ModelRefused("response is not an object")
        usage = raw.get("usage") or {}
        total_input = int_field(usage, "input_tokens")
        cached = int_field(usage.get("input_tokens_details") if isinstance(usage, dict) else None, "cached_tokens")
        completion = Completion(
            text="",
            model=str(raw.get("model") or call.model),
            input_tokens=max(0, total_input - cached),
            output_tokens=int_field(usage, "output_tokens"),
            cache_read_tokens=cached,
        )
        status = raw.get("status")
        if status == "failed":
            error = raw.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise BilledError(completion, ModelError(f"response failed: {message or 'no detail'}"))
        if status == "incomplete":
            details = raw.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            raise BilledError(completion, ModelRefused(f"response incomplete ({reason or 'no reason'})"))
        texts: list[str] = []
        for item in raw.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise BilledError(completion, ModelRefused(f"model refused: {part.get('refusal', '')}"[:300]))
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
        text = "".join(texts)
        if not text.strip():
            raise BilledError(completion, ModelRefused("response has no text output"))
        completion.text = text
        return completion


class OpenAISDKTransport:
    """Send requests with the `openai` SDK. SDK retries are off; the provider retries."""

    def __init__(self, client: Any, sdk: Any):
        self.client = client
        self.sdk = sdk

    @classmethod
    def from_settings(cls, settings: LLMSettings) -> "OpenAISDKTransport":
        try:
            import openai
        except ImportError as exc:
            raise MissingExtraError("openai") from exc
        kwargs: dict[str, Any] = {"max_retries": 0}
        key = os.environ.get(settings.key_env(), "").strip()
        if key:
            kwargs["api_key"] = key
        if settings.base_url:
            kwargs["base_url"] = settings.base_url
        try:
            client = openai.OpenAI(**kwargs)
        except Exception as exc:  # missing credentials and similar
            raise ModelError(f"could not create the OpenAI client: {exc}") from exc
        return cls(client, openai)

    def send(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        try:
            response = self.client.responses.create(**request, timeout=timeout_s)
        except Exception as exc:
            raise classify_sdk_error(exc, self.sdk) from exc
        if isinstance(response, dict):
            return response
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            return dump(mode="json")
        raise ModelError("unexpected response type from SDK")
