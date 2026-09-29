"""Cross-run dedup store: which fingerprints earlier campaigns already saw.

Lives at `<root>/.aqa/state/findings.json` (root is the target repo). Each
record keeps `first_seen`, `last_seen`, `campaigns`, `count`, `status`,
`title`, and `kind`. A record is `open` while campaigns keep seeing it and
becomes `fixed` when a later full run no longer does; seeing it again
reopens it and marks the finding `regressed`.

`mark_seen` annotates each finding's environment with `seen_before`
(`"true"`/`"false"`), `first_seen`, and `regressed` when it applies.
Marking the same campaign twice does not count it twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from swarmqa.models import Finding

STATE_VERSION = 1


def state_path(root: Path) -> Path:
    return Path(root) / ".aqa" / "state" / "findings.json"


@dataclass
class SeenResult:
    new: list[Finding] = field(default_factory=list)
    recurring: list[Finding] = field(default_factory=list)
    regressed: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def _clean(record: dict) -> dict:
    """Coerce a stored record's fields to the types mark_seen relies on."""
    record = dict(record)
    campaigns = record.get("campaigns")
    record["campaigns"] = [str(item) for item in campaigns] if isinstance(campaigns, list) else []
    count = record.get("count")
    record["count"] = count if isinstance(count, int) and not isinstance(count, bool) else len(record["campaigns"])
    if record.get("status") not in ("open", "fixed"):
        record["status"] = "open"
    return record


class FindingState:
    """Load, update, and save the store. Unreadable files start empty."""

    def __init__(self, root: Path):
        self.path = state_path(root)
        self.records: dict[str, dict] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            records = data.get("findings", {}) if isinstance(data, dict) else {}
            if not isinstance(records, dict):
                records = {}
            self.records = {str(k): _clean(v) for k, v in records.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.records = {}

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STATE_VERSION, "findings": self.records}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return self.path

    def mark_seen(
        self,
        findings: list[Finding],
        campaign_id: str,
        *,
        full_run: bool = True,
        now: str | None = None,
    ) -> SeenResult:
        stamp = now or _now()
        result = SeenResult()
        seen_now: set[str] = set()
        for finding in findings:
            key = finding.fingerprint
            if key in seen_now:
                continue
            seen_now.add(key)
            record = self.records.get(key)
            if record is None:
                record = {
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "campaigns": [campaign_id],
                    "count": 1,
                    "status": "open",
                    "title": finding.title,
                    "kind": finding.kind,
                }
                self.records[key] = record
                result.new.append(finding)
                seen_before = False
            else:
                campaigns = list(record.get("campaigns", []))
                seen_before = any(item != campaign_id for item in campaigns)
                if campaign_id not in campaigns:
                    campaigns.append(campaign_id)
                    record["count"] = int(record.get("count", 0)) + 1
                    record["last_seen"] = stamp
                record["campaigns"] = campaigns
                if record.get("status") == "fixed":
                    record["status"] = "open"
                    record.pop("fixed_in", None)
                    finding.environment["regressed"] = "true"
                    result.regressed.append(finding)
                (result.recurring if seen_before else result.new).append(finding)
            finding.environment["seen_before"] = "true" if seen_before else "false"
            finding.environment["first_seen"] = str(record.get("first_seen", stamp))
        if full_run:
            for key, record in self.records.items():
                if key not in seen_now and record.get("status") == "open":
                    record["status"] = "fixed"
                    record["fixed_in"] = campaign_id
                    result.fixed.append(key)
        return result


def mark_seen(
    findings: list[Finding],
    campaign_id: str,
    *,
    root: Path,
    full_run: bool = True,
    now: str | None = None,
) -> SeenResult:
    """Record this campaign's findings in the store under `root` and save it.

    `full_run` should be false when the campaign ran only part of the suite
    (stopped early, filtered intents): then nothing is marked fixed.
    """
    state = FindingState(root)
    result = state.mark_seen(findings, campaign_id, full_run=full_run, now=now)
    state.save()
    return result


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
