# Model providers

`swarmqa/llm/` is the only place SwarmQA talks to a language model. Everything else uses the `ModelProvider` protocol in `swarmqa/llm/protocol.py` (see `docs/CONTRACTS.md`, "Phase 1 contracts (v2)") and treats any `ModelError` as fail-open.

| Provider | Module | Install |
| --- | --- | --- |
| Anthropic (Claude) | `swarmqa.llm.anthropic_provider` | `uv tool install 'swarmqa[anthropic]'` |
| OpenAI | `swarmqa.llm.openai_provider` | `uv tool install 'swarmqa[openai]'` |
| Claude Code CLI (`claude_cli`) | `swarmqa.llm.cli_provider` | core install; `claude` on `PATH`, signed in |
| Codex CLI (`codex_cli`) | `swarmqa.llm.cli_provider` | core install; `codex` on `PATH`, signed in |
| Chat (`chat`) | `swarmqa.explorer.remote` | core install; driven through `aqa mcp` (see `docs/mcp.md`) |
| System One (`system_one`) | `swarmqa.llm.system_one` | core install; a Jev-compatible server (see "System One") |
| Fake (tests, dry runs) | `swarmqa.llm.fake` | core install |

`claude_cli` and `codex_cli` run `claude -p` or `codex exec` once per call, with the call's JSON schema as the structured output, so a campaign runs on your Claude or ChatGPT plan instead of an API key. Their per-call cost is recorded as 0. A screenshot is written to a temporary folder the CLI may read. Each call starts a process (about 5 seconds per step for Claude Code), so they suit one or two devices; use an API provider for a large swarm. `claude_cli` model names are Claude Code aliases (`sonnet` for steps, `opus` for judging by default); `codex_cli` uses Codex's configured model unless you set one, and a ChatGPT sign-in only accepts the models your plan allows.

`chat` is not called by SwarmQA at all: the agent in a Claude Code or Codex chat chooses each step through the MCP session tools.

The core install needs neither SDK. `create_provider` imports an SDK only when it builds a live transport. If the SDK is missing it raises `MissingExtraError`, a `ModelError` whose message names the extra to install.

## System One

`system_one` asks a System One model (typed question in, calibrated probabilities over a fixed set of answers out) instead of a language model. One step takes well under a second, against 6 to 30 seconds for `claude_cli`. It speaks the `POST /v1/systemone` contract shared by TypeSafe's hosted Jev and the open servers that copy it (Laya, kev, decider, OpenJev):

```json
{"model": "jev-latest",
 "state": "Goal: ...\n\nScreen:\ne1 window \"Demo\" ...\n\nRecent steps:\n...",
 "questions": {"next_step": {"type": "choice", "instructions": "Pick the one action ...",
                             "criteria": {"o1": "Tap button \"Settings\" (id settings)",
                                          "o2": "Type into textfield \"Search\" (id search)",
                                          "back": "Go back to the previous screen.",
                                          "done": "The goal is already met on this screen."}}}}
```

```json
{"model": "jev-1.13.0",
 "answers": {"next_step": {"choice": "o1", "probabilities": {"o1": 0.93, "o2": 0.04, "back": 0.02, "done": 0.01}, "confidence": 0.81}},
 "usage": {"input_tokens": 812, "output_tokens": 0}}
```

- The options are the screen's actionable controls (`screen_graph.actionable_controls`, named by `element_name`), plus `back` and `done`. The model can only answer with something the loop can do. A control whose tap had no effect on this screen earlier in the session is left out.
- `confidence` is Jev's `1 - H(p)/ln K` (1 means certain, 0 means uniform). When a server sends only probabilities, swarmqa computes it the same way. `probabilities` may be a map or OpenJev's `[{value, probability}]` list.
- Below `min_confidence`, the step goes to `escalate_to` (any other provider, for example `claude_cli` or `anthropic`), with that provider's own step timeout. With no `escalate_to`, the provider raises `ModelRefused` and the agent loop takes its rule-based step, as it does for any `ModelError`. Transport errors, timeouts and an answer that is not one of the options also go this way.
- `judge_screen`, `propose_flows` and `computer_use` go to `escalate_to`, or fail open without it. A System One model does not look at screenshots here.
- **Triage.** With `triage = true`, after each agent worker finishes, every finding gets one call: a `choice` among `real_bug`, `environment_artifact` (the simulator or test environment), `harness_artifact` (swarmqa's own mistake) and `expected_behavior`, plus a `noul` asking whether a person would notice it. A confident artifact verdict makes the finding `advisory` (it is kept, not dropped). The verdict goes into the finding's `environment` (`system_one_verdict`, `system_one_confidence`, `system_one_user_visible`), its details, and the Triage block of `findings/<id>.md`. Each worker also writes `workers/<id>/system_one.json` with call counts, escalations, latency p50/p95 and the triage verdicts.
- Cost: input tokens at TypeSafe's published $0.042 per million when `base_url` is on `typesafe.ai`, otherwise 0 (set `price_per_mtok` to override).

```toml
[llm]
enabled = true
provider = "system_one"
step_model = "haiku"            # read by the escalation provider

[llm.system_one]
base_url = "http://127.0.0.1:8000"   # or "https://api.typesafe.ai"
model = "jev-latest"
api_key_env = "TYPESAFE_API_KEY"     # unset or empty: no Authorization header
min_confidence = 0.5
trust_done = false                   # false: a "done" goes to escalate_to, or the rules carry on
done_min_confidence = 0.8            # with trust_done: a wrong "done" ends the goal, so it needs more
escalate_to = "claude_cli"           # optional
state_format = "compact"             # "tree" sends the numbered accessibility tree
max_choices = 40                     # Jev takes up to 255; small models do worse past a few dozen
triage = true

[explorer.decision]
max_model_calls = 40                 # System One calls are cheap; the default cap is low
```

The step timeout defaults to 5 seconds for `system_one` (`explorer.decision.model_timeout_s` overrides it). A local model on a laptop CPU needs about 10.

`state_format = "compact"` (the default) sends the goal, the screen's visible text and the last three steps. The options carry the control names. Small encoder models such as Laya lose the goal in a long state: on a test screen, Laya chose correctly 3 times out of 4 with the compact state and once out of 4 with the full tree. `tree` sends the numbered accessibility tree (`serialize.tree_text`) for long-context models such as Jev.

**Privacy.** The goal, the accessibility tree text (labels, identifiers, values) and finding titles and steps go to `base_url`. With TypeSafe's hosted Jev, that UI text leaves the machine. A local server keeps it on the machine.

### Running an open model locally

On an 8 GB Apple-silicon Mac, Laya (0.4B, Apache-2.0) is the open model that fits beside a simulator. Install it in its own environment, outside the repo:

```bash
python3.11 -m venv ~/.venvs/jev
~/.venvs/jev/bin/pip install 'laya[serve]'
LAYA_HOST=127.0.0.1 LAYA_MODELS=english LAYA_DEFAULT_MODEL=english LAYA_MAX_LOADED=1 LAYA_JEV_STRICT=1 ~/.venvs/jev/bin/laya-serve
```

`LAYA_JEV_STRICT=1` serves the exact Jev response shape. The larger open models need more memory than this Mac has: kev-4b needs about 10 GB under MLX, decider-2b is 3.8 GB of bf16 weights plus torch, and OpenJev, JEV-9B and Clef need 12 to 24 GB of GPU memory. They speak the same contract, so pointing `base_url` at one of them on a bigger machine needs no code change. See `docs/system-one-benchmark.md` for measured numbers.

## Usage

```python
from swarmqa.llm.settings import LLMSettings, create_provider
from swarmqa.llm.usage import UsageMeter
from swarmqa.spend import SpendMeter

meter = UsageMeter(spend=SpendMeter(cap=5.0))   # optional; forwards cost to the campaign meter
provider = create_provider(LLMSettings(provider="anthropic", max_cost=2.0), meter=meter)
decision = provider.decide_step(obs, "Change the account email", history)
```

`LLMSettings.from_mapping(table)` reads a TOML-style table with the same field names. The campaign config's `[llm]` table is passed straight through: `enabled = true` turns the provider on for the agent explorer (`explorer.engine = "agent"`), and the other keys are these settings.

## Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `provider` | `anthropic` | `anthropic`, `openai`, `claude_cli`, `codex_cli`, `chat`, `system_one` or `fake` |
| `step_model`, `judge_model`, `flows_model`, `computer_use_model` | provider default | Model per call kind |
| `api_key_env` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Env var holding the key. If it is unset, the SDK's own credential lookup still runs (for example an `ant auth login` profile). |
| `base_url` | SDK default | For proxies or gateways |
| `timeout_s` | none | Upper limit on any one call. The smaller of this and the method's `timeout_s` applies. |
| `max_retries` | 2 | Retries after transient errors (connection errors, 408, 409, 429, 5xx) and timeouts |
| `max_calls`, `max_cost` | none | Caps on this provider's calls and spend. Past a cap, calls raise `ModelBudgetExceeded`. |
| `currency` | `USD` | Currency written to `Usage.currency`. It should match the price table. |
| `prices` | built in | `{model: ModelPrice(input, output, cache_read, cache_write)}` per million tokens. Adds to or overrides the defaults. |
| `effort` | step `low`, judge `medium`, flows `high`, computer use `medium` | Reasoning effort per call kind. It is not sent to models that do not accept it. |
| `max_tokens` | step 4096, judge 16000, flows 16000, computer use 8000 | Output cap per call kind. With thinking on, thinking tokens count against it. |
| `max_image_edge` | 1568 | Screenshots with a longer edge are downscaled before sending |
| `max_prompt_chars` | 200000 | Diff and file text for `propose_flows` is cut at this length, with a visible `[truncated: …]` marker |
| `history_limit` | 12 | How many recent steps `decide_step` sees |
| `step_screenshot` | false | Also send the screenshot to `decide_step` (costs more; the tree alone is usually enough) |
| `anthropic_fallbacks` | true | Opt Opus 5.5-class requests into server-side refusal fallback |
| `system_one` | `{}` | The `[llm.system_one]` table (see "System One") |

## Default models

Anthropic, taken from the model guidance current when this was written:

- **Steps: `claude-haiku-4-5`.** Step decisions are frequent, small and checkable, so the plan calls for a fast, cheap model. Haiku takes structured outputs and images, but not `effort`, so no effort is sent to it.
- **Judgments, flow proposals and computer use: `claude-opus-5-5`.** This is the current Opus and Anthropic's default recommendation, at $4 / $20 per million tokens. Accuracy matters most for these calls. Thinking is always on for this model; `effort` controls how deep it goes.

To trade quality against cost, set `effort` or pick a different model per call kind. For example, `judge_model = "claude-sonnet-5-5"` costs half as much.

OpenAI: `gpt-5.4-mini` for steps, and `gpt-5.5` for everything else. **Check these names against OpenAI's current model list before relying on them.** They were chosen from the model names in the SDK and have not been tested against the live API.

## How a call works

1. `BaseProvider` (`swarmqa/llm/base.py`) builds a provider-neutral `ModelCall`. It holds the static system prompt from `swarmqa/llm/prompts/*.md` (shipped as package data), the user text, an optional screenshot, and the JSON schema from `swarmqa/llm/schemas.py`.
2. The call and spend caps are checked. The spend check adds a rough estimate of the input cost of the next call.
3. The adapter turns the call into its wire request:
   - **Anthropic** (Messages API): structured output through `output_config.format` (JSON schema). Forced tool use is not used because Opus 5.5 rejects it. The system prompt is sent as a text block with `cache_control: ephemeral`, so repeated calls of the same kind can read it from the prompt cache once it is long enough (the minimum length depends on the model). The image goes in as a base64 PNG block before the text. `output_config.effort` is sent except to Haiku. For Opus 5 and 5.5, Fable 5.x and Sonnet 5.5, the request sends `fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta, so a classifier refusal is retried on Anthropic's recommended fallback model instead of failing.
   - **OpenAI** (Responses API): `text.format` with a strict JSON schema. The system prompt goes in `instructions`. The image is sent as an `input_image` data URL. `reasoning.effort` is sent to `gpt-5*` and `o*` models. `store: false` is set. OpenAI caches long shared prefixes automatically.
4. The transport sends the request. Timeouts and transient errors are retried with exponential backoff (0.5 s doubling, plus jitter, honouring `retry-after`), all inside the call's `timeout_s` deadline. The SDKs' own retries are turned off.
5. The adapter reads the response into a `Completion`:
   - Refusals (`stop_reason: "refusal"`, or an OpenAI `refusal` part), truncated output (`max_tokens`, `incomplete`) and missing text raise `ModelRefused`. The tokens are still counted.
   - `parse.py` validates the JSON: action kinds, required fields per kind, element ids, confidence in 0..1, and enums. Anything wrong raises `ModelRefused`.
6. Every result carries a `Usage` with tokens, cost, currency and latency. The provider's `UsageMeter` also records it.

### Accessibility tree and element ids

`serialize.tree_text` numbers elements depth-first (`e1`, `e2`, …) on compact lines such as `e6 button "Save" id=save_btn frame=20,700,353,44`. The model answers with an `element_id`, which the provider maps back to an `ElementQuery(role, label, identifier)`. This avoids asking the model to invent queries. The tree is capped at 400 elements, and a visible note says how many were left out.

### Screenshots and coordinates

`images.prepare_image` downscales screenshots whose long edge is over `max_image_edge`, using Pillow's LANCZOS filter, and re-encodes non-PNG input as PNG. An iPhone screenshot of 1179×2556 pixels is sent as 723×1568, which is about 1.5k image tokens. The model gives coordinates and bounding boxes in the pixels of the image it saw. `PreparedImage.to_points` maps them back to driver points using the original size and `ScreenObservation.scale`. `decide_step` works in points directly, from the tree frames.

### Judgments

`judge_screen` uses the `judge_visual` or `judge_confusion` prompt. A non-empty `Rubric.instructions` replaces the prompt. Issues with confidence below `rubric.min_confidence` are dropped. An `element_id` the tree does not contain is ignored rather than refused.

### Computer use

`computer_use` asks for one structured coordinate action (`tap_point`, `swipe`, `type`, `key`, `done`, `give_up`) from the screenshot. It does not run Anthropic's computer-use toolset. That toolset is a multi-turn loop in which the client answers each tool call with a fresh screenshot, and on Opus 5.5 it is only available as `computer_toolset_20260801`. The SwarmQA agent loop already re-observes the screen between steps, so one action per call fits the contract. The toolset could be added later as another mode if single actions are not accurate enough.

## Cost

`pricing.py` holds prices per million tokens. Anthropic defaults are first-party API rates, with cache writes at 1.25× input and cache reads at the model's rate (for example $0.20 on Opus 5.5). `Usage.input_tokens` counts uncached input plus cache writes. `Usage.cache_read_tokens` counts cache reads. Cost prices each part at its own rate.

No OpenAI prices ship, because they could not be checked when this was written. Set `prices` for the OpenAI models you use. A model with no price costs 0 and triggers one warning, and `max_cost` cannot see its spend.

`UsageMeter(spend=SpendMeter(...))` forwards each call's cost to the campaign spend meter. One meter can be shared by several providers, and caps are checked against the shared totals.

## Testing: record and replay

Tests never touch the network and need neither SDK. Providers take a `Transport` (`transport.py`) with a single method, `send(request, *, timeout_s) -> dict`:

- `ReplayTransport([...])` returns queued response dicts in order, or raises a queued exception (`TransportTimeout`, `TransientError`, `ModelError`). It keeps each request so tests can check it.
- `RecordingTransport(live, directory)` wraps a live SDK transport and saves each response as `NNN.json`, with the request beside it and image data removed. Use it to refresh fixtures against the real API.

The fixtures in `tests/fixtures/llm/{anthropic,openai}/` are hand-written in the shape of real API responses. Before the first live benchmark run, record real ones with `RecordingTransport` and compare them with these.

## Not yet verified

- No live API calls have been made. Request shapes were checked against the installed `anthropic` 1.9 and `openai` 3.20 SDKs using a mock HTTP layer, but not against the live services.
- OpenAI default model names and all OpenAI prices.
- Whether the system prompts are long enough to hit Anthropic's minimum cacheable prefix. Haiku 4.5 needs 4096 tokens, so its step prompt will not cache as written.
