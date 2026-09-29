"""`reports/<campaign-id>/findings.json`, schema version 2.

The file is `{"schema_version": 2, "campaign_id", "sha", "findings": [...]}`
and validates against `swarmqa/schemas/findings.v2.json`. `validate` checks
the subset of JSON Schema that file uses, so no schema library is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swarmqa.models import FINDINGS_SCHEMA_VERSION, Finding
from swarmqa.serialize import to_plain

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "findings.v2.json"
FINDINGS_JSON = "findings.json"


def findings_payload(campaign_id: str, findings: list[Finding], *, sha: str | None = None) -> dict:
    """The findings.json document as plain data. Environment values become strings."""
    items = []
    for finding in findings:
        data = to_plain(finding)
        data["environment"] = {str(k): "" if v is None else str(v) for k, v in (finding.environment or {}).items()}
        data["confidence"] = min(1.0, max(0.0, float(finding.confidence)))
        items.append(data)
    return {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "sha": sha,
        "findings": items,
    }


def write_findings_json(
    campaign_dir: Path, campaign_id: str, findings: list[Finding], *, sha: str | None = None
) -> Path:
    """Write `<campaign_dir>/findings.json`. Raises ValueError if it would not validate."""
    payload = findings_payload(campaign_id, findings, sha=sha)
    errors = validate(payload)
    if errors:
        raise ValueError("findings.json does not match schema v2: " + "; ".join(errors[:5]))
    path = Path(campaign_dir) / FINDINGS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_findings_json(path: Path) -> list[Finding]:
    """Read findings from a v2 file, a v1 file, or a bare list of finding objects."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("findings", [])
    return [Finding.from_dict(dict(item)) for item in items]


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(payload: Any, schema: dict | None = None) -> list[str]:
    """Return schema violations as `path: message` strings; empty when valid."""
    root = schema or load_schema()
    errors: list[str] = []
    _check(payload, root, root, "$", errors)
    return errors


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
    "number": (int, float),
    "integer": (int,),
}


def _is_type(value: Any, name: str) -> bool:
    if name in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def _check(value: Any, schema: dict, root: dict, where: str, errors: list[str]) -> None:
    ref = schema.get("$ref")
    if ref:
        target: Any = root
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        _check(value, target, root, where, errors)
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} not in {schema['enum']}")
    kind = schema.get("type")
    if kind is not None:
        names = kind if isinstance(kind, list) else [kind]
        if not any(_is_type(value, name) for name in names):
            errors.append(f"{where}: expected {kind}, got {type(value).__name__}")
            return
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing {key}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], root, f"{where}.{key}", errors)
            elif extra is False:
                errors.append(f"{where}: unexpected key {key}")
            elif isinstance(extra, dict):
                _check(item, extra, root, f"{where}.{key}", errors)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], root, f"{where}[{index}]", errors)
