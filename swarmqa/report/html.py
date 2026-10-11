"""The HTML campaign report.

`write_html_report(campaign_dir)` reads what the campaign wrote
(`summary.json`, `status.json`, `findings.json`, `screen_graph.json`,
`workers/*/result.json`) and writes `<campaign_dir>/report.html`, returning
its path. It is called by the orchestrator after `summary.md`, and by
`aqa try` and `aqa report --html`. It must never raise: on any error it
returns None and the campaign still succeeds.

The page is one file with inline CSS and no scripts or network requests.
Media are linked by paths relative to the campaign folder, so the folder can
be zipped or uploaded as a CI artifact and the page still works.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import swarmqa
from swarmqa.models import Finding
from swarmqa.report.findings_json import load_findings_json
from swarmqa.report.paths import repro_command, resolve
from swarmqa.report.summary import (
    SEVERITIES,
    TO_CHECK_NOTE,
    confirmation_text,
    finding_sort_key,
    is_problem,
    verdict,
)

REPORT_HTML = "report.html"
_MAX_IMAGES = 4
_MAX_SCREENS = 120
_PLATFORMS = {"ios": "iOS", "macos": "macOS"}
_HIDDEN_ENV = frozenset({"target_identifier", "target_label", "seen_before", "first_seen", "regressed"})


@dataclass
class Campaign:
    """Everything the page shows, read from one campaign folder."""

    root: Path
    summary: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    graph: dict = field(default_factory=dict)
    shards: list[dict] = field(default_factory=list)


def write_html_report(campaign_dir: Path) -> Path | None:
    """Write `<campaign_dir>/report.html` and return its path, or None on any error."""
    try:
        root = Path(campaign_dir)
        page = render_html(load_campaign(root))
        path = root / REPORT_HTML
        path.write_text(page, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - the report must never break a campaign
        return None


def load_campaign(root: Path) -> Campaign:
    """Read the campaign folder. Missing or broken files count as empty."""
    summary = _object(root / "summary.json")
    shards = [item for item in summary.get("results") or [] if isinstance(item, dict)]
    if not shards:
        shards = [data for data in map(_object, sorted(root.glob("workers/*/result.json"))) if data]
    return Campaign(
        root=root,
        summary=summary,
        status=_object(root / "status.json"),
        findings=_load_findings(root, shards),
        graph=_object(root / "screen_graph.json"),
        shards=shards,
    )


def render_html(campaign: Campaign) -> str:
    """The whole page as one HTML string."""
    findings = sorted(campaign.findings, key=finding_sort_key)
    app = _app_info(findings)
    sections = [
        _header(campaign, app),
        _counts(findings),
        _findings(campaign.root, findings),
        _coverage(campaign),
        _shards(campaign),
        _footer(campaign.root, findings),
    ]
    title = f"SwarmQA: {app['name'] or _campaign_id(campaign)}"
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        + "\n".join(sections[:1])
        + "\n<main>\n"
        + "\n".join(sections[1:-1])
        + "\n</main>\n"
        + sections[-1]
        + "\n</body>\n</html>\n"
    )


# Loading ------------------------------------------------------------------


def _object(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_findings(root: Path, shards: list[dict]) -> list[Finding]:
    path = root / "findings.json"
    if path.is_file():
        try:
            return load_findings_json(path)
        except Exception:  # noqa: BLE001 - fall back to the findings in summary.json
            pass
    found: dict[str, Finding] = {}
    for shard in shards:
        for item in shard.get("findings") or []:
            try:
                finding = Finding.from_dict(dict(item))
            except (TypeError, ValueError):
                continue
            found.setdefault(finding.id, finding)
    return list(found.values())


def _app_info(findings: list[Finding]) -> dict[str, str]:
    info = {"name": "", "platform": "", "version": ""}
    for finding in findings:
        env = finding.environment or {}
        path_name = Path(env["path"]).stem if env.get("path") else ""
        info["name"] = info["name"] or env.get("bundle_id", "") or path_name
        info["platform"] = info["platform"] or _PLATFORMS.get(env.get("platform", ""), env.get("platform", ""))
        info["version"] = info["version"] or env.get("version", "")
    return info


def _campaign_id(campaign: Campaign) -> str:
    return str(campaign.summary.get("campaign_id") or campaign.status.get("campaign_id") or campaign.root.name)


def _parse_time(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _span(shard: dict) -> tuple[datetime | None, datetime | None]:
    return _parse_time(shard.get("started_at") or ""), _parse_time(shard.get("finished_at") or "")


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    try:
        return max(0.0, (end - start).total_seconds()) if start and end else None
    except TypeError:
        return None


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _timing(shards: list[dict]) -> tuple[str, str]:
    starts = [s for s, _ in map(_span, shards) if s]
    ends = [e for _, e in map(_span, shards) if e]
    try:
        start, end = (min(starts) if starts else None), (max(ends) if ends else None)
    except TypeError:
        return "", ""
    shown = start.strftime("%Y-%m-%d %H:%M %Z").strip() if start else ""
    return shown, _duration(_seconds(start, end))


# Small helpers ------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _href(root: Path, path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return _e(candidate.as_uri())
    return _e(quote(candidate.as_posix(), safe="/"))


def _exists(root: Path, path: str) -> bool:
    try:
        return resolve(root, path).is_file()
    except OSError:
        return False


def _largest_first(root: Path, paths: list[str]) -> list[str]:
    unique = [path for index, path in enumerate(paths) if path and path not in paths[:index]]
    present = [path for path in unique if _exists(root, path)]
    return sorted(present, key=lambda path: -resolve(root, path).stat().st_size)


def _badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {_e(kind)}">{_e(text)}</span>'


def _list(items: list[str], tag: str = "ul", code: bool = False) -> str:
    wrap = (lambda text: f"<code>{_e(text)}</code>") if code else _e
    return f"<{tag}>" + "".join(f"<li>{wrap(item)}</li>" for item in items) + f"</{tag}>"


# Sections -----------------------------------------------------------------


def _header(campaign: Campaign, app: dict[str, str]) -> str:
    started, duration = _timing(campaign.shards)
    facts = [
        ("App", app["name"]),
        ("Platform", app["platform"]),
        ("Version", app["version"]),
        ("Campaign", _campaign_id(campaign)),
        ("Started", started),
        ("Duration", duration),
    ]
    items = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts if v)
    findings = campaign.findings
    problems = [finding for finding in findings if is_problem(finding)]
    tone = "bad" if problems else ("warn" if findings else "good")
    return (
        f'<header class="top"><p class="eyebrow">SwarmQA report</p>'
        f"<h1>{_e(app['name'] or 'Campaign ' + _campaign_id(campaign))}</h1>"
        f'<p class="verdict {tone}">{_e(verdict(findings))}</p><dl class="facts">{items}</dl></header>'
    )


def _counts(findings: list[Finding]) -> str:
    if not findings:
        return '<section aria-labelledby="counts"><h2 id="counts">Counts</h2><p>No findings.</p></section>'

    def row(name: str, group: list[Finding]) -> str:
        confirmed = sum(not f.advisory and f.confirmation == "confirmed" for f in group)
        advisory = sum(f.advisory for f in group)
        cells = [confirmed, len(group) - confirmed - advisory, advisory, len(group)]
        return f'<tr><th scope="row">{name}</th>' + "".join(f"<td>{n}</td>" for n in cells) + "</tr>"

    rows = [
        row(_badge(name, f"sev-{name}"), [f for f in findings if f.severity == name])
        for name in SEVERITIES
        if any(f.severity == name for f in findings)
    ]
    return (
        '<section aria-labelledby="counts"><h2 id="counts">Counts</h2><div class="scroll"><table>'
        "<caption>Findings by severity</caption><thead><tr><th scope=\"col\">Severity</th>"
        '<th scope="col">Confirmed</th><th scope="col">Not confirmed</th><th scope="col">Advisory</th>'
        '<th scope="col">Total</th></tr></thead><tbody>'
        + "".join(rows)
        + row("All", findings).replace("<tr>", '<tr class="total">', 1)
        + "</tbody></table></div></section>"
    )


def _findings(root: Path, findings: list[Finding]) -> str:
    problems = [finding for finding in findings if is_problem(finding)]
    others = [finding for finding in findings if not is_problem(finding)]
    parts = [f'<section aria-labelledby="findings"><h2 id="findings">Findings ({len(problems)})</h2>']
    parts.append("".join(_card(root, f) for f in problems) or "<p>No problems found.</p>")
    if others:
        parts.append(
            f'<h2 id="to-check">To check ({len(others)})</h2>'
            f'<p class="note">These are {_e(TO_CHECK_NOTE)}. Check them before you act.</p>'
            + "".join(_card(root, f) for f in others)
        )
    return "".join(parts) + "</section>"


def _card(root: Path, finding: Finding) -> str:
    badges = [_badge(finding.severity, f"sev-{finding.severity}"), _badge(finding.category or finding.kind)]
    confirmation = confirmation_text(finding)
    if confirmation:
        badges.append(_badge(confirmation, f"confirm-{finding.confirmation}"))
    if finding.advisory:
        badges.append(_badge("advisory: model only", "advisory"))
    badges.append(f'<span class="meta">confidence {finding.confidence:.0%}</span>')
    parts = [
        f'<article class="card" id="finding-{_e(finding.id)}"><h3>{_e(finding.title)}</h3>',
        f'<p class="badges">{" ".join(badges)}</p>',
        _target(finding),
        _steps(finding),
        _media(root, finding),
        _sources(finding),
        _repro(root, finding),
        _occurrences(finding),
        _details(finding),
        "</article>",
    ]
    return "".join(part for part in parts if part)


def _target(finding: Finding) -> str:
    env = finding.environment or {}
    label, identifier = env.get("target_label", ""), env.get("target_identifier", "")
    lines = []
    if label or identifier:
        shown = f"{label} ({identifier})" if label and identifier else label or identifier
        lines.append(f"On: <code>{_e(shown)}</code>")
    if env.get("regressed") == "true":
        lines.append("Regressed: it was fixed before and came back.")
    elif env.get("seen_before") == "true":
        lines.append(f"Seen in an earlier run (first {_e(env.get('first_seen', '?'))}).")
    return "".join(f'<p class="meta">{line}</p>' for line in lines)


def _steps(finding: Finding) -> str:
    return f"<h4>Steps</h4>{_list(finding.steps, 'ol')}" if finding.steps else ""


def _media(root: Path, finding: Finding) -> str:
    images = _largest_first(root, [*finding.screenshots, *finding.evidence.frames])[:_MAX_IMAGES]
    shots = "".join(
        f'<a href="{_href(root, path)}"><img src="{_href(root, path)}" loading="lazy" '
        f'alt="Screenshot {index} of {_e(finding.title)}"></a>'
        for index, path in enumerate(images, start=1)
    )
    video = ""
    clip = finding.evidence.video_clip
    if clip:
        href = _href(root, clip)
        video = (f'<video controls preload="metadata" src="{href}" aria-label="Clip of {_e(finding.title)}">'
                 f'<a href="{href}">Download the clip</a></video>')
    elif finding.video:
        video = f'<p><a href="{_href(root, finding.video)}">Watch the full video</a></p>'
    if not (shots or video):
        return ""
    return (f'<div class="shots">{shots}</div>' if shots else "") + video


def _sources(finding: Finding) -> str:
    if not finding.suspected_sources:
        return ""
    return f"<h4>Suspected source files</h4>{_list(finding.suspected_sources, code=True)}"


def _repro(root: Path, finding: Finding) -> str:
    replay = finding.repro or finding.replay_json
    if not replay:
        return ""
    return f"<h4>Replay</h4><pre><code>{_e(repro_command(resolve(root, replay)))}</code></pre>"


def _occurrences(finding: Finding) -> str:
    count = len(finding.occurrences)
    if not count:
        return ""
    noun = "place" if count == 1 else "places"
    return f"<details><summary>Also seen in {count} more {noun}</summary>{_list(finding.occurrences)}</details>"


def _details(finding: Finding) -> str:
    env = {k: v for k, v in (finding.environment or {}).items() if k not in _HIDDEN_ENV and v}
    facts = [("ID", finding.id), ("Kind", finding.kind), ("Workers", ", ".join(finding.worker_ids)), *env.items()]
    rows = "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts if v)
    text = f"<pre>{_e(finding.details.strip())}</pre>" if finding.details.strip() else ""
    return f'<details><summary>Details</summary>{text}<dl class="env">{rows}</dl></details>'


def _coverage(campaign: Campaign) -> str:
    head = '<section aria-labelledby="coverage"><h2 id="coverage">Coverage</h2>'
    nodes = [node for node in campaign.graph.get("nodes") or [] if isinstance(node, dict)]
    if not nodes:
        return head + "<p>No screen map was saved for this campaign.</p></section>"
    coverage = campaign.graph.get("coverage") or {}
    controls = int(coverage.get("controls", sum(len(n.get("controls") or []) for n in nodes)))
    tried = int(coverage.get("tried", sum(len(n.get("tried") or []) for n in nodes)))
    ratio = f" ({tried / controls:.0%})" if controls else ""
    stats = (f'<p class="stats"><strong>{len(nodes)}</strong> screens seen. '
             f"<strong>{tried}</strong> of <strong>{controls}</strong> controls tried{ratio}.</p>")
    tiles = "".join(_screen_tile(campaign.root, node) for node in nodes[:_MAX_SCREENS])
    hidden = len(nodes) - _MAX_SCREENS
    more = f'<p class="note">{hidden} more screens are in screen_graph.json.</p>' if hidden > 0 else ""
    return head + stats + f'<div class="grid">{tiles}</div>{more}</section>'


def _screen_tile(root: Path, node: dict) -> str:
    screen = str(node.get("id", ""))
    shot = node.get("screenshot")
    visits = int(node.get("visits") or 0)
    controls = len(node.get("controls") or [])
    tried = len(node.get("tried") or [])
    if shot and _exists(root, str(shot)):
        href = _href(root, str(shot))
        image = f'<a href="{href}"><img src="{href}" loading="lazy" alt="Screen {_e(screen)}"></a>'
    else:
        image = '<div class="blank">No screenshot</div>'
    caption = f"<code>{_e(screen)}</code><br>{visits} visit{'' if visits == 1 else 's'}, {tried}/{controls} tried"
    return f"<figure>{image}<figcaption>{caption}</figcaption></figure>"


def _shards(campaign: Campaign) -> str:
    head = '<section aria-labelledby="shards"><h2 id="shards">Shards</h2>'
    if not campaign.shards:
        return head + "<p>No shards ran.</p></section>"
    coverage = campaign.summary.get("coverage") or {}
    names = ("completed", "failed", "cancelled", "not_started")
    counts = ", ".join(f"{coverage[k]} {k.replace('_', ' ')}" for k in names if coverage.get(k))
    spend = campaign.summary.get("spend") or {}
    spent = spend.get("estimated_spent") or 0
    lines = [counts and f"{counts}.", spent and f"Estimated spend: {float(spent):.2f} {spend.get('currency', '')}."]
    note = "".join(f'<p class="note">{_e(line)}</p>' for line in lines if line)
    rows = "".join(_shard_row(shard) for shard in campaign.shards)
    return (
        head + note + '<div class="scroll"><table><caption>One row per shard</caption><thead><tr>'
        + "".join(f'<th scope="col">{name}</th>' for name in
                  ("Shard", "Kind", "Status", "Duration", "Stop reason", "Findings"))
        + f"</tr></thead><tbody>{rows}</tbody></table></div></section>"
    )


def _shard_row(shard: dict) -> str:
    status = str(shard.get("status", ""))
    cells = [
        f'<th scope="row">{_e(shard.get("shard_name") or shard.get("shard_id") or "")}</th>',
        f"<td>{_e(shard.get('shard_kind') or '')}</td>",
        f"<td>{_badge(status, f'status-{status}')}</td>",
        f"<td>{_e(_duration(_seconds(*_span(shard))))}</td>",
        f"<td>{_e(_stop_reason(shard))}</td>",
        f"<td>{len(shard.get('findings') or [])}</td>",
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def _stop_reason(shard: dict) -> str:
    steps = [step for step in shard.get("steps") or [] if isinstance(step, dict)]
    stop = next((s.get("message", "") for s in reversed(steps) if s.get("action") == "stop"), "")
    error = (shard.get("error") or "").strip().splitlines()
    return str(stop or (error[0] if error else ""))


def _footer(root: Path, findings: list[Finding]) -> str:
    replay = next((f.repro or f.replay_json for f in findings if f.repro or f.replay_json), None)
    example = repro_command(resolve(root, replay)) if replay else "aqa replay findings/<id>.replay.json"
    return (
        '<footer><p>Run the campaign again: <code>aqa run</code>. '
        f"Replay one finding: <code>{_e(example)}</code>.</p>"
        f"<p>Made by SwarmQA {_e(swarmqa.__version__)}.</p></footer>"
    )


_CSS = """
:root{color-scheme:light dark;--bg:#f7f7f8;--fg:#1b1b1f;--muted:#55565e;--card:#fff;--line:#d9dae0;
--code:#eef0f3;--critical:#b00020;--high:#c2410c;--medium:#8a6100;--low:#3f5f8a;--good:#1b7a3a;
--warn:#8a6100;--bad:#b00020;--advisory:#6b4fa3}
@media (prefers-color-scheme:dark){:root{--bg:#141418;--fg:#ececf1;--muted:#a9abb6;--card:#1e1f25;
--line:#34363f;--code:#2a2c33;--critical:#ff6b7f;--high:#ff9b5e;--medium:#f2c75c;--low:#8fb4ea;
--good:#5fd38a;--warn:#f2c75c;--bad:#ff6b7f;--advisory:#c3a6ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header.top,main,footer{max-width:1100px;margin:0 auto;padding:16px}
header.top{padding-top:32px}
h1{margin:0 0 8px;font-size:1.8rem;word-break:break-word}
h2{margin:32px 0 12px;font-size:1.3rem;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{margin:0 0 8px;font-size:1.1rem}
h4{margin:16px 0 4px;font-size:.95rem;color:var(--muted)}
.eyebrow,.meta,.note,figcaption,footer{color:var(--muted)}
.eyebrow{margin:0;text-transform:uppercase;letter-spacing:.08em;font-size:.8rem}
.verdict{font-size:1.25rem;font-weight:600;margin:0 0 16px}
.verdict.good{color:var(--good)}.verdict.warn{color:var(--warn)}.verdict.bad{color:var(--bad)}
.facts,.env{display:flex;flex-wrap:wrap;gap:8px 24px;margin:0}
.facts dt,.env dt{font-size:.75rem;text-transform:uppercase;color:var(--muted)}
.facts dd,.env dd{margin:0;word-break:break-all}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
.badges{margin:0 0 8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.badge{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:0 10px;font-size:.8rem;
font-weight:600;color:var(--muted)}
.sev-critical{color:var(--critical)}.sev-high{color:var(--high)}.sev-medium{color:var(--medium)}
.sev-low{color:var(--low)}.confirm-confirmed,.status-passed{color:var(--good)}
.confirm-flaky{color:var(--warn)}.status-failed,.status-error{color:var(--bad)}.advisory{color:var(--advisory)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.88em}
code{background:var(--code);border-radius:4px;padding:1px 4px;word-break:break-all}
pre{background:var(--code);border-radius:6px;padding:10px;overflow-x:auto;white-space:pre-wrap}
pre code{padding:0;background:none}
.shots{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.shots img{max-height:360px;max-width:100%;border:1px solid var(--line);border-radius:6px}
.shots a:first-child img{max-height:480px}
video{display:block;max-width:100%;max-height:420px;margin:12px 0;border-radius:6px}
details{margin:12px 0}summary{cursor:pointer;font-weight:600}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;background:var(--card)}
caption{text-align:left;color:var(--muted);padding:4px 0}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
td{font-variant-numeric:tabular-nums}tr.total th,tr.total td{font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px}
figure img{width:100%;height:auto;display:block;border-radius:4px}
figcaption{font-size:.8rem;margin-top:4px}
.blank{height:120px;display:flex;align-items:center;justify-content:center;font-size:.8rem;color:var(--muted)}
a{color:inherit}
footer{border-top:1px solid var(--line);margin-top:32px;font-size:.9rem}
@media (max-width:600px){body{font-size:15px}h1{font-size:1.4rem}.card{padding:12px}}
"""
