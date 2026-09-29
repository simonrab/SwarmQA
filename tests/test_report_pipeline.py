"""finalize_findings end to end, and summary.md with advisory findings apart."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from swarmqa.models import CampaignResult, Evidence, Finding, WorkerResult
from swarmqa.report.findings_json import load_findings_json, validate
from swarmqa.report.pipeline import finalize_findings
from swarmqa.report.state import FindingState
from swarmqa.report.summary import render_summary_md, write_summary
from swarmqa.reporter.findings import write_replay
from swarmqa.testing import sample_config


class FakeTools:
    """Answers `git grep` and `ffmpeg` like the real binaries would."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "git":
            if args[-1] == '"home.refresh"':
                return SimpleNamespace(returncode=0, stdout='App/HomeView.swift:4:.accessibilityIdentifier("home.refresh")\n')
            return SimpleNamespace(returncode=1, stdout="")
        Path(args[-1]).write_bytes(b"clip")
        return SimpleNamespace(returncode=0, stdout="")


def _campaign(tmp_path: Path) -> tuple[Path, list[Finding]]:
    campaign = tmp_path / "reports" / "c1"
    media = campaign / "workers" / "w1" / "media"
    media.mkdir(parents=True)
    (media / "session.mp4").write_bytes(b"video")
    (media / "shot.png").write_bytes(b"png")
    write_replay("f-1", campaign, [
        {"action": "launch"},
        {"action": "click", "target": {"label": "Refresh", "identifier": "home.refresh"}},
    ])
    common = dict(worker_id="w1", backend="local", video="workers/w1/media/session.mp4")
    dead = Finding(
        id="f-1", title='Tapping button "Refresh" did nothing', severity="medium", kind="unresponsive",
        steps=["launch", 'tap "Refresh"'], fingerprint="dead", replay_json="findings/f-1.replay.json",
        screenshots=["workers/w1/media/shot.png"], confidence=0.8,
        environment={"event_ts": "1010", "video_start_ts": "1000"}, **common,
    )
    duplicate = Finding(
        id="f-1b", title=dead.title, severity="medium", kind="unresponsive", steps=[], fingerprint="dead",
        worker_id="w2", backend="local",
    )
    judged = Finding(
        id="f-2", title="Labels overlap", severity="low", kind="visual_judgment", steps=[], fingerprint="judged",
        advisory=True, confidence=0.4, **common,
    )
    return campaign, [dead, duplicate, judged]


def test_finalize_findings_end_to_end(tmp_path: Path):
    campaign, findings = _campaign(tmp_path)
    original = findings[0]
    tools = FakeTools()
    repo = tmp_path / "app"
    repo.mkdir()

    final = finalize_findings(
        campaign, "c1", findings, config=sample_config(), repo=repo, sha="abc", runner=tools, ffmpeg="ffmpeg"
    )
    assert [f.id for f in final] == ["f-1", "f-2"]
    dead = final[0]
    assert dead is original  # enriched in place
    assert dead.worker_ids == ["w1", "w2"]
    assert dead.repro == "findings/f-1.replay.json"
    assert (campaign / "findings" / "f-1.repro.md").is_file()
    assert dead.evidence.frames == ["workers/w1/media/shot.png"]
    assert dead.evidence.video_clip == "media/f-1.clip.mp4"
    ffmpeg_call = next(call for call in tools.calls if call[0] == "ffmpeg")
    assert ffmpeg_call[ffmpeg_call.index("-ss") + 1] == "5.000"
    assert dead.suspected_sources == ["App/HomeView.swift:4"]
    assert dead.environment["seen_before"] == "false"
    assert final[1].repro is None and final[1].evidence.video_clip is None  # no replay, no timestamps

    payload = json.loads((campaign / "findings.json").read_text())
    assert validate(payload) == [] and payload["sha"] == "abc"
    assert load_findings_json(campaign / "findings.json")[0].suspected_sources == ["App/HomeView.swift:4"]
    markdown = (campaign / "findings" / "f-1.md").read_text()
    assert "## Triage" in markdown and "App/HomeView.swift:4" in markdown and "media/f-1.clip.mp4" in markdown
    assert set(FindingState(repo).records) == {"dead", "judged"}

    # A second run sees the dead tap again and the judged issue no longer.
    _, again = _campaign(tmp_path / "second")
    rerun = finalize_findings(
        campaign, "c2", again[:1], config=sample_config(), repo=repo, runner=tools, ffmpeg=""
    )
    assert rerun[0].environment["seen_before"] == "true"
    assert FindingState(repo).records["judged"]["status"] == "fixed"


def test_finalize_without_repo_skips_source_map_and_state(tmp_path: Path):
    campaign, findings = _campaign(tmp_path)
    final = finalize_findings(campaign, "c1", findings, config=sample_config(), ffmpeg="")
    assert final[0].suspected_sources == [] and "seen_before" not in final[0].environment
    assert final[0].evidence.video_clip is None
    assert (campaign / "findings.json").is_file()
    assert not (tmp_path / ".aqa").exists()


def test_finalize_notes_evidence_errors_and_keeps_going(tmp_path: Path):
    campaign, findings = _campaign(tmp_path)

    def broken(args):
        raise RuntimeError("tool exploded")

    final = finalize_findings(campaign, "c1", findings, config=sample_config(), repo=tmp_path, runner=broken,
                              ffmpeg="ffmpeg")
    assert "clip: RuntimeError: tool exploded" in final[0].environment["evidence_errors"]
    assert final[0].repro == "findings/f-1.replay.json"
    assert validate(json.loads((campaign / "findings.json").read_text())) == []


def _result(tmp_path: Path, findings: list[Finding]) -> CampaignResult:
    return CampaignResult(
        campaign_id="c1",
        report_dir=str(tmp_path),
        results=[WorkerResult(worker_id="w1", shard_id="s1", status="failed", findings=findings,
                              shard_name="crawl", shard_kind="exploratory")],
    )


def test_summary_separates_advisory_findings_and_shows_evidence(tmp_path: Path):
    firm = Finding(
        id="f-1", title="Crash on Save", severity="critical", kind="crash", steps=[], fingerprint="a",
        worker_id="w1", backend="local", repro="findings/f-1.replay.json",
        suspected_sources=["App/Save.swift:10"], evidence=Evidence(video_clip="media/f-1.clip.mp4"),
        environment={"seen_before": "true", "first_seen": "2026-01-01T00:00:00Z"},
    )
    soft = Finding(
        id="f-2", title="Labels overlap", severity="low", kind="visual_judgment", steps=[], fingerprint="b",
        worker_id="w1", backend="local", advisory=True, confidence=0.4,
    )
    friction = Finding(
        id="f-3", title="Export took 3x the gold path", severity="low", kind="friction_path", steps=[],
        fingerprint="c", worker_id="w1", backend="local", advisory=True,
    )
    text = render_summary_md(_result(tmp_path, [soft, firm, friction]))
    firm_block = text.split("## Findings")[1].split("## Advisory findings")[0]
    advisory_block = text.split("## Advisory findings")[1].split("## Friction (advisory)")[0]
    assert "Crash on Save" in firm_block and "Labels overlap" not in firm_block
    assert "category crash, confidence 1.00" in firm_block
    assert "seen before (first 2026-01-01T00:00:00Z)" in firm_block
    assert "`App/Save.swift:10`" in firm_block
    assert "clip: media/f-1.clip.mp4" in firm_block
    assert f"`aqa replay {tmp_path / 'findings' / 'f-1.replay.json'}`" in firm_block
    assert "Labels overlap" in advisory_block
    assert "category visual, confidence 0.40, advisory" in advisory_block
    assert "Export took 3x" not in advisory_block
    assert text.index("## Findings") < text.index("## Advisory findings") < text.index("## Friction (advisory)")


def test_summary_with_no_findings(tmp_path: Path):
    path = write_summary(_result(tmp_path, []), tmp_path)
    text = path.read_text()
    assert "No findings." in text and "No advisory findings." in text
