"""Model provider settings and the provider factory.

`LLMSettings.from_mapping` reads the campaign config's `[llm]` table (all
keys but `enabled`).
`create_provider` imports a model SDK only when it builds a live
transport, so the core install never needs `anthropic` or `openai`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal, Mapping

from swarmqa.llm.pricing import ModelPrice
from swarmqa.llm.protocol import ModelError, ModelProvider

ProviderName = Literal["anthropic", "openai", "fake", "chat", "claude_cli", "codex_cli", "system_one"]
CallKind = Literal["step", "judge", "flows", "computer_use"]
CALL_KINDS: tuple[str, ...] = ("step", "judge", "flows", "computer_use")

DEFAULT_MODELS: dict[str, dict[str, str]] = {
    # Haiku for the many cheap step decisions; Opus for judgments, flow
    # proposals and coordinate actions, where accuracy matters more.
    "anthropic": {
        "step": "claude-haiku-4-5",
        "judge": "claude-opus-5-5",
        "flows": "claude-opus-5-5",
        "computer_use": "claude-opus-5-5",
    },
    # Check these against OpenAI's current model list before relying on them.
    "openai": {
        "step": "gpt-5.4-mini",
        "judge": "gpt-5.5",
        "flows": "gpt-5.5",
        "computer_use": "gpt-5.5",
    },
    "fake": {kind: "fake" for kind in CALL_KINDS},
    # A coding agent in a chat drives each step through MCP (swarmqa.explorer.remote).
    "chat": {kind: "chat" for kind in CALL_KINDS},
    # Coding-agent CLIs on the user's own plan (swarmqa.llm.cli_provider).
    # Claude Code model aliases; "default" leaves the CLI's own choice.
    "claude_cli": {"step": "sonnet", "judge": "opus", "flows": "opus", "computer_use": "opus"},
    "codex_cli": {kind: "default" for kind in CALL_KINDS},
    # A Jev-compatible System One server (swarmqa.llm.system_one); its model
    # is `[llm.system_one] model`, and these only name it in reports.
    "system_one": {kind: "jev-latest" for kind in CALL_KINDS},
}

DEFAULT_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "fake": "",
    "chat": "",
    "claude_cli": "",
    "codex_cli": "",
    "system_one": "",
}

DEFAULT_EFFORT: dict[str, str] = {"step": "low", "judge": "medium", "flows": "high", "computer_use": "medium"}

DEFAULT_MAX_TOKENS: dict[str, int] = {"step": 4096, "judge": 16000, "flows": 16000, "computer_use": 8000}

# Seconds one explore step may wait for the model when `explorer.decision.model_timeout_s`
# is unset. A CLI call starts a whole agent process, so it gets longer than an API call.
DEFAULT_STEP_TIMEOUT_S = 30.0
CLI_STEP_TIMEOUT_S = 90.0
# A System One answer takes well under a second. An escalated step gets the
# escalation provider's own default (see `SystemOneProvider`).
SYSTEM_ONE_STEP_TIMEOUT_S = 5.0


def step_timeout_default(provider: str | None) -> float:
    if provider == "system_one":
        return SYSTEM_ONE_STEP_TIMEOUT_S
    return CLI_STEP_TIMEOUT_S if provider in ("claude_cli", "codex_cli") else DEFAULT_STEP_TIMEOUT_S


class MissingExtraError(ModelError):
    """The provider's SDK is not installed."""

    def __init__(self, provider: str):
        super().__init__(
            f"the {provider} provider needs the '{provider}' extra: "
            f"uv tool install 'swarmqa[{provider}]' (or pip install 'swarmqa[{provider}]')"
        )
        self.provider = provider


@dataclass
class LLMSettings:
    """Which provider and models to use, and the limits on them.

    Model fields left empty take the provider's default. `api_key_env`
    names the environment variable holding the key; when it is unset the
    SDK's own credential lookup still applies. `timeout_s`, when set, caps
    every call below the method's own `timeout_s`. `max_calls` and
    `max_cost` cap this provider's calls and spend (in `currency`).
    `prices` add to or override `pricing.DEFAULT_PRICES`. `effort` and
    `max_tokens` are per call kind (`step`, `judge`, `flows`,
    `computer_use`). `max_image_edge` bounds the screenshot's long edge in
    pixels; `max_prompt_chars` bounds diff text for `propose_flows`.
    `anthropic_fallbacks` opts Opus/Sonnet 5.5-class requests into
    server-side refusal fallback. `system_one` is the `[llm.system_one]`
    table (see `swarmqa.llm.system_one.SystemOneSettings`); with
    `provider = "system_one"`, its `escalate_to` provider is built from
    these same settings.
    """

    provider: ProviderName = "anthropic"
    step_model: str = ""
    judge_model: str = ""
    flows_model: str = ""
    computer_use_model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    timeout_s: float | None = None
    max_retries: int = 2
    max_calls: int | None = None
    max_cost: float | None = None
    currency: str = "USD"
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    effort: dict[str, str] = field(default_factory=dict)
    max_tokens: dict[str, int] = field(default_factory=dict)
    max_image_edge: int = 1568
    max_prompt_chars: int = 200_000
    history_limit: int = 12
    step_screenshot: bool = False
    anthropic_fallbacks: bool = True
    system_one: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provider not in DEFAULT_MODELS:
            raise ValueError(f"unknown llm provider: {self.provider!r}")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.max_calls is not None and self.max_calls <= 0:
            raise ValueError("max_calls must be > 0 when set")
        if self.max_cost is not None and self.max_cost <= 0:
            raise ValueError("max_cost must be > 0 when set")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise ValueError("timeout_s must be > 0 when set")
        for key in (*self.effort, *self.max_tokens):
            if key not in CALL_KINDS:
                raise ValueError(f"unknown call kind: {key!r}")
        if self.provider == "system_one":
            from swarmqa.llm.system_one import SystemOneSettings

            escalate_to = SystemOneSettings.from_mapping(self.system_one).escalate_to
            if escalate_to and escalate_to not in DEFAULT_MODELS:
                raise ValueError(f"unknown system_one.escalate_to provider: {escalate_to!r}")

    def model_for(self, kind: str) -> str:
        explicit = getattr(self, f"{kind}_model")
        return explicit or DEFAULT_MODELS[self.provider][kind]

    def effort_for(self, kind: str) -> str:
        return self.effort.get(kind) or DEFAULT_EFFORT[kind]

    def max_tokens_for(self, kind: str) -> int:
        return self.max_tokens.get(kind) or DEFAULT_MAX_TOKENS[kind]

    def key_env(self) -> str:
        return self.api_key_env or DEFAULT_API_KEY_ENV[self.provider]

    def call_timeout(self, requested: float) -> float:
        return min(requested, self.timeout_s) if self.timeout_s else requested

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LLMSettings":
        """Build from a TOML-style table. Unknown keys raise ValueError.

        `prices` maps model -> {input, output, cache_read?, cache_write?}.
        """
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown llm settings: {', '.join(unknown)}")
        values = dict(raw)
        if "prices" in values:
            values["prices"] = {
                model: price if isinstance(price, ModelPrice) else ModelPrice(**price)
                for model, price in dict(values["prices"]).items()
            }
        return cls(**values)


def create_provider(
    settings: LLMSettings | None = None,
    *,
    transport: Any = None,
    meter: Any = None,
) -> ModelProvider:
    """Build the configured provider.

    Pass `transport` to replay recorded responses (no SDK needed). Without
    one, the live SDK transport is built and a missing SDK raises
    `MissingExtraError` naming the extra to install.
    """
    settings = settings or LLMSettings()
    if settings.provider == "system_one":
        from dataclasses import replace

        from swarmqa.llm.system_one import create_system_one_provider

        escalate_to = settings.system_one.get("escalate_to") or ""
        escalate = create_provider(replace(settings, provider=escalate_to), meter=meter) if escalate_to else None
        return create_system_one_provider(
            settings.system_one,
            escalate=escalate,
            transport=transport,
            meter=meter,
            max_retries=settings.max_retries,
            max_calls=settings.max_calls,
            escalate_timeout_s=step_timeout_default(escalate_to) if escalate_to else None,
        )
    if settings.provider == "fake":
        from swarmqa.llm.fake import FakeModelProvider

        return FakeModelProvider()
    if settings.provider == "chat":
        from swarmqa.explorer.remote import RemoteController

        return RemoteController(idle_timeout_s=settings.timeout_s)  # type: ignore[return-value]
    if settings.provider in ("claude_cli", "codex_cli"):
        from swarmqa.llm.cli_provider import ClaudeCLIProvider, CLITransport, CodexCLIProvider

        cls = ClaudeCLIProvider if settings.provider == "claude_cli" else CodexCLIProvider
        return cls(settings, transport or CLITransport(cls.cli), meter=meter)
    if settings.provider == "anthropic":
        from swarmqa.llm.anthropic_provider import AnthropicProvider, AnthropicSDKTransport

        live = transport or AnthropicSDKTransport.from_settings(settings)
        return AnthropicProvider(settings, live, meter=meter)
    from swarmqa.llm.openai_provider import OpenAIProvider, OpenAISDKTransport

    live = transport or OpenAISDKTransport.from_settings(settings)
    return OpenAIProvider(settings, live, meter=meter)
