"""The structured build failure."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from swarmqa.errors import AQAError

TAIL_LINES = 30
MAX_ERROR_LINES = 10
_ERROR_LINE = re.compile(r"(^|\s)(error|fatal):\s|\*\* BUILD FAILED \*\*|The following build commands failed")


@dataclass
class BuildFailure:
    """Why a build of one SHA for one platform failed.

    `stage` is `checkout`, `detect`, `build`, `product` or `codesign`.
    `log_tail` holds the error lines from the log followed by its last lines.
    """

    repo: str
    sha: str
    platform: str
    stage: str
    message: str
    log_path: str | None = None
    log_tail: str = ""

    @property
    def first_error(self) -> str:
        for line in self.log_tail.splitlines():
            if _ERROR_LINE.search(line):
                return line.strip()
        return self.message.splitlines()[0] if self.message else ""

    def summary(self) -> str:
        head = f"{self.platform} build of {self.sha[:12] or '?'} failed at {self.stage}: {self.message}"
        return f"{head}\n{self.log_tail}" if self.log_tail else head


class BuildFailed(AQAError):
    """Raised by `ShaBuilder.build`; `failure` carries the details."""

    def __init__(self, failure: BuildFailure):
        self.failure = failure
        super().__init__(failure.summary())


def log_excerpt(path: str | Path | None, *, offset: int = 0, lines: int = TAIL_LINES) -> str:
    """Error lines (deduplicated, at most 10) and then the last `lines` lines of a log."""
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    all_lines = text.rstrip().splitlines()
    tail = all_lines[-lines:]
    errors: list[str] = []
    for line in all_lines[:-lines] if len(all_lines) > lines else []:
        if _ERROR_LINE.search(line) and line not in errors and line not in tail:
            errors.append(line)
    errors = errors[:MAX_ERROR_LINES]
    if errors:
        return "\n".join(errors + ["..."] + tail)
    return "\n".join(tail)
