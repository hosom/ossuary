"""Agent B -- the clusterer.

Reads every issue from every session plus the corpus-wide tool statistics, and
emits named clusters.

Batching: at a few hundred sessions the whole issue set fits in one call, so it
makes one call. Above `BATCH_THRESHOLD` issues it batches and runs a merge pass
that reconciles the resulting cluster sets. There is deliberately no semantic
pre-batching or embedding router -- at this scale it buys nothing and adds a
failure mode that is hard to debug when it silently mis-routes.

Clusters arrive through a tool rather than as a structured final payload. That
costs a little strictness and buys two things: partial results survive a run
that stops early, and the agent works identically on backends that have no
structured-output mode at all.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..aggregate import render_tool_stats
from ..backends import AgentBackend, build_backend
from ..config import AgentConfig
from ..models import StoredIssue, ToolStats

# Issue count above which we split into batches and add a merge pass. Set from
# the section 7 guidance of roughly 1,500 sessions, assuming a few issues per
# session.
BATCH_THRESHOLD = 4000
BATCH_SIZE = 1500


class ProposedCluster(BaseModel):
    """One cluster as the model proposes it, before ids are assigned."""

    name: str = Field(description="Short human-readable name for this failure mode")
    summary: str = Field(description="What these issues have in common and why it matters")
    member_issue_ids: list[str] = Field(
        default_factory=list, description="issue_id values belonging to this cluster"
    )
    existing_cluster_id: str | None = Field(
        default=None,
        description=(
            "If this matches a cluster from the stored taxonomy, its cluster_id. "
            "Null if this is a genuinely new failure mode."
        ),
    )


class ClusterProposal(BaseModel):
    clusters: list[ProposedCluster] = Field(default_factory=list)


def build_clusterer_backend(config: AgentConfig) -> AgentBackend:
    """Construct Agent B's backend. Does not require credentials."""
    return build_backend(
        config.model,
        temperature=config.temperature,
        # One turn per cluster plus a closing turn, so the cap has to leave room
        # for the model to actually report what it found.
        max_turns=max(config.max_turns, 2),
        max_tokens=config.max_tokens,
        extra=config.extra,
    )


def render_issues(issues: list[StoredIssue], *, max_description: int = 600) -> str:
    """One block per issue, addressed by `issue_id` so the model can reference them."""
    from ..elide import elide_tail

    lines = [f"ISSUES ({len(issues)} total)", ""]
    for issue in issues:
        lines.append(f"issue_id: {issue.issue_id}")
        lines.append(
            f"  session: {issue.session_id}  source: {issue.source}  "
            f"severity: {issue.severity}  phase: {issue.phase}  "
            f"confidence: {issue.confidence:.2f}"
        )
        lines.append(f"  title: {issue.title}")
        lines.append(f"  description: {elide_tail(issue.description, max_description)}")
        if issue.evidence_event_indices:
            preview = ", ".join(str(i) for i in issue.evidence_event_indices[:12])
            more = (
                f" (+{len(issue.evidence_event_indices) - 12} more)"
                if len(issue.evidence_event_indices) > 12
                else ""
            )
            lines.append(f"  evidence events: {preview}{more}")
        lines.append("")
    return "\n".join(lines)


def render_taxonomy(known: list[dict]) -> str:
    """The stored taxonomy, so later runs assign rather than re-invent.

    This is what stops the report reshuffling between runs and what makes "new
    issue types this run" meaningful rather than an artifact of naming drift.
    """
    if not known:
        return (
            "STORED TAXONOMY: empty. This is the first run, so every cluster you "
            "propose is new. Leave existing_cluster_id null for all of them."
        )
    lines = [f"STORED TAXONOMY ({len(known)} known cluster(s) from previous runs)", ""]
    for entry in known:
        lines.append(f"cluster_id: {entry.get('cluster_id')}")
        lines.append(f"  name: {entry.get('name')}")
        lines.append(f"  summary: {entry.get('summary')}")
        lines.append("")
    lines.append(
        "Assign issues to one of these clusters when they are the same failure "
        "mode, by setting existing_cluster_id. Only propose a new cluster when "
        "the failure mode is genuinely not among them."
    )
    return "\n".join(lines)


def build_prompt(
    issues: list[StoredIssue],
    stats: list[ToolStats],
    known_taxonomy: list[dict],
    *,
    batch_note: str = "",
) -> str:
    sections = [
        render_taxonomy(known_taxonomy),
        "",
        render_issues(issues),
        "",
        render_tool_stats(stats),
    ]
    if batch_note:
        sections.insert(0, batch_note)
        sections.insert(1, "")
    sections.extend(["", REPORTING_NOTE])
    return "\n".join(sections)


REPORTING_NOTE = (
    "Call propose_cluster once per cluster, as soon as you have decided on it. "
    "Do not save them up: you have a limited number of turns and anything "
    "unreported when you run out is lost. When every issue_id has been placed, "
    "reply with a one-line summary."
)


def batches(issues: list[StoredIssue]) -> list[list[StoredIssue]]:
    """Split only when the set is genuinely too large for one call."""
    if len(issues) <= BATCH_THRESHOLD:
        return [issues]
    return [issues[i : i + BATCH_SIZE] for i in range(0, len(issues), BATCH_SIZE)]


MERGE_NOTE = (
    "This is a MERGE pass. The clusters below were proposed independently over "
    "separate batches of the same corpus, so the same failure mode may appear "
    "more than once under different names. Produce one reconciled set: merge "
    "duplicates, keep the clearest name, and union their member_issue_ids. Do "
    "not drop any issue_id that appears below."
)


def render_cluster_sets(proposals: list[ProposedCluster]) -> str:
    lines = [f"PROPOSED CLUSTERS ({len(proposals)}) FROM ALL BATCHES", ""]
    for index, cluster in enumerate(proposals):
        lines.append(f"proposal {index}:")
        lines.append(f"  name: {cluster.name}")
        lines.append(f"  summary: {cluster.summary}")
        lines.append(f"  existing_cluster_id: {cluster.existing_cluster_id}")
        lines.append(
            f"  member_issue_ids ({len(cluster.member_issue_ids)}): "
            + ", ".join(cluster.member_issue_ids)
        )
        lines.append("")
    return "\n".join(lines)
