"""Short video clips around a failure, and frames in time order.

A clip covers `[t - 5s, t + 2s]` of the session video, where `t` is the
failure's offset into the recording: `environment["event_ts"] -
environment["video_start_ts"]` (both Unix epoch seconds as strings), or
`environment["video_offset_s"]` when the recorder knows the offset directly.
The clip goes to `media/<finding-id>.clip.mp4`. ffmpeg is optional: when it
is missing, the timestamps are missing, or ffmpeg fails, the finding keeps
`evidence.video_clip = None`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from swarmqa.models import Finding
from swarmqa.report.paths import Runner, default_runner, public, resolve

CLIP_BEFORE_S = 5.0
CLIP_AFTER_S = 2.0


def event_offset(finding: Finding) -> float | None:
    """Seconds from the start of the recording to the failure, or None when unknown."""
    environment = finding.environment or {}
    try:
        if environment.get("video_offset_s", "") != "":
            offset = float(environment["video_offset_s"])
        else:
            offset = float(environment["event_ts"]) - float(environment["video_start_ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return offset if offset >= 0 else None


def clip_window(offset: float, *, before: float = CLIP_BEFORE_S, after: float = CLIP_AFTER_S) -> tuple[float, float]:
    """`(start, duration)` in seconds for a clip around `offset`."""
    start = max(0.0, offset - before)
    return start, (offset + after) - start


def ffmpeg_commands(ffmpeg: str, video: Path, start: float, duration: float, out: Path) -> list[list[str]]:
    """Stream copy first (fast, cuts at keyframes), then a re-encode fallback."""
    head = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{duration:.3f}"]
    return [
        [*head, "-c", "copy", str(out)],
        [*head, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(out)],
    ]


def trim_clip(
    campaign_dir: Path,
    finding: Finding,
    *,
    runner: Runner | None = None,
    ffmpeg: str | None = None,
) -> str | None:
    """Cut the clip and set `finding.evidence.video_clip`. Returns the public path or None.

    `ffmpeg` is the binary; None looks it up on PATH and an empty string turns
    clipping off. An existing clip is kept.
    """
    if finding.evidence.video_clip:
        return finding.evidence.video_clip
    binary = shutil.which("ffmpeg") if ffmpeg is None else ffmpeg
    offset = event_offset(finding)
    if not binary or not finding.video or offset is None:
        return None
    campaign_dir = Path(campaign_dir)
    video = resolve(campaign_dir, finding.video)
    if not video.is_file():
        return None
    out = campaign_dir / "media" / f"{finding.id}.clip.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    start, duration = clip_window(offset)
    run = runner or default_runner
    for args in ffmpeg_commands(binary, video, start, duration, out):
        try:
            completed = run(args)
        except (OSError, ValueError):
            break
        if getattr(completed, "returncode", 1) == 0 and out.is_file() and out.stat().st_size > 0:
            finding.evidence.video_clip = public(campaign_dir, out)
            return finding.evidence.video_clip
    out.unlink(missing_ok=True)
    return None


def collect_frames(campaign_dir: Path, finding: Finding) -> list[str]:
    """Union `evidence.frames` and `screenshots` into `evidence.frames`, oldest first.

    Frames are ordered by file modification time when every file exists;
    otherwise the recorded order is kept.
    """
    merged: list[str] = []
    for path in [*finding.evidence.frames, *finding.screenshots]:
        if path and path not in merged:
            merged.append(path)
    try:
        times = [resolve(campaign_dir, path).stat().st_mtime for path in merged]
    except OSError:
        times = None
    if times is not None:
        order = sorted(range(len(merged)), key=lambda index: (times[index], index))
        merged = [merged[index] for index in order]
    finding.evidence.frames = merged
    return merged
