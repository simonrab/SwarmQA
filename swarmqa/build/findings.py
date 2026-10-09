"""Turn a build failure into a critical finding.

A build that fails means nothing downstream can run, so it is reported the
same way as an app that cannot launch: `kind="launch"`, `severity="critical"`,
category `crash` (from `category_for_kind`). `kind="launch"` also means
`aqa verify` can check the fix without a replay flow: it rebuilds and
launches. The fingerprint uses the platform and the first error line, not the
SHA, so the same break on the next commit deduplicates into one finding.
"""

from __future__ import annotations

import re

from swarmqa.build.errors import BuildFailure
from swarmqa.models import Finding, category_for_kind
from swarmqa.reporter.findings import fingerprint_for

KIND = "launch"


def build_failure_finding(
    failure: BuildFailure,
    *,
    worker_id: str = "builder",
    backend: str = "local",
    shard_id: str = "build",
) -> Finding:
    short = failure.sha[:12] if failure.sha else "unknown"
    platform = failure.platform or "app"
    title = f"{_label(platform)} build failed"
    first_error = _normalise(failure.first_error)
    details = [f"{failure.message}", ""]
    if failure.log_path:
        details.append(f"Build log: {failure.log_path}")
    if failure.log_tail:
        details += ["", "Log tail:", "```", failure.log_tail, "```"]
    environment = {"stage": failure.stage, "platform": platform, "sha": failure.sha, "repo": failure.repo}
    if failure.log_path:
        environment["build_log"] = failure.log_path
    return Finding(
        id=f"build-{platform}-{short}",
        title=title,
        severity="critical",
        kind=KIND,
        steps=[f"check out {failure.repo or 'the repo'} at {failure.sha or '?'}",
               f"build for {platform} ({failure.stage} failed)"],
        fingerprint=fingerprint_for(KIND, title, f"{failure.repo}:{failure.stage}:{first_error}"),
        worker_id=worker_id,
        backend=backend,
        shard_id=shard_id,
        environment=environment,
        details="\n".join(details).strip(),
        category=category_for_kind(KIND),
    )


def _label(platform: str) -> str:
    return {"ios": "iOS", "macos": "macOS"}.get(platform, platform)


def _normalise(line: str) -> str:
    # Drop absolute paths up to the checkout so the fingerprint survives a new SHA.
    line = re.sub(r"/\S*/checkouts/[^/\s]+/[0-9a-f]{40}/", "", line)
    return re.sub(r"\s+", " ", line).strip().lower()
