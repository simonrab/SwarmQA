"""Small shared helpers. Chunk owners import these instead of reimplementing them."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_duration(value: str) -> float:
    """Parse a duration into seconds.

    Accepted forms: `90s`, `5m`, `2h`, `1h30m`, `1h2m3s`.
    A bare number is rejected so units stay explicit in config and CLI flags.
    """
    text = value.strip().lower()
    if not text or text.isdigit():
        raise ValueError(f"duration must include a unit (s, m, h): {value!r}")
    match = _DURATION.fullmatch(text)
    if not match or match.group(0) == "":
        raise ValueError(f"unrecognized duration: {value!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    if hours == minutes == seconds == 0 and text not in {"0s", "0m", "0h"}:
        raise ValueError(f"unrecognized duration: {value!r}")
    return float(hours * 3600 + minutes * 60 + seconds)


def new_campaign_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned or "shard")[:limit].strip("-") or "shard"
