"""findings.json v2, clips, frames, source map, and the cross-run state store."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmqa.models import Evidence, Finding
from swarmqa.report.clips import clip_window, collect_frames, event_offset, trim_clip
from swarmqa.report.dedup import dedup_findings
from swarmqa.report.findings_json import load_findings_json, validate, write_findings_json
from swarmqa.report.source_map import finding_targets, map_finding, rank, suspected_sources
from swarmqa.report.state import FindingState, mark_seen, state_path


def _finding(**overrides) -> Finding:
    data = dict(
        id="f-1",
        title="Tapping Refresh did nothing",
        severity="medium",
        kind="unresponsive",
        steps=["launch", 'tap "Refresh"'],
        fingerprint="aaaa000000000001",
        worker_id="w1",
        backend="local",
    )
    data.update(overrides)
    return Finding(**data)


# findings.json --------------------------------------------------------------


def test_findings_json_validates_against_schema_v2(tmp_path: Path):
    findings = [
        _finding(environment={"event_ts": "12.5"}, confidence=0.8, suspected_sources=["App/Home.swift:12"]),
        _finding(
            id="f-2",
            kind="visual_judgment",
            title="Overlapping labels",
            fingerprint="bbbb",
            advisory=True,
            confidence=0.4,
            evidence=Evidence(video_clip="media/f-2.clip.mp4", frames=["media/a.png"]),
        ),
    ]
    path = write_findings_json(tmp_path, "c1", findings, sha="abc123")
    assert path == tmp_path / "findings.json"
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 2
    assert payload["campaign_id"] == "c1"
    assert payload["sha"] == "abc123"
    assert validate(payload) == []
    assert payload["findings"][1]["category"] == "visual"
    assert payload["findings"][1]["evidence"] == {"video_clip": "media/f-2.clip.mp4", "frames": ["media/a.png"]}

    loaded = load_findings_json(path)
    assert [f.id for f in loaded] == ["f-1", "f-2"]
    assert loaded[1].advisory and loaded[1].evidence.frames == ["media/a.png"]
    assert loaded[0].suspected_sources == ["App/Home.swift:12"]


def test_validator_catches_violations():
    base = {"schema_version": 2, "campaign_id": "c", "sha": None, "findings": []}
    assert validate(base) == []
    assert validate({**base, "schema_version": 1})
    assert validate({**base, "extra": 1})
    assert validate({"schema_version": 2, "findings": []})  # missing campaign_id
    bad = json.loads(json.dumps(base))
    bad["findings"] = [{"id": "x"}]
    errors = validate(bad)
    assert any("missing title" in error for error in errors)
    good = {
        "id": "x", "title": "t", "severity": "low", "kind": "crash", "category": "crash", "steps": [],
        "fingerprint": "f", "worker_id": "w", "backend": "local", "confidence": 1, "advisory": False,
    }
    assert validate({**base, "findings": [good]}) == []
    assert validate({**base, "findings": [{**good, "severity": "urgent"}]})
    assert validate({**base, "findings": [{**good, "confidence": 1.5}]})
    assert validate({**base, "findings": [{**good, "advisory": 1}]})
    assert validate({**base, "findings": [{**good, "environment": {"a": 1}}]})


def test_write_coerces_environment_values_to_strings(tmp_path: Path):
    finding = _finding(environment={"count": 3})  # type: ignore[dict-item]
    payload = json.loads(write_findings_json(tmp_path, "c1", [finding]).read_text())
    assert payload["findings"][0]["environment"] == {"count": "3"}


def test_load_accepts_v1_findings(tmp_path: Path):
    v1 = {
        "id": "f1", "title": "Missing control: Save", "severity": "high", "kind": "missing_control",
        "steps": ["click Save"], "fingerprint": "abc", "worker_id": "w1", "backend": "local",
        "shard_id": "s", "screenshots": [], "video": None, "replay_json": None,
        "environment": {}, "details": "", "worker_ids": ["w1"],
    }
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"schema_version": 1, "campaign_id": "c", "findings": [v1]}))
    [finding] = load_findings_json(path)
    assert finding.category == "broken" and finding.confidence == 1.0 and not finding.advisory
    path.write_text(json.dumps([v1]))
    assert load_findings_json(path)[0].id == "f1"


# Clips and frames -----------------------------------------------------------


class FakeFfmpeg:
    def __init__(self, fail_first: bool = False, fail_all: bool = False):
        self.calls: list[list[str]] = []
        self.fail_first = fail_first
        self.fail_all = fail_all

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail_all or (self.fail_first and len(self.calls) == 1):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        Path(args[-1]).write_bytes(b"clip")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _video_finding(tmp_path: Path, **env) -> Finding:
    video = tmp_path / "workers" / "w1" / "media" / "session.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    return _finding(video="workers/w1/media/session.mp4", environment=env)


def test_clip_window_and_offset():
    assert clip_window(12.0) == (7.0, 7.0)
    assert clip_window(3.0) == (0.0, 5.0)
    assert event_offset(_finding(environment={"event_ts": "110.5", "video_start_ts": "100"})) == 10.5
    assert event_offset(_finding(environment={"video_offset_s": "4"})) == 4.0
    assert event_offset(_finding(environment={"event_ts": "90", "video_start_ts": "100"})) is None
    assert event_offset(_finding(environment={"event_ts": "x", "video_start_ts": "100"})) is None
    assert event_offset(_finding()) is None


def test_trim_clip_builds_the_ffmpeg_command(tmp_path: Path):
    finding = _video_finding(tmp_path, event_ts="1000.0", video_start_ts="990.0")
    ffmpeg = FakeFfmpeg()
    assert trim_clip(tmp_path, finding, runner=ffmpeg, ffmpeg="ffmpeg") == "media/f-1.clip.mp4"
    assert finding.evidence.video_clip == "media/f-1.clip.mp4"
    assert (tmp_path / "media" / "f-1.clip.mp4").is_file()
    video = str(tmp_path / "workers" / "w1" / "media" / "session.mp4")
    assert ffmpeg.calls == [
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "5.000", "-i", video, "-t", "7.000", "-c", "copy",
         str(tmp_path / "media" / "f-1.clip.mp4")]
    ]


def test_trim_clip_falls_back_to_reencode(tmp_path: Path):
    finding = _video_finding(tmp_path, video_offset_s="2")
    ffmpeg = FakeFfmpeg(fail_first=True)
    assert trim_clip(tmp_path, finding, runner=ffmpeg, ffmpeg="/usr/bin/ffmpeg") == "media/f-1.clip.mp4"
    assert len(ffmpeg.calls) == 2
    assert "libx264" in ffmpeg.calls[1]
    assert ffmpeg.calls[1][ffmpeg.calls[1].index("-ss") + 1] == "0.000"


def test_trim_clip_skips_when_ffmpeg_missing_or_failing(tmp_path: Path, monkeypatch):
    finding = _video_finding(tmp_path, video_offset_s="2")
    ffmpeg = FakeFfmpeg()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert trim_clip(tmp_path, finding, runner=ffmpeg) is None
    assert trim_clip(tmp_path, finding, runner=ffmpeg, ffmpeg="") is None
    assert ffmpeg.calls == []

    failing = FakeFfmpeg(fail_all=True)
    assert trim_clip(tmp_path, finding, runner=failing, ffmpeg="ffmpeg") is None
    assert len(failing.calls) == 2
    assert finding.evidence.video_clip is None
    assert not (tmp_path / "media" / "f-1.clip.mp4").exists()

    def raising(args):
        raise FileNotFoundError(args[0])

    assert trim_clip(tmp_path, finding, runner=raising, ffmpeg="ffmpeg") is None


def test_trim_clip_skips_without_video_or_timestamps(tmp_path: Path):
    ffmpeg = FakeFfmpeg()
    assert trim_clip(tmp_path, _finding(environment={"video_offset_s": "3"}), runner=ffmpeg, ffmpeg="ffmpeg") is None
    assert trim_clip(tmp_path, _video_finding(tmp_path), runner=ffmpeg, ffmpeg="ffmpeg") is None
    gone = _finding(video="workers/none.mp4", environment={"video_offset_s": "3"})
    assert trim_clip(tmp_path, gone, runner=ffmpeg, ffmpeg="ffmpeg") is None
    assert ffmpeg.calls == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_trim_clip_with_real_ffmpeg(tmp_path: Path):
    video = tmp_path / "session.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=10:size=64x64:rate=5",
         "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    finding = _finding(video=str(video), environment={"video_offset_s": "6"})
    assert trim_clip(tmp_path, finding) == "media/f-1.clip.mp4"


def test_collect_frames_orders_by_time(tmp_path: Path):
    media = tmp_path / "media"
    media.mkdir()
    for index, name in enumerate(["late.png", "early.png", "mid.png"]):
        path = media / name
        path.write_bytes(b"x")
        os.utime(path, (100 + [30, 10, 20][index],) * 2)
    finding = _finding(screenshots=["media/late.png", "media/mid.png"], evidence=Evidence(frames=["media/early.png"]))
    assert collect_frames(tmp_path, finding) == ["media/early.png", "media/mid.png", "media/late.png"]
    missing = _finding(screenshots=["media/b.png"], evidence=Evidence(frames=["media/a.png"]))
    assert collect_frames(tmp_path, missing) == ["media/a.png", "media/b.png"]


# Source map -----------------------------------------------------------------


class FakeGit:
    def __init__(self, outputs: dict[str, tuple[int, str]]):
        self.outputs = outputs
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        code, out = self.outputs.get(args[-1], (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")


def test_source_map_prefers_identifier_and_source_files(tmp_path: Path):
    git = FakeGit(
        {
            '"home.refresh"': (0, "README.md:3:home.refresh\nApp/Home.swift:42:.accessibilityIdentifier(\"home.refresh\")\n"),
            '"Refresh"': (0, "App/Base.lproj/Main.storyboard:7:title=\"Refresh\"\nApp/en.lproj/L.strings:1:\"Refresh\" = \"Refresh\";\n"),
        }
    )
    hits = suspected_sources(tmp_path, "home.refresh", "Refresh", runner=git)
    assert hits == [
        "App/Home.swift:42",
        "README.md:3",
        "App/Base.lproj/Main.storyboard:7",
        "App/en.lproj/L.strings:1",
    ]
    assert git.calls[0] == ["git", "-C", str(tmp_path), "grep", "-n", "-I", "-F", "--full-name", "-e", '"home.refresh"']


def test_source_map_caps_at_five_and_handles_failures(tmp_path: Path):
    many = "\n".join(f"A/F{i}.swift:{i}:x" for i in range(9))
    assert len(suspected_sources(tmp_path, "id", "", runner=FakeGit({'"id"': (0, many)}))) == 5
    not_repo = FakeGit({'"id"': (128, "")})
    assert suspected_sources(tmp_path, "id", "Label", runner=not_repo) == []
    assert suspected_sources(tmp_path, "", "", runner=not_repo) == []
    assert rank(["a.txt:1", "b.xib:2", "c.m:3"]) == ["c.m:3", "b.xib:2", "a.txt:1"]


def test_finding_targets_from_environment_then_replay(tmp_path: Path):
    assert finding_targets(_finding(environment={"target_identifier": "x", "target_label": "X"})) == ("x", "X")
    replay = tmp_path / "findings" / "f-1.replay.json"
    replay.parent.mkdir(parents=True)
    replay.write_text(json.dumps({"version": 1, "name": "f-1", "steps": [
        {"action": "launch"},
        {"action": "click", "target": {"label": "Refresh", "identifier": "home.refresh"}},
        {"action": "key", "keys": ["escape"]},
    ]}))
    finding = _finding(replay_json="findings/f-1.replay.json")
    assert finding_targets(finding, tmp_path) == ("home.refresh", "Refresh")
    assert finding_targets(finding) == ("", "")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_source_map_with_a_real_repo(tmp_path: Path):
    repo = tmp_path / "app"
    (repo / "App").mkdir(parents=True)
    (repo / "App" / "HomeView.swift").write_text(
        'struct HomeView: View {\n  var body: some View {\n    Button("Refresh") { }\n'
        '      .accessibilityIdentifier("home.refresh")\n  }\n}\n'
    )
    (repo / "notes.txt").write_text("Refresh is broken\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    finding = _finding(environment={"target_identifier": "home.refresh", "target_label": "Refresh"})
    assert map_finding(finding, repo) == ["App/HomeView.swift:4", "App/HomeView.swift:3"]

    plain = tmp_path / "plain"
    plain.mkdir()
    other = _finding(environment={"target_identifier": "home.refresh"})
    assert map_finding(other, plain) == []


# Cross-run state ------------------------------------------------------------


def test_state_new_recurring_and_fixed(tmp_path: Path):
    a = _finding(fingerprint="a")
    b = _finding(id="f-2", fingerprint="b", title="Crash on Save", kind="crash")
    first = mark_seen([a, b], "c1", root=tmp_path, now="2026-01-01T00:00:00Z")
    assert [f.fingerprint for f in first.new] == ["a", "b"] and not first.recurring
    assert a.environment["seen_before"] == "false"
    assert a.environment["first_seen"] == "2026-01-01T00:00:00Z"
    assert state_path(tmp_path).is_file()

    a2 = _finding(fingerprint="a")
    second = mark_seen([a2], "c2", root=tmp_path, now="2026-01-02T00:00:00Z")
    assert second.recurring == [a2] and not second.new
    assert a2.environment["seen_before"] == "true"
    assert a2.environment["first_seen"] == "2026-01-01T00:00:00Z"
    assert second.fixed == ["b"]
    records = FindingState(tmp_path).records
    assert records["a"]["count"] == 2 and records["a"]["campaigns"] == ["c1", "c2"]
    assert records["a"]["last_seen"] == "2026-01-02T00:00:00Z"
    assert records["b"]["status"] == "fixed" and records["b"]["fixed_in"] == "c2"

    # Marking the same campaign again does not count twice.
    again = mark_seen([_finding(fingerprint="a")], "c2", root=tmp_path)
    assert FindingState(tmp_path).records["a"]["count"] == 2
    assert again.recurring

    # A partial run marks nothing fixed; a fixed fingerprint coming back regresses.
    partial = mark_seen([_finding(fingerprint="b")], "c3", root=tmp_path, full_run=False)
    assert partial.regressed and partial.regressed[0].environment["regressed"] == "true"
    records = FindingState(tmp_path).records
    assert records["b"]["status"] == "open" and records["a"]["status"] == "open"


def test_state_survives_a_corrupt_file(tmp_path: Path):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    result = mark_seen([_finding()], "c1", root=tmp_path)
    assert len(result.new) == 1


def test_dedup_in_place_keeps_the_first_object():
    first = _finding(screenshots=["a.png"])
    second = _finding(worker_id="w2", screenshots=["b.png"], advisory=False)
    merged = dedup_findings([first, second, first], in_place=True)
    assert merged == [first] and merged[0] is first
    assert first.worker_ids == ["w1", "w2"] and first.screenshots == ["a.png", "b.png"]
    copies = dedup_findings([first])
    assert copies[0] is not first


@pytest.mark.parametrize("findings", [[], "oops", {"fp": {"campaigns": "c1", "count": "many", "status": 3}}])
def test_malformed_state_file_starts_clean(tmp_path: Path, findings):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "findings": findings}), encoding="utf-8")
    result = mark_seen([_finding(fingerprint="fp")], "c2", root=tmp_path)
    assert len(result.new) + len(result.recurring) == 1


def test_an_early_crash_is_not_the_final_step_crashing():
    from swarmqa.models import WorkerResult
    from swarmqa.report.repro import _judge

    crash = _finding(kind="crash", fingerprint="crash-fp")
    watcher = SimpleNamespace(fingerprints=[], crash_calls=[])

    def crash_after(steps: int) -> Finding:
        return _finding(kind="crash", fingerprint=f"c{steps}", steps=[f"s{i}" for i in range(steps)])

    early = WorkerResult(worker_id="r", shard_id="replay", status="failed",
                         findings=[crash_after(1), crash_after(4)])
    assert _judge(early, watcher, crash, crash.id, 4).reason != "the final step crashed the app"
    final = WorkerResult(worker_id="r", shard_id="replay", status="failed", findings=[crash_after(4)])
    assert _judge(final, watcher, crash, crash.id, 4).reproduced
