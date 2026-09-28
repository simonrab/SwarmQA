# Decision backends

Exploratory hunting asks a pluggable `DecisionEvaluator` for the next action
(`observe → decide → act`). Build one with `swarmqa.decision.build_evaluator(config)`
from `explorer.decision`, or inject `evaluator=` in tests.

## Modes

| `explorer.decision.mode` | Behavior |
| --- | --- |
| `heuristic` (default) | Classic button → menu → missing/done order. CI / FakeDriver unchanged. |
| `system_one` | Try System One first (escalate threshold 0). Fail-open to heuristic on errors, low confidence, or exhausted `max_model_calls`. |
| `cascade` | Heuristic first; System One when `stall_count >= escalate_after`; computer-use if System One is missing/failed and still stalled. Fail-open to heuristic. |
| `computer_use` | After `stall_count >= escalate_after`, shell-out (or fake) backend. Fail-open to heuristic. |

System One is a typed chooser over a **finite** candidate list (enabled buttons that share goal words, then goal-related menu paths — the same pool the heuristic considers). It does not free-form drive the mouse.

## System One (PR2)

Provider `http` (default) POSTs JSON to the URL in the env var named by
`system_one.endpoint_env` (default `AQA_SYSTEM_ONE_ENDPOINT`). Optional bearer
token from `system_one.api_key_env` (default `AQA_SYSTEM_ONE_API_KEY`). Timeout is
`decision.model_timeout_s`. Stdlib `urllib` only — no vendor SDKs in core.

Request body (shape):

```json
{
  "goal": "Save Help",
  "candidates": [
    {"id": "click:0", "kind": "click", "label": "Save", "query": {"role": "button", "label": "Save", "identifier": null}},
    {"id": "click:1", "kind": "click", "label": "Help", "query": {"role": "button", "label": "Help", "identifier": null}}
  ],
  "tree": [{"role": "window", "label": "Sample", "enabled": true, "children": []}],
  "min_confidence": 0.55
}
```

Expected response:

```json
{"choice_id": "click:1", "confidence": 0.9}
```

Confidence below `min_confidence`, unknown `choice_id`, transport errors, or an
unset endpoint all fail open to the heuristic. Provider `fake` picks the first
candidate (or a scripted `choice_id`) for tests — no network.

When `cache_observations` is true, identical tree fingerprints reuse the prior
System One action and do not consume another model call.

### Privacy

**When System One is enabled** (`mode` is `system_one` or `cascade` with a live
HTTP provider), accessibility **labels, roles, identifiers, and menu path text**
leave the machine in the request body (goal string, candidate list, and a depth-
limited tree summary). Do not point `AQA_SYSTEM_ONE_ENDPOINT` at a third party
unless that egress is approved for the app under test. Heuristic-only mode never
sends UI text off-box.

## Computer use (PR3)

`ComputerUseEvaluator` runs the command named by the env var in
`explorer.decision.computer_use.command_env` (default `AQA_COMPUTER_USE_COMMAND`).

- Final argv token: screenshot path (`Observation.screenshot_path`).
- Optional stdin: a11y hint JSON when `include_a11y_hint = true` (goal, tokens, tree).
- Stdout: one JSON object, e.g. `{ "action": "click", "x": 10, "y": 20 }`,
  `{ "action": "click", "label": "Save" }`, or
  `{ "action": "menu", "path": ["File", "Export"] }`.
- Coordinates map to the nearest `UIElement` by frame-center distance
  (`map_point_to_element`).
- Caps: `computer_use.max_calls` **and** shared `decision.max_model_calls`.
- Timeout: `decision.model_timeout_s`. Stdlib `subprocess` only.
- CI: set `computer_use.provider = "fake"` (see `swarmqa.decision.providers.fake`).
  Default mode stays `heuristic` so no live commands run in CI.

## Cascade fail-open

Every model stage error, timeout, missing env, empty budget, or missing module
returns the heuristic action for that observation. Heuristic mode never calls
model backends.

## Config

See `docs/config.md` for `[explorer.decision]` and `[explorer.decision.system_one]`
defaults and validation. Caps: `escalate_after`, `max_model_calls`,
`model_timeout_s`, `system_one.min_confidence`, `system_one.include_tree_depth`.
