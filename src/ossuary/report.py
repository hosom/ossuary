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

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
KEY_BY_RANK = {3: "high", 2: "medium", 1: "low", 0: "clean"}

# The dot strip is the brand's one recurring device: one dot = one session. The
# bar row underneath shares its rhythm but does not wrap, so it is only drawn for
# corpora small enough to read; past that the dots carry the pattern alone.
MAX_TRACE_BARS = 60
# One dot per call while that is literally true; past this the row is redrawn
# proportionally and says so, rather than silently standing for the wrong count.
MAX_TOOL_DOTS = 24
MAX_TOOL_ROWS = 8


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


def session_trace(manifest: RunManifest) -> list[dict[str, Any]]:
    """One entry per scanned session, in scan order.

    The colour is the worst thing found in that session, so a corpus reads as a
    strip at a glance. A session that errored is `error`, not `clean` -- absence
    of findings because nothing ran must never look like absence of findings
    because nothing was wrong.
    """
    counts = [len(scan.issues) for scan in manifest.scans]
    ceiling = max(counts, default=0)
    trace = []
    for scan in manifest.scans:
        if scan.error:
            key = "error"
        else:
            key = KEY_BY_RANK[
                max((SEVERITY_RANK.get(i.severity, 0) for i in scan.issues), default=0)
            ]
        count = len(scan.issues)
        plural = "" if count == 1 else "s"
        note = "scan errored" if scan.error else f"{count} issue{plural}"
        if scan.hit_turn_cap:
            note += " · hit turn cap"
        trace.append(
            {
                "session_id": scan.session_id,
                "key": key,
                "issue_count": count,
                "height": 6 + round(66 * count / ceiling) if ceiling else 6,
                "tip": f"{scan.session_id} — {note}",
            }
        )
    return trace


def phase_distribution(issues: list[StoredIssue]) -> list[dict[str, Any]]:
    """Issues per phase, widest first, each bar coloured by its worst member."""
    total = len(issues)
    buckets: dict[str, list[StoredIssue]] = {}
    for issue in issues:
        buckets.setdefault(issue.phase, []).append(issue)
    rows = [
        {
            "phase": phase,
            "count": len(members),
            "pct": f"{100 * len(members) / total:.0f}%",
            "key": KEY_BY_RANK[
                max((SEVERITY_RANK.get(m.severity, 0) for m in members), default=0)
            ],
        }
        for phase, members in buckets.items()
    ]
    rows.sort(key=lambda r: (-r["count"], r["phase"]))
    return rows


def tool_dots(stats: list[Any]) -> list[dict[str, Any]]:
    """The dot strip applied to tool calls: rust for errored results, verdigris
    for the rest.

    Above `MAX_TOOL_DOTS` calls the row stops being one dot per call and becomes
    a proportional redraw. That is flagged per row rather than left to be assumed
    either way.
    """
    rows = []
    for stat in sorted(stats, key=lambda s: -s.call_count)[:MAX_TOOL_ROWS]:
        calls = stat.call_count
        errors = min(stat.error_count, calls)
        proportional = calls > MAX_TOOL_DOTS
        if proportional:
            shown = MAX_TOOL_DOTS
            rust = round(shown * errors / calls) if calls else 0
            if errors and rust == 0:
                rust = 1
        else:
            shown, rust = calls, errors
        rows.append(
            {
                "stat": stat,
                "dots": ["rust"] * rust + ["verdigris"] * (shown - rust),
                "proportional": proportional,
            }
        )
    return rows


def _verdict(session_count: int, severity_counts: dict[str, int]) -> dict[str, str]:
    if session_count == 0:
        return {"key": "skipped", "label": "Empty"}
    if severity_counts["high"]:
        return {"key": "failed", "label": "Failed"}
    if severity_counts["medium"] or severity_counts["low"]:
        return {"key": "degraded", "label": "Degraded"}
    return {"key": "passed", "label": "Clean"}


def _duration(manifest: RunManifest) -> str:
    if manifest.finished_at is None:
        return "—"
    seconds = int((manifest.finished_at - manifest.started_at).total_seconds())
    if seconds < 0:
        return "—"
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def build_context(
    manifest: RunManifest, store: SessionStore | None = None
) -> dict[str, Any]:
    issues: list[StoredIssue] = [i for scan in manifest.scans for i in scan.issues]
    issues_by_id = {issue.issue_id: issue for issue in issues}

    clusters = []
    for cluster in manifest.clusters:
        members = [issues_by_id[i] for i in cluster.member_issue_ids if i in issues_by_id]
        members.sort(key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), -i.confidence))
        top_severity = max((SEVERITY_RANK.get(m.severity, 0) for m in members), default=0)
        clusters.append(
            {
                "cluster": cluster,
                "members": members,
                "session_count": len(cluster.affected_sessions),
                "issue_count": len(members),
                "top_severity": top_severity,
                "severity_key": KEY_BY_RANK[top_severity],
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

    # The headline is the dominant failure mode where there is one, because that
    # is the sentence the reader would otherwise have to assemble themselves. It
    # is never editorialised -- it is the clusterer's own name for the cluster.
    affected = len({i.session_id for i in issues})
    if clusters:
        headline = clusters[0]["cluster"].name
        lede = clusters[0]["cluster"].summary
    elif issues:
        headline = f"{len(issues)} issues found, none clustered"
        lede = max(issues, key=lambda i: SEVERITY_RANK.get(i.severity, 0)).title
    elif manifest.session_count:
        headline = f"No issues found across {manifest.session_count} sessions"
        lede = "Every scanned session completed without a finding worth reporting."
    else:
        headline = "No sessions were scanned"
        lede = "The run produced no transcripts to examine."

    detail = (
        f"{len(issues)} issues in {affected} of {manifest.session_count} sessions — "
        f"{severity_counts['high']} high, {severity_counts['medium']} medium, "
        f"{severity_counts['low']} low. "
        f"{len(clusters)} clusters, "
        f"{len([c for c in clusters if c['cluster'].is_new_this_run])} new this run."
    )

    return {
        "manifest": manifest,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "report_date": datetime.now().strftime("%Y.%m.%d"),
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
        "verdict": _verdict(manifest.session_count, severity_counts),
        "headline": headline,
        "lede": lede,
        "detail": detail,
        "affected_session_count": affected,
        "duration": _duration(manifest),
        "trace": session_trace(manifest),
        "trace_has_bars": 0 < len(manifest.scans) <= MAX_TRACE_BARS,
        "phase_bars": phase_distribution(issues),
        "tool_rows": tool_dots(manifest.tool_stats),
        "tool_rows_shown": min(len(manifest.tool_stats), MAX_TOOL_ROWS),
        "max_trace_bars": MAX_TRACE_BARS,
        "max_tool_dots": MAX_TOOL_DOTS,
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
