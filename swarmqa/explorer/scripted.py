"""C4 — execute a scripted shard against one driver.

See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from swarmqa.driver.query import find_element
from swarmqa.driver.v1_adapter import as_v2
from swarmqa.errors import (
    AppCrashedError,
    AppMissingError,
    ElementNotFoundError,
    UITimeoutError,
)
from swarmqa.models import (
    Action,
    CampaignConfig,
    ElementQuery,
    Finding,
    Shard,
    StepResult,
    WorkerResult,
)
from swarmqa.reporter.findings import fingerprint_for, write_finding, write_replay
from swarmqa.video_policy import keep_video, should_start_video
from swarmqa.visual.judge import apply_judgment


class _AssertionFailed(Exception):
    """An assert action did not match the accessibility tree."""


_STEP_FAILURES = (
    _AssertionFailed,
    AppCrashedError,
    UITimeoutError,
    ElementNotFoundError,
    AppMissingError,
)


def run_scripted(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    worker_id: str,
    work_dir: Path,
) -> WorkerResult:
    """Run shard.actions. Record step pass/fail and findings on failure."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    campaign_dir, relative = _campaign_context(work_dir)
    started_at = _timestamp()
    environment = _environment(driver)
    findings: list[Finding] = []
    steps: list[StepResult] = []
    replays: dict[str, list[dict]] = {}
    status = "passed"
    judge_error: str | None = None
    video_started = False
    video_path: Path | None = None

    try:
        try:
            driver.launch()
        except Exception as exc:
            status = "failed"
            _record_launch_failure(
                exc,
                shard=shard,
                config=config,
                worker_id=worker_id,
                environment=environment,
                findings=findings,
                steps=steps,
                replays=replays,
            )
        else:
            environment = _environment(driver)
            if should_start_video(config.video.mode, shard.kind):
                try:
                    driver.start_video()
                    video_started = True
                except Exception:
                    video_started = False
            status, judge_error = _run_actions(
                shard,
                driver,
                config,
                worker_id=worker_id,
                work_dir=work_dir,
                campaign_dir=campaign_dir,
                relative=relative,
                environment=environment,
                findings=findings,
                steps=steps,
                replays=replays,
            )
    except Exception:
        if status == "passed":
            status = "error"
        raise
    finally:
        if video_started:
            video_path = _stop_video(driver)
            if video_path is not None and not keep_video(config.video.mode, shard.kind, status):
                Path(video_path).unlink(missing_ok=True)
                video_path = None
            elif video_path is not None and not Path(video_path).is_file():
                video_path = None
        _safe_close(driver)

    if findings and status == "passed":
        status = "failed"
    _persist_findings(
        findings,
        replays,
        campaign_dir=campaign_dir,
        relative=relative,
        video_path=video_path,
    )
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        status=status,  # type: ignore[arg-type]
        findings=findings,
        steps=steps,
        started_at=started_at,
        finished_at=_timestamp(),
        error=judge_error,
        backend=config.backend,
        shard_name=shard.name,
        shard_kind=shard.kind,
    )


def _run_actions(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    worker_id: str,
    work_dir: Path,
    campaign_dir: Path,
    relative: bool,
    environment: dict[str, str],
    findings: list[Finding],
    steps: list[StepResult],
    replays: dict[str, list[dict]],
) -> tuple[str, str | None]:
    executed: list[Action] = []
    descriptions: list[str] = []
    screenshots: list[Path] = []
    status = "passed"
    judge_error: str | None = None
    stop = config.explorer.on_step_failure != "continue"
    for index, action in enumerate(shard.actions):
        description = _describe(action, index)
        try:
            shot = _execute(driver, action, index)
            owned: Path | None = None
            if shot is not None:
                owned = _own_media(shot, work_dir)
                screenshots.append(owned)
            executed.append(action)
            descriptions.append(description)
            steps.append(StepResult(index=index, action=action.action, status="passed", message=description))
            if owned is not None and config.visual.judgment.enabled:
                judged, judge_error = apply_judgment(
                    owned,
                    config,
                    worker_id=worker_id,
                    shard_id=shard.id,
                    backend=config.backend,
                    error=judge_error,
                    name=_screenshot_name(action, index),
                    evidence=_public_path(owned, campaign_dir, relative),
                    steps=list(descriptions),
                    environment=environment,
                )
                if judged is not None:
                    findings.append(judged)
                    replays[judged.id] = [_action_dict(item) for item in executed]
                    status = "failed"
        except _STEP_FAILURES as exc:
            executed.append(action)
            descriptions.append(description)
            failure_shot = _capture_failure_shot(driver, index, work_dir)
            if failure_shot is not None:
                screenshots.append(failure_shot)
            finding = _build_finding(
                kind=_kind_from(exc),
                action=action,
                shard=shard,
                config=config,
                worker_id=worker_id,
                environment=environment,
                step_lines=list(descriptions),
                screenshots=_public_paths(screenshots, campaign_dir, relative),
                details=str(exc),
                index=index,
            )
            findings.append(finding)
            replays[finding.id] = [_action_dict(item) for item in executed]
            steps.append(
                StepResult(
                    index=index,
                    action=action.action,
                    status="failed",
                    message=f"{description}: {exc}",
                )
            )
            status = "failed"
            if stop:
                _skip_rest(shard.actions, index, steps)
                break
    return status, judge_error


def _record_launch_failure(
    exc: Exception,
    *,
    shard: Shard,
    config: CampaignConfig,
    worker_id: str,
    environment: dict[str, str],
    findings: list[Finding],
    steps: list[StepResult],
    replays: dict[str, list[dict]],
) -> None:
    target = environment.get("path") or config.app.path or ""
    title = "App failed to launch"
    finding = Finding(
        id=f"f-{shard.id}-launch",
        title=title,
        severity="critical",
        kind="launch",
        steps=[f"launch: {exc}"],
        fingerprint=fingerprint_for("launch", title, target),
        worker_id=worker_id,
        backend=config.backend,
        shard_id=shard.id,
        environment=dict(environment),
        details=str(exc),
    )
    findings.append(finding)
    replays[finding.id] = [{"action": "launch"}]
    for index, action in enumerate(shard.actions):
        steps.append(
            StepResult(
                index=index,
                action=action.action,
                status="skipped",
                message="skipped after launch failure",
            )
        )


def _execute(driver, action: Action, index: int) -> Path | None:
    kind = action.action
    if kind == "launch":
        driver.launch()
    elif kind == "relaunch":
        driver.relaunch()
    elif kind == "click":
        driver.click(action.target or ElementQuery())
    elif kind == "type":
        driver.type_text(action.target or ElementQuery(), action.text or "")
    elif kind == "key":
        driver.keychord(list(action.keys))
    elif kind == "scroll":
        driver.scroll(0 if action.delta is None else action.delta, action.target)
    elif kind == "menu":
        driver.select_menu(list(action.path))
    elif kind == "wait":
        timeout = 5.0 if action.timeout_s is None else action.timeout_s
        driver.wait_for(action.target or ElementQuery(), timeout)
    elif kind == "screenshot":
        return Path(driver.screenshot(_screenshot_name(action, index)))
    elif kind == "assert":
        _check_assert(driver, action)
    elif kind == "tap_point":
        as_v2(driver).tap_point(*action.point)
    elif kind == "swipe":
        as_v2(driver).swipe(action.point, action.end)
    else:
        raise ValueError(f"unknown action: {kind}")
    return None


def _check_assert(driver, action: Action) -> None:
    query = action.target or ElementQuery()
    expect_present = action.exists is not False
    try:
        find_element(driver.accessibility_tree(), query)
        present = True
    except ElementNotFoundError:
        present = False
    label = _query_text(query) or "element"
    if expect_present and not present:
        raise _AssertionFailed(f"expected {label} to exist")
    if not expect_present and present:
        raise _AssertionFailed(f"expected {label} to be absent")


def _capture_failure_shot(driver, index: int, work_dir: Path) -> Path | None:
    try:
        path = Path(driver.screenshot(f"failure-{index}"))
    except Exception:
        return None
    return _own_media(path, work_dir)


def _build_finding(
    *,
    kind: str,
    action: Action,
    shard: Shard,
    config: CampaignConfig,
    worker_id: str,
    environment: dict[str, str],
    step_lines: list[str],
    screenshots: list[str],
    details: str,
    index: int,
) -> Finding:
    target = _target_text(action)
    title = _title(kind, target)
    return Finding(
        id=f"f-{shard.id}-{index}-{kind}",
        title=title,
        severity=_severity(kind, config.app.maturity),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        steps=step_lines,
        fingerprint=fingerprint_for(kind, title, target),
        worker_id=worker_id,
        backend=config.backend,
        shard_id=shard.id,
        screenshots=screenshots,
        environment=dict(environment),
        details=details,
    )


def _persist_findings(
    findings: list[Finding],
    replays: dict[str, list[dict]],
    *,
    campaign_dir: Path,
    relative: bool,
    video_path: Path | None,
) -> None:
    video_public = _public_path(video_path, campaign_dir, relative) if video_path else None
    for finding in findings:
        if video_public:
            finding.video = video_public
        replay_path = write_replay(finding.id, campaign_dir, replays.get(finding.id, []))
        finding.replay_json = _public_path(replay_path, campaign_dir, relative)
        write_finding(finding, campaign_dir)


def _skip_rest(actions: list[Action], failed_index: int, steps: list[StepResult]) -> None:
    for index, action in enumerate(actions[failed_index + 1 :], start=failed_index + 1):
        steps.append(
            StepResult(
                index=index,
                action=action.action,
                status="skipped",
                message="stopped after step failure",
            )
        )


def _kind_from(exc: BaseException) -> str:
    if isinstance(exc, _AssertionFailed):
        return "assertion"
    if isinstance(exc, AppCrashedError):
        return "crash"
    if isinstance(exc, UITimeoutError):
        return "timeout"
    if isinstance(exc, ElementNotFoundError):
        return "missing_control"
    if isinstance(exc, AppMissingError):
        return "launch"
    return "assertion"


def _severity(kind: str, maturity: str) -> str:
    if kind in {"crash", "launch"}:
        return "critical"
    if kind == "timeout":
        return "high"
    if kind == "missing_control":
        return "high" if maturity == "prototype" else "medium"
    return "medium"


def _title(kind: str, target: str) -> str:
    labels = {
        "launch": "App failed to launch",
        "crash": "App crashed",
        "timeout": "Timed out",
        "missing_control": "Missing control",
        "assertion": "Assertion failed",
    }
    base = labels.get(kind, kind.replace("_", " ").title())
    if kind == "launch" or not target:
        return base
    return f"{base}: {target}"


def _environment(driver) -> dict[str, str]:
    try:
        meta = driver.metadata()
    except Exception:
        return {"path": "", "bundle_id": "", "version": ""}
    return {
        "path": meta.path or "",
        "bundle_id": meta.bundle_id or "",
        "version": meta.version or "",
    }


def _action_dict(action: Action) -> dict:
    payload: dict = {"action": action.action}
    if action.target is not None:
        query: dict[str, str] = {}
        if action.target.role:
            query["role"] = action.target.role
        if action.target.label:
            query["label"] = action.target.label
        if action.target.identifier:
            query["identifier"] = action.target.identifier
        if action.target.value is not None:
            query["value"] = action.target.value
        if query:
            payload["target"] = query
    if action.text is not None:
        payload["text"] = action.text
    if action.keys:
        payload["keys"] = list(action.keys)
    if action.delta is not None:
        payload["delta"] = action.delta
    if action.path:
        payload["path"] = list(action.path)
    if action.timeout_s is not None:
        payload["timeout_s"] = action.timeout_s
    if action.name:
        payload["name"] = action.name
    if action.exists is not None:
        payload["exists"] = action.exists
    return payload


def _describe(action: Action, index: int) -> str:
    label = _target_text(action)
    kind = action.action
    if kind == "click":
        return f'click "{label}"' if label else "click"
    if kind == "type":
        typed = action.text or ""
        return f'type "{typed}" into "{label}"' if label else f'type "{typed}"'
    if kind == "key":
        return "key " + "+".join(action.keys)
    if kind == "menu":
        return "menu " + " > ".join(action.path)
    if kind == "wait":
        timeout = 5.0 if action.timeout_s is None else action.timeout_s
        shown = int(timeout) if float(timeout).is_integer() else timeout
        return f'wait for "{label}" {shown}s' if label else f"wait {shown}s"
    if kind == "scroll":
        delta = 0 if action.delta is None else action.delta
        return f"scroll {delta}"
    if kind == "screenshot":
        return f'screenshot "{_screenshot_name(action, index)}"'
    if kind == "assert":
        expectation = "exists" if action.exists is not False else "does not exist"
        return f'assert "{label}" {expectation}' if label else f"assert {expectation}"
    if kind in {"launch", "relaunch"}:
        return kind
    return kind


def _screenshot_name(action: Action, index: int) -> str:
    return action.name or f"step-{index}"


def _target_text(action: Action | None) -> str:
    if action is None:
        return ""
    if action.target is not None:
        text = _query_text(action.target)
        if text:
            return text
    if action.path:
        return " > ".join(action.path)
    if action.name:
        return action.name
    return ""


def _query_text(query: ElementQuery) -> str:
    if query.label:
        return query.label
    if query.identifier:
        return query.identifier
    if query.value not in (None, ""):
        return str(query.value)
    if query.role:
        return query.role
    return ""


def _campaign_context(work_dir: Path) -> tuple[Path, bool]:
    """Return the campaign directory and whether artifact paths should be relative.

    Worker directories live at `<campaign>/workers/<id>`. Anywhere else, the
    work directory is the campaign root and paths stay absolute.
    """
    if work_dir.parent.name == "workers":
        return work_dir.parent.parent, True
    return work_dir, False


def _public_paths(paths: list[Path], campaign_dir: Path, relative: bool) -> list[str]:
    return [_public_path(path, campaign_dir, relative) for path in paths]


def _public_path(path: Path, campaign_dir: Path, relative: bool) -> str:
    path = Path(path)
    if relative:
        try:
            return path.resolve().relative_to(Path(campaign_dir).resolve()).as_posix()
        except ValueError:
            pass
    if path.is_absolute():
        return str(path)
    return str(path.resolve())


def _own_media(path: Path, work_dir: Path) -> Path:
    path = Path(path)
    media = Path(work_dir) / "media"
    try:
        path.resolve().relative_to(media.resolve())
        return path
    except ValueError:
        media.mkdir(parents=True, exist_ok=True)
        destination = media / path.name
        if path.is_file() and destination.resolve() != path.resolve():
            destination.write_bytes(path.read_bytes())
            return destination
        return path


def _stop_video(driver) -> Path | None:
    try:
        stopped = driver.stop_video()
    except Exception:
        stopped = getattr(driver, "video_path", None)
    if stopped is None:
        stopped = getattr(driver, "video_path", None)
    return Path(stopped) if stopped else None


def _safe_close(driver) -> None:
    try:
        driver.close()
    except Exception:
        return


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
