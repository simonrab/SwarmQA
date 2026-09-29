"""Shared campaign types.

Chunk implementations construct these objects. Extend them in this module only
when every chunk needs the field; otherwise keep chunk-local types local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BackendName = Literal["local", "vm", "cloud"]
PrMode = Literal["off", "human", "autonomous"]
VideoMode = Literal["always", "on_failure", "exploratory_only"]
OverrunPolicy = Literal["drain", "cancel"]
Maturity = Literal["prototype", "shipped"]
ShardStrategy = Literal["intent", "suite", "exploratory_seed"]
ShardKind = Literal["scripted", "exploratory", "suite", "visual"]
ShardStatus = Literal["passed", "failed", "cancelled", "error", "pending", "running"]
Severity = Literal["critical", "high", "medium", "low"]
FindingKind = Literal[
    "crash",
    "timeout",
    "assertion",
    "missing_control",
    "visual",
    "visual_judgment",
    "suite_failure",
    "unresponsive",
    "error_state",
    "launch",
    "friction_path",
]
FindingCategory = Literal["broken", "visual", "confusing", "crash"]
FINDINGS_SCHEMA_VERSION = 2
DecisionMode = Literal["heuristic", "system_one", "computer_use", "cascade"]
SystemOneProvider = Literal["http", "fake"]
ComputerUseProvider = Literal["command", "fake"]
VisualJudgmentProvider = Literal["command", "fake"]
FrictionCompareTo = Literal["gold", "prior_p50"]
ActionType = Literal[
    "click",
    "type",
    "key",
    "scroll",
    "menu",
    "wait",
    "screenshot",
    "assert",
    "launch",
    "relaunch",
]
StepFailurePolicy = Literal["stop", "continue"]
FailOn = Literal["scripted", "any", "never"]
LocalIsolation = Literal["thread", "subprocess"]
TargetPlatform = Literal["macos", "ios"]
DriverKind = Literal["auto", "fake", "legacy", "runner"]


@dataclass
class AppTarget:
    path: str | None = None
    build_command: str | None = None
    bundle_id: str | None = None
    launch_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    maturity: Maturity = "shipped"
    platform: TargetPlatform = "macos"
    simulator: str | None = None
    simulators: list[str] = field(default_factory=list)


@dataclass
class CampaignBudgets:
    max_wall_time_s: float | None = None
    max_worker_minutes: float | None = None
    on_budget: OverrunPolicy = "drain"


@dataclass
class SpendConfig:
    max_spend: float | None = None
    currency: str = "USD"
    overrun: OverrunPolicy = "drain"


@dataclass
class PrConfig:
    mode: PrMode = "off"
    max_iterations: int = 3
    max_wall_time_s: float = 3600
    max_pr_updates: int = 5
    fix_command: str | None = None


@dataclass
class VideoConfig:
    mode: VideoMode = "always"


@dataclass
class IssuesConfig:
    github: bool = False
    linear: bool = False
    template: str | None = None
    github_repo: str | None = None
    github_token_env: str = "GH_TOKEN"
    linear_api_key_env: str = "LINEAR_API_KEY"
    linear_team: str | None = None


@dataclass
class VisualJudgmentConfig:
    """Appearance check for one saved screenshot. Off unless ``enabled``.

    ``provider="command"`` runs the shell command from the environment
    variable named by ``command_env`` (default ``AQA_VISUAL_JUDGE_COMMAND``),
    or from ``command`` when that variable is unset. The PNG path is the
    last argument. ``provider="fake"`` returns ``judgment`` and does not
    spawn a process. An empty ``judgment``, or the text ``fine``, means
    the screen is fine.
    """

    enabled: bool = False
    provider: VisualJudgmentProvider = "command"
    command_env: str = "AQA_VISUAL_JUDGE_COMMAND"
    command: str = ""
    judgment: str = ""
    timeout_s: float = 60.0


@dataclass
class VisualConfig:
    enabled: bool = False
    baseline_dir: str = "baselines"
    threshold: float = 0.01
    judgment: VisualJudgmentConfig = field(default_factory=VisualJudgmentConfig)


@dataclass
class SystemOneConfig:
    provider: SystemOneProvider = "http"
    endpoint_env: str = "AQA_SYSTEM_ONE_ENDPOINT"
    api_key_env: str = "AQA_SYSTEM_ONE_API_KEY"
    min_confidence: float = 0.55
    include_tree_depth: int = 4


@dataclass
class ComputerUseConfig:
    provider: ComputerUseProvider = "command"
    command_env: str = "AQA_COMPUTER_USE_COMMAND"
    max_calls: int = 3
    include_a11y_hint: bool = True


@dataclass
class DecisionConfig:
    mode: DecisionMode = "heuristic"
    escalate_after: int = 3
    max_model_calls: int = 8
    model_timeout_s: float = 30.0
    cache_observations: bool = True
    system_one: SystemOneConfig = field(default_factory=SystemOneConfig)
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)


@dataclass
class FrictionConfig:
    """Advisory UX friction metering for exploratory hunts.

    ``fail_ci`` is reserved and not wired to ``fail_on`` yet (default false).
    Under ``fail_on=scripted``, exploratory ``friction_path`` findings stay
    exit 0 like other exploratory findings.

    ``allow_step_ratio`` maps intent_id → max allowed ``step_ratio`` (inclusive);
    hunts at or below that ratio do not emit. Shard tags may override via
    ``allow_step_ratio:N``.
    """

    enabled: bool = True
    emit_threshold: int = 50
    min_extra_steps: int = 3
    min_backtrack_rate: float = 0.15
    personas: list[str] = field(default_factory=lambda: ["expert", "first_time"])
    fail_ci: bool = False
    compare_to: FrictionCompareTo = "gold"
    klm: bool = True
    allow_step_ratio: dict[str, float] = field(default_factory=dict)


@dataclass
class ExplorerConfig:
    max_steps: int = 40
    max_time_s: float = 120
    on_step_failure: StepFailurePolicy = "stop"
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    friction: FrictionConfig = field(default_factory=FrictionConfig)


@dataclass
class CoverageConfig:
    scripted: bool = True
    exploratory: bool = True
    visual: bool = False


@dataclass
class SuiteConfig:
    command: str | None = None


@dataclass
class CloudConfig:
    adapter: str = "simulator"
    cost_per_worker_minute: float = 0.05
    estimated_shard_minutes: float = 1.0
    endpoint_env: str | None = None
    token_env: str | None = None


@dataclass
class VmConfig:
    provider: str = "tart"
    image: str | None = None
    tart_bin: str = "tart"
    recycle: bool = True
    cost_per_worker_minute: float = 0.0


@dataclass
class DriverConfig:
    """`auto` is legacy on darwin (or for `app.platform = "ios"`), else fake.

    `legacy` is the AppleScript macOS driver or the simctl/idb iOS driver,
    chosen by `app.platform`. `runner` is reserved for the XCUITest runner.
    """

    kind: DriverKind = "auto"


@dataclass
class LocalConfig:
    isolation: LocalIsolation = "thread"


@dataclass
class CampaignConfig:
    """Validated campaign settings. Defaults match the locked product decisions."""

    app: AppTarget = field(default_factory=AppTarget)
    intents: list[str] = field(default_factory=list)
    backend: BackendName = "local"
    workers: int = 2
    shard_strategy: ShardStrategy = "intent"
    budgets: CampaignBudgets = field(default_factory=CampaignBudgets)
    spend: SpendConfig = field(default_factory=SpendConfig)
    pr: PrConfig = field(default_factory=PrConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    issues: IssuesConfig = field(default_factory=IssuesConfig)
    visual: VisualConfig = field(default_factory=VisualConfig)
    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    suite: SuiteConfig = field(default_factory=SuiteConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    vm: VmConfig = field(default_factory=VmConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    driver: DriverConfig = field(default_factory=DriverConfig)
    fail_on: FailOn = "scripted"
    report_root: str = "reports"
    gui_worker_warn_threshold: int = 2


@dataclass
class CliOverrides:
    """Flags that replace config values for a single invocation."""

    app: str | None = None
    intent: list[str] | None = None
    backend: str | None = None
    workers: int | None = None
    max_wall_time: str | None = None
    max_spend: float | None = None
    spend_currency: str | None = None
    video_mode: str | None = None
    pr_mode: str | None = None
    config_path: str | None = None


@dataclass
class RunOptions:
    resume_campaign_id: str | None = None
    reset_spend: bool = False


@dataclass
class ElementQuery:
    role: str | None = None
    label: str | None = None
    identifier: str | None = None
    value: str | None = None


@dataclass
class UIElement:
    role: str
    label: str = ""
    identifier: str | None = None
    value: str | None = None
    enabled: bool = True
    frame: tuple[float, float, float, float] | None = None
    children: list["UIElement"] = field(default_factory=list)


@dataclass
class Action:
    """One step in the owned JSON recorded-flow format (schema version 1)."""

    action: ActionType
    target: ElementQuery | None = None
    text: str | None = None
    keys: list[str] = field(default_factory=list)
    delta: int | None = None
    path: list[str] = field(default_factory=list)
    timeout_s: float | None = None
    name: str | None = None
    exists: bool | None = None


@dataclass
class FlowDocument:
    version: int
    name: str
    steps: list[Action] = field(default_factory=list)


@dataclass
class Shard:
    id: str
    kind: ShardKind
    name: str
    source_path: str | None = None
    actions: list[Action] = field(default_factory=list)
    goal: str = ""
    constraints: list[str] = field(default_factory=list)
    suite_command: str | None = None
    seed: str | None = None
    visual_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class BuildMetadata:
    path: str | None = None
    bundle_id: str | None = None
    version: str | None = None
    backend: str = "local"


@dataclass
class BuildArtifact:
    """One built app for one platform, produced by the builder.

    `app_path` is the `.app` bundle (simulator build for ios). `sha` is empty
    when the build did not come from a specific commit.
    """

    platform: TargetPlatform
    app_path: str
    bundle_id: str
    sha: str = ""
    log_path: str | None = None


@dataclass
class StepResult:
    index: int
    action: str
    status: Literal["passed", "failed", "skipped"]
    message: str = ""


@dataclass
class Evidence:
    """Media attached to a finding beyond the full-session video.

    `video_clip` is a short clip trimmed around the failure. `frames` are
    screenshots in time order. Paths are relative to the campaign report
    directory, like every other media path on `Finding`.
    """

    video_clip: str | None = None
    frames: list[str] = field(default_factory=list)


_CATEGORY_BY_KIND: dict[str, FindingCategory] = {
    "crash": "crash",
    "launch": "crash",
    "visual": "visual",
    "visual_judgment": "visual",
    "friction_path": "confusing",
}


def category_for_kind(kind: str) -> FindingCategory:
    """Default category for a finding kind: crash, visual, confusing, or broken."""
    return _CATEGORY_BY_KIND.get(kind, "broken")


@dataclass
class Finding:
    """One problem found by a worker. Findings v2 (`schemas/findings.v2.json`).

    v2 adds `category`, `confidence`, `advisory`, `repro`,
    `suspected_sources`, and `evidence`. All v2 fields have defaults, so v1
    constructors keep working. `category` defaults from `kind`. A finding
    that only a model reported is `advisory` with its `confidence` below 1.
    `repro` is the path to a replay script. `suspected_sources` are
    repo-relative `path` or `path:line` strings.
    """

    id: str
    title: str
    severity: Severity
    kind: FindingKind
    steps: list[str]
    fingerprint: str
    worker_id: str
    backend: str
    shard_id: str = ""
    screenshots: list[str] = field(default_factory=list)
    video: str | None = None
    replay_json: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    details: str = ""
    worker_ids: list[str] = field(default_factory=list)
    category: FindingCategory | None = None
    confidence: float = 1.0
    advisory: bool = False
    repro: str | None = None
    suspected_sources: list[str] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)

    def __post_init__(self) -> None:
        if self.worker_id and self.worker_id not in self.worker_ids:
            self.worker_ids.insert(0, self.worker_id)
        if self.category is None:
            self.category = category_for_kind(self.kind)
        if isinstance(self.evidence, dict):
            self.evidence = Evidence(**self.evidence)

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        """Build a Finding from v1 or v2 JSON. Unknown keys raise TypeError."""
        return cls(**data)


@dataclass
class WorkerResult:
    worker_id: str
    shard_id: str
    status: ShardStatus
    findings: list[Finding] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    error: str | None = None
    estimated_cost: float = 0.0
    worker_minutes: float = 0.0
    backend: str = "local"
    shard_name: str = ""
    shard_kind: str = ""


@dataclass
class Coverage:
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    not_started: int = 0
    stop_reason: str | None = None


@dataclass
class SpendSummary:
    max_spend: float | None = None
    currency: str = "USD"
    estimated_spent: float = 0.0
    stop_reason: str | None = None
    note: str | None = None


@dataclass
class CampaignResult:
    campaign_id: str
    report_dir: str
    results: list[WorkerResult] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    spend: SpendSummary = field(default_factory=SpendSummary)
    exit_code: int = 0
    backend: str = "local"


@dataclass
class IssueRef:
    tracker: Literal["github", "linear", "local"]
    identifier: str
    url: str | None = None
    finding_id: str = ""


@dataclass
class FixProposal:
    branch: str
    title: str
    body: str
    commit_message: str
    changed_files: list[str] = field(default_factory=list)


@dataclass
class FixLoopResult:
    mode: PrMode
    iterations: int
    pr_updates: int
    branch: str | None = None
    pr_url: str | None = None
    stop_reason: str | None = None
    remaining_finding_ids: list[str] = field(default_factory=list)
    draft_path: str | None = None


@dataclass
class ActiveWorker:
    worker_id: str
    shard_id: str
    shard_name: str
    mode: str
    step: str = ""


@dataclass
class CampaignStatus:
    campaign_id: str
    state: Literal["running", "finished", "stopped"]
    backend: str
    workers_configured: int
    active_workers: list[ActiveWorker] = field(default_factory=list)
    queue_depth: int = 0
    spend_estimated: float = 0.0
    spend_cap: float | None = None
    spend_currency: str = "USD"
    spend_stop_reason: str | None = None
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    report_dir: str = ""
