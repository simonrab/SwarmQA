"""C1 — campaign config loading, CLI overrides, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmqa.cli import build_parser, main, overrides_from_namespace
from swarmqa.config import load_config, validate_config
from swarmqa.errors import ConfigError
from swarmqa.models import (
    AppTarget,
    CampaignBudgets,
    CampaignConfig,
    CliOverrides,
    CloudConfig,
    ComputerUseConfig,
    CoverageConfig,
    DecisionConfig,
    ExplorerConfig,
    FrictionConfig,
    IssuesConfig,
    LocalConfig,
    PrConfig,
    SpendConfig,
    SuiteConfig,
    SystemOneConfig,
    VideoConfig,
    VisualConfig,
    VmConfig,
)
from swarmqa.testing import sample_config
from swarmqa.util import parse_duration

TEMPLATE = Path(__file__).resolve().parents[1] / "swarmqa" / "templates" / "aqa.config.toml"

_CLOUD_SPEND = "spend.max_spend: required and must be > 0 when backend is cloud"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "aqa.config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_template_maps_onto_campaign_config():
    config = load_config(TEMPLATE)
    assert config == CampaignConfig(
        intents=["intents/"],
        issues=IssuesConfig(template="templates/issue.md"),
    )
    assert config.workers == 2
    assert config.backend == "local"
    assert config.pr.mode == "off"
    assert config.pr.max_wall_time_s == parse_duration("1h")
    assert config.video.mode == "always"
    assert config.spend.currency == "USD"
    assert config.spend.max_spend is None
    assert config.budgets.max_wall_time_s is None
    assert config.app.maturity == "shipped"
    assert config.gui_worker_warn_threshold == 2
    assert config.explorer.max_steps == 40
    assert config.explorer.max_time_s == 120
    assert config.explorer.decision == DecisionConfig()
    assert config.explorer.decision.mode == "heuristic"
    assert config.explorer.decision.system_one == SystemOneConfig()
    assert config.explorer.decision.computer_use == ComputerUseConfig()
    assert config.explorer.friction == FrictionConfig()
    assert config.explorer.friction.enabled is True
    assert config.explorer.friction.fail_ci is False
    assert config.visual.threshold == 0.01
    assert config.cloud.cost_per_worker_minute == 0.05
    assert validate_config(config) == []
    assert validate_config(sample_config()) == []


def test_empty_file_uses_locked_defaults(tmp_path: Path):
    config = load_config(_write(tmp_path, ""))
    assert config == sample_config()
    assert config.workers == 2
    assert config.pr.mode == "off"
    assert config.video.mode == "always"


def test_full_document_round_trip(tmp_path: Path):
    path = _write(
        tmp_path,
        """
intents = ["flows/smoke.md", "flows/"]

[app]
path = "/tmp/MyApp.app"
build_command = "xcodebuild -scheme MyApp"
bundle_id = "dev.example.myapp"
launch_args = ["--safe"]
maturity = "prototype"

[app.env]
AQA = "1"

[campaign]
backend = "vm"
workers = 3
shard_strategy = "suite"
max_wall_time = "1h30m"
max_worker_minutes = 45
on_budget = "cancel"
fail_on = "any"
report_root = "out/reports"
gui_worker_warn_threshold = 4

[local]
isolation = "subprocess"

[vm]
provider = "tart"
image = "ghcr.io/example/macos:latest"
tart_bin = "/opt/tart"
recycle = false
cost_per_worker_minute = 0.02

[cloud]
adapter = "simulator"
cost_per_worker_minute = 0.08
estimated_shard_minutes = 2.5
endpoint_env = "AQA_CLOUD_ENDPOINT"
token_env = "AQA_CLOUD_TOKEN"

[spend]
max_spend = 25.5
currency = "EUR"
overrun = "cancel"

[video]
mode = "on_failure"

[pr]
mode = "autonomous"
max_iterations = 4
max_wall_time = "90s"
max_pr_updates = 2
fix_command = "echo fix"

[issues]
github = true
linear = true
template = "custom/issue.md"
github_repo = "acme/app"
github_token_env = "GH"
linear_api_key_env = "LIN"
linear_team = "QA"

[visual]
enabled = true
baseline_dir = "shots"
threshold = 0.2

[coverage]
scripted = true
exploratory = false
visual = true

[explorer]
max_steps = 10
max_time_s = 30
on_step_failure = "continue"

[explorer.decision]
mode = "cascade"
escalate_after = 2
max_model_calls = 4
model_timeout_s = 15.0
cache_observations = false

[explorer.decision.system_one]
provider = "fake"
endpoint_env = "S1_URL"
api_key_env = "S1_KEY"
min_confidence = 0.7
include_tree_depth = 2

[explorer.decision.computer_use]
provider = "fake"
command_env = "CU_CMD"
max_calls = 1
include_a11y_hint = false

[explorer.friction]
enabled = true
emit_threshold = 60
min_extra_steps = 4
min_backtrack_rate = 0.2
personas = ["first_time"]
fail_ci = false
compare_to = "prior_p50"
klm = false

[suite]
command = "xcodebuild test -scheme MyApp"
""",
    )
    config = load_config(path)
    assert config == CampaignConfig(
        app=AppTarget(
            path="/tmp/MyApp.app",
            build_command="xcodebuild -scheme MyApp",
            bundle_id="dev.example.myapp",
            launch_args=["--safe"],
            env={"AQA": "1"},
            maturity="prototype",
        ),
        intents=["flows/smoke.md", "flows/"],
        backend="vm",
        workers=3,
        shard_strategy="suite",
        budgets=CampaignBudgets(
            max_wall_time_s=parse_duration("1h30m"),
            max_worker_minutes=45,
            on_budget="cancel",
        ),
        spend=SpendConfig(max_spend=25.5, currency="EUR", overrun="cancel"),
        pr=PrConfig(
            mode="autonomous",
            max_iterations=4,
            max_wall_time_s=parse_duration("90s"),
            max_pr_updates=2,
            fix_command="echo fix",
        ),
        video=VideoConfig(mode="on_failure"),
        issues=IssuesConfig(
            github=True,
            linear=True,
            template="custom/issue.md",
            github_repo="acme/app",
            github_token_env="GH",
            linear_api_key_env="LIN",
            linear_team="QA",
        ),
        visual=VisualConfig(enabled=True, baseline_dir="shots", threshold=0.2),
        explorer=ExplorerConfig(
            max_steps=10,
            max_time_s=30,
            on_step_failure="continue",
            decision=DecisionConfig(
                mode="cascade",
                escalate_after=2,
                max_model_calls=4,
                model_timeout_s=15.0,
                cache_observations=False,
                system_one=SystemOneConfig(
                    provider="fake",
                    endpoint_env="S1_URL",
                    api_key_env="S1_KEY",
                    min_confidence=0.7,
                    include_tree_depth=2,
                ),
                computer_use=ComputerUseConfig(
                    provider="fake",
                    command_env="CU_CMD",
                    max_calls=1,
                    include_a11y_hint=False,
                ),
            ),
            friction=FrictionConfig(
                enabled=True,
                emit_threshold=60,
                min_extra_steps=4,
                min_backtrack_rate=0.2,
                personas=["first_time"],
                fail_ci=False,
                compare_to="prior_p50",
                klm=False,
            ),
        ),
        coverage=CoverageConfig(scripted=True, exploratory=False, visual=True),
        suite=SuiteConfig(command="xcodebuild test -scheme MyApp"),
        cloud=CloudConfig(
            adapter="simulator",
            cost_per_worker_minute=0.08,
            estimated_shard_minutes=2.5,
            endpoint_env="AQA_CLOUD_ENDPOINT",
            token_env="AQA_CLOUD_TOKEN",
        ),
        vm=VmConfig(
            provider="tart",
            image="ghcr.io/example/macos:latest",
            tart_bin="/opt/tart",
            recycle=False,
            cost_per_worker_minute=0.02,
        ),
        local=LocalConfig(isolation="subprocess"),
        fail_on="any",
        report_root="out/reports",
        gui_worker_warn_threshold=4,
    )
    assert config.budgets.max_wall_time_s == 5400
    assert config.pr.max_wall_time_s == 90


def test_intent_file_and_directory_paths_are_kept(tmp_path: Path):
    intent_file = tmp_path / "smoke.md"
    intent_file.write_text("# smoke\n", encoding="utf-8")
    intent_dir = tmp_path / "pack"
    intent_dir.mkdir()
    path = _write(
        tmp_path,
        f'intents = ["{intent_file}", "{intent_dir}"]\n',
    )
    config = load_config(path)
    assert config.intents == [str(intent_file), str(intent_dir)]
    assert intent_file.is_file()
    assert intent_dir.is_dir()


def test_missing_file_and_invalid_toml(tmp_path: Path):
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError) as exc:
        load_config(missing, CliOverrides(workers=4, max_spend=10))
    assert exc.value.errors == ["config: not found"]
    assert str(exc.value) == "config: not found"

    with pytest.raises(ConfigError) as exc_none:
        load_config(None)
    assert exc_none.value.errors == ["config: not found"]

    directory = tmp_path / "cfgdir"
    directory.mkdir()
    with pytest.raises(ConfigError) as exc_dir:
        load_config(directory)
    assert exc_dir.value.errors == ["config: not found"]

    broken = _write(tmp_path, '[campaign\nbackend = "local"\n')
    with pytest.raises(ConfigError) as exc_toml:
        load_config(broken)
    assert exc_toml.value.errors == ["config: invalid toml"]
    assert str(exc_toml.value) == "config: invalid toml"

    garbage = tmp_path / "bad.toml"
    garbage.write_bytes(b"\xff\xfe not toml")
    with pytest.raises(ConfigError) as exc_bytes:
        load_config(garbage)
    assert exc_bytes.value.errors == ["config: invalid toml"]


def test_section_must_be_a_table(tmp_path: Path):
    path = _write(tmp_path, 'campaign = "local"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.errors == ["campaign: must be a table"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: setattr(c, "backend", "docker"), ["backend: must be one of local, vm, cloud"]),
        (lambda c: setattr(c, "workers", 0), ["workers: must be an integer >= 1"]),
        (lambda c: setattr(c, "workers", -3), ["workers: must be an integer >= 1"]),
        (lambda c: setattr(c, "workers", 1.5), ["workers: must be an integer >= 1"]),
        (lambda c: setattr(c, "workers", True), ["workers: must be an integer >= 1"]),
        (lambda c: setattr(c, "workers", "2"), ["workers: must be an integer >= 1"]),
        (
            lambda c: setattr(c.video, "mode", "sometimes"),
            ["video.mode: must be one of always, on_failure, exploratory_only"],
        ),
        (
            lambda c: setattr(c.pr, "mode", "robot"),
            ["pr.mode: must be one of off, human, autonomous"],
        ),
        (
            lambda c: setattr(c.app, "maturity", "beta"),
            ["app.maturity: must be one of prototype, shipped"],
        ),
        (
            lambda c: setattr(c.visual, "threshold", 1.1),
            ["visual.threshold: must be between 0 and 1 inclusive"],
        ),
        (
            lambda c: setattr(c.visual, "threshold", -0.01),
            ["visual.threshold: must be between 0 and 1 inclusive"],
        ),
        (
            lambda c: setattr(c.visual, "threshold", "0.1"),
            ["visual.threshold: must be between 0 and 1 inclusive"],
        ),
        (
            lambda c: setattr(c.spend, "currency", ""),
            ["spend.currency: must be a non-empty string"],
        ),
        (
            lambda c: setattr(c.spend, "currency", "   "),
            ["spend.currency: must be a non-empty string"],
        ),
        (
            lambda c: setattr(c.spend, "currency", 12),
            ["spend.currency: must be a non-empty string"],
        ),
        (lambda c: setattr(c, "backend", "cloud"), [_CLOUD_SPEND]),
        (
            lambda c: (setattr(c, "backend", "cloud"), setattr(c.spend, "max_spend", 0)),
            [_CLOUD_SPEND],
        ),
        (
            lambda c: (setattr(c, "backend", "cloud"), setattr(c.spend, "max_spend", -4)),
            [_CLOUD_SPEND],
        ),
        (
            lambda c: (setattr(c, "backend", "cloud"), setattr(c.spend, "max_spend", True)),
            [_CLOUD_SPEND],
        ),
        (
            lambda c: setattr(c.pr, "max_iterations", 0),
            ["pr.max_iterations: must be an integer >= 1"],
        ),
        (
            lambda c: setattr(c.pr, "max_pr_updates", 0),
            ["pr.max_pr_updates: must be an integer >= 1"],
        ),
        (
            lambda c: setattr(c.explorer, "max_steps", 0),
            ["explorer.max_steps: must be an integer >= 1"],
        ),
        (
            lambda c: setattr(c.explorer, "max_time_s", 0),
            ["explorer.max_time_s: must be > 0"],
        ),
        (
            lambda c: setattr(c.explorer, "max_time_s", -5),
            ["explorer.max_time_s: must be > 0"],
        ),
        (
            lambda c: setattr(c, "shard_strategy", "random"),
            ["shard_strategy: must be one of intent, suite, exploratory_seed"],
        ),
        (
            lambda c: setattr(c.budgets, "on_budget", "pause"),
            ["budgets.on_budget: must be one of drain, cancel"],
        ),
        (
            lambda c: setattr(c, "fail_on", "always"),
            ["fail_on: must be one of scripted, any, never"],
        ),
        (
            lambda c: setattr(c.local, "isolation", "container"),
            ["local.isolation: must be one of thread, subprocess"],
        ),
        (
            lambda c: setattr(c.spend, "overrun", "retry"),
            ["spend.overrun: must be one of drain, cancel"],
        ),
        (
            lambda c: setattr(c.explorer, "on_step_failure", "skip"),
            ["explorer.on_step_failure: must be one of stop, continue"],
        ),
        (
            lambda c: setattr(c.explorer.decision, "mode", "llm"),
            [
                "explorer.decision.mode: must be one of heuristic, system_one, "
                "computer_use, cascade"
            ],
        ),
        (
            lambda c: setattr(c.explorer.decision, "escalate_after", 0),
            ["explorer.decision.escalate_after: must be an integer >= 1"],
        ),
        (
            lambda c: setattr(c.explorer.decision, "max_model_calls", -1),
            ["explorer.decision.max_model_calls: must be an integer >= 0"],
        ),
        (
            lambda c: setattr(c.explorer.decision, "model_timeout_s", 0),
            ["explorer.decision.model_timeout_s: must be > 0"],
        ),
        (
            lambda c: setattr(c.explorer.decision.system_one, "provider", "grpc"),
            ["explorer.decision.system_one.provider: must be one of http, fake"],
        ),
        (
            lambda c: setattr(c.explorer.decision.system_one, "min_confidence", 1.5),
            [
                "explorer.decision.system_one.min_confidence: must be between 0 and 1 "
                "inclusive"
            ],
        ),
        (
            lambda c: setattr(c.explorer.decision.system_one, "include_tree_depth", 0),
            ["explorer.decision.system_one.include_tree_depth: must be an integer >= 1"],
        ),
        (
            lambda c: setattr(c.explorer.decision.computer_use, "provider", "sdk"),
            ["explorer.decision.computer_use.provider: must be one of command, fake"],
        ),
        (
            lambda c: setattr(c.explorer.decision.computer_use, "max_calls", -1),
            ["explorer.decision.computer_use.max_calls: must be an integer >= 0"],
        ),
        (
            lambda c: setattr(c.explorer.friction, "compare_to", "baseline"),
            ["explorer.friction.compare_to: must be one of gold, prior_p50"],
        ),
        (
            lambda c: setattr(c.explorer.friction, "emit_threshold", 101),
            ["explorer.friction.emit_threshold: must be an integer <= 100"],
        ),
        (
            lambda c: setattr(c.explorer.friction, "fail_ci", "yes"),  # type: ignore[arg-type]
            ["explorer.friction.fail_ci: must be a boolean"],
        ),
    ],
)
def test_each_validation_rule(mutate, expected):
    config = sample_config()
    mutate(config)
    assert validate_config(config) == expected


def test_validation_boundaries_and_combined_errors():
    config = sample_config()
    config.workers = 1
    config.visual.threshold = 0
    config.pr.max_iterations = 1
    config.pr.max_pr_updates = 1
    config.explorer.max_steps = 1
    config.explorer.max_time_s = 0.001
    config.backend = "cloud"
    config.spend.max_spend = 0.01
    assert validate_config(config) == []
    config.visual.threshold = 1
    config.spend.max_spend = 10
    assert validate_config(config) == []

    broken = sample_config()
    broken.backend = "docker"
    broken.workers = 0
    broken.video.mode = "sometimes"
    broken.pr.mode = "robot"
    broken.app.maturity = "beta"
    broken.visual.threshold = 2
    broken.spend.currency = ""
    broken.pr.max_iterations = 0
    broken.pr.max_pr_updates = 0
    broken.explorer.max_steps = 0
    broken.explorer.max_time_s = 0
    assert validate_config(broken) == [
        "backend: must be one of local, vm, cloud",
        "workers: must be an integer >= 1",
        "video.mode: must be one of always, on_failure, exploratory_only",
        "pr.mode: must be one of off, human, autonomous",
        "app.maturity: must be one of prototype, shipped",
        "visual.threshold: must be between 0 and 1 inclusive",
        "spend.currency: must be a non-empty string",
        "pr.max_iterations: must be an integer >= 1",
        "pr.max_pr_updates: must be an integer >= 1",
        "explorer.max_steps: must be an integer >= 1",
        "explorer.max_time_s: must be > 0",
    ]


def test_cloud_without_max_spend_fails_fast(tmp_path: Path):
    cases = [
        '[campaign]\nbackend = "cloud"\n',
        '[campaign]\nbackend = "cloud"\n[spend]\nmax_spend = 0\n',
        '[campaign]\nbackend = "cloud"\n[spend]\nmax_spend = -1\n',
        '[campaign]\nbackend = "cloud"\n[spend]\nmax_spend = 0.0\n',
    ]
    for text in cases:
        path = _write(tmp_path, text)
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        assert exc.value.errors == [_CLOUD_SPEND]
        assert str(exc.value) == _CLOUD_SPEND

    allowed = _write(
        tmp_path,
        '[campaign]\nbackend = "cloud"\n[spend]\nmax_spend = 10\ncurrency = "USD"\n',
    )
    config = load_config(allowed)
    assert config.backend == "cloud"
    assert config.spend.max_spend == 10
    assert config.spend.currency == "USD"


def test_local_max_spend_is_kept(tmp_path: Path):
    path = _write(
        tmp_path,
        '[campaign]\nbackend = "local"\n[spend]\nmax_spend = 15.5\ncurrency = "USD"\n',
    )
    config = load_config(path)
    assert config.backend == "local"
    assert config.spend.max_spend == 15.5
    assert validate_config(config) == []

    via_model = sample_config()
    via_model.spend.max_spend = 10
    assert validate_config(via_model) == []
    assert via_model.spend.max_spend == 10


def test_vm_without_max_spend_is_valid(tmp_path: Path):
    path = _write(tmp_path, '[campaign]\nbackend = "vm"\n')
    config = load_config(path)
    assert config.backend == "vm"
    assert config.spend.max_spend is None


def test_bad_duration(tmp_path: Path):
    path = _write(
        tmp_path,
        """
[campaign]
max_wall_time = "soon"
[pr]
max_wall_time = "nope"
""",
    )
    with pytest.raises(ValueError) as soon:
        parse_duration("soon")
    with pytest.raises(ValueError) as nope:
        parse_duration("nope")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.errors == [
        f"budgets.max_wall_time_s: {soon.value}",
        f"pr.max_wall_time_s: {nope.value}",
    ]

    bare = _write(tmp_path, '[campaign]\nmax_wall_time = "30"\n')
    with pytest.raises(ValueError) as bare_error:
        parse_duration("30")
    with pytest.raises(ConfigError) as exc_bare:
        load_config(bare)
    assert exc_bare.value.errors == [f"budgets.max_wall_time_s: {bare_error.value}"]

    numeric = _write(tmp_path, "[campaign]\nmax_wall_time = 90\n")
    with pytest.raises(ConfigError) as exc_numeric:
        load_config(numeric)
    assert exc_numeric.value.errors == ["budgets.max_wall_time_s: must be a duration string"]

    replaced = _write(tmp_path, '[campaign]\nmax_wall_time = "soon"\n')
    config = load_config(replaced, CliOverrides(max_wall_time="5m"))
    assert config.budgets.max_wall_time_s == parse_duration("5m")


def test_ios_platform_and_simulator_pool(tmp_path: Path):
    path = _write(
        tmp_path,
        """
[app]
platform = "ios"
simulator = "iPhone 16"
simulators = ["iPhone 16", "iPhone 16 Pro"]
""",
    )
    config = load_config(path)
    assert config.app.platform == "ios"
    assert config.app.simulator == "iPhone 16"
    assert config.app.simulators == ["iPhone 16", "iPhone 16 Pro"]

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    broken = _write(bad_dir, '[app]\nplatform = "android"\n')
    with pytest.raises(ConfigError) as exc:
        load_config(broken)
    assert exc.value.errors == ["app.platform: must be one of macos, ios"]


def test_unknown_enum_values_from_toml(tmp_path: Path):
    path = _write(
        tmp_path,
        """
[app]
maturity = "beta"

[campaign]
backend = "kubernetes"

[video]
mode = "sometimes"

[pr]
mode = "robot"
""",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.errors == [
        "backend: must be one of local, vm, cloud",
        "video.mode: must be one of always, on_failure, exploratory_only",
        "pr.mode: must be one of off, human, autonomous",
        "app.maturity: must be one of prototype, shipped",
    ]


def test_driver_kind_from_toml(tmp_path: Path):
    assert load_config(_write(tmp_path, "")).driver.kind == "auto"
    config = load_config(_write(tmp_path, "[driver]\nkind = \"fake\"\n"))
    assert config.driver.kind == "fake"
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "[driver]\nkind = \"android\"\n"))
    assert exc.value.errors == ["driver.kind: must be one of auto, fake, legacy, runner"]


def test_driver_kind_selects_local_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import swarmqa.driver as driver_mod
    from swarmqa.backends.local import _default_driver_factory
    from swarmqa.driver.fake import FakeDriver

    monkeypatch.setattr(driver_mod.sys, "platform", "darwin")
    config = CampaignConfig()
    config.driver.kind = "fake"
    driver = _default_driver_factory(config)(config.app, tmp_path)
    assert isinstance(driver, FakeDriver)


def test_pr_mode_off_skips_fix_loop(tmp_path: Path):
    config = load_config(_write(tmp_path, "[pr]\nmode = \"off\"\n"))
    assert config.pr.mode == "off"
    args = build_parser().parse_args(["run", "--pr-mode", "off"])
    assert overrides_from_namespace(args).pr_mode == "off"

def test_cli_overrides_replace_file_values(tmp_path: Path):
    path = _write(
        tmp_path,
        """
intents = ["intents/"]

[app]
bundle_id = "dev.example.keep"
maturity = "prototype"

[campaign]
backend = "local"
workers = 2
max_wall_time = "1h"

[spend]
max_spend = 1
currency = "USD"

[video]
mode = "always"

[pr]
mode = "human"
""",
    )
    overrides = CliOverrides(
        app="/Applications/Demo.app",
        intent=["one.md", "pack/"],
        backend="vm",
        workers=4,
        max_wall_time="2h",
        max_spend=12.5,
        spend_currency="EUR",
        video_mode="exploratory_only",
        pr_mode="autonomous",
    )
    config = load_config(path, overrides)
    assert config.app.path == "/Applications/Demo.app"
    assert config.app.bundle_id == "dev.example.keep"
    assert config.app.maturity == "prototype"
    assert config.intents == ["one.md", "pack/"]
    assert config.backend == "vm"
    assert config.workers == 4
    assert config.budgets.max_wall_time_s == parse_duration("2h")
    assert config.spend.max_spend == 12.5
    assert config.spend.currency == "EUR"
    assert config.video.mode == "exploratory_only"
    assert config.pr.mode == "autonomous"

    partial = load_config(path, CliOverrides(workers=9))
    assert partial.workers == 9
    assert partial.intents == ["intents/"]
    assert partial.backend == "local"
    assert partial.spend.max_spend == 1

    untouched = load_config(path, CliOverrides())
    assert untouched.workers == 2
    assert untouched.intents == ["intents/"]


def test_cli_flags_build_overrides_and_load(tmp_path: Path):
    path = _write(tmp_path, 'intents = ["from-file.md"]\n[campaign]\nbackend = "local"\n')
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--config",
            str(path),
            "--app",
            "/tmp/MyApp.app",
            "--intent",
            "a.md",
            "--intent",
            "b/",
            "--backend",
            "cloud",
            "--workers",
            "6",
            "--max-wall-time",
            "90s",
            "--max-spend",
            "10",
            "--spend-currency",
            "USD",
            "--video-mode",
            "on_failure",
            "--pr-mode",
            "human",
        ]
    )
    overrides = overrides_from_namespace(args)
    config = load_config(Path(args.config), overrides)
    assert config.app.path == "/tmp/MyApp.app"
    assert config.intents == ["a.md", "b/"]
    assert config.backend == "cloud"
    assert config.workers == 6
    assert config.budgets.max_wall_time_s == 90
    assert config.spend.max_spend == 10
    assert config.spend.currency == "USD"
    assert config.video.mode == "on_failure"
    assert config.pr.mode == "human"


def test_cli_run_reports_config_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = _write(tmp_path, '[campaign]\nbackend = "cloud"\n')
    code = main(["run", "--config", str(path)])
    assert code == 2
    captured = capsys.readouterr()
    assert _CLOUD_SPEND in captured.err
    assert captured.out == ""


def test_cloud_cap_can_come_from_the_cli(tmp_path: Path):
    path = _write(tmp_path, '[campaign]\nbackend = "local"\n')
    config = load_config(
        path,
        CliOverrides(backend="cloud", max_spend=10, spend_currency="USD"),
    )
    assert config.backend == "cloud"
    assert config.spend.max_spend == 10

    with pytest.raises(ConfigError) as exc:
        load_config(path, CliOverrides(backend="cloud"))
    assert exc.value.errors == [_CLOUD_SPEND]


def test_duration_error_joins_validation_errors(tmp_path: Path):
    path = _write(
        tmp_path,
        """
[campaign]
backend = "cloud"
workers = 0
max_wall_time = "soon"
""",
    )
    with pytest.raises(ValueError) as soon:
        parse_duration("soon")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert exc.value.errors == [
        f"budgets.max_wall_time_s: {soon.value}",
        "workers: must be an integer >= 1",
        _CLOUD_SPEND,
    ]
    assert str(exc.value) == "\n".join(exc.value.errors)


def test_driver_kind_legacy_follows_platform(tmp_path: Path):
    from swarmqa.backends.local import _default_driver_factory
    from swarmqa.driver.ios import IOSSimulatorDriver
    from swarmqa.driver.macos import MacOSDriver

    config = CampaignConfig()
    config.driver.kind = "legacy"
    factory = _default_driver_factory(config)
    assert isinstance(factory(config.app, tmp_path / "mac"), MacOSDriver)
    config.app.platform = "ios"
    assert isinstance(factory(config.app, tmp_path / "ios"), IOSSimulatorDriver)


def test_llm_table_is_off_by_default_and_validated(tmp_path: Path):
    from swarmqa.config import llm_settings

    config = load_config(_write(tmp_path, ""))
    assert config.llm.enabled is False
    assert llm_settings(config).provider == "anthropic"

    config = load_config(_write(tmp_path, '[llm]\nenabled = true\nprovider = "openai"\nmax_cost = 2.5\n'))
    assert config.llm.enabled is True
    settings = llm_settings(config)
    assert settings.provider == "openai" and settings.max_cost == 2.5

    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, '[llm]\nprovider = "gemini"\n'))
    assert exc.value.errors == ["llm.provider: must be one of anthropic, openai, fake"]
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "[llm]\nmodel_name = 1\n"))
    assert exc.value.errors == ["llm: unknown llm settings: model_name"]
