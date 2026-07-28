"""Ossuary as an MCP server -- the whole of its agent-facing surface.

This module exposes the deterministic half of Ossuary -- discovery,
normalization, the outline, the shape measurements, the corpus statistics,
redaction -- as MCP tools, and lets whatever agent is already running drive the
investigation. Ossuary brings no model of its own; the reasoning happens on the
far side of this boundary.

That arrangement is what makes the credential question disappear. Inside Claude
Code or Copilot CLI the inference is already paid for and already authenticated,
so there is nothing here to hold a key or ask whose subscription is being spent.
It is also the only arrangement where the operator can watch the investigation
happen and interrupt it.

The tradeoff is worth stating: a run driven this way is a conversation, not a
measurement. There is no turn cap and no prompt version to hash, and the host
agent brings its own system prompt and its own context, so two runs over the
same corpus can differ for reasons that have nothing to do with the transcripts.

Two properties this module is responsible for keeping:

  * Findings accumulate in memory and land in `.ossuary/run.json` only when
    `ossuary_write_run` is called, so an abandoned exploration leaves nothing
    behind for `ossuary report` to render as though it were finished.
  * `_Run` is shared by every agent talking to this process, which is what lets
    a coordinator fan out one investigator per session and still write a single
    reconciled run at the end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import ALL_SOURCES
from .aggregate import compute_tool_stats, corpus_event_count, render_tool_stats
from .models import Issue, ProposedCluster, RunManifest, SessionScan, StoredIssue, ToolStats
from .pipeline import artifact_dir, corpus_summary, issue_id_for, make_run_id, write_manifest
from .redact import Redactor
from .store import DEFAULT_EVENT_BUDGET, SessionStore
from .taxonomy import TAXONOMY_FILENAME, Taxonomy

SERVER_NAME = "ossuary"

#: Same ceiling as the scanner's own tool. One call must not be able to consume
#: the host agent's whole context window.
MAX_EVENT_SPAN = 40


@dataclass
class _Run:
    """One exploration, held in memory until `ossuary_write_run` is called."""

    run_id: str = field(default_factory=make_run_id)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    issues: list[StoredIssue] = field(default_factory=list)
    clusters: list[ProposedCluster] = field(default_factory=list)


class _State:
    """Discovery is lazy and cached: an outline request should not re-parse."""

    def __init__(self, roots: list[Path] | None, *, redact: bool) -> None:
        self.roots = roots
        self.store = SessionStore(redactor=Redactor(enabled=redact), roots=roots)
        self.redact = redact
        self.loaded = False
        self.stats: list[ToolStats] = []
        self.run = _Run()

    def ensure_loaded(self) -> None:
        if self.loaded:
            return
        for ref in self.store.discover(list(ALL_SOURCES), roots=self.roots):
            try:
                self.store.load(ref)
            except Exception:  # noqa: BLE001 - one bad file must not end discovery
                continue
        self.stats = compute_tool_stats(self.store.sessions)
        self.loaded = True

    def resolve(self, session_id: str) -> str:
        """Accept an id prefix, the way every other Ossuary command does."""
        self.ensure_loaded()
        ids = [s.session_id for s in self.store.sessions]
        if session_id in ids:
            return session_id
        matches = [i for i in ids if i.startswith(session_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"no session matching {session_id!r}. Call ossuary_sources to list them."
            )
        raise ValueError(f"{session_id!r} is ambiguous: {', '.join(matches[:8])}")


def _server_class() -> Any:
    """The MCP SDK's ergonomic server, under whichever name this major uses.

    `FastMCP` was renamed `MCPServer` in mcp 2.0 and the old import path removed.
    The surface Ossuary depends on -- the `tool` decorator, `list_tools`,
    `call_tool`, and a stdio `run` -- is the same in both, so both are supported
    rather than pinning to the superseded major.

    Import failures are re-raised with the original error attached. A bare "the
    SDK is missing" would be a lie on any machine where it is installed but
    incompatible, which is exactly the case worth diagnosing quickly.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP
    except ImportError:
        pass
    try:
        from mcp.server import MCPServer  # mcp 2.x

        return MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "the installed MCP SDK exposes neither mcp.server.fastmcp.FastMCP "
            f"(1.x) nor mcp.server.MCPServer (2.x): {exc}. `mcp` is a core "
            "dependency of ossuary, so reinstall with: pip install ossuary"
        ) from exc


def build_server(roots: list[Path] | None = None, *, redact: bool = True) -> Any:
    """Construct the MCP server. Importing this module must not need `mcp`."""
    state = _State(roots, redact=redact)
    mcp = _server_class()(SERVER_NAME)

    @mcp.tool()
    def ossuary_sources() -> str:
        """List the agent session transcripts Ossuary found on this machine.

        Start here. Returns one row per session with the id to pass to the other
        tools, its source CLI, event count, and last-modified time.
        """
        state.ensure_loaded()
        sessions = state.store.sessions
        if not sessions:
            return (
                "No sessions found. Ossuary looks under the default transcript "
                "directories for Claude Code, Codex, and Copilot; pass explicit "
                "roots when starting the server to look elsewhere."
            )
        lines = [f"{len(sessions)} session(s):", ""]
        for session in sessions:
            degraded = (
                f", {session.parse_error_count} degraded line(s)"
                if session.parse_error_count
                else ""
            )
            lines.append(
                f"  {session.session_id}  source={session.source}  "
                f"events={len(session.events)}{degraded}"
            )
        lines.append("")
        lines.append(
            "Redaction is " + ("on" if state.redact else "OFF -- transcripts are verbatim")
            + ". Call ossuary_outline on a session to begin."
        )
        return "\n".join(lines)

    @mcp.tool()
    def ossuary_outline(session_id: str) -> str:
        """Every event in one session at low resolution, in order.

        Read this in full before reading any individual event. Look down the
        columns, not just across the rows: repeated identical byte counts, a
        suspiciously round number, a long duration next to an empty body, a gap
        in the timestamps, or a tool called many times in a row are all visible
        as shapes in the table.
        """
        return state.store.outline(state.resolve(session_id))

    @mcp.tool()
    def ossuary_read_events(session_id: str, start: int, end: int) -> str:
        """Read full events from a session by index range.

        `end` is exclusive; at most 40 events per call. Payloads are redacted
        and elided with explicit `[[ossuary:...]]` markers, which are never part
        of the original transcript.
        """
        resolved = state.resolve(session_id)
        span_end = min(end, start + MAX_EVENT_SPAN)
        body = state.store.read_events(
            resolved, start, span_end, per_event_budget=DEFAULT_EVENT_BUDGET
        )
        if end > span_end:
            body += (
                f"\n\n[[ossuary:elided events {span_end}..{end}; "
                f"at most {MAX_EVENT_SPAN} events per call -- "
                f"call ossuary_read_events again from {span_end} to continue]]"
            )
        return body

    @mcp.tool()
    def ossuary_search_session(session_id: str, pattern: str) -> str:
        """Search one session's text with a Python regular expression.

        Use this when you have a specific hypothesis to check, rather than
        reading forward hoping to find something.
        """
        return state.store.search_session(state.resolve(session_id), pattern)

    @mcp.tool()
    def ossuary_read_event_slice(
        session_id: str, event_index: int, offset: int = 0, limit: int = 8000
    ) -> str:
        """Read one oversized payload a page at a time, by byte offset."""
        return state.store.read_event_slice(
            state.resolve(session_id), event_index, offset, limit
        )

    @mcp.tool()
    def ossuary_tool_stats(tool_name: str = "") -> str:
        """Corpus-wide statistics for one tool, or the top tools if unnamed.

        Check this before concluding a tool behaved abnormally in one session. A
        result that looks odd here may be normal for that tool everywhere -- and
        if it is normal everywhere, that is a different and often more
        interesting finding than a one-off.
        """
        state.ensure_loaded()
        if not tool_name:
            return render_tool_stats(state.stats, limit=20)
        matches = [s for s in state.stats if s.tool_name == tool_name]
        if not matches:
            known = ", ".join(sorted({s.tool_name for s in state.stats})[:25])
            return (
                f"No corpus statistics for tool {tool_name!r}. "
                f"Tools seen in this corpus: {known or 'none'}"
            )
        return render_tool_stats(matches)

    @mcp.tool()
    def ossuary_report_issue(
        session_id: str,
        title: str,
        description: str,
        severity: str,
        phase: str,
        evidence_event_indices: list[int],
        confidence: float,
    ) -> str:
        """Record one issue you have found in a session.

        Call this as soon as you find each issue rather than saving them up.
        `severity` is low, medium, or high. `phase` is where the problem
        originates: prompt, tool, model, harness, user, or unknown.
        `evidence_event_indices` are the events a reader should look at to check
        the claim -- an issue nobody can verify is not useful.
        """
        resolved = state.resolve(session_id)
        session = state.store.get(resolved)
        issue = Issue(
            title=title.strip(),
            description=description.strip(),
            severity=severity,  # type: ignore[arg-type]
            phase=phase,  # type: ignore[arg-type]
            evidence_event_indices=sorted(set(evidence_event_indices)),
            confidence=max(0.0, min(1.0, confidence)),
        )
        position = sum(1 for i in state.run.issues if i.session_id == resolved)
        state.run.issues.append(
            StoredIssue(
                **issue.model_dump(),
                issue_id=issue_id_for(resolved, position, issue.title),
                session_id=resolved,
                source=session.source if session else "claude-code",
                session_path=session.path if session else "",
            )
        )
        return (
            f"Recorded issue {len(state.run.issues)}: {issue.title!r} "
            f"({state.run.issues[-1].issue_id})."
        )

    @mcp.tool()
    def ossuary_propose_cluster(
        name: str,
        summary: str,
        member_issue_ids: list[str],
        existing_cluster_id: str = "",
    ) -> str:
        """Group recorded issues into one recurring failure mode.

        Group by underlying cause, not surface wording. Name the cluster for the
        failure rather than the symptom. Set `existing_cluster_id` when this is
        the same failure mode as one `ossuary_known_clusters` already lists, even
        if you would have named it differently -- stable names between runs are
        what make "new this run" mean anything.
        """
        state.run.clusters.append(
            ProposedCluster(
                name=name.strip(),
                summary=summary.strip(),
                member_issue_ids=list(member_issue_ids),
                existing_cluster_id=existing_cluster_id or None,
            )
        )
        return f"Recorded cluster {len(state.run.clusters)}: {name!r}."

    @mcp.tool()
    def ossuary_known_clusters() -> str:
        """The failure modes named in previous runs, from `.ossuary/taxonomy.json`."""
        known = Taxonomy(artifact_dir(Path.cwd()) / TAXONOMY_FILENAME).known()
        if not known:
            return "No stored taxonomy. Every cluster you propose is new."
        lines = [f"{len(known)} known cluster(s):", ""]
        for entry in known:
            lines.append(f"  {entry.get('cluster_id')}: {entry.get('name')}")
            lines.append(f"      {entry.get('summary')}")
        return "\n".join(lines)

    @mcp.tool()
    def ossuary_write_run(investigator: str = "") -> str:
        """Write everything recorded so far to `.ossuary/`, for `ossuary report`.

        Call this once, at the end. Nothing is persisted before it, so an
        abandoned exploration leaves no artifacts behind.

        `investigator` is how you want to be named in the report -- your harness
        and model, if you know them. Ossuary cannot see which model is on the
        other end of these tools, so it records what you say or nothing at all.
        """
        state.ensure_loaded()
        run = state.run
        sessions = state.store.sessions
        by_session: dict[str, list[StoredIssue]] = {}
        for issue in run.issues:
            by_session.setdefault(issue.session_id, []).append(issue)

        taxonomy = Taxonomy(artifact_dir(Path.cwd()) / TAXONOMY_FILENAME)
        clusters = taxonomy.reconcile(
            run.clusters,
            run_id=run.run_id,
            issue_lookup={i.issue_id: i.session_id for i in run.issues},
        )
        taxonomy.update(clusters, run_id=run.run_id)
        taxonomy.save()

        manifest = RunManifest(
            run_id=run.run_id,
            started_at=run.started_at,
            finished_at=datetime.now(timezone.utc),
            investigator=investigator.strip(),
            redaction_enabled=state.redact,
            session_count=len(sessions),
            event_count=corpus_event_count(sessions),
            issue_count=len(run.issues),
            sources=corpus_summary(sessions),
            scans=[
                SessionScan(
                    session_id=s.session_id,
                    source=s.source,
                    path=s.path,
                    content_hash=s.content_hash,
                    issues=by_session.get(s.session_id, []),
                )
                for s in sessions
            ],
            tool_stats=state.stats,
            clusters=clusters,
        )
        path = write_manifest(manifest, Path.cwd())
        state.run = _Run()
        return (
            f"Wrote {path} with {manifest.issue_count} issue(s) and "
            f"{len(clusters)} cluster(s). Run `ossuary report` to render the HTML."
        )

    return mcp


def main() -> None:
    """Entry point for `ossuary-mcp`. Speaks MCP over stdio.

    Roots come from `OSSUARY_ROOTS` (os.pathsep-separated) so a plugin manifest
    can point the server at a transcript directory without shell quoting.
    Redaction is on unless `OSSUARY_NO_REDACT` is set, and turning it off means
    transcripts reach the host agent verbatim, credentials included.
    """
    raw = os.environ.get("OSSUARY_ROOTS", "").strip()
    roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p] or None
    redact = not os.environ.get("OSSUARY_NO_REDACT")
    build_server(roots, redact=redact).run()


__all__ = ["MAX_EVENT_SPAN", "SERVER_NAME", "build_server", "main"]
