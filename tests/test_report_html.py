"""The self-contained HTML campaign report (`swarmqa.report.html`)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import swarmqa
from swarmqa.explorer.screen_graph import Control, ScreenGraph
from swarmqa.models import CampaignResult, Evidence, Finding, StepResult, WorkerResult
from swarmqa.report.findings_json import write_findings_json
from swarmqa.report.html import write_html_report
from swarmqa.report.summary import verdict, write_summary


def _finding(fid: str, **extra) -> Finding:
    base = dict(
        id=fid, title=f"Title {fid}", severity="high", kind="crash", steps=["Open the app", "Tap Save"],
        fingerprint=f"fp-{fid}", worker_id="w1", backend="local",
        environment={"bundle_id": "dev.swarmqa.PlantedBugs", "platform": "ios", "version": "1.2"},
    )
    base.update(extra)
    return Finding(**base)


def _campaign(tmp_path: Path, findings: list[Finding]) -> Path:
    root = tmp_path / "reports" / "camp-1"
    (root / "media").mkdir(parents=True)
    (root / "media" / "small.png").write_bytes(b"x" * 10)
    (root / "media" / "big.png").write_bytes(b"x" * 1000)
    (root / "media" / "home.png").write_bytes(b"x")
    result = CampaignResult(
        campaign_id="camp-1", report_dir=str(root),
        results=[WorkerResult(
            worker_id="w1", shard_id="s1", status="failed", findings=findings, shard_name="crawl",
            shard_kind="exploratory", started_at="2026-01-01T10:00:00Z", finished_at="2026-01-01T10:03:12Z",
            steps=[StepResult(index=0, action="stop", status="passed", message="max_steps")],
        )],
    )
    write_summary(result, root)
    write_findings_json(root, "camp-1", findings)
    graph = ScreenGraph()
    graph.add_screen("abc123", [Control(key="button|Save|", role="button", label="Save")], "media/home.png")
    graph.add_screen("abc123", [])
    graph.mark_tried("abc123", "button|Save|")
    graph.add_screen("def456", [Control(key="button|Back|", role="button", label="Back")])
    graph.save(root / "screen_graph.json")
    return root


def test_page_shows_every_section(tmp_path: Path):
    crash = _finding(
        "crash", severity="critical", confirmation="confirmed", confirm_attempts=2, confirm_hits=2,
        screenshots=["media/small.png", "media/big.png"], evidence=Evidence(video_clip="media/crash.clip.mp4"),
        repro="findings/crash.replay.json", suspected_sources=["App/Save.swift:10"],
        occurrences=["Save button on Settings", "Save button on Profile"], details="Stack <trace>",
    )
    crash.environment.update(target_identifier="save.button", target_label="Save")
    flaky = _finding("flaky", confirmation="flaky", confirm_attempts=2, confirm_hits=1, video="media/full.mp4")
    model = _finding("model", severity="low", kind="visual_judgment", advisory=True, confidence=0.4)
    gone = _finding("gone", severity="critical", confirmation="not_reproduced", confirm_attempts=2)
    root = _campaign(tmp_path, [model, flaky, gone, crash])
    page = write_html_report(root)
    assert page == root / "report.html"
    text = page.read_text(encoding="utf-8")
    assert "dev.swarmqa.PlantedBugs" in text and "iOS" in text and "1.2" in text and "camp-1" in text
    assert "3m 12s" in text and "2026-01-01 10:00" in text
    assert "2 problems found, 1 critical, 1 high; 2 more findings to check" in text
    assert text.index("Title crash") < text.index("Title flaky") < text.index("To check (2)")
    assert text.index("To check (2)") < text.index("Title gone")
    assert "Confirmed 2/2" in text and "Flaky 1/2" in text and "Not reproduced 0/2" in text
    assert "advisory: model only" in text
    assert text.index('src="media/big.png"') < text.index('src="media/small.png"')
    assert '<video controls preload="metadata" src="media/crash.clip.mp4"' in text
    assert 'href="media/full.mp4"' in text
    assert "<code>App/Save.swift:10</code>" in text
    assert "aqa replay" in text and "crash.replay.json" in text
    assert "Also seen in 2 more places" in text and "Save button on Profile" in text
    assert "Save (save.button)" in text
    assert "Stack &lt;trace&gt;" in text and "<trace>" not in text
    assert "<strong>2</strong> screens seen" in text and "<strong>1</strong> of <strong>2</strong>" in text
    assert 'src="media/home.png"' in text and "2 visits" in text and "No screenshot" in text
    assert "crawl" in text and "max_steps" in text and "status-failed" in text
    assert "aqa run" in text and swarmqa.__version__ in text
    assert "prefers-color-scheme:dark" in text


def test_page_is_self_contained_and_escapes_data(tmp_path: Path):
    evil = _finding("evil", title='<script>alert("x")</script>', steps=["<b>tap</b>"],
                    screenshots=["media/big.png"])
    root = _campaign(tmp_path, [evil])
    text = write_html_report(root).read_text(encoding="utf-8")
    assert "<script" not in text
    assert "&lt;script&gt;" in text and "&lt;b&gt;tap&lt;/b&gt;" in text
    assert not re.search(r'(src|href)="(https?:)?//', text)
    assert "@import" not in text and "<link" not in text


def test_page_renders_with_nothing_in_the_folder(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    text = write_html_report(root).read_text(encoding="utf-8")
    assert "No problems found" in text and "No findings." in text
    assert "No screen map" in text and "No shards ran." in text


def test_page_falls_back_to_summary_findings_and_broken_files(tmp_path: Path):
    root = _campaign(tmp_path, [_finding("one")])
    (root / "findings.json").write_text("{broken", encoding="utf-8")
    (root / "screen_graph.json").write_text("[]", encoding="utf-8")
    text = write_html_report(root).read_text(encoding="utf-8")
    assert "Title one" in text and "1 problem found, 1 high" in text and "No screen map" in text


def test_page_uses_worker_results_without_a_summary(tmp_path: Path):
    root = tmp_path / "c"
    (root / "workers" / "w1").mkdir(parents=True)
    payload = {"worker_id": "w1", "shard_id": "s-x", "status": "passed", "shard_name": "smoke", "findings": []}
    (root / "workers" / "w1" / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    text = write_html_report(root).read_text(encoding="utf-8")
    assert "smoke" in text and "status-passed" in text


def test_write_html_report_never_raises(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert write_html_report(blocker) is None


def test_verdict_words():
    assert verdict([]) == "No problems found"
    assert verdict([_finding("a", advisory=True)]) == (
        "No problems found; 1 finding to check (advisory or not reproduced)"
    )
    assert verdict([_finding("a", severity="critical"), _finding("b", severity="low")]) == (
        "2 problems found, 1 critical, 1 low"
    )
