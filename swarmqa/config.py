"""C1 — load, override, and validate campaign configuration.

`load_config` reads `aqa.config.toml`, applies `CliOverrides`, and returns a
`CampaignConfig`. `validate_config` reports field-level problems. See
docs/CONTRACTS.md section C1 and docs/config.md.
"""

from __future__ import annotations

import math
import sys
import tomllib
from pathlib import Path
from typing import Any

from swarmqa.errors import ConfigError
from swarmqa.models import CampaignConfig, CliOverrides
from swarmqa.util import parse_duration

_BACKENDS = ("local", "vm", "cloud")
_VIDEO_MODES = ("always", "on_failure", "exploratory_only")
_MATURITIES = ("prototype", "shipped")
_PLATFORMS = ("macos", "ios")
_SHARD_STRATEGIES = ("intent", "suite", "exploratory_seed")
_OVERRUN = ("drain", "cancel")
_FAIL_ON = ("scripted", "any", "never")
_ISOLATION = ("thread", "subprocess")
_DRIVER_KINDS = ("auto", "fake", "legacy", "runner")
_ENGINES = ("legacy", "agent")
_STEP_FAILURE = ("stop", "continue")
_DECISION_MODES = ("heuristic", "system_one", "computer_use", "cascade")
_SYSTEM_ONE_PROVIDERS = ("http", "fake")
_COMPUTER_USE_PROVIDERS = ("command", "fake")
_VISUAL_JUDGMENT_PROVIDERS = ("command", "fake")
_FRICTION_COMPARE = ("gold", "prior_p50")

_NOT_FOUND = "config: not found"
_INVALID_TOML = "config: invalid toml"
_PR_DEPRECATED = (
    "WARNING: config: the [pr] table is deprecated and ignored. The built-in PR loop "
    "was removed; coding agents open fix PRs through `aqa mcp` (see docs/agents.md). "
    "Delete [pr] from your config to silence this warning."
)


DEFAULT_CONFIG = "aqa.config.toml"


def load_config_or_defaults(path: str | Path | None) -> CampaignConfig:
    """Load `path`, or defaults when no path was given and `aqa.config.toml` is absent.

    An explicit path that does not exist raises `config: not found`, so a
    mistyped `--config` never runs silently on defaults.
    """
    if path is None:
        default = Path(DEFAULT_CONFIG)
        if not default.is_file():
            return CampaignConfig()
        path = default
    return load_config(Path(path))


def load_config(path: Path | None, overrides: CliOverrides | None = None) -> CampaignConfig:
    """Read TOML, apply CLI overrides, validate, and return CampaignConfig.

    Raise ConfigError with field-level messages. A missing file is
    `config: not found`. `backend = cloud` with missing or non-positive
    max_spend fails fast. Local backends that set max_spend still load, and
    the returned config keeps the value so the orchestrator can attach the
    ignore note.
    """
    document = _read_toml(path)
    config = CampaignConfig()
    errors: list[str] = []
    _apply_document(config, document, errors)
    if overrides is not None:
        _apply_overrides(config, overrides, errors)
    errors.extend(validate_config(config))
    if errors:
        raise ConfigError(errors)
    return config


def validate_config(config: CampaignConfig) -> list[str]:
    """Return field-level errors. An empty list means the config is usable."""
    errors: list[str] = []
    _enum(errors, "backend", config.backend, _BACKENDS)
    _int_at_least(errors, "workers", config.workers, 1)
    _enum(errors, "video.mode", config.video.mode, _VIDEO_MODES)
    _enum(errors, "app.maturity", config.app.maturity, _MATURITIES)
    _enum(errors, "app.platform", config.app.platform, _PLATFORMS)
    _number_between(errors, "visual.threshold", config.visual.threshold, 0, 1)
    _validate_visual_judgment(errors, config)
    _validate_llm(errors, config)
    _enum(errors, "explorer.engine", config.explorer.engine, _ENGINES)
    _validate_checks(errors, config)
    _validate_build(errors, config)
    _nonempty_str(errors, "spend.currency", config.spend.currency)
    _check_max_spend(errors, config)
    _int_at_least(errors, "explorer.max_steps", config.explorer.max_steps, 1)
    _number_above(errors, "explorer.max_time_s", config.explorer.max_time_s, 0)

    _enum(errors, "shard_strategy", config.shard_strategy, _SHARD_STRATEGIES)
    _enum(errors, "budgets.on_budget", config.budgets.on_budget, _OVERRUN)
    _enum(errors, "fail_on", config.fail_on, _FAIL_ON)
    _enum(errors, "local.isolation", config.local.isolation, _ISOLATION)
    _enum(errors, "driver.kind", config.driver.kind, _DRIVER_KINDS)
    _enum(errors, "spend.overrun", config.spend.overrun, _OVERRUN)
    _enum(errors, "explorer.on_step_failure", config.explorer.on_step_failure, _STEP_FAILURE)
    _validate_decision(errors, config)
    _validate_friction(errors, config)

    _optional_seconds(errors, "budgets.max_wall_time_s", config.budgets.max_wall_time_s)
    if config.budgets.max_worker_minutes is not None and not _is_number_at_least(
        config.budgets.max_worker_minutes, 0
    ):
        errors.append("budgets.max_worker_minutes: must be >= 0")
    _int_at_least(errors, "gui_worker_warn_threshold", config.gui_worker_warn_threshold, 0)
    _nonempty_str(errors, "report_root", config.report_root)

    _string_list(errors, "intents", config.intents, allow_blank=False)
    _string_list(errors, "app.launch_args", config.app.launch_args, allow_blank=True)
    _string_list(errors, "app.simulators", config.app.simulators, allow_blank=False)
    _string_table(errors, "app.env", config.app.env)
    for field, value in (
        ("app.path", config.app.path),
        ("app.build_command", config.app.build_command),
        ("app.bundle_id", config.app.bundle_id),
        ("app.simulator", config.app.simulator),
        ("vm.image", config.vm.image),
        ("cloud.endpoint_env", config.cloud.endpoint_env),
        ("cloud.token_env", config.cloud.token_env),
        ("issues.template", config.issues.template),
        ("issues.github_repo", config.issues.github_repo),
        ("issues.linear_team", config.issues.linear_team),
        ("suite.command", config.suite.command),
    ):
        _optional_str(errors, field, value)
    for field, value in (
        ("vm.provider", config.vm.provider),
        ("vm.tart_bin", config.vm.tart_bin),
        ("cloud.adapter", config.cloud.adapter),
        ("visual.baseline_dir", config.visual.baseline_dir),
        ("issues.github_token_env", config.issues.github_token_env),
        ("issues.linear_api_key_env", config.issues.linear_api_key_env),
    ):
        _nonempty_str(errors, field, value)

    _bool_field(errors, "vm.recycle", config.vm.recycle)
    _bool_field(errors, "issues.github", config.issues.github)
    _bool_field(errors, "issues.linear", config.issues.linear)
    _bool_field(errors, "visual.enabled", config.visual.enabled)
    _bool_field(errors, "coverage.scripted", config.coverage.scripted)
    _bool_field(errors, "coverage.exploratory", config.coverage.exploratory)
    _bool_field(errors, "coverage.visual", config.coverage.visual)
    _number_at_least(errors, "vm.cost_per_worker_minute", config.vm.cost_per_worker_minute, 0)
    _number_at_least(errors, "cloud.cost_per_worker_minute", config.cloud.cost_per_worker_minute, 0)
    _number_above(errors, "cloud.estimated_shard_minutes", config.cloud.estimated_shard_minutes, 0)
    return errors


def _read_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        raise ConfigError([_NOT_FOUND])
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError([_NOT_FOUND])
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError([_INVALID_TOML]) from None
    except OSError:
        raise ConfigError([_NOT_FOUND]) from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError([_INVALID_TOML]) from None
    if not isinstance(document, dict):
        raise ConfigError([_INVALID_TOML])
    return document


def _apply_document(config: CampaignConfig, document: dict[str, Any], errors: list[str]) -> None:
    if "intents" in document:
        config.intents = _copied(document["intents"])
    app = _section(document, "app", errors)
    _assign(app, "path", config.app, "path")
    _assign(app, "build_command", config.app, "build_command")
    _assign(app, "bundle_id", config.app, "bundle_id")
    _assign(app, "launch_args", config.app, "launch_args")
    _assign(app, "env", config.app, "env")
    _assign(app, "maturity", config.app, "maturity")
    _assign(app, "platform", config.app, "platform")
    _assign(app, "simulator", config.app, "simulator")
    _assign(app, "simulators", config.app, "simulators")
    _assign(app, "source_dir", config.app, "source_dir")

    campaign = _section(document, "campaign", errors)
    _assign(campaign, "backend", config, "backend")
    _assign(campaign, "workers", config, "workers")
    _assign(campaign, "shard_strategy", config, "shard_strategy")
    _assign(campaign, "fail_on", config, "fail_on")
    _assign(campaign, "report_root", config, "report_root")
    _assign(campaign, "gui_worker_warn_threshold", config, "gui_worker_warn_threshold")
    _assign(campaign, "on_budget", config.budgets, "on_budget")
    _assign(campaign, "max_worker_minutes", config.budgets, "max_worker_minutes")
    _apply_duration(
        campaign,
        "max_wall_time",
        config.budgets,
        "max_wall_time_s",
        "budgets.max_wall_time_s",
        errors,
    )

    local = _section(document, "local", errors)
    _assign(local, "isolation", config.local, "isolation")

    driver = _section(document, "driver", errors)
    _assign(driver, "kind", config.driver, "kind")

    config.checks.settings = dict(_section(document, "checks", errors))
    config.build.settings = dict(_section(document, "build", errors))

    llm = dict(_section(document, "llm", errors))
    if "enabled" in llm:
        config.llm.enabled = llm.pop("enabled")
    config.llm.settings = llm

    vm = _section(document, "vm", errors)
    _assign(vm, "provider", config.vm, "provider")
    _assign(vm, "image", config.vm, "image")
    _assign(vm, "tart_bin", config.vm, "tart_bin")
    _assign(vm, "recycle", config.vm, "recycle")
    _assign(vm, "cost_per_worker_minute", config.vm, "cost_per_worker_minute")

    cloud = _section(document, "cloud", errors)
    _assign(cloud, "adapter", config.cloud, "adapter")
    _assign(cloud, "cost_per_worker_minute", config.cloud, "cost_per_worker_minute")
    _assign(cloud, "estimated_shard_minutes", config.cloud, "estimated_shard_minutes")
    _assign(cloud, "endpoint_env", config.cloud, "endpoint_env")
    _assign(cloud, "token_env", config.cloud, "token_env")

    spend = _section(document, "spend", errors)
    _assign(spend, "max_spend", config.spend, "max_spend")
    _assign(spend, "currency", config.spend, "currency")
    _assign(spend, "overrun", config.spend, "overrun")

    video = _section(document, "video", errors)
    _assign(video, "mode", config.video, "mode")

    if "pr" in document:
        # The PR loop was removed: coding agents open PRs over MCP now.
        print(_PR_DEPRECATED, file=sys.stderr)

    issues = _section(document, "issues", errors)
    _assign(issues, "github", config.issues, "github")
    _assign(issues, "linear", config.issues, "linear")
    _assign(issues, "template", config.issues, "template")
    _assign(issues, "github_repo", config.issues, "github_repo")
    _assign(issues, "github_token_env", config.issues, "github_token_env")
    _assign(issues, "linear_api_key_env", config.issues, "linear_api_key_env")
    _assign(issues, "linear_team", config.issues, "linear_team")

    visual = _section(document, "visual", errors)
    _assign(visual, "enabled", config.visual, "enabled")
    _assign(visual, "baseline_dir", config.visual, "baseline_dir")
    _assign(visual, "threshold", config.visual, "threshold")
    _apply_visual_judgment(visual, config, errors)

    coverage = _section(document, "coverage", errors)
    _assign(coverage, "scripted", config.coverage, "scripted")
    _assign(coverage, "exploratory", config.coverage, "exploratory")
    _assign(coverage, "visual", config.coverage, "visual")

    explorer = _section(document, "explorer", errors)
    _assign(explorer, "engine", config.explorer, "engine")
    _assign(explorer, "max_steps", config.explorer, "max_steps")
    _assign(explorer, "max_time_s", config.explorer, "max_time_s")
    _assign(explorer, "on_step_failure", config.explorer, "on_step_failure")
    _apply_decision(explorer, config, errors)
    _apply_friction(explorer, config, errors)

    suite = _section(document, "suite", errors)
    _assign(suite, "command", config.suite, "command")


def _apply_visual_judgment(
    visual: dict[str, Any], config: CampaignConfig, errors: list[str]
) -> None:
    judgment = _section(visual, "judgment", errors, path="visual.judgment")
    _assign(judgment, "enabled", config.visual.judgment, "enabled")
    if "provider" in judgment:
        _assign(judgment, "provider", config.visual.judgment, "provider")
    elif "mode" in judgment:
        _assign(judgment, "mode", config.visual.judgment, "provider")
    _assign(judgment, "command_env", config.visual.judgment, "command_env")
    _assign(judgment, "command", config.visual.judgment, "command")
    _assign(judgment, "judgment", config.visual.judgment, "judgment")
    _assign(judgment, "timeout_s", config.visual.judgment, "timeout_s")


def _validate_visual_judgment(errors: list[str], config: CampaignConfig) -> None:
    judgment = config.visual.judgment
    _bool_field(errors, "visual.judgment.enabled", judgment.enabled)
    _enum(
        errors,
        "visual.judgment.provider",
        judgment.provider,
        _VISUAL_JUDGMENT_PROVIDERS,
    )
    _nonempty_str(errors, "visual.judgment.command_env", judgment.command_env)
    _optional_str(errors, "visual.judgment.command", judgment.command)
    _optional_str(errors, "visual.judgment.judgment", judgment.judgment)
    _number_above(errors, "visual.judgment.timeout_s", judgment.timeout_s, 0)


def _apply_decision(
    explorer: dict[str, Any], config: CampaignConfig, errors: list[str]
) -> None:
    decision = _section(explorer, "decision", errors, path="explorer.decision")
    _assign(decision, "mode", config.explorer.decision, "mode")
    _assign(decision, "escalate_after", config.explorer.decision, "escalate_after")
    _assign(decision, "max_model_calls", config.explorer.decision, "max_model_calls")
    _assign(decision, "model_timeout_s", config.explorer.decision, "model_timeout_s")
    _assign(decision, "cache_observations", config.explorer.decision, "cache_observations")

    system_one = _section(
        decision, "system_one", errors, path="explorer.decision.system_one"
    )
    _assign(system_one, "provider", config.explorer.decision.system_one, "provider")
    _assign(system_one, "endpoint_env", config.explorer.decision.system_one, "endpoint_env")
    _assign(system_one, "api_key_env", config.explorer.decision.system_one, "api_key_env")
    _assign(
        system_one, "min_confidence", config.explorer.decision.system_one, "min_confidence"
    )
    _assign(
        system_one,
        "include_tree_depth",
        config.explorer.decision.system_one,
        "include_tree_depth",
    )

    computer_use = _section(
        decision, "computer_use", errors, path="explorer.decision.computer_use"
    )
    _assign(computer_use, "provider", config.explorer.decision.computer_use, "provider")
    _assign(
        computer_use, "command_env", config.explorer.decision.computer_use, "command_env"
    )
    _assign(computer_use, "max_calls", config.explorer.decision.computer_use, "max_calls")
    _assign(
        computer_use,
        "include_a11y_hint",
        config.explorer.decision.computer_use,
        "include_a11y_hint",
    )


def _apply_friction(
    explorer: dict[str, Any], config: CampaignConfig, errors: list[str]
) -> None:
    friction = _section(explorer, "friction", errors, path="explorer.friction")
    _assign(friction, "enabled", config.explorer.friction, "enabled")
    _assign(friction, "emit_threshold", config.explorer.friction, "emit_threshold")
    _assign(friction, "min_extra_steps", config.explorer.friction, "min_extra_steps")
    _assign(friction, "min_backtrack_rate", config.explorer.friction, "min_backtrack_rate")
    _assign(friction, "personas", config.explorer.friction, "personas")
    _assign(friction, "fail_ci", config.explorer.friction, "fail_ci")
    _assign(friction, "compare_to", config.explorer.friction, "compare_to")
    _assign(friction, "klm", config.explorer.friction, "klm")
    if "allow_step_ratio" in friction:
        raw = friction["allow_step_ratio"]
        if isinstance(raw, dict):
            # Coerce numeric values to float; validation catches bad shapes.
            coerced: dict[str, float] = {}
            for key, item in raw.items():
                if isinstance(key, str) and isinstance(item, (int, float)) and type(item) is not bool:
                    coerced[key] = float(item)
                else:
                    coerced[key] = item  # type: ignore[assignment]
            config.explorer.friction.allow_step_ratio = coerced
        else:
            config.explorer.friction.allow_step_ratio = raw  # type: ignore[assignment]


def _validate_friction(errors: list[str], config: CampaignConfig) -> None:
    friction = config.explorer.friction
    _bool_field(errors, "explorer.friction.enabled", friction.enabled)
    _int_at_least(errors, "explorer.friction.emit_threshold", friction.emit_threshold, 0)
    if type(friction.emit_threshold) is int and friction.emit_threshold > 100:
        errors.append("explorer.friction.emit_threshold: must be an integer <= 100")
    _int_at_least(errors, "explorer.friction.min_extra_steps", friction.min_extra_steps, 0)
    _number_between(
        errors,
        "explorer.friction.min_backtrack_rate",
        friction.min_backtrack_rate,
        0,
        1,
    )
    _string_list(errors, "explorer.friction.personas", friction.personas, allow_blank=False)
    _bool_field(errors, "explorer.friction.fail_ci", friction.fail_ci)
    _enum(errors, "explorer.friction.compare_to", friction.compare_to, _FRICTION_COMPARE)
    _bool_field(errors, "explorer.friction.klm", friction.klm)
    _float_table(errors, "explorer.friction.allow_step_ratio", friction.allow_step_ratio)


def _validate_decision(errors: list[str], config: CampaignConfig) -> None:
    decision = config.explorer.decision
    _enum(errors, "explorer.decision.mode", decision.mode, _DECISION_MODES)
    _int_at_least(errors, "explorer.decision.escalate_after", decision.escalate_after, 1)
    _int_at_least(errors, "explorer.decision.max_model_calls", decision.max_model_calls, 0)
    _number_above(errors, "explorer.decision.model_timeout_s", decision.model_timeout_s, 0)
    _bool_field(
        errors, "explorer.decision.cache_observations", decision.cache_observations
    )

    system_one = decision.system_one
    _enum(
        errors,
        "explorer.decision.system_one.provider",
        system_one.provider,
        _SYSTEM_ONE_PROVIDERS,
    )
    _number_between(
        errors,
        "explorer.decision.system_one.min_confidence",
        system_one.min_confidence,
        0,
        1,
    )
    _int_at_least(
        errors,
        "explorer.decision.system_one.include_tree_depth",
        system_one.include_tree_depth,
        1,
    )
    _nonempty_str(
        errors, "explorer.decision.system_one.endpoint_env", system_one.endpoint_env
    )
    _nonempty_str(
        errors, "explorer.decision.system_one.api_key_env", system_one.api_key_env
    )

    computer_use = decision.computer_use
    _enum(
        errors,
        "explorer.decision.computer_use.provider",
        computer_use.provider,
        _COMPUTER_USE_PROVIDERS,
    )
    _int_at_least(
        errors, "explorer.decision.computer_use.max_calls", computer_use.max_calls, 0
    )
    _bool_field(
        errors,
        "explorer.decision.computer_use.include_a11y_hint",
        computer_use.include_a11y_hint,
    )
    _nonempty_str(
        errors,
        "explorer.decision.computer_use.command_env",
        computer_use.command_env,
    )


def _apply_overrides(config: CampaignConfig, overrides: CliOverrides, errors: list[str]) -> None:
    if overrides.app is not None:
        config.app.path = overrides.app
    if overrides.intent is not None:
        config.intents = _copied(overrides.intent)
    if overrides.backend is not None:
        config.backend = overrides.backend  # type: ignore[assignment]
    if overrides.workers is not None:
        config.workers = overrides.workers
    if overrides.max_wall_time is not None:
        _drop_field(errors, "budgets.max_wall_time_s")
        _set_duration(
            overrides.max_wall_time,
            config.budgets,
            "max_wall_time_s",
            "budgets.max_wall_time_s",
            errors,
        )
    if overrides.max_spend is not None:
        config.spend.max_spend = overrides.max_spend
    if overrides.spend_currency is not None:
        config.spend.currency = overrides.spend_currency
    if overrides.video_mode is not None:
        config.video.mode = overrides.video_mode  # type: ignore[assignment]


_LLM_PROVIDERS = ("anthropic", "openai", "fake")


def llm_settings(config: CampaignConfig):
    """The `[llm]` table as `LLMSettings`. Raises ValueError or TypeError on bad keys."""
    from swarmqa.llm.settings import LLMSettings

    return LLMSettings.from_mapping(config.llm.settings)


def _validate_llm(errors: list[str], config: CampaignConfig) -> None:
    if not isinstance(config.llm.enabled, bool):
        errors.append("llm.enabled: must be true or false")
    provider = config.llm.settings.get("provider", "anthropic")
    if provider not in _LLM_PROVIDERS:
        errors.append(f"llm.provider: must be one of {', '.join(_LLM_PROVIDERS)}")
        return
    try:
        llm_settings(config)
    except (TypeError, ValueError) as exc:
        errors.append(f"llm: {exc}")


def _validate_build(errors: list[str], config: CampaignConfig) -> None:
    from swarmqa.build.settings import BuildSettings

    try:
        BuildSettings.from_mapping(config.build.settings)
    except ConfigError as exc:
        errors.extend(exc.errors)


def _validate_checks(errors: list[str], config: CampaignConfig) -> None:
    from swarmqa.checks.config import checks_settings

    try:
        checks_settings(config)
    except (TypeError, ValueError) as exc:
        errors.append(f"checks: {exc}")


def _section(
    document: dict[str, Any],
    name: str,
    errors: list[str],
    *,
    path: str | None = None,
) -> dict[str, Any]:
    if name not in document:
        return {}
    value = document[name]
    label = path or name
    if not isinstance(value, dict):
        errors.append(f"{label}: must be a table")
        return {}
    return value


def _assign(table: dict[str, Any], key: str, target: object, attr: str) -> None:
    if key not in table:
        return
    setattr(target, attr, _copied(table[key]))


def _copied(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _apply_duration(
    table: dict[str, Any],
    key: str,
    target: object,
    attr: str,
    field: str,
    errors: list[str],
) -> None:
    if key not in table:
        return
    _set_duration(table[key], target, attr, field, errors)


def _set_duration(value: object, target: object, attr: str, field: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{field}: must be a duration string")
        return
    try:
        seconds = parse_duration(value)
    except ValueError as exc:
        errors.append(f"{field}: {exc}")
        return
    setattr(target, attr, seconds)


def _drop_field(errors: list[str], field: str) -> None:
    prefix = f"{field}:"
    errors[:] = [item for item in errors if not item.startswith(prefix)]


def _check_max_spend(errors: list[str], config: CampaignConfig) -> None:
    spend = config.spend.max_spend
    if config.backend == "cloud":
        if not _is_number(spend) or spend <= 0:
            errors.append("spend.max_spend: required and must be > 0 when backend is cloud")
        return
    if spend is not None and not _is_number(spend):
        errors.append("spend.max_spend: must be a number")


def _enum(errors: list[str], field: str, value: object, choices: tuple[str, ...]) -> None:
    if value not in choices:
        errors.append(f"{field}: must be one of {', '.join(choices)}")


def _int_at_least(errors: list[str], field: str, value: object, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        errors.append(f"{field}: must be an integer >= {minimum}")


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _is_number_at_least(value: object, minimum: float) -> bool:
    return _is_number(value) and value >= minimum  # type: ignore[operator]


def _number_between(errors: list[str], field: str, value: object, low: float, high: float) -> None:
    if not _is_number(value) or value < low or value > high:  # type: ignore[operator]
        errors.append(f"{field}: must be between {low} and {high} inclusive")


def _number_above(errors: list[str], field: str, value: object, minimum: float) -> None:
    if not _is_number(value) or value <= minimum:  # type: ignore[operator]
        errors.append(f"{field}: must be > {minimum}")


def _number_at_least(errors: list[str], field: str, value: object, minimum: float) -> None:
    if not _is_number_at_least(value, minimum):
        errors.append(f"{field}: must be >= {minimum}")


def _optional_seconds(errors: list[str], field: str, value: object) -> None:
    if value is not None and not _is_number_at_least(value, 0):
        errors.append(f"{field}: must be >= 0")


def _nonempty_str(errors: list[str], field: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: must be a non-empty string")


def _optional_str(errors: list[str], field: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        errors.append(f"{field}: must be a string")


def _bool_field(errors: list[str], field: str, value: object) -> None:
    if type(value) is not bool:
        errors.append(f"{field}: must be a boolean")


def _string_list(errors: list[str], field: str, value: object, *, allow_blank: bool) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{field}: must be an array of strings")
        return
    if not allow_blank and any(not item.strip() for item in value):
        errors.append(f"{field}: must be an array of strings")


def _string_table(errors: list[str], field: str, value: object) -> None:
    if isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return
    errors.append(f"{field}: must be a table of strings")


def _float_table(errors: list[str], field: str, value: object) -> None:
    if isinstance(value, dict) and all(
        isinstance(key, str)
        and isinstance(item, (int, float))
        and type(item) is not bool
        for key, item in value.items()
    ):
        return
    errors.append(f"{field}: must be a table of numbers")
