"""Report rendering.

A single self-contained HTML file -- inline CSS and JS, no CDN links -- so it can
be attached to a ticket and still work on a machine with no network.

Reads artifacts only. It never triggers inference, because the report design will
be iterated on dozens of times and must not re-pay for a scan each round.
"""

from __future__ import annotations

import html
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .elide import elide_middle
from .models import RunManifest, StoredIssue
from .store import SessionStore

TEMPLATE_DIR = Path(__file__).parent / "templates"
EVIDENCE_BUDGET = 1200
MAX_EVIDENCE_PER_ISSUE = 3
MAX_EXAMPLE_SESSIONS = 5


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # Unconditional, not `select_autoescape`: that helper keys off the file
        # extension, and this template is `.html.j2`, so it would leave escaping
        # OFF. Everything interpolated here -- issue titles, cluster names,
        # evidence excerpts -- is model output derived from arbitrary transcript
        # content, so it is exactly the input that must never be trusted as
        # markup.
        autoescape=True,
    )
    env.filters["pct"] = lambda value: f"{value:.1%}"
    env.filters["commafy"] = lambda value: f"{value:,}"
    return env


def collect_evidence(
    manifest: RunManifest, store: SessionStore | None
) -> dict[str, list[dict[str, Any]]]:
    """Evidence excerpts per issue, read fresh from the transcripts.

    Excerpts go through the same labelled-elision path as everything else, so an
    excerpt that ends abruptly in the report ended that way on disk unless it
    carries a marker.
    """
    evidence: dict[str, list[dict[str, Any]]] = {}
    if store is None:
        return evidence

    for scan in manifest.scans:
        session = store.get(scan.session_id)
        if session is None:
            continue
        for issue in scan.issues:
            excerpts: list[dict[str, Any]] = []
            for index in issue.evidence_event_indices[:MAX_EVIDENCE_PER_ISSUE]:
                event = session.by_index(index)
                if event is None:
                    continue
                text = event.text or event.raw or ""
                excerpts.append(
                    {
                        "index": index,
                        "kind": event.kind,
                        "role": event.role,
                        "tool_name": event.tool_name,
                        "ts": event.ts.isoformat() if event.ts else None,
                        "text": elide_middle(text, EVIDENCE_BUDGET) if text else "",
                        "shape": event.shape.model_dump() if event.shape else None,
                    }
                )
            if excerpts:
                evidence[issue.issue_id] = excerpts
    return evidence


def build_context(
    manifest: RunManifest, store: SessionStore | None = None
) -> dict[str, Any]:
    issues: list[StoredIssue] = [i for scan in manifest.scans for i in scan.issues]
    issues_by_id = {issue.issue_id: issue for issue in issues}

    clusters = []
    for cluster in manifest.clusters:
        members = [issues_by_id[i] for i in cluster.member_issue_ids if i in issues_by_id]
        severity_rank = {"high": 3, "medium": 2, "low": 1}
        members.sort(key=lambda i: (-severity_rank.get(i.severity, 0), -i.confidence))
        clusters.append(
            {
                "cluster": cluster,
                "members": members,
                "session_count": len(cluster.affected_sessions),
                "issue_count": len(members),
                "top_severity": max(
                    (severity_rank.get(m.severity, 0) for m in members), default=0
                ),
                "example_sessions": cluster.affected_sessions[:MAX_EXAMPLE_SESSIONS],
                "phases": sorted({m.phase for m in members}),
            }
        )
    clusters.sort(key=lambda c: (-c["session_count"], -c["top_severity"], c["cluster"].name))

    claimed = {i for c in manifest.clusters for i in c.member_issue_ids}
    orphans = [issue for issue in issues if issue.issue_id not in claimed]

    severity_counts = {level: 0 for level in ("high", "medium", "low")}
    phase_counts: dict[str, int] = {}
    for issue in issues:
        severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
        phase_counts[issue.phase] = phase_counts.get(issue.phase, 0) + 1

    failed = [s for s in manifest.scans if s.error]
    capped = [s for s in manifest.scans if s.hit_turn_cap]

    return {
        "manifest": manifest,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "clusters": clusters,
        "new_clusters": [c for c in clusters if c["cluster"].is_new_this_run],
        "issues": issues,
        "orphan_issues": orphans,
        "severity_counts": severity_counts,
        "phase_counts": dict(sorted(phase_counts.items(), key=lambda kv: -kv[1])),
        "tool_stats": manifest.tool_stats,
        "evidence": collect_evidence(manifest, store),
        "failed_scans": failed,
        "capped_scans": capped,
    }


def render_html(manifest: RunManifest, store: SessionStore | None = None) -> str:
    template = _environment().get_template("report.html.j2")
    return template.render(**build_context(manifest, store))


def write_report(
    manifest: RunManifest,
    out: Path,
    *,
    store: SessionStore | None = None,
    open_browser: bool = False,
) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(manifest, store), encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(out.resolve().as_uri())
        except Exception:  # noqa: BLE001 - a headless box must not fail the run
            pass
    return out


def escape(text: str) -> str:
    return html.escape(text, quote=True)
