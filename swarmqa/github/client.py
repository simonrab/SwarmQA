"""A small GitHub REST client on top of `gh api`, so it uses the user's `gh` login.

Every call goes through the `swarmqa.devices.commands` runner seam (tests
pass a fake). Request bodies go through a temp file (`gh api --input`), so
nested JSON such as a check run's `output` survives. Failures raise
`GitHubError` with the HTTP status when `gh` reports one.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

from swarmqa.build.runner import build_runner
from swarmqa.devices.commands import Runner, run
from swarmqa.errors import AQAError

API_TIMEOUT_S = 60.0
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class GitHubError(AQAError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class PullRequest:
    number: int
    head_sha: str
    head_ref: str = ""
    base_ref: str = ""
    title: str = ""
    url: str = ""
    state: str = "open"
    merge_commit_sha: str | None = None
    merged_at: str | None = None
    updated_at: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PullRequest":
        head = data.get("head") or {}
        base = data.get("base") or {}
        return cls(
            number=int(data["number"]),
            head_sha=str(head.get("sha") or ""),
            head_ref=str(head.get("ref") or ""),
            base_ref=str(base.get("ref") or ""),
            title=str(data.get("title") or ""),
            url=str(data.get("html_url") or ""),
            state=str(data.get("state") or "open"),
            merge_commit_sha=data.get("merge_commit_sha"),
            merged_at=data.get("merged_at"),
            updated_at=str(data.get("updated_at") or ""),
        )


class GitHubClient:
    def __init__(self, repo: str, *, runner: Runner | None = None, gh: str = "gh"):
        self.repo = repo
        self.runner = runner or build_runner
        self.gh = gh

    def api(self, path: str, *, method: str = "GET", body: Any = None) -> Any:
        args = [self.gh, "api", "--method", method, "-H", "Accept: application/vnd.github+json", path]
        temp = None
        try:
            if body is not None:
                handle, temp = tempfile.mkstemp(prefix="aqa-gh-", suffix=".json")
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(body, stream)
                args += ["--input", temp]
            result = run(self.runner, args, timeout=API_TIMEOUT_S)
        finally:
            if temp:
                os.unlink(temp)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip()
            match = _HTTP_STATUS.search(detail)
            status = int(match.group(1)) if match else None
            raise GitHubError(f"gh api {method} {path} failed: {detail or result.returncode}", status)
        text = (result.stdout or "").strip()
        return json.loads(text) if text else None

    def _repo(self, path: str = "") -> str:
        return f"repos/{self.repo}{path}"

    # Reads -------------------------------------------------------------------

    def default_branch(self) -> str:
        return str(self.api(self._repo())["default_branch"])

    def branch_head(self, branch: str) -> str:
        return str(self.api(self._repo(f"/commits/{branch}"))["sha"])

    def pull(self, number: int) -> PullRequest:
        return PullRequest.from_api(self.api(self._repo(f"/pulls/{int(number)}")))

    def open_pulls(self) -> list[PullRequest]:
        data = self.api(self._repo("/pulls?state=open&sort=updated&direction=desc&per_page=100")) or []
        return [PullRequest.from_api(item) for item in data]

    def latest_pull(self) -> PullRequest | None:
        pulls = self.open_pulls()
        return pulls[0] if pulls else None

    def latest_merged(self, branch: str) -> PullRequest | None:
        data = self.api(self._repo(
            f"/pulls?state=closed&base={branch}&sort=updated&direction=desc&per_page=30")) or []
        merged = [PullRequest.from_api(item) for item in data if item.get("merged_at")]
        merged.sort(key=lambda pull: pull.merged_at or "", reverse=True)
        return merged[0] if merged else None

    def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        data = self.api(self._repo(f"/commits/{sha}/pulls")) or []
        return [PullRequest.from_api(item) for item in data]

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return list(self.api(self._repo(f"/issues/{int(number)}/comments?per_page=100")) or [])

    # Writes ------------------------------------------------------------------

    def create_check_run(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.api(self._repo("/check-runs"), method="POST", body=body)

    def update_check_run(self, check_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return self.api(self._repo(f"/check-runs/{int(check_id)}"), method="PATCH", body=body)

    def create_status(self, sha: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.api(self._repo(f"/statuses/{sha}"), method="POST", body=body)

    def create_comment(self, number: int, body: str) -> dict[str, Any]:
        return self.api(self._repo(f"/issues/{int(number)}/comments"), method="POST", body={"body": body})

    def update_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self.api(self._repo(f"/issues/comments/{int(comment_id)}"), method="PATCH", body={"body": body})


def detect_repo(runner: Runner | None = None, *, cwd: str | None = None) -> str | None:
    """`owner/name` of the repo `gh` sees in `cwd`, or None."""
    result = run(runner or build_runner, ["gh", "repo", "view", "--json", "nameWithOwner",
                                          "-q", ".nameWithOwner"], cwd=cwd, timeout=API_TIMEOUT_S)
    name = (result.stdout or "").strip()
    return name if result.ok and name.count("/") == 1 else None
